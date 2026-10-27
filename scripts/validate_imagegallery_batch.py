#!/usr/bin/env python3
"""Validate and, when evidence is conclusive, repair a published Studio Gallery batch."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import publish_imagegallery_block as publisher


DEFAULT_ADAPTER = Path(__file__).resolve().parents[1] / "references" / "adapter.template.json"
DEFAULT_TIMEOUT = 30
IMAGE_PAGE_SIZE = 50


class BatchValidationError(RuntimeError):
    def __init__(self, step: str, summary: str, *, http_status: int | None = None):
        self.step = step
        self.safe_summary = summary[:240]
        self.http_status = http_status
        super().__init__(self.safe_summary)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_locator(value: Any) -> str:
    locator = str(value or "").strip()
    if not re.fullmatch(r"block-v1:[A-Za-z0-9._:+@=-]+", locator):
        return ""
    return locator


def safe_error(exc: Exception, step: str) -> str:
    summary = getattr(exc, "safe_summary", "")
    if isinstance(summary, str) and summary:
        return summary[:240]
    if isinstance(exc, publisher.TemplateCallError):
        return f"Studio request failed (HTTP {exc.status_code}) during {step}"
    return f"Batch validation failed during {step}"


def safe_summary_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    summary = re.sub(r"[\r\n\t]+", " ", value)
    summary = re.sub(
        r"(?i)(\"?(?:authorization|cookie|csrf(?:token)?|password|secret|session|token|credential(?:s)?|accesskeyid|accesskeysecret|securitytoken|accesstoken|signature)\"?\s*[:=]\s*\"?)([^\"\s,;}]+)",
        r"\1<redacted>",
        summary,
    )
    summary = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", summary)
    return summary[:240]


def safe_http_status(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def safe_publish_state(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    state: dict[str, Any] = {
        "status": raw.get("status") if raw.get("status") in {"not_started", "in_progress", "failed", "completed"} else "not_started",
        "completed_stage": raw.get("completed_stage") if isinstance(raw.get("completed_stage"), str) else "",
        "active_step": publisher.safe_step_name(raw.get("active_step")) if raw.get("active_step") else "",
        "failed_step": publisher.safe_step_name(raw.get("failed_step")) if raw.get("failed_step") else "",
        "asset_ids": [item for item in raw.get("asset_ids", []) if isinstance(item, str) and item] if isinstance(raw.get("asset_ids"), list) else [],
        "images": publisher.safe_image_checkpoint_records(raw.get("images")),
        "steps": [],
        "images_started": bool(raw.get("images_started", False)),
        "reconciliation_count": raw.get("reconciliation_count", 0) if isinstance(raw.get("reconciliation_count", 0), int) else 0,
    }
    state["steps"] = [
        publisher.safe_step_record(item)
        for item in raw.get("steps", [])
        if isinstance(item, dict)
    ] if isinstance(raw.get("steps"), list) else []
    error_summary = raw.get("error_summary")
    if isinstance(error_summary, str) and error_summary:
        state["error_summary"] = safe_summary_text(error_summary)
    failure_http_status = raw.get("failure_http_status")
    if isinstance(failure_http_status, int):
        state["failure_http_status"] = failure_http_status
    failed_image = raw.get("failed_page_image")
    if isinstance(failed_image, str) and failed_image:
        state["failed_page_image"] = Path(failed_image.replace("\\", "/")).name
    document_id = raw.get("audio_document_id")
    if isinstance(document_id, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", document_id):
        state["audio_document_id"] = document_id
    return state


def refresh_publish_checkpoint(entry: dict[str, Any], operation_path: Path) -> None:
    try:
        persisted_operation = json.loads(operation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    persisted_entries = [
        candidate for candidate in persisted_operation.get("galleries", [])
        if isinstance(candidate, dict) and candidate.get("gallery_key") == entry.get("gallery_key")
    ] if isinstance(persisted_operation, dict) else []
    if len(persisted_entries) == 1:
        entry["publish"] = safe_publish_state(persisted_entries[0].get("publish"))


def load_operation(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchValidationError("operation_file", "operation file is not readable JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("galleries"), list) or not payload["galleries"]:
        raise BatchValidationError("operation_file", "operation file must contain a non-empty galleries list")
    keys = [entry.get("gallery_key") for entry in payload["galleries"] if isinstance(entry, dict)]
    if len(keys) != len(payload["galleries"]) or any(not isinstance(key, str) or not key.strip() for key in keys):
        raise BatchValidationError("operation_file", "each gallery entry must have a gallery_key")
    if len(keys) != len(set(keys)):
        raise BatchValidationError("operation_file", "gallery_key values must be unique")
    for entry in payload["galleries"]:
        entry["publish"] = safe_publish_state(entry.get("publish"))
        initial_vertical = entry.get("initial_vertical_publish")
        if isinstance(initial_vertical, dict):
            source_status = initial_vertical.get("status")
            safe_vertical = {"status": source_status if source_status in {"completed", "failed", "in_progress", "pending"} else "unknown"}
            status_code = initial_vertical.get("http_status")
            if isinstance(status_code, int):
                safe_vertical["http_status"] = status_code
            failed_step = initial_vertical.get("failed_step")
            if isinstance(failed_step, str) and failed_step:
                safe_vertical["failed_step"] = publisher.safe_step_name(failed_step)
            error_summary = initial_vertical.get("error_summary")
            if isinstance(error_summary, str) and error_summary:
                safe_vertical["error_summary"] = safe_summary_text(error_summary)
            entry["initial_vertical_publish"] = safe_vertical
        history = entry.get("repair_history")
        entry["repair_history"] = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        attempts = entry.get("repair_attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            attempts = 0
        entry["repair_attempts"] = max(attempts, len(entry["repair_history"]))
    return payload


class StudioClient:
    def __init__(self, args: argparse.Namespace, *, studio_base: str, studio_url: str, course_key: str):
        self.args = args
        self.studio_base = studio_base.rstrip("/")
        self.studio_url = studio_url
        self.course_key = course_key
        self.account = str(args.account or os.environ.get("FIRA_SAAS_OP_ACCOUNT", "")).strip()
        self.password = str(args.password or os.environ.get("FIRA_SAAS_OP_PASSWORD", "")).strip()
        self.courses_base = str(args.courses_base or publisher.derive_courses_base_from_studio_base(self.studio_base)).strip()
        self.session, self.csrftoken, auth_ready = publisher.build_studio_session_from_args(
            studio_base=self.studio_base,
            studio_url=studio_url,
            args=args,
            allow_login=True,
        )
        if not auth_ready:
            raise BatchValidationError("authentication", "execute mode requires Studio credentials or --cookie")
        self.can_refresh_auth = bool(self.account and self.password and self.courses_base)

    def refresh_auth(self) -> None:
        if self.can_refresh_auth:
            self.session, self.csrftoken = publisher.login_and_get_studio_session(
                account=self.account,
                password=self.password,
                courses_base=self.courses_base,
                studio_csrf_url=self.studio_url,
            )
            return
        raise BatchValidationError("authentication", "Studio authentication refresh is unavailable")

    def _headers(self, referer: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.studio_base,
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.csrftoken:
            headers["X-CSRFToken"] = self.csrftoken
        if referer:
            headers["Referer"] = referer
        return headers

    def _call_with_auth_retry(self, request_once: Callable[[], Any]) -> tuple[Any, list[dict[str, Any]]]:
        result, retry_events = publisher.call_with_auth_retry(
            request_once,
            self.refresh_auth if self.can_refresh_auth else None,
            max_retries=publisher.normalize_auth_retry_max_retries(self.args.auth_retry_max_retries),
            delay_seconds=max(0.0, float(self.args.auth_retry_delay_seconds)),
        )
        return result, publisher.safe_auth_retry_events(retry_events)

    def request_json(
        self,
        step: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        referer: str = "",
    ) -> tuple[Any, int, str | None, list[dict[str, Any]]]:
        def request_once():
            response = self.session.request(
                method=method,
                url=url,
                headers=self._headers(referer),
                params=params,
                json=body,
                timeout=self.args.timeout,
            )
            payload, parse_warning = publisher.decode_response_payload(response)
            if response.status_code >= 400:
                raise publisher.TemplateCallError(step, response.status_code, payload)
            return payload, response.status_code, parse_warning

        result, safe_retries = self._call_with_auth_retry(request_once)
        payload, status, parse_warning = result
        return payload, status, parse_warning, safe_retries

    def request_template(self, step: str, template: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        def request_once():
            context["csrftoken"] = self.csrftoken
            return publisher.call_template(self.session, step, template, context, True, self.args.timeout)

        result, safe_retries = self._call_with_auth_retry(request_once)
        return publisher.safe_step_record(result), safe_retries


def read_json_call(client: StudioClient, step: str, method: str, url: str, *, params=None, body=None, referer="") -> tuple[Any, dict[str, Any] | None]:
    try:
        payload, status, parse_warning, retries = client.request_json(step, method, url, params=params, body=body, referer=referer)
    except Exception as exc:
        diagnostic: dict[str, Any] = {"step": publisher.safe_step_name(step), "status": "failed", "error_summary": safe_error(exc, step)}
        status = safe_http_status(exc)
        if status is not None:
            diagnostic["http_status"] = status
        return None, diagnostic
    diagnostic = {"step": publisher.safe_step_name(step), "status": "ok", "http_status": status}
    if retries:
        diagnostic["auth_retry_attempts"] = len(retries)
        diagnostic["auth_retry_events"] = retries
    if parse_warning:
        diagnostic["status"] = "invalid_response"
        diagnostic["error_summary"] = "Studio returned invalid JSON"
    return payload, diagnostic


def map_gallery_owners(course_structure: dict[str, Any]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    for record in publisher.iter_section_vertical_records(course_structure):
        vertical = record.get("vertical") if isinstance(record, dict) else None
        if not isinstance(vertical, dict):
            continue
        vertical_locator = safe_locator(vertical.get("block_location"))
        for block in vertical.get("blocks", []):
            if not isinstance(block, dict) or publisher.block_category(block) != "imagesgallery":
                continue
            locator = safe_locator(publisher.block_locator(block))
            if locator:
                owners.setdefault(locator, []).append(vertical_locator)
    return owners


def manifest_image_count(manifest: dict[str, Any]) -> int:
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise BatchValidationError("manifest", "manifest must contain at least one image item")
    return len(items)


def valid_file_records(payload: Any) -> tuple[list[dict[str, Any]] | None, int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        return None, None
    files = payload["files"]
    total_count = payload.get("total_count")
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count < 0:
        return None, None
    ids: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            return None, None
        asset_id = item.get("id") or item.get("asset_key")
        if not isinstance(asset_id, str) or not asset_id.strip() or asset_id in ids:
            return None, None
        ids.add(asset_id)
    return files, total_count


def get_files(client: StudioClient, gallery_locator: str) -> tuple[list[dict[str, Any]] | None, int | None, dict[str, Any] | None]:
    url = f"{client.studio_base}/preview/xblock/{gallery_locator}/handler/get_files"
    payload, diagnostic = read_json_call(client, "get_files", "POST", url, body={"current_page": 0, "page_size": 100}, referer=publisher.build_container_block_url(client.studio_base, gallery_locator))
    if diagnostic and diagnostic["status"] != "ok":
        return None, None, diagnostic
    files, total_count = valid_file_records(payload)
    if files is None:
        return None, None, {"step": "get_files", "status": "invalid_response", "error_summary": "Studio get_files response has an invalid shape"}
    return files, total_count, diagnostic


def validate_gallery(client: StudioClient, entry: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    locator = safe_locator(entry.get("gallery_locator"))
    expected_count = manifest_image_count(manifest)
    referer = publisher.build_container_block_url(client.studio_base, locator)
    observed: dict[str, Any] = {}
    checks: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []

    files, total_count, file_diagnostic = get_files(client, locator)
    if file_diagnostic:
        diagnostics.append(file_diagnostic)
    if files is None:
        checks["images"] = "unverified"
    else:
        observed["image_file_count"] = len(files)
        observed["image_total_count"] = total_count
        checks["images"] = "pass" if len(files) == total_count == expected_count else "mismatch"

    config_payload, config_diagnostic = read_json_call(
        client,
        "get_play_config",
        "POST",
        f"{client.studio_base}/preview/xblock/{locator}/handler/get_play_config",
        body={},
        referer=referer,
    )
    if config_diagnostic:
        diagnostics.append(config_diagnostic)
    play_config = config_payload.get("play_config") if isinstance(config_payload, dict) else None
    time_config = play_config.get("time_config") if isinstance(play_config, dict) else None
    if config_diagnostic and config_diagnostic["status"] != "ok" or not isinstance(time_config, list):
        checks["play_config"] = "unverified"
        if not (config_diagnostic and config_diagnostic["status"] != "ok"):
            diagnostics.append({"step": "get_play_config", "status": "invalid_response", "error_summary": "Studio play_config.time_config is not a list"})
    else:
        observed["time_config_count"] = len(time_config)
        checks["play_config"] = "pass" if files is not None and len(time_config) == len(files) else "mismatch" if files is not None else "unverified"

    audio_payload, audio_diagnostic = read_json_call(
        client,
        "get_audios",
        "POST",
        f"{client.studio_base}/preview/xblock/{locator}/handler/get_audios",
        body={},
        referer=referer,
    )
    if audio_diagnostic:
        diagnostics.append(audio_diagnostic)
    audios = audio_payload.get("audios") if isinstance(audio_payload, dict) else None
    if audio_diagnostic and audio_diagnostic["status"] != "ok" or not isinstance(audios, list) or any(not isinstance(item, dict) for item in audios):
        checks["audio_bindings"] = "unverified"
        if not (audio_diagnostic and audio_diagnostic["status"] != "ok"):
            diagnostics.append({"step": "get_audios", "status": "invalid_response", "error_summary": "Studio get_audios.audios is not a list of bindings"})
    else:
        observed["audio_binding_count"] = len(audios)
        checks["audio_bindings"] = "pass" if len(audios) == 1 else "mismatch"

    if "unverified" in checks.values():
        status = "unverified"
    elif "mismatch" in checks.values():
        status = "mismatch"
    else:
        status = "verified"
    failed = next((item for item in diagnostics if item.get("status") != "ok"), None)
    result: dict[str, Any] = {
        "status": status,
        "expected": {"image_count": expected_count, "audio_binding_count": 1},
        "observed": observed,
        "checks": checks,
        "checked_at": utc_now(),
    }
    if failed:
        result["failed_step"] = failed["step"]
        if isinstance(failed.get("http_status"), int):
            result["http_status"] = failed["http_status"]
        result["error_summary"] = failed.get("error_summary", "Studio response could not be verified")
    return result


def invalid_gallery_result(entry: dict[str, Any], expected_count: int | None, *, step: str, summary: str, http_status: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unverified",
        "expected": {"image_count": expected_count, "audio_binding_count": 1},
        "observed": {},
        "checks": {"images": "unverified", "play_config": "unverified", "audio_bindings": "unverified"},
        "failed_step": publisher.safe_step_name(step),
        "error_summary": summary[:240],
        "checked_at": utc_now(),
    }
    if isinstance(http_status, int):
        result["http_status"] = http_status
    return result


def scan_course_gallery_references(
    client: StudioClient,
    *,
    expected_gallery_locators: set[str],
) -> tuple[dict[str, list[str]] | None, list[str], str]:
    structure_payload, diagnostic = read_json_call(
        client,
        "load_live_course_structure",
        "GET",
        f"{client.studio_base}/course/{client.course_key}",
        params={"format": "concise"},
        referer=client.studio_url,
    )
    if diagnostic and diagnostic["status"] != "ok":
        return None, [], str(diagnostic.get("error_summary") or "Studio course structure could not be verified")
    try:
        structure = publisher.normalize_course_structure_payload(structure_payload)
    except Exception:
        return None, [], "Studio course structure could not be normalized"
    if not isinstance(structure.get("chapters"), list):
        return None, [], "Studio course structure has an invalid shape"
    locators: list[str] = []
    seen: set[str] = set()
    for record in publisher.iter_section_vertical_records(structure):
        vertical = record.get("vertical") if isinstance(record, dict) else None
        if not isinstance(vertical, dict):
            continue
        for block in vertical.get("blocks", []):
            if not isinstance(block, dict) or publisher.block_category(block) != "imagesgallery":
                continue
            locator = safe_locator(publisher.block_locator(block))
            if not locator or locator in seen:
                return None, [], "Studio course structure contains a missing or duplicate Gallery locator"
            seen.add(locator)
            locators.append(locator)
    if not locators or not expected_gallery_locators.issubset(seen):
        return None, locators, "Studio course structure no longer contains the complete Gallery inventory"
    references: dict[str, list[str]] = {}
    for locator in locators:
        files, total_count, file_diagnostic = get_files(client, locator)
        if file_diagnostic and file_diagnostic["status"] != "ok" or files is None:
            return None, locators, "A complete Gallery reference scan could not be verified"
        if len(files) != total_count:
            return None, locators, "A complete Gallery reference scan could not be verified"
        for item in files:
            asset_id = str(item.get("id") or item.get("asset_key") or "").strip()
            references.setdefault(asset_id, []).append(locator)
    return references, locators, ""


def list_course_assets(client: StudioClient) -> tuple[dict[str, dict[str, Any]] | None, str]:
    assets_by_id: dict[str, dict[str, Any]] = {}
    page = 0
    expected_total: int | None = None
    while page < 10000:
        payload, diagnostic = read_json_call(
            client,
            "list_course_assets",
            "GET",
            f"{client.studio_base}/assets/{client.course_key}/",
            params={"sort": "uploadDate", "direction": "desc", "asset_type": "Images", "page_size": IMAGE_PAGE_SIZE, "page": page},
            referer=client.studio_url,
        )
        if diagnostic and diagnostic["status"] != "ok" or not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
            return None, "Studio image asset listing could not be verified"
        assets = payload["assets"]
        if any(not isinstance(asset, dict) or not isinstance(asset.get("id"), str) or not asset["id"] for asset in assets):
            return None, "Studio image asset listing contains an invalid record"
        total = payload.get("totalCount")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return None, "Studio image asset listing has an invalid totalCount"
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            return None, "Studio image asset listing changed during the read"
        for asset in assets:
            if asset["id"] in assets_by_id:
                return None, "Studio image asset listing contains duplicate IDs"
            assets_by_id[asset["id"]] = asset
        if len(assets_by_id) == expected_total:
            return assets_by_id, ""
        if len(assets_by_id) > expected_total or not assets:
            return None, "Studio image asset listing is incomplete"
        if "page" in payload and payload.get("page") != page:
            return None, "Studio image asset listing returned an unexpected page"
        page += 1
    return None, "Studio image asset listing exceeded the page limit"


def repair_gallery(
    client: StudioClient,
    operation: dict[str, Any],
    entry: dict[str, Any],
    manifest: dict[str, Any],
    args: argparse.Namespace,
    adapter: dict[str, Any],
    expected_gallery_locators: set[str],
    store,
    attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history = entry.setdefault("repair_history", [])
    if attempt is None:
        prior_publish = safe_publish_state(entry.get("publish"))
        attempt = {
            "attempt": len(history) + 1,
            "status": "running",
            "started_at": utc_now(),
            "trigger_validation": copy.deepcopy(entry.get("validation", {})),
            "initial_publish": prior_publish,
            "steps": [],
        }
        history.append(attempt)
    else:
        prior_publish = safe_publish_state(attempt.get("initial_publish"))
        attempt["status"] = "running"
        attempt["resumed_at"] = utc_now()
        for key in ("finished_at", "failed_step", "error_summary", "http_status"):
            attempt.pop(key, None)
    entry["repair_attempts"] = max(1, int(entry.get("repair_attempts", 0)))
    store()

    locator = safe_locator(entry.get("gallery_locator"))
    referer = publisher.build_container_block_url(client.studio_base, locator)
    files, total_count, diagnostic = get_files(client, locator)
    if files is None:
        raise BatchValidationError("get_files_before_repair", (diagnostic or {}).get("error_summary", "Gallery files could not be verified before repair"), http_status=(diagnostic or {}).get("http_status"))
    if len(files) != total_count:
        raise BatchValidationError("get_files_before_repair", "Gallery file listing was incomplete before repair")
    removal_asset_keys: list[str] = []
    candidate_asset_ids: list[str] = []
    saved_candidates = attempt.get("candidate_asset_ids")
    if isinstance(saved_candidates, list) and all(isinstance(item, str) and item for item in saved_candidates):
        candidate_asset_ids = list(dict.fromkeys(saved_candidates))
    else:
        for item in files:
            asset_id = str(item.get("id") or item.get("asset_key") or "").strip()
            asset_key = str(item.get("asset_key") or item.get("id") or "").strip()
            if not asset_id or not asset_key or asset_id in candidate_asset_ids or asset_key in removal_asset_keys:
                raise BatchValidationError("get_files_before_repair", "Gallery returned duplicate or unidentified assets before repair")
            candidate_asset_ids.append(asset_id)
            removal_asset_keys.append(asset_key)
        for asset_id in prior_publish.get("asset_ids", []):
            if asset_id not in candidate_asset_ids:
                candidate_asset_ids.append(asset_id)
        for image in prior_publish.get("images", []):
            asset_id = image.get("asset_id") if isinstance(image, dict) else ""
            if isinstance(asset_id, str) and asset_id and asset_id not in candidate_asset_ids:
                candidate_asset_ids.append(asset_id)
    saved_removal_keys = attempt.get("removal_asset_keys")
    if isinstance(saved_removal_keys, list) and all(isinstance(item, str) and item for item in saved_removal_keys):
        removal_asset_keys = list(dict.fromkeys(saved_removal_keys))
    else:
        candidate_set = set(candidate_asset_ids)
        for item in files:
            asset_id = str(item.get("id") or item.get("asset_key") or "").strip()
            asset_key = str(item.get("asset_key") or item.get("id") or "").strip()
            if asset_id in candidate_set and asset_key and asset_key not in removal_asset_keys:
                removal_asset_keys.append(asset_key)
    attempt["candidate_asset_ids"] = candidate_asset_ids
    attempt["removal_asset_keys"] = removal_asset_keys
    store()

    attempt_steps = attempt.setdefault("steps", [])
    remove_step = next((step for step in attempt_steps if step.get("step") == "remove_gallery_files"), None)
    if removal_asset_keys and (not remove_step or remove_step.get("status") != "completed"):
        current_keys = {
            str(item.get("asset_key") or item.get("id") or "").strip()
            for item in files
            if isinstance(item, dict)
        }
        pending_removals = [asset_key for asset_key in removal_asset_keys if asset_key in current_keys]
        if not remove_step:
            remove_step = {"step": "remove_gallery_files", "status": "started", "asset_ids": candidate_asset_ids}
            attempt_steps.append(remove_step)
        else:
            remove_step.update({"status": "started", "asset_ids": candidate_asset_ids})
            remove_step.pop("error_summary", None)
            remove_step.pop("http_status", None)
        store()
        if not pending_removals:
            remove_step.update({"status": "completed", "verified_absent": True})
            store()
        else:
            try:
                _payload, status, _warning, retries = client.request_json(
                    "remove_gallery_files",
                    "POST",
                    f"{client.studio_base}/xblock/{locator}/handler/remove_files",
                    body={"assets": pending_removals},
                    referer=referer,
                )
                remove_step.update({"status": "completed", "http_status": status})
                if retries:
                    remove_step["auth_retry_attempts"] = len(retries)
                    remove_step["auth_retry_events"] = retries
                store()
            except Exception as exc:
                remove_step.update({"status": "failed", "error_summary": safe_error(exc, "remove_gallery_files")})
                status = safe_http_status(exc)
                if status is not None:
                    remove_step["http_status"] = status
                store()
                raise BatchValidationError("remove_gallery_files", remove_step["error_summary"], http_status=status) from None

    if candidate_asset_ids:
        after_remove, after_total_count, verify_diagnostic = get_files(client, locator)
        if after_remove is None:
            raise BatchValidationError("verify_removed_gallery_files", (verify_diagnostic or {}).get("error_summary", "Gallery file removal could not be verified"), http_status=(verify_diagnostic or {}).get("http_status"))
        if len(after_remove) != after_total_count:
            raise BatchValidationError("verify_removed_gallery_files", "Gallery file listing was incomplete after removal")
        after_ids = {str(item.get("id") or item.get("asset_key") or "") for item in after_remove}
        if any(asset_id in after_ids for asset_id in candidate_asset_ids):
            raise BatchValidationError("verify_removed_gallery_files", "Gallery still references an image scheduled for replacement")

    cleanup = attempt.get("asset_cleanup")
    if not isinstance(cleanup, dict):
        cleanup = {"status": "complete", "steps": [], "deleted_asset_ids": [], "retained_asset_ids": [], "skipped_asset_ids": []}
        attempt["asset_cleanup"] = cleanup
    cleanup.setdefault("steps", [])
    cleanup.setdefault("deleted_asset_ids", [])
    cleanup.setdefault("retained_asset_ids", [])
    cleanup.setdefault("skipped_asset_ids", [])
    cleanup["status"] = "complete"
    cleanup.pop("error_summary", None)
    store()
    if candidate_asset_ids:
        references, _locators, reference_error = scan_course_gallery_references(
            client,
            expected_gallery_locators=expected_gallery_locators,
        )
        assets, asset_error = list_course_assets(client)
        if references is None or assets is None:
            cleanup["status"] = "incomplete"
            cleanup["error_summary"] = (reference_error or asset_error or "Course asset/reference scan could not be verified")[:240]
            for asset_id in candidate_asset_ids:
                asset_step = next((step for step in cleanup["steps"] if step.get("asset_id") == asset_id), None)
                if not asset_step:
                    asset_step = {"asset_id": asset_id}
                    cleanup["steps"].append(asset_step)
                if asset_step.get("status") not in {"deleted", "retained", "not_found"}:
                    asset_step.update({"status": "skipped", "reason": "incomplete_course_scan"})
                    if asset_id not in cleanup["skipped_asset_ids"]:
                        cleanup["skipped_asset_ids"].append(asset_id)
            store()
        else:
            cleanup["skipped_asset_ids"] = []
            for asset_id in candidate_asset_ids:
                asset_step = next((step for step in cleanup["steps"] if step.get("asset_id") == asset_id), None)
                if not asset_step:
                    asset_step = {"asset_id": asset_id, "status": "pending"}
                    cleanup["steps"].append(asset_step)
                if asset_step.get("status") in {"deleted", "retained", "not_found"}:
                    continue
                if asset_id not in assets:
                    was_deleting = asset_step.get("status") == "started"
                    asset_step.update({
                        "status": "deleted" if was_deleting else "not_found",
                        "reason": "delete_confirmed_by_asset_listing" if was_deleting else "asset_not_in_complete_course_listing",
                    })
                    if was_deleting and asset_id not in cleanup["deleted_asset_ids"]:
                        cleanup["deleted_asset_ids"].append(asset_id)
                    store()
                    continue
                referenced_by = sorted(set(references.get(asset_id, [])))
                if referenced_by:
                    asset_step.update({"status": "retained", "reason": "currently_referenced", "referenced_by": referenced_by})
                    if asset_id not in cleanup["retained_asset_ids"]:
                        cleanup["retained_asset_ids"].append(asset_id)
                    store()
                    continue

                fresh_references, _fresh_locators, fresh_error = scan_course_gallery_references(
                    client,
                    expected_gallery_locators=expected_gallery_locators,
                )
                if fresh_references is None:
                    asset_step.update({"status": "skipped", "reason": "pre_delete_reference_scan_incomplete", "error_summary": fresh_error[:240]})
                    cleanup["status"] = "incomplete"
                    if asset_id not in cleanup["skipped_asset_ids"]:
                        cleanup["skipped_asset_ids"].append(asset_id)
                    store()
                    continue
                referenced_by = sorted(set(fresh_references.get(asset_id, [])))
                if referenced_by:
                    asset_step.update({"status": "retained", "reason": "referenced_before_delete", "referenced_by": referenced_by})
                    if asset_id not in cleanup["retained_asset_ids"]:
                        cleanup["retained_asset_ids"].append(asset_id)
                    store()
                    continue
                asset_step["status"] = "started"
                asset_step["references_checked_at"] = utc_now()
                store()
                try:
                    _payload, status, _warning, retries = client.request_json(
                        "delete_course_asset",
                        "DELETE",
                        f"{client.studio_base}/assets/{client.course_key}/{quote(asset_id, safe='')}",
                        referer=client.studio_url,
                    )
                    asset_step.update({"status": "deleted", "http_status": status})
                    if retries:
                        asset_step["auth_retry_attempts"] = len(retries)
                        asset_step["auth_retry_events"] = retries
                    if asset_id not in cleanup["deleted_asset_ids"]:
                        cleanup["deleted_asset_ids"].append(asset_id)
                except Exception as exc:
                    asset_step.update({"status": "failed", "error_summary": safe_error(exc, "delete_course_asset")})
                    status = safe_http_status(exc)
                    if status is not None:
                        asset_step["http_status"] = status
                    cleanup["status"] = "incomplete"
                store()

    attempt["prior_asset_ids"] = list(prior_publish.get("asset_ids", []))
    checkpoint = safe_publish_state(entry.get("publish"))
    if not attempt.get("publish_started") and not attempt.get("publish") and checkpoint.get("status") == "completed" and checkpoint.get("completed_stage") == "saved":
        entry["publish"] = {
            "gallery_key": entry["gallery_key"],
            "status": "in_progress",
            "completed_stage": "block_created",
            "active_step": "",
            "failed_step": "",
            "asset_ids": [],
            "images": [],
            "steps": [],
            "images_started": False,
            "reconciliation_count": 0,
        }
    attempt["publish_started"] = True
    store()
    publish_args = argparse.Namespace(
        studio_url=args.studio_url,
        manifest=str(Path(entry["manifest"]).expanduser().resolve()),
        adapter=args.adapter,
        execute=True,
        timeout=args.timeout,
        vertical_block_id=entry["vertical_locator"],
        block_url=publisher.build_container_block_url(client.studio_base, entry["vertical_locator"]),
        account=args.account,
        password=args.password,
        courses_base=args.courses_base,
        cookie=args.cookie,
        csrf_token=args.csrf_token,
        skip_oss_multipart=args.skip_oss_multipart,
        auth_retry_max_retries=args.auth_retry_max_retries,
        auth_retry_delay_seconds=args.auth_retry_delay_seconds,
        operation_file=str(args.operation_file),
        gallery_key=entry["gallery_key"],
    )
    try:
        checkpoint = safe_publish_state(entry.get("publish"))
        if checkpoint.get("status") == "completed" and checkpoint.get("completed_stage") == "saved":
            publish_result = checkpoint
        else:
            publish_result = publisher.run_single_publish(publish_args)
            refresh_publish_checkpoint(entry, Path(args.operation_file).expanduser().resolve())
        attempt["publish"] = {
            "status": "completed",
            "completed_stage": publish_result.get("completed_stage", "saved"),
            "asset_ids": [item for item in publish_result.get("asset_ids", []) if isinstance(item, str)],
            "steps": [publisher.safe_step_record(item) for item in publish_result.get("steps", []) if isinstance(item, dict)],
        }
    except Exception as exc:
        refresh_publish_checkpoint(entry, Path(args.operation_file).expanduser().resolve())
        attempt["status"] = "failed"
        attempt["failed_step"] = publisher.safe_step_name(getattr(exc, "step_name", getattr(exc, "step", "republish_gallery")))
        attempt["error_summary"] = safe_error(exc, attempt["failed_step"])
        if safe_http_status(exc) is not None:
            attempt["http_status"] = safe_http_status(exc)
        attempt["finished_at"] = utc_now()
        store()
        return attempt
    store()

    vertical_publish = attempt.get("vertical_publish")
    if not isinstance(vertical_publish, dict) or vertical_publish.get("status") != "completed":
        vertical_publish = {"step": "publish_vertical", "status": "started"}
        attempt["vertical_publish"] = vertical_publish
        store()
        try:
            context = {
                "studio_base": client.studio_base,
                "vertical_block_id": entry["vertical_locator"],
                "block_url": publisher.build_container_block_url(client.studio_base, entry["vertical_locator"]),
                "csrftoken": client.csrftoken,
            }
            record, retries = client.request_template("publish_vertical", adapter["publish_vertical"], context)
            vertical_publish.update({"status": "completed", "http_status": record.get("http_status")})
            if retries:
                vertical_publish["auth_retry_attempts"] = len(retries)
                vertical_publish["auth_retry_events"] = retries
        except Exception as exc:
            vertical_publish.update({"status": "failed", "error_summary": safe_error(exc, "publish_vertical")})
            if safe_http_status(exc) is not None:
                vertical_publish["http_status"] = safe_http_status(exc)
        attempt["finished_at"] = utc_now()
        attempt["status"] = "completed" if vertical_publish["status"] == "completed" else "failed"
        store()

    final_validation = validate_gallery(client, entry, manifest)
    entry["validation"] = final_validation
    attempt["final_validation"] = copy.deepcopy(final_validation)
    if final_validation["status"] == "verified" and vertical_publish["status"] == "completed":
        attempt["status"] = "completed"
    elif final_validation["status"] == "unverified":
        attempt["status"] = "unverified"
    elif final_validation["status"] == "verified":
        attempt["status"] = "failed"
    else:
        attempt["status"] = "mismatch"
    attempt["finished_at"] = utc_now()
    store()
    return attempt


def resumable_repair_attempt(entry: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("repair_attempts") != 1:
        return None
    history = entry.get("repair_history")
    if not isinstance(history, list) or len(history) != 1:
        return None
    attempt = history[0]
    if not isinstance(attempt, dict) or attempt.get("attempt") != 1:
        return None
    if attempt.get("status") not in {"running", "failed"} or "final_validation" in attempt:
        return None
    return attempt


def initial_vertical_published(entry: dict[str, Any]) -> bool:
    record = entry.get("initial_vertical_publish")
    return isinstance(record, dict) and record.get("status") == "completed"


def summarize_batch(operation: dict[str, Any]) -> dict[str, Any]:
    entries = operation["galleries"]
    all_verified = bool(entries)
    results = []
    for entry in entries:
        validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
        vertical_ok = initial_vertical_published(entry)
        history = entry.get("repair_history") if isinstance(entry.get("repair_history"), list) else []
        repair_attempts = entry.get("repair_attempts", 0)
        if repair_attempts > 0:
            vertical = history[-1].get("vertical_publish") if history and isinstance(history[-1], dict) else None
            vertical_ok = vertical_ok and repair_attempts == 1 and isinstance(vertical, dict) and vertical.get("status") == "completed"
        gallery_ok = validation.get("status") == "verified" and vertical_ok
        all_verified = all_verified and gallery_ok
        publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
        item = {
            "gallery_key": entry.get("gallery_key", ""),
            "status": validation.get("status", "unverified"),
            "initial_vertical_publish_status": (entry.get("initial_vertical_publish") or {}).get("status", "missing") if isinstance(entry.get("initial_vertical_publish"), dict) else "missing",
            "expected": validation.get("expected", {}),
            "observed": validation.get("observed", {}),
            "repair_attempts": repair_attempts,
        }
        publish_report = {key: publish[key] for key in ("status", "completed_stage", "failed_step", "failure_http_status", "error_summary", "failed_page_image", "asset_ids") if key in publish}
        if publish_report:
            item["publish"] = publish_report
        initial_vertical = entry.get("initial_vertical_publish") if isinstance(entry.get("initial_vertical_publish"), dict) else {}
        vertical_report = {key: initial_vertical[key] for key in ("status", "failed_step", "http_status", "error_summary") if key in initial_vertical}
        if vertical_report:
            item["initial_vertical_publish"] = vertical_report
        for key in ("failed_step", "http_status", "error_summary"):
            if key in validation:
                item[key] = validation[key]
        if "failed_step" not in item and isinstance(publish.get("failed_step"), str) and publish["failed_step"]:
            item["failed_step"] = publish["failed_step"]
        if "http_status" not in item and isinstance(publish.get("failure_http_status"), int):
            item["http_status"] = publish["failure_http_status"]
        if "error_summary" not in item and isinstance(publish.get("error_summary"), str) and publish["error_summary"]:
            item["error_summary"] = publish["error_summary"]
        if isinstance(publish.get("failed_page_image"), str):
            item["failed_page_image"] = publish["failed_page_image"]
        if initial_vertical.get("status") != "completed":
            item["failed_step"] = item.get("failed_step") or initial_vertical.get("failed_step", "publish_vertical")
            if "http_status" not in item and isinstance(initial_vertical.get("http_status"), int):
                item["http_status"] = initial_vertical["http_status"]
            if "error_summary" not in item and isinstance(initial_vertical.get("error_summary"), str):
                item["error_summary"] = initial_vertical["error_summary"]
        if history:
            latest_repair = history[-1] if isinstance(history[-1], dict) else {}
            item["repair_status"] = latest_repair.get("status", "unknown")
            vertical_publish = latest_repair.get("vertical_publish") if isinstance(latest_repair.get("vertical_publish"), dict) else {}
            item["repair_vertical_publish_status"] = vertical_publish.get("status", "missing")
            if "failed_step" not in item and isinstance(latest_repair.get("failed_step"), str):
                item["failed_step"] = latest_repair["failed_step"]
            if "http_status" not in item and isinstance(latest_repair.get("http_status"), int):
                item["http_status"] = latest_repair["http_status"]
            if "error_summary" not in item and isinstance(latest_repair.get("error_summary"), str):
                item["error_summary"] = latest_repair["error_summary"]
        item["asset_ids"] = [value for value in (entry.get("publish") or {}).get("asset_ids", []) if isinstance(value, str)] if isinstance(entry.get("publish"), dict) else []
        results.append(item)
    return {
        "status": "verified" if all_verified else "incomplete",
        "all_sections_verified": all_verified,
        "gallery_count": len(results),
        "verified_count": sum(item["status"] == "verified" for item in results),
        "galleries": results,
        "completed_at": utc_now(),
    }


def run_validation(args: argparse.Namespace) -> dict[str, Any]:
    operation_path = Path(args.operation_file).expanduser().resolve()
    operation = load_operation(operation_path)
    store = lambda: publisher.atomic_write_json(operation_path, operation)
    parsed_studio_url = urlsplit(args.studio_url)
    if parsed_studio_url.username or parsed_studio_url.password or parsed_studio_url.scheme not in {"http", "https"}:
        raise BatchValidationError("studio_url", "--studio-url must be an HTTP(S) Studio URL without embedded credentials")
    studio_base = f"{parsed_studio_url.scheme}://{parsed_studio_url.netloc}" if parsed_studio_url.netloc else ""
    vertical_from_url = publisher.parse_vertical_block_id(args.studio_url)
    course_key = publisher.course_key_from_vertical_block_id(vertical_from_url or "")
    if not studio_base or not course_key or not vertical_from_url or "+type@vertical+" not in vertical_from_url:
        raise BatchValidationError("studio_url", "--studio-url must contain a Studio vertical locator")
    args.studio_url = publisher.build_container_block_url(studio_base, vertical_from_url)
    for entry in operation["galleries"]:
        entry["gallery_locator"] = safe_locator(entry.get("gallery_locator"))
        entry["vertical_locator"] = safe_locator(entry.get("vertical_locator"))
        if not entry["gallery_locator"] or not entry["vertical_locator"]:
            entry["validation"] = invalid_gallery_result(entry, None, step="operation_file", summary="Gallery or vertical locator is invalid")
    store()
    client = StudioClient(args, studio_base=studio_base, studio_url=args.studio_url, course_key=course_key)
    adapter = publisher.read_json(Path(args.adapter).expanduser().resolve())
    if not isinstance(adapter, dict) or not isinstance(adapter.get("publish_vertical"), dict):
        raise BatchValidationError("adapter", "adapter does not define publish_vertical")

    structure_payload, structure_diagnostic = read_json_call(
        client,
        "load_live_course_structure",
        "GET",
        f"{studio_base}/course/{course_key}",
        params={"format": "concise"},
        referer=args.studio_url,
    )
    structure = None
    structure_error = ""
    if structure_diagnostic and structure_diagnostic["status"] != "ok":
        structure_error = str(structure_diagnostic.get("error_summary") or "Studio course structure could not be read")
    else:
        try:
            structure = publisher.normalize_course_structure_payload(structure_payload)
            if not isinstance(structure.get("chapters"), list):
                structure_error = "Studio course structure has an invalid shape"
        except Exception:
            structure_error = "Studio course structure could not be normalized"
    owners = map_gallery_owners(structure) if structure is not None and not structure_error else {}

    for entry in operation["galleries"]:
        if not entry.get("gallery_locator") or not entry.get("vertical_locator"):
            store()
            continue
        manifest_path = Path(str(entry.get("manifest", ""))).expanduser().resolve()
        try:
            manifest = publisher.ensure_manifest(manifest_path)
            expected_count = manifest_image_count(manifest)
        except Exception:
            entry["validation"] = invalid_gallery_result(entry, None, step="manifest", summary="Local Gallery manifest or package could not be validated")
            store()
            continue
        if structure_error:
            entry["validation"] = invalid_gallery_result(entry, expected_count, step="load_live_course_structure", summary=structure_error, http_status=(structure_diagnostic or {}).get("http_status"))
            store()
            continue
        gallery_owners = owners.get(entry["gallery_locator"], [])
        if len(gallery_owners) != 1 or gallery_owners[0] != entry["vertical_locator"]:
            entry["validation"] = invalid_gallery_result(entry, expected_count, step="course_structure_lookup", summary="Gallery locator is missing, ambiguous, or owned by a different vertical")
            store()
            continue
        publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
        if publish.get("status") != "completed" or publish.get("completed_stage") != "saved":
            entry["validation"] = invalid_gallery_result(entry, expected_count, step="initial_gallery_publish", summary="Initial Gallery publishing did not complete")
            store()
            continue

        entry["validation"] = validate_gallery(client, entry, manifest)
        store()
        attempts = entry.get("repair_attempts", 0)
        resume_attempt = resumable_repair_attempt(entry) if attempts >= 1 else None
        if resume_attempt is not None and entry["validation"]["status"] == "unverified":
            continue
        if resume_attempt is None and entry["validation"]["status"] != "mismatch":
            continue
        if attempts >= 1 and resume_attempt is None:
            entry["validation"]["repair_limit_reached"] = True
            store()
            continue
        try:
            repair_gallery(client, operation, entry, manifest, args, adapter, set(owners), store, attempt=resume_attempt)
        except Exception as exc:
            history = entry.setdefault("repair_history", [])
            attempt = history[-1] if history and isinstance(history[-1], dict) else {"attempt": 1, "steps": []}
            attempt["status"] = "failed"
            attempt["failed_step"] = publisher.safe_step_name(getattr(exc, "step", getattr(exc, "step_name", "repair")))
            attempt["error_summary"] = safe_error(exc, attempt["failed_step"])
            if safe_http_status(exc) is not None:
                attempt["http_status"] = safe_http_status(exc)
            attempt["finished_at"] = utc_now()
            if entry.get("validation", {}).get("status") != "unverified":
                entry["validation"]["repair_status"] = "failed"
            store()

    operation["batch_result"] = summarize_batch(operation)
    store()
    return operation["batch_result"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and safely repair a published Studio ImageGallery batch")
    parser.add_argument("--studio-url", required=True, help="Studio URL for a target vertical in this course")
    parser.add_argument("--operation-file", required=True, help="shared batch-operation.json owned by the batch caller")
    parser.add_argument("--execute", action="store_true", help="read Studio and perform one safe repair for confirmed mismatches")
    parser.add_argument("--adapter", default=str(DEFAULT_ADAPTER), help="request template adapter JSON")
    parser.add_argument("--account", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--cookie", default="")
    parser.add_argument("--csrf-token", default="")
    parser.add_argument("--courses-base", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--auth-retry-max-retries", type=int, default=publisher.DEFAULT_AUTH_RETRY_MAX_RETRIES)
    parser.add_argument("--auth-retry-delay-seconds", type=float, default=publisher.DEFAULT_AUTH_RETRY_DELAY_SECONDS)
    parser.add_argument("--skip-oss-multipart", action="store_true", help="debug only: skip the Gallery publisher's OSS audio upload")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required because a confirmed mismatch can trigger repair")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main() -> int:
    try:
        args = parse_args()
        result = run_validation(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["all_sections_verified"] else 1
    except SystemExit:
        raise
    except Exception as exc:
        error = {"status": "failed", "error_summary": safe_error(exc, getattr(exc, "step", "batch_validation"))}
        if safe_http_status(exc) is not None:
            error["http_status"] = safe_http_status(exc)
        print(json.dumps(error, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
