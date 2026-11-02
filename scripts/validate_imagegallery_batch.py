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
MAX_REPAIR_CYCLES = 2
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


def course_structure_payload_is_complete(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    if isinstance(payload.get("chapters"), list):
        for chapter in payload["chapters"]:
            if not isinstance(chapter, dict) or not isinstance(chapter.get("sections"), list):
                return False
            for section in chapter["sections"]:
                if not isinstance(section, dict) or not isinstance(section.get("verticals"), list):
                    return False
                for vertical in section["verticals"]:
                    if not isinstance(vertical, dict) or not isinstance(vertical.get("blocks"), list):
                        return False
                    if any(not isinstance(block, dict) for block in vertical["blocks"]):
                        return False
        return True

    container_categories = {"course", "chapter", "sequential", "vertical"}

    def valid_node(node: Any, depth: int = 0) -> bool:
        if not isinstance(node, dict):
            return False
        category = str(node.get("category", "")).strip().lower()
        requires_children = category in container_categories or depth <= 3
        child_info = node.get("child_info")
        if isinstance(child_info, dict):
            children = child_info.get("children")
            if not isinstance(children, list):
                return False
        elif "child_info" in node:
            return False
        else:
            children = node.get("children")
            if requires_children and not isinstance(children, list):
                return False
            if children is None:
                return not requires_children
            if not isinstance(children, list):
                return False
        return all(valid_node(child, depth + 1) for child in children)

    return str(payload.get("category", "")).strip().lower() == "course" and valid_node(payload)


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


def expected_image_records(entry: dict[str, Any], manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str] | None, str]:
    expected = [
        {"page": int(item["page"]), "image": publisher.manifest_image_name(item)}
        for item in manifest["items"]
    ]
    publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
    checkpoint_images = publisher.safe_image_checkpoint_records(publish.get("images"))
    if len(checkpoint_images) == len(expected):
        checkpoint_matches = all(
            checkpoint["page"] == image["page"] and checkpoint["image"] == image["image"]
            for checkpoint, image in zip(checkpoint_images, expected)
        )
        ids = [checkpoint["asset_id"] for checkpoint in checkpoint_images]
        if not checkpoint_matches or len(set(ids)) != len(ids):
            return expected, None, "mismatch"
        return expected, ids, "pass"

    asset_ids = publish.get("asset_ids")
    if (
        isinstance(asset_ids, list)
        and len(asset_ids) == len(expected)
        and all(isinstance(asset_id, str) and asset_id for asset_id in asset_ids)
        and len(set(asset_ids)) == len(asset_ids)
    ):
        return expected, list(asset_ids), "pass"
    return expected, None, "unverified"


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
    expected_images, expected_asset_ids, checkpoint_status = expected_image_records(entry, manifest)
    expected_names = [image["image"] for image in expected_images]
    referer = publisher.build_container_block_url(client.studio_base, locator)
    observed: dict[str, Any] = {}
    checks: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []

    files, total_count, file_diagnostic = get_files(client, locator)
    if file_diagnostic:
        diagnostics.append(file_diagnostic)
    if files is None:
        checks["images"] = "unverified"
        checks["image_count"] = "unverified"
        checks["image_names"] = "unverified"
        checks["image_asset_ids"] = "unverified"
        checks["image_order"] = "unverified"
    else:
        observed["image_file_count"] = len(files)
        observed["image_total_count"] = total_count
        observed_images = []
        for index, file_record in enumerate(files):
            raw_name = (
                file_record.get("display_name")
                or file_record.get("name")
                or file_record.get("filename")
                or file_record.get("file_name")
                or ""
            )
            image_name = Path(str(raw_name).replace("\\", "/")).name if raw_name else ""
            observed_images.append({
                "page": expected_images[index]["page"] if index < len(expected_images) else index + 1,
                "image": image_name,
                "asset_id": str(file_record.get("id") or file_record.get("asset_key") or ""),
            })
        observed["images"] = observed_images
        checks["image_count"] = "pass" if len(files) == total_count == expected_count else "mismatch"
        observed_names = [image["image"] for image in observed_images]
        if all(observed_names):
            checks["image_names"] = "pass" if sorted(observed_names) == sorted(expected_names) else "mismatch"
            checks["image_order"] = "pass" if observed_names == expected_names else "mismatch"
        else:
            checks["image_names"] = "unverified"
            checks["image_order"] = "unverified"
        observed_asset_ids = [image["asset_id"] for image in observed_images]
        if expected_asset_ids is None:
            checks["image_asset_ids"] = checkpoint_status
        else:
            checks["image_asset_ids"] = "pass" if observed_asset_ids == expected_asset_ids else "mismatch"
        image_checks = [checks[key] for key in ("image_count", "image_names", "image_asset_ids", "image_order")]
        if "mismatch" in image_checks:
            checks["images"] = "mismatch"
        elif "unverified" in image_checks:
            checks["images"] = "unverified"
        else:
            checks["images"] = "pass"

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
        "expected": {
            "image_count": expected_count,
            "images": [
                {**image, **({"asset_id": expected_asset_ids[index]} if expected_asset_ids is not None else {})}
                for index, image in enumerate(expected_images)
            ],
            "audio_binding_count": 1,
        },
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
    if not course_structure_payload_is_complete(structure_payload):
        return None, [], "Studio course structure is incomplete"
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
    entry["repair_attempts"] = max(int(entry.get("repair_attempts", 0)), int(attempt.get("attempt", 1)))
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
    if not attempt.get("publish_started") and not attempt.get("publish"):
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


def normalize_child_snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    children = value.get("children")
    count = value.get("direct_child_count")
    if not isinstance(children, list) or not isinstance(count, int) or isinstance(count, bool) or count != len(children):
        return None
    normalized = []
    for child in children:
        if not isinstance(child, dict):
            return None
        locator = safe_locator(child.get("locator"))
        block_type = child.get("type")
        order = child.get("order")
        if (
            not locator
            or not isinstance(block_type, str)
            or not block_type.strip()
            or not isinstance(order, int)
            or isinstance(order, bool)
        ):
            return None
        normalized.append({"locator": locator, "type": block_type.strip().lower(), "order": order})
    if len({child["locator"] for child in normalized}) != len(normalized):
        return None
    return {"direct_child_count": count, "children": normalized}


def observed_child_snapshot(vertical: dict[str, Any]) -> dict[str, Any] | None:
    blocks = vertical.get("blocks")
    if not isinstance(blocks, list):
        return None
    if any(
        not isinstance(block, dict)
        or not safe_locator(publisher.block_locator(block))
        or not publisher.block_category(block)
        for block in blocks
    ):
        return None
    return normalize_child_snapshot(publisher.vertical_child_snapshot(vertical))


def expected_vertical_snapshots(operation: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str], set[str], set[str]]:
    expected: dict[str, dict[str, Any]] = {}
    missing: set[str] = set()
    ambiguous: set[str] = set()
    unresolved: set[str] = set()
    for entry in operation.get("galleries", []):
        if not isinstance(entry, dict):
            continue
        locator = safe_locator(entry.get("vertical_locator"))
        if not locator:
            continue
        integrity = entry.get("vertical_integrity") if isinstance(entry.get("vertical_integrity"), dict) else {}
        if integrity.get("status") in {"unresolved", "failed", "blocked"}:
            unresolved.add(locator)
        snapshot_value = integrity.get("expected") if "expected" in integrity else integrity.get("after")
        snapshot = normalize_child_snapshot(snapshot_value)
        if snapshot is None:
            missing.add(locator)
            continue
        if locator in expected and expected[locator] != snapshot:
            ambiguous.add(locator)
        else:
            expected[locator] = snapshot
    return expected, missing, ambiguous, unresolved


def final_vertical_snapshot_contract(snapshot: dict[str, Any], gallery_locator: str) -> bool:
    children = snapshot.get("children") if isinstance(snapshot, dict) else None
    return bool(
        snapshot.get("direct_child_count") == 2
        and isinstance(children, list)
        and len(children) == 2
        and children[0].get("locator") == gallery_locator
        and children[0].get("type") == "imagesgallery"
        and children[0].get("order") == 1
        and children[1].get("type") == "html"
        and children[1].get("order") == 2
    )


def live_vertical_structure_status(
    structure: dict[str, Any],
    locator: str,
    *,
    gallery_locator: str,
    expected: dict[str, dict[str, Any]],
    missing: set[str],
    ambiguous: set[str],
) -> str:
    if locator not in expected or locator in missing or locator in ambiguous:
        return "unverified"
    if not final_vertical_snapshot_contract(expected[locator], gallery_locator):
        return "mismatch"
    matches = [
        record.get("vertical")
        for record in publisher.iter_section_vertical_records(structure)
        if isinstance(record.get("vertical"), dict)
        and safe_locator(record["vertical"].get("block_location")) == locator
    ]
    if len(matches) != 1:
        return "unverified"
    if not isinstance(matches[0].get("published"), bool):
        return "unverified"
    observed = observed_child_snapshot(matches[0])
    if observed is None:
        return "unverified"
    return "verified" if observed == expected[locator] else "mismatch"


def _section_url(studio_url: str, locator: str) -> str:
    if not locator:
        return studio_url
    parsed = urlsplit(studio_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return studio_url
    return f"{parsed.scheme}://{parsed.netloc}/container/{locator}"


def _publish_step_message(step: Any) -> str:
    step_name = str(step or "")
    image_match = re.fullmatch(r"upload_image\[(\d+)\]", step_name)
    if image_match:
        return f"第 {image_match.group(1)} 页图片上传失败。"
    messages = {
        "create_block": "创建 imagegallery 组件失败。",
        "delete_block": "替换原组件失败。",
        "resolve_replacement_target": "无法确认要替换的原组件。",
        "remove_old_html_summary": "替换原组件失败。",
        "verify_vertical_before_create": "小节结构检查未通过，未创建 imagegallery。",
        "verify_vertical_before_move": "小节结构检查未通过，未继续调整组件。",
        "verify_vertical_before_delete": "小节结构检查未通过，未继续替换原组件。",
        "verify_vertical_after_create": "imagegallery 创建后的小节结构检查未通过。",
        "reorder_children": "调整小节组件顺序失败。",
        "change_files_order": "调整 imagegallery 图片顺序失败。",
        "upload_audio_prepare": "准备音频上传失败。",
        "upload_audio_multipart": "音频文件上传失败。",
        "upload_audio": "音频文件上传失败。",
        "upload_audio_register": "注册音频失败。",
        "save_audio_binding": "保存音频绑定失败。",
        "submit_studio_edits": "保存 imagegallery 展示设置失败。",
        "save_block": "保存 imagegallery 字幕配置失败。",
        "publish_vertical": "发布小节失败。",
    }
    if step_name.startswith("upload_image["):
        return "图片上传失败。"
    if step_name.startswith("save_") or step_name.startswith("update_play_config"):
        return "保存 imagegallery 的字幕或音频配置失败。"
    if step_name in {"target_preflight", "create_block", "verify_vertical_after_create", "publish_structure"}:
        return "imagegallery 替换或发布流程失败。"
    return messages.get(step_name, "imagegallery 发布流程未完成。")


def _validation_message(check_name: str, status: str) -> str:
    if status == "unverified":
        messages = {
            "images": "暂时无法确认 imagegallery 图片是否完整。",
            "image_count": "暂时无法确认 imagegallery 图片数量。",
            "image_names": "暂时无法确认 imagegallery 图片是否与清单一致。",
            "image_asset_ids": "暂时无法确认 imagegallery 图片资源。",
            "image_order": "暂时无法确认 imagegallery 图片顺序。",
            "play_config": "暂时无法确认已保存字幕配置。",
            "audio_bindings": "暂时无法确认音频绑定。",
        }
        return messages.get(check_name, "暂时无法确认 imagegallery 的完整状态。")
    messages = {
        "images": "imagegallery 图片与本地清单不一致。",
        "image_count": "imagegallery 图片数量与本地清单不一致。",
        "image_names": "imagegallery 图片名称与本地清单不一致。",
        "image_asset_ids": "imagegallery 图片资源与发布记录不一致。",
        "image_order": "imagegallery 图片顺序与本地清单不一致。",
        "play_config": "已保存字幕配置条目数与图片数量不一致。",
        "audio_bindings": "imagegallery 的音频绑定数量不是一个。",
    }
    return messages.get(check_name, "imagegallery 内容与本地清单不一致。")


OPERATOR_REPORT_MODES = {"categories", "sections"}
OPERATOR_REPORT_CATEGORY_ORDER = (
    "开头出现 >",
    "章节未发布",
    "加粗识别失败",
    "正文与 Markdown 标题顺序",
    "ImageGallery 相关问题",
    "小节组件结构",
    "课程级问题",
    "其他问题",
)


def _category_grouped_findings(
    section_groups: list[dict[str, Any]],
    course_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    def add(category: str, finding: dict[str, Any]) -> None:
        group = grouped.setdefault(category, {"name": category, "findings": []})
        if finding not in group["findings"]:
            group["findings"].append(finding)

    for section in section_groups:
        for finding in section["findings"]:
            add(finding.get("category") or "其他问题", {
                "name": section["name"],
                "locator": section["locator"],
                "studio_url": section["studio_url"],
                "element_name": finding.get("element_name") or section["name"],
                "element_locator": finding.get("element_locator") or section["locator"],
                "element_studio_url": finding.get("element_studio_url") or section["studio_url"],
                "status": finding["status"],
                "source": finding["source"],
                "message": finding["message"],
            })
    for finding in course_findings:
        add(finding.get("category") or "课程级问题", {
            "name": "课程",
            "locator": "",
            "studio_url": finding["studio_url"],
            "status": finding["status"],
            "source": finding["source"],
            "message": finding["message"],
        })

    order = {name: index for index, name in enumerate(OPERATOR_REPORT_CATEGORY_ORDER)}
    return sorted(grouped.values(), key=lambda group: (order.get(group["name"], len(order)), group["name"]))


def _operator_report(operation: dict[str, Any], *, report_mode: str = "categories") -> dict[str, Any]:
    if report_mode not in OPERATOR_REPORT_MODES:
        raise ValueError("Unsupported operator report mode")
    course_check = operation.get("course_structure_verification")
    course_check = course_check if isinstance(course_check, dict) else {}
    section_records = course_check.get("subsections") if isinstance(course_check.get("subsections"), list) else []

    groups: dict[str, dict[str, Any]] = {}
    section_by_locator: dict[str, dict[str, Any]] = {}
    vertical_owners: dict[str, str] = {}
    vertical_details: dict[str, dict[str, str]] = {}
    for subsection in section_records:
        if not isinstance(subsection, dict):
            continue
        section_locator = safe_locator(subsection.get("section_locator"))
        vertical_locator = safe_locator(subsection.get("vertical_locator"))
        if not section_locator:
            continue
        route = section_by_locator.get(section_locator)
        if route is None:
            route = {
                "name": str(subsection.get("section_title") or subsection.get("vertical_title") or "课程小节"),
                "locator": section_locator,
                "studio_url": _section_url(str(subsection.get("studio_url") or ""), section_locator),
                "findings": [],
            }
            section_by_locator[section_locator] = route
        if vertical_locator:
            vertical_owners[vertical_locator] = section_locator
            vertical_details[vertical_locator] = {
                "name": str(subsection.get("vertical_title") or subsection.get("section_title") or "赋能内容"),
                "locator": vertical_locator,
                "studio_url": str(subsection.get("studio_url") or ""),
            }

    def ensure_group(locator: str, name: str, studio_url: str) -> dict[str, Any]:
        locator = safe_locator(locator)
        key = locator or studio_url or name or "course-section"
        if key not in groups:
            known = section_by_locator.get(locator)
            groups[key] = {
                "name": (known or {}).get("name") or name or "课程小节",
                "locator": locator,
                "studio_url": (known or {}).get("studio_url") or studio_url,
                "findings": [],
            }
        return groups[key]

    def add_section_finding(
        locator: str,
        fallback_name: str,
        fallback_url: str,
        status: str,
        source: str,
        message: str,
        category: str,
    ) -> None:
        if status not in {"issue", "unverified"}:
            return
        route_locator = vertical_owners.get(safe_locator(locator), locator)
        route = section_by_locator.get(route_locator)
        group = ensure_group(
            route_locator,
            str((route or {}).get("name") or fallback_name),
            str((route or {}).get("studio_url") or fallback_url),
        )
        element = vertical_details.get(safe_locator(locator))
        if element:
            element_name = element["name"]
            element_locator = element["locator"]
            element_studio_url = element["studio_url"]
        else:
            element_name = str((route or {}).get("name") or fallback_name or "课程小节")
            element_locator = safe_locator(locator) or route_locator
            element_studio_url = str((route or {}).get("studio_url") or fallback_url)
        finding = {
            "status": status,
            "source": source,
            "category": category,
            "element_name": element_name,
            "element_locator": element_locator,
            "element_studio_url": element_studio_url,
            "message": message,
        }
        if finding not in group["findings"]:
            group["findings"].append(finding)

    for subsection in section_records:
        if not isinstance(subsection, dict):
            continue
        locator = safe_locator(subsection.get("vertical_locator"))
        if subsection.get("status") == "issue":
            issues = subsection.get("issues") if isinstance(subsection.get("issues"), list) else []
            for issue in issues:
                message = "小节组件结构与预期不一致。" if issue == "structure_mismatch" else "Studio 确认该小节尚未发布。" if issue == "published_false" else "小节发布前置检查未完成。"
                category = "小节组件结构" if issue == "structure_mismatch" else "章节未发布"
                add_section_finding(locator, str(subsection.get("section_title") or "课程小节"), str(subsection.get("studio_url") or ""), "issue", "publisher", message, category)
        if subsection.get("status") == "unverified":
            add_section_finding(locator, str(subsection.get("section_title") or "课程小节"), str(subsection.get("studio_url") or ""), "unverified", "publisher", "暂时无法确认小节组件结构和发布状态。", "小节结构校验")

    for entry in operation.get("galleries", []):
        if not isinstance(entry, dict):
            continue
        locator = safe_locator(entry.get("vertical_locator"))
        fallback_url = vertical_details.get(locator, {}).get("studio_url", "")
        publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
        steps = publish.get("steps") if isinstance(publish.get("steps"), list) else []
        failed_steps = [item.get("step") for item in steps if isinstance(item, dict) and item.get("status") in {"failed", "blocked"}]
        if not failed_steps and publish.get("status") != "completed":
            failed_steps = [publish.get("failed_step") or "publish"]
        for step in failed_steps:
            category = "章节未发布" if str(step) == "publish_vertical" else "小节组件结构" if str(step).startswith("verify_vertical") else "ImageGallery 相关问题"
            add_section_finding(locator, "课程小节", fallback_url, "issue", "publisher", _publish_step_message(step), category)

        initial_vertical = entry.get("initial_vertical_publish") if isinstance(entry.get("initial_vertical_publish"), dict) else {}
        if not initial_vertical:
            add_section_finding(locator, "课程小节", fallback_url, "unverified", "publisher", "暂时无法确认小节发布请求结果。", "章节未发布")
        elif initial_vertical.get("status") != "completed":
            add_section_finding(locator, "课程小节", fallback_url, "issue", "publisher", "发布小节的请求未成功。", "章节未发布")

        integrity = entry.get("vertical_integrity") if isinstance(entry.get("vertical_integrity"), dict) else {}
        integrity_steps = integrity.get("steps") if isinstance(integrity.get("steps"), list) else []
        integrity_failures = [
            item.get("step") for item in integrity_steps
            if isinstance(item, dict) and item.get("status") in {"failed", "blocked"}
        ]
        for step in integrity_failures:
            add_section_finding(locator, "课程小节", fallback_url, "issue", "publisher", _publish_step_message(step), "小节组件结构")
        if integrity.get("status") in {"failed", "unresolved", "blocked"} and not integrity_failures:
            add_section_finding(locator, "课程小节", fallback_url, "issue", "publisher", "小节结构安全检查未完成，imagegallery 替换未完成。", "小节组件结构")

        validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
        checks = validation.get("checks") if isinstance(validation.get("checks"), dict) else {}
        if validation.get("status") == "mismatch":
            mismatches = [key for key, value in checks.items() if value == "mismatch"]
            for check_name in mismatches or ["gallery"]:
                add_section_finding(locator, "课程小节", fallback_url, "issue", "publisher", _validation_message(str(check_name), "mismatch"), "ImageGallery 相关问题")
        elif validation.get("status") in {"unverified", "failed"}:
            failed_step = str(validation.get("failed_step") or "")
            related_check = {"get_files": "images", "get_play_config": "play_config", "get_audios": "audio_bindings"}.get(failed_step, "gallery")
            add_section_finding(locator, "课程小节", fallback_url, "unverified", "publisher", _validation_message(related_check, "unverified"), "ImageGallery 相关问题")

        history = entry.get("repair_history") if isinstance(entry.get("repair_history"), list) else []
        for attempt in history:
            if not isinstance(attempt, dict) or attempt.get("status") in {"completed", "verified"}:
                continue
            repair_step = attempt.get("failed_step") or "repair"
            add_section_finding(locator, "课程小节", fallback_url, "issue", "publisher", f"imagegallery 自动修复未完成：{_publish_step_message(repair_step)}", "ImageGallery 相关问题")

    course_findings: list[dict[str, Any]] = []

    if course_check.get("status") == "unverified" and not section_records:
        course_findings.append({
            "status": "unverified",
            "source": "publisher",
            "category": "课程级问题",
            "message": "暂时无法读取课程结构，无法确认本次发布状态。",
            "studio_url": "",
        })
    if course_check.get("status") == "incomplete" and not any(group["findings"] for group in groups.values()):
        course_findings.append({
            "status": "issue",
            "source": "publisher",
            "category": "课程级问题",
            "message": "课程组件结构校验发现需要处理的问题。",
            "studio_url": "",
        })
    visible_section_groups = [group for group in groups.values() if group["findings"]]
    issue_count = sum(finding["status"] == "issue" for group in visible_section_groups for finding in group["findings"])
    issue_count += sum(finding["status"] == "issue" for finding in course_findings)
    unverified_count = sum(finding["status"] == "unverified" for group in visible_section_groups for finding in group["findings"])
    unverified_count += sum(finding["status"] == "unverified" for finding in course_findings)
    status = "issue" if issue_count else "unverified" if unverified_count else "pass"
    summary = (
        f"发现 {issue_count} 项已确认问题，另有 {unverified_count} 项无法确认。请按下方 Studio 链接复核。"
        if status != "pass"
        else ""
    )
    if report_mode == "categories":
        visible_groups = _category_grouped_findings(visible_section_groups, course_findings)
    else:
        visible_groups = visible_section_groups
    lines = [summary] if summary else []
    if report_mode == "categories":
        for group in visible_groups:
            lines.extend(("", f"### {group['name']}"))
            for finding in group["findings"]:
                name = str(finding.get("name") or "课程小节")
                url = str(finding.get("studio_url") or "")
                element_name = str(finding.get("element_name") or name)
                element_locator = safe_locator(finding.get("element_locator"))
                element_url = str(finding.get("element_studio_url") or url)
                if element_locator and element_locator != safe_locator(finding.get("locator")) and element_url:
                    section_link = f"[{name}]({url})" if url else name
                    route = f"{section_link}：[查看“{element_name}”]({element_url})"
                else:
                    route = f"[{element_name}]({element_url})" if element_url else name
                label = "已确认" if finding["status"] == "issue" else "无法确认"
                lines.append(f"- {route}（{label}）：{finding['message']}")
    else:
        for group in visible_groups:
            name = str(group.get("name") or "课程小节")
            url = str(group.get("studio_url") or "")
            heading = f"### [{name}]({url})" if url else f"### {name}"
            lines.extend(("", heading))
            for finding in group["findings"]:
                label = "已确认问题" if finding["status"] == "issue" else "无法确认"
                lines.append(f"- {label}：{finding['message']}")
        if course_findings:
            lines.extend(("", "### 课程"))
            if course_url:
                lines[-1] = f"### [课程]({course_url})"
            for finding in course_findings:
                label = "已确认问题" if finding["status"] == "issue" else "无法确认"
                lines.append(f"- {label}：{finding['message']}")
    return {
        "status": status,
        "mode": report_mode,
        "issue_count": issue_count,
        "unverified_count": unverified_count,
        "summary": summary,
        "groups": visible_groups,
        "course_findings": course_findings,
        "text": "\n".join(lines),
    }


def verify_final_course_structure(
    client: StudioClient,
    *,
    studio_base: str,
    course_key: str,
    studio_url: str,
    operation: dict[str, Any],
) -> dict[str, Any]:
    payload, diagnostic = read_json_call(
        client,
        "verify_final_course_structure",
        "GET",
        f"{studio_base}/course/{course_key}",
        params={"format": "concise"},
        referer=studio_url,
    )
    if diagnostic and diagnostic.get("status") != "ok":
        summary = safe_summary_text(diagnostic.get("error_summary") or "Final Studio course structure could not be read")
        result = {
            "status": "unverified",
            "issue_count": 0,
            "unverified_count": 1,
            "subsection_count": 0,
            "verified_count": 0,
            "subsections": [],
            "failed_step": "verify_final_course_structure",
            "error_summary": summary,
        }
        if isinstance(diagnostic.get("http_status"), int):
            result["http_status"] = diagnostic["http_status"]
        return result

    if not course_structure_payload_is_complete(payload):
        return {
            "status": "unverified",
            "issue_count": 0,
            "unverified_count": 1,
            "subsection_count": 0,
            "verified_count": 0,
            "subsections": [],
            "failed_step": "verify_final_course_structure",
            "error_summary": "Final Studio course structure is incomplete",
        }

    try:
        structure = publisher.normalize_course_structure_payload(payload)
        records = publisher.iter_section_vertical_records(structure) if isinstance(structure.get("chapters"), list) else []
    except Exception:
        records = []
    if not records:
        return {
            "status": "unverified",
            "issue_count": 0,
            "unverified_count": 1,
            "subsection_count": 0,
            "verified_count": 0,
            "subsections": [],
            "failed_step": "verify_final_course_structure",
            "error_summary": "Final Studio course structure contained no verifiable subsections",
        }

    expected_by_vertical, missing_expectations, ambiguous_expectations, unresolved_operations = expected_vertical_snapshots(operation)
    target_verticals = {
        safe_locator(entry.get("vertical_locator"))
        for entry in operation.get("galleries", [])
        if isinstance(entry, dict) and safe_locator(entry.get("vertical_locator"))
    }
    target_verticals |= set(expected_by_vertical) | missing_expectations | ambiguous_expectations | unresolved_operations
    gallery_by_vertical: dict[str, str] = {}
    for entry in operation.get("galleries", []):
        if not isinstance(entry, dict):
            continue
        locator = safe_locator(entry.get("vertical_locator"))
        gallery_locator = safe_locator(entry.get("gallery_locator"))
        if locator and gallery_locator:
            prior = gallery_by_vertical.get(locator)
            gallery_by_vertical[locator] = gallery_locator if prior in {None, gallery_locator} else ""
    integrity_statuses: dict[str, set[str]] = {}
    for entry in operation.get("galleries", []):
        if not isinstance(entry, dict):
            continue
        locator = safe_locator(entry.get("vertical_locator"))
        integrity = entry.get("vertical_integrity") if isinstance(entry.get("vertical_integrity"), dict) else {}
        if locator and isinstance(integrity.get("status"), str):
            integrity_statuses.setdefault(locator, set()).add(integrity["status"])
    subsection_results = []
    issue_count = 0
    unverified_count = 0
    observed_verticals = set()
    for record in records:
        section = record.get("section") if isinstance(record.get("section"), dict) else {}
        vertical = record.get("vertical") if isinstance(record.get("vertical"), dict) else {}
        locator = safe_locator(vertical.get("block_location"))
        if locator not in target_verticals:
            continue
        if locator:
            observed_verticals.add(locator)
        observed = observed_child_snapshot(vertical)
        issues = []
        unverified_reasons = []
        expected = expected_by_vertical.get(locator)

        if not locator:
            unverified_reasons.append("vertical_locator_missing")
        if observed is None:
            structure_status = "unverified"
            unverified_reasons.append("observed_structure_invalid")
        elif locator in ambiguous_expectations:
            structure_status = "unverified"
            unverified_reasons.append("expected_structure_ambiguous")
        elif locator in missing_expectations:
            structure_status = "unverified"
            unverified_reasons.append("expected_structure_missing")
        elif expected is not None and not final_vertical_snapshot_contract(expected, gallery_by_vertical.get(locator, "")):
            structure_status = "issue"
            issues.append("expected_structure_contract_invalid")
        elif expected is not None and observed != expected:
            structure_status = "issue"
            issues.append("structure_mismatch")
        elif expected is not None:
            structure_status = "verified"
        else:
            structure_status = "unverified"
            unverified_reasons.append("expected_structure_missing")

        published_value = vertical.get("published")
        published = published_value if isinstance(published_value, bool) else None
        if published is False:
            issues.append("published_false")
        elif published is None:
            unverified_reasons.append("published_state_missing")

        if issues:
            status = "issue"
        elif unverified_reasons:
            status = "unverified"
        else:
            status = "verified"
        issue_count += len(issues)
        unverified_count += len(unverified_reasons)
        subsection = {
            "section_title": str(section.get("name", "")),
            "section_locator": safe_locator(section.get("block_location")),
            "vertical_title": str(vertical.get("name", "")),
            "vertical_locator": locator,
            "studio_url": publisher.build_container_block_url(studio_base, locator) if locator else "",
            "status": status,
            "structure_status": structure_status,
            "expected": expected,
            "observed": observed,
            "published": published,
            "publisher_integrity_status": (
                next(iter(integrity_statuses[locator]))
                if locator in integrity_statuses and len(integrity_statuses[locator]) == 1
                else "ambiguous" if locator in integrity_statuses else "missing"
            ),
            "issues": issues,
            "unverified_reasons": unverified_reasons,
        }
        subsection_results.append(subsection)

    owners = map_gallery_owners(structure)
    for item in subsection_results:
        locator = safe_locator(item.get("vertical_locator"))
        gallery_locator = gallery_by_vertical.get(locator, "")
        if not gallery_locator or not locator:
            continue
        owner_matches = owners.get(gallery_locator, [])
        if len(owner_matches) != 1 or owner_matches[0] != locator:
            item["issues"].append("gallery_owner_mismatch")
            item["status"] = "issue"
            issue_count += 1

    for locator in sorted(target_verticals - observed_verticals):
        issues = ["target_vertical_missing"]
        if locator in unresolved_operations:
            issues.append("publisher_vertical_integrity_unresolved")
        subsection_results.append({
            "section_title": "",
            "section_locator": "",
            "vertical_title": "",
            "vertical_locator": locator,
            "studio_url": publisher.build_container_block_url(studio_base, locator),
            "status": "issue",
            "structure_status": "issue",
            "expected": expected_by_vertical.get(locator),
            "observed": None,
            "published": None,
            "issues": issues,
            "unverified_reasons": [],
        })
        issue_count += len(issues)

    verified_count = sum(item["status"] == "verified" for item in subsection_results)
    if issue_count:
        status = "incomplete"
    elif unverified_count:
        status = "unverified"
    else:
        status = "verified"
    return {
        "status": status,
        "issue_count": issue_count,
        "unverified_count": unverified_count,
        "subsection_count": len(subsection_results),
        "verified_count": verified_count,
        "subsections": subsection_results,
    }


def summarize_batch(operation: dict[str, Any], *, report_mode: str = "categories") -> dict[str, Any]:
    entries = operation["galleries"]
    course_check = operation.get("course_structure_verification")
    if not isinstance(course_check, dict):
        course_check = {"status": "unverified", "issue_count": 0, "unverified_count": 1, "subsection_count": 0, "verified_count": 0, "subsections": []}
    vertical_results = {
        safe_locator(item.get("vertical_locator")): item
        for item in course_check.get("subsections", [])
        if isinstance(item, dict) and safe_locator(item.get("vertical_locator"))
    }
    all_verified = bool(entries)
    results = []
    gallery_issue_count = 0
    gallery_unverified_count = 0
    operation_issue_count = 0
    for entry in entries:
        validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
        vertical_locator = safe_locator(entry.get("vertical_locator"))
        vertical = vertical_results.get(vertical_locator, {})
        vertical_ok = vertical.get("status") == "verified" and vertical.get("published") is True
        history = entry.get("repair_history") if isinstance(entry.get("repair_history"), list) else []
        publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
        initial_vertical = entry.get("initial_vertical_publish") if isinstance(entry.get("initial_vertical_publish"), dict) else {}
        original_publish = history[0].get("initial_publish") if history and isinstance(history[0], dict) else publish
        original_publish = original_publish if isinstance(original_publish, dict) else publish
        publish_steps = original_publish.get("steps") if isinstance(original_publish.get("steps"), list) else []
        has_failed_publish_step = any(
            isinstance(step, dict) and step.get("status") in {"failed", "blocked"}
            for step in publish_steps
        )
        operation_ok = (
            original_publish.get("status") == "completed"
            and initial_vertical.get("status") == "completed"
            and not has_failed_publish_step
        )
        repair_attempts = entry.get("repair_attempts", 0)
        gallery_ok = validation.get("status") == "verified" and vertical_ok
        repair_ok = True
        if history and isinstance(history[-1], dict):
            latest_repair = history[-1]
            repair_vertical = latest_repair.get("vertical_publish") if isinstance(latest_repair.get("vertical_publish"), dict) else {}
            repair_validation = latest_repair.get("final_validation") if isinstance(latest_repair.get("final_validation"), dict) else {}
            repair_ok = (
                latest_repair.get("status") == "completed"
                and repair_vertical.get("status") == "completed"
                and repair_validation.get("status") == "verified"
            )
        # Studio's current saved state is authoritative; earlier write responses are diagnostics only.
        all_verified = all_verified and gallery_ok
        if validation.get("status") in {"mismatch", "failed"} or (validation.get("status") == "verified" and not vertical_ok):
            gallery_issue_count += 1
        elif validation.get("status") != "verified":
            gallery_unverified_count += 1
        operation_status = "completed" if operation_ok and repair_ok else "has_issues"
        if operation_status != "completed":
            operation_issue_count += 1
        item = {
            "gallery_key": entry.get("gallery_key", ""),
            "status": validation.get("status", "unverified"),
            "operation_status": operation_status,
            "initial_vertical_publish_status": (entry.get("initial_vertical_publish") or {}).get("status", "missing") if isinstance(entry.get("initial_vertical_publish"), dict) else "missing",
            "expected": validation.get("expected", {}),
            "observed": validation.get("observed", {}),
            "repair_attempts": repair_attempts,
        }
        publish_report = {key: publish[key] for key in ("status", "completed_stage", "active_step", "failed_step", "failure_http_status", "error_summary", "failed_page_image", "asset_ids", "images", "steps") if key in publish}
        if publish_report:
            item["publish"] = publish_report
        if history and isinstance(history[0], dict) and isinstance(history[0].get("initial_publish"), dict):
            item["initial_publish"] = copy.deepcopy(history[0]["initial_publish"])
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
            item["repair_history"] = copy.deepcopy(history)
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
        integrity = entry.get("vertical_integrity") if isinstance(entry.get("vertical_integrity"), dict) else {}
        if integrity:
            vertical = next((
                subsection for subsection in (operation.get("course_structure_verification") or {}).get("subsections", [])
                if isinstance(subsection, dict) and subsection.get("vertical_locator") == safe_locator(entry.get("vertical_locator"))
            ), None)
            item["vertical_integrity"] = {
                "status": vertical.get("structure_status", "unverified") if isinstance(vertical, dict) else "unverified",
                "published": vertical.get("published") if isinstance(vertical, dict) else None,
                "before": integrity.get("before"),
                "expected": integrity.get("expected"),
                "observed": vertical.get("observed") if isinstance(vertical, dict) else None,
                "steps": copy.deepcopy(integrity.get("steps", [])),
            }
        item["asset_ids"] = [value for value in (entry.get("publish") or {}).get("asset_ids", []) if isinstance(value, str)] if isinstance(entry.get("publish"), dict) else []
        results.append(item)

    course_issues = course_check.get("issue_count", 0) if isinstance(course_check.get("issue_count"), int) else 0
    course_unverified = course_check.get("unverified_count", 0) if isinstance(course_check.get("unverified_count"), int) else 1
    all_verified = all_verified and course_check.get("status") == "verified" and course_issues == 0 and course_unverified == 0
    operator_report = _operator_report(operation, report_mode=report_mode)
    result = {
        "status": "verified" if all_verified else "incomplete",
        "all_sections_verified": all_verified,
        "gallery_count": len(results),
        "verified_count": sum(item["status"] == "verified" for item in results),
        "issue_count": gallery_issue_count + course_issues,
        "unverified_count": gallery_unverified_count + course_unverified,
        "operation_issue_count": operation_issue_count,
        "course_structure_verification": course_check,
        "operator_report": operator_report,
        "galleries": results,
        "completed_at": utc_now(),
    }
    existing_result = operation.get("batch_result") if isinstance(operation.get("batch_result"), dict) else {}
    skill_provenance = operation.get("skill_provenance", existing_result.get("skill_provenance"))
    if isinstance(skill_provenance, dict):
        result["skill_provenance"] = copy.deepcopy(skill_provenance)
    provenance = operation.get("provenance", existing_result.get("provenance"))
    if isinstance(provenance, dict):
        result["provenance"] = copy.deepcopy(provenance)
    return result


def run_validation(args: argparse.Namespace) -> dict[str, Any]:
    operation_path = Path(args.operation_file).expanduser().resolve()
    operation = load_operation(operation_path)
    report_mode = args.report_mode or "categories"
    operation["operator_report_mode"] = report_mode
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

    def read_live_structure() -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        payload, diagnostic = read_json_call(
            client,
            "load_live_course_structure",
            "GET",
            f"{studio_base}/course/{course_key}",
            params={"format": "concise"},
            referer=args.studio_url,
        )
        if diagnostic and diagnostic["status"] != "ok":
            return None, diagnostic, str(diagnostic.get("error_summary") or "Studio course structure could not be read")
        if not course_structure_payload_is_complete(payload):
            return None, diagnostic, "Studio course structure is incomplete"
        try:
            structure = publisher.normalize_course_structure_payload(payload)
        except Exception:
            return None, diagnostic, "Studio course structure could not be normalized"
        if not isinstance(structure.get("chapters"), list):
            return None, diagnostic, "Studio course structure has an invalid shape"
        return structure, diagnostic, ""

    def audit_batch_round(round_number: int) -> dict[str, list[str]]:
        structure, structure_diagnostic, structure_error = read_live_structure()
        owners = map_gallery_owners(structure) if structure is not None else {}
        expected_verticals, missing_verticals, ambiguous_verticals, _ = expected_vertical_snapshots(operation)
        vertical_records = []
        if structure is not None:
            try:
                vertical_records = [
                    record["vertical"] for record in publisher.iter_section_vertical_records(structure)
                    if isinstance(record.get("vertical"), dict)
                ]
            except Exception:
                structure_error = "Studio course structure could not be inspected"
                structure = None
                owners = {}
                vertical_records = []
        for entry in operation["galleries"]:
            gallery_locator = safe_locator(entry.get("gallery_locator"))
            vertical_locator = safe_locator(entry.get("vertical_locator"))
            if not gallery_locator or not vertical_locator:
                validation = invalid_gallery_result(entry, None, step="operation_file", summary="Gallery or vertical locator is invalid")
            else:
                manifest_path = Path(str(entry.get("manifest", ""))).expanduser().resolve()
                try:
                    manifest = publisher.ensure_manifest(manifest_path)
                    validation = validate_gallery(client, entry, manifest)
                except Exception:
                    validation = invalid_gallery_result(entry, None, step="manifest", summary="Local Gallery manifest or package could not be validated")

            checks = validation.setdefault("checks", {})
            vertical_matches = [
                item for item in vertical_records
                if safe_locator(item.get("block_location")) == vertical_locator
            ]
            if structure_error:
                vertical_structure_status = "unverified"
                owner_status = "unverified"
                publication_status = "unverified"
                validation["pre_repair_structure_failed_step"] = "load_live_course_structure"
                validation["pre_repair_structure_error"] = structure_error[:240]
                if isinstance((structure_diagnostic or {}).get("http_status"), int):
                    validation["pre_repair_structure_http_status"] = structure_diagnostic["http_status"]
            else:
                vertical_structure_status = live_vertical_structure_status(
                    structure,
                    vertical_locator,
                    gallery_locator=gallery_locator,
                    expected=expected_verticals,
                    missing=missing_verticals,
                    ambiguous=ambiguous_verticals,
                ) if len(vertical_matches) == 1 else "mismatch"
                owner_matches = owners.get(gallery_locator, [])
                owner_status = "pass" if len(owner_matches) == 1 and owner_matches[0] == vertical_locator else "mismatch"
                published_value = vertical_matches[0].get("published") if len(vertical_matches) == 1 else False
                publication_status = "unverified" if not isinstance(published_value, bool) else "pass" if published_value else "mismatch"
                validation["observed_vertical"] = {
                    "published": published_value if isinstance(published_value, bool) else None,
                    "child_snapshot": observed_child_snapshot(vertical_matches[0]) if len(vertical_matches) == 1 else None,
                }
            checks["vertical_structure"] = "pass" if vertical_structure_status == "verified" else vertical_structure_status
            checks["gallery_owner"] = owner_status
            checks["vertical_publication"] = publication_status
            validation["audit_round"] = round_number
            publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
            validation["publish_status"] = publish.get("status", "not_started")
            if publish.get("failed_step"):
                validation["publish_failed_step"] = publish["failed_step"]
            states = list(checks.values())
            if "unverified" in states:
                validation["status"] = "unverified"
            elif "mismatch" in states:
                validation["status"] = "mismatch"
            else:
                validation["status"] = "verified"
            content_mismatches = any(
                checks.get(name) == "mismatch"
                for name in ("images", "play_config", "audio_bindings")
            )
            publication_mismatch = checks.get("vertical_publication") == "mismatch"
            validation["repairable"] = bool(
                validation["status"] == "mismatch"
                and checks.get("vertical_structure") == "pass"
                and owner_status == "pass"
                and all(state in {"pass", "mismatch"} for state in states)
                and (content_mismatches or publication_mismatch)
            )
            if validation["status"] == "mismatch" and not validation["repairable"]:
                validation["repair_skip_reason"] = "The current section structure or Gallery ownership is not safe to change automatically"
            entry["validation"] = validation
            store()
        return owners

    owners = audit_batch_round(0)
    for cycle in range(1, MAX_REPAIR_CYCLES + 1):
        repair_targets = []
        for entry in operation["galleries"]:
            validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
            if validation.get("status") != "mismatch" or not validation.get("repairable"):
                continue
            attempts = entry.get("repair_attempts", 0)
            resume_attempt = resumable_repair_attempt(entry) if attempts else None
            if resume_attempt is None and attempts >= MAX_REPAIR_CYCLES:
                validation["repair_limit_reached"] = True
                store()
                continue
            try:
                manifest = publisher.ensure_manifest(Path(str(entry.get("manifest", ""))).expanduser().resolve())
            except Exception:
                validation["repairable"] = False
                validation["repair_skip_reason"] = "The local Gallery package could not be checked before repair"
                store()
                continue
            repair_targets.append((entry, manifest, resume_attempt))
        if not repair_targets:
            break
        for entry, manifest, resume_attempt in repair_targets:
            try:
                repair_gallery(client, operation, entry, manifest, args, adapter, set(owners), store, attempt=resume_attempt)
            except Exception as exc:
                history = entry.setdefault("repair_history", [])
                attempt = history[-1] if history and isinstance(history[-1], dict) else {"attempt": len(history) + 1, "steps": []}
                attempt["status"] = "failed"
                attempt["failed_step"] = publisher.safe_step_name(getattr(exc, "step", getattr(exc, "step_name", "repair")))
                attempt["error_summary"] = safe_error(exc, attempt["failed_step"])
                if safe_http_status(exc) is not None:
                    attempt["http_status"] = safe_http_status(exc)
                attempt["finished_at"] = utc_now()
                entry["repair_attempts"] = max(entry.get("repair_attempts", 0), attempt.get("attempt", 1))
                validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
                validation["repair_status"] = "failed"
                entry["validation"] = validation
                store()
        owners = audit_batch_round(cycle)

    operation["course_structure_verification"] = verify_final_course_structure(
        client,
        studio_base=studio_base,
        course_key=course_key,
        studio_url=args.studio_url,
        operation=operation,
    )
    store()
    operation["batch_result"] = summarize_batch(operation, report_mode=report_mode)
    store()
    return operation["batch_result"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and safely repair a published Studio ImageGallery batch")
    parser.add_argument("--studio-url", help="Studio URL for a target vertical in this course")
    parser.add_argument("--operation-file", required=True, help="shared batch-operation.json owned by the batch caller")
    parser.add_argument("--execute", action="store_true", help="read Studio and perform up to two safe repair cycles for confirmed mismatches")
    parser.add_argument(
        "--report-mode",
        choices=sorted(OPERATOR_REPORT_MODES),
        default=None,
        help="group operator findings by check category (default) or by subsection",
    )
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
    args.cookie = args.cookie or os.environ.get("FIRA_SAAS_OP_COOKIE", "")
    args.csrf_token = args.csrf_token or os.environ.get("FIRA_SAAS_OP_CSRF_TOKEN", "")
    if not args.studio_url:
        parser.error("--studio-url is required")
    if not args.execute:
        parser.error("--execute is required because a confirmed mismatch can trigger repair")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main() -> int:
    args = None
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
