#!/usr/bin/env python3
"""Validate and, when evidence is conclusive, repair a published Studio Gallery batch."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import publish_imagegallery_block as publisher


DEFAULT_ADAPTER = Path(__file__).resolve().parents[1] / "references" / "adapter.template.json"
DEFAULT_TIMEOUT = 30
MAX_REPAIR_CYCLES = 2
LOCAL_AUDIO_FINGERPRINT_CACHE_KEY = "_verified_local_audio_fingerprint"


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


def sanitize_operation_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        forbidden = ("response", "request", "authorization", "cookie", "csrf", "password", "secret", "token", "credential")
        safe = {}
        for key, item in value.items():
            if any(word in str(key).lower() for word in forbidden):
                continue
            if key in {"upload_key_path", "download_key_path"}:
                safe[key] = publisher.safe_object_key_path(item)
            else:
                safe[key] = sanitize_operation_evidence(item)
        return safe
    if isinstance(value, list):
        return [sanitize_operation_evidence(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(\"?(?:authorization|cookie|csrf(?:token)?|password|secret|session|token|credential(?:s)?|accesskeyid|accesskeysecret|securitytoken|accesstoken|signature)\"?\s*[:=]\s*\"?)([^\"\s,;}]+)", r"\1<redacted>", value)
        return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    return value


def safe_http_status(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def play_config_is_untrusted(validation: Any) -> bool:
    if not isinstance(validation, dict):
        return False
    return validation.get("play_config_untrusted") is True or validation.get("play_config_read_failed") is True


def safe_publish_state(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    state: dict[str, Any] = {
        "status": raw.get("status") if raw.get("status") in {"not_started", "in_progress", "failed", "partial", "completed"} else "not_started",
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
    audio_upload_evidence = safe_audio_evidence(raw.get("audio_upload_evidence"))
    if audio_upload_evidence:
        state["audio_upload_evidence"] = audio_upload_evidence
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
        entry["repair_publish"] = safe_publish_state(persisted_entries[0].get("repair_publish"))
        repair_locator = safe_locator(persisted_entries[0].get("repair_gallery_locator"))
        if repair_locator:
            entry["repair_gallery_locator"] = repair_locator


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
        if not isinstance(entry.get("publish"), dict):
            entry["publish"] = {}
        else:
            entry["publish"] = sanitize_operation_evidence(entry["publish"])
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
        entry["repair_history"] = [sanitize_operation_evidence(item) for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        entry["audio_evidence"] = safe_audio_evidence(entry.get("audio_evidence"))
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
            raise BatchValidationError("authentication", "Studio access requires credentials or --cookie")
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


course_structure_payload_is_complete = publisher.course_structure_payload_is_complete


def iter_course_nodes(payload: Any):
    pending = [payload]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        children: list[Any] = []
        child_info = node.get("child_info")
        if isinstance(child_info, dict) and isinstance(child_info.get("children"), list):
            children.extend(child_info["children"])
        for key in ("children", "chapters", "sections", "verticals", "blocks"):
            value = node.get(key)
            if isinstance(value, list):
                children.extend(value)
        pending.extend(reversed(children))


def publication_state_for_node(node: dict[str, Any]) -> dict[str, Any]:
    published = node.get("published")
    has_changes = node.get("has_changes")
    visibility_present = "visibility_state" in node
    visibility_state = node.get("visibility_state")
    issues = []
    unverified_reasons = []

    if published is False:
        issues.append("published_false")
    elif not isinstance(published, bool):
        unverified_reasons.append("published_state_missing")
    if has_changes is True or visibility_state == "needs_attention":
        issues.append("unpublished_changes")
    elif not isinstance(has_changes, bool):
        unverified_reasons.append("pending_changes_state_missing")
    if visibility_present and (not isinstance(visibility_state, str) or not visibility_state.strip()):
        unverified_reasons.append("visibility_state_invalid")

    status = "unverified" if unverified_reasons else "mismatch" if issues else "pass"
    evidence: dict[str, Any] = {
        "status": status,
        "published": published if isinstance(published, bool) else None,
        "has_changes": has_changes if isinstance(has_changes, bool) else None,
        "issues": issues,
        "unverified_reasons": unverified_reasons,
    }
    if isinstance(visibility_state, str) and visibility_state.strip():
        evidence["visibility_state"] = visibility_state.strip()
    return evidence


def saved_publication_states(payload: Any, target_verticals: set[str]) -> dict[str, dict[str, Any]]:
    matches: dict[str, list[dict[str, Any]]] = {locator: [] for locator in target_verticals}
    for node in iter_course_nodes(payload):
        locator = safe_locator(node.get("id") or node.get("block_location") or node.get("locator"))
        if locator in matches and "+type@vertical+" in locator:
            matches[locator].append(node)

    results = {}
    for locator, nodes in matches.items():
        if len(nodes) == 1:
            results[locator] = publication_state_for_node(nodes[0])
        else:
            results[locator] = {
                "status": "unverified",
                "published": None,
                "has_changes": None,
                "issues": [],
                "unverified_reasons": ["publication_state_missing" if not nodes else "publication_state_ambiguous"],
            }
    return results


def read_saved_publication_states(
    client: StudioClient,
    *,
    studio_base: str,
    course_key: str,
    studio_url: str,
    target_verticals: set[str],
    step: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    payload, diagnostic = read_json_call(
        client,
        step,
        "GET",
        f"{studio_base}/course/{course_key}",
        referer=studio_url,
    )
    if diagnostic and diagnostic.get("status") != "ok":
        states = {
            locator: {
                "status": "unverified",
                "published": None,
                "has_changes": None,
                "issues": [],
                "unverified_reasons": ["publication_state_read_failed"],
                "failed_step": publisher.safe_step_name(step),
                "error_summary": safe_summary_text(diagnostic.get("error_summary") or "Saved Studio publication state could not be read"),
            }
            for locator in target_verticals
        }
        if isinstance(diagnostic.get("http_status"), int):
            for state in states.values():
                state["http_status"] = diagnostic["http_status"]
        return states, diagnostic
    return saved_publication_states(payload, target_verticals), diagnostic


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
    repair_publish = entry.get("repair_publish")
    publish = repair_publish if isinstance(repair_publish, dict) and repair_publish.get("status") == "completed" else entry.get("publish")
    publish = publish if isinstance(publish, dict) else {}
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


def audio_binding_id(binding: dict[str, Any]) -> str:
    for key in ("document_id", "audio_id", "id"):
        value = binding.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def audio_length_from_headers(headers: Any) -> int | None:
    if not isinstance(headers, Mapping):
        return None
    content_range = headers.get("Content-Range") or headers.get("content-range")
    if isinstance(content_range, str):
        match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", content_range.strip(), re.IGNORECASE)
        if match:
            return int(match.group(1))
    content_length = headers.get("Content-Length") or headers.get("content-length")
    try:
        length = int(content_length)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def read_audio_range_byte(download_url: str, *, timeout: int) -> tuple[str, int | None, int | None]:
    """Check a GET-signed audio object URL without downloading the full MP3."""
    response = None
    try:
        response = publisher.requests.get(
            download_url,
            headers={"Range": "bytes=0-0"},
            timeout=timeout,
            stream=True,
        )
        status = int(response.status_code)
        object_length = audio_length_from_headers(getattr(response, "headers", {}))
        if status in {200, 206}:
            raw = getattr(response, "raw", None)
            byte = raw.read(1) if raw is not None else b""
            return ("accessible" if byte else "unverified"), status, object_length
        if status == 404:
            return "missing", status, object_length
        if status < 400:
            return "unverified", status, object_length
        # OSS commonly includes NoSuchKey in the XML body even when a proxy
        # changes the HTTP status. Read only a small prefix of an error body.
        raw = getattr(response, "raw", None)
        prefix = raw.read(1024) if raw is not None else b""
        if b"NoSuchKey" in prefix:
            return "missing", status, object_length
        return "unverified", status, object_length
    except Exception:
        return "unverified", None, None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def audio_download_url(client: StudioClient, locator: str, audio_id: str, referer: str) -> tuple[str, dict[str, Any] | None]:
    payload, diagnostic = read_json_call(
        client,
        "get_audio_download_url",
        "POST",
        f"{client.studio_base}/preview/xblock/{locator}/handler/get_audio_download_url",
        body={"audio_id": audio_id},
        referer=referer,
    )
    if diagnostic and diagnostic.get("status") != "ok":
        return "", diagnostic
    if not isinstance(payload, dict):
        return "", {"step": "get_audio_download_url", "status": "invalid_response", "error_summary": "Studio did not return an audio download address"}
    url = next((payload.get(key) for key in ("download_url", "url") if isinstance(payload.get(key), str) and payload.get(key).strip()), "")
    parsed = urlsplit(url)
    if not url or parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return "", {"step": "get_audio_download_url", "status": "invalid_response", "error_summary": "Studio returned an invalid audio download address"}
    return url, diagnostic


def local_audio_fingerprint(manifest: dict[str, Any]) -> tuple[int | None, str]:
    path = Path(str((manifest.get("audio") or {}).get("_full_audio_abs", "")))
    try:
        digest = hashlib.sha256()
        with path.open("rb") as audio_file:
            for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return path.stat().st_size, digest.hexdigest()
    except OSError:
        return None, ""


def local_audio_fingerprint_is_complete(size: Any, digest: Any) -> bool:
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
        and isinstance(digest, str)
        and re.fullmatch(r"[a-f0-9]{64}", digest) is not None
    )


def verified_local_audio_fingerprint(manifest: dict[str, Any]) -> tuple[int | None, str, bool]:
    try:
        size, digest = local_audio_fingerprint(manifest)
    except Exception:
        return None, "", False
    return size, digest, local_audio_fingerprint_is_complete(size, digest)


def local_audio_fingerprint_for_manifest(
    manifest: dict[str, Any],
    *,
    refresh: bool = False,
) -> tuple[int | None, str, bool]:
    if refresh:
        manifest.pop(LOCAL_AUDIO_FINGERPRINT_CACHE_KEY, None)

    cached = manifest.get(LOCAL_AUDIO_FINGERPRINT_CACHE_KEY)
    if isinstance(cached, dict):
        size = cached.get("size_bytes")
        digest = cached.get("sha256")
        if local_audio_fingerprint_is_complete(size, digest):
            return size, digest, True

    size, digest, complete = verified_local_audio_fingerprint(manifest)
    if complete:
        manifest[LOCAL_AUDIO_FINGERPRINT_CACHE_KEY] = {
            "size_bytes": size,
            "sha256": digest,
        }
    return size, digest, complete


def audio_upload_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    sources = [
        (entry.get(field) or {}).get("audio_upload_evidence")
        for field in ("repair_publish", "publish")
        if isinstance(entry.get(field), dict)
    ]
    sources.append(entry.get("audio_evidence"))
    for evidence in sources:
        if not isinstance(evidence, dict):
            continue
        safe = {
            key: evidence[key]
            for key in (
                "upload_key_path", "bucket", "region", "multipart_part_count", "multipart_completed"
            )
            if key in evidence
        }
        if safe:
            return safe_audio_evidence(safe)
    return {}


def safe_audio_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in ("audio_id", "upload_key_path", "download_key_path", "bucket", "region", "object_accessibility"):
        item = value.get(key)
        if isinstance(item, str):
            safe[key] = publisher.safe_object_key_path(item) if key in {"upload_key_path", "download_key_path"} else item[:500]
    for key in ("local_size_bytes", "multipart_part_count", "download_http_status", "object_length_bytes"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            safe[key] = item
    digest = value.get("local_sha256")
    if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest):
        safe["local_sha256"] = digest
    for key in ("multipart_completed", "key_paths_match"):
        item = value.get(key)
        if isinstance(item, bool):
            safe[key] = item
    return safe


def load_package_targets(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        payload = publisher.read_json(path)
    except Exception as exc:
        raise BatchValidationError("targets_file", "local package targets file is not readable JSON") from exc
    targets = payload.get("targets") if isinstance(payload, dict) else None
    if not isinstance(targets, list) or not targets or any(not isinstance(item, dict) for item in targets):
        raise BatchValidationError("targets_file", "local package targets file must contain a non-empty targets list")
    return payload, targets


def course_identity(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("block-v1:"):
        return publisher.course_key_from_vertical_block_id(value)
    vertical_locator = publisher.parse_vertical_block_id(value)
    if vertical_locator:
        return publisher.course_key_from_vertical_block_id(vertical_locator)
    return value if value.startswith("course-v1:") else ""


def operation_course_matches(operation: dict[str, Any], course_key: str) -> bool:
    identities = [course_identity(operation.get(key)) for key in ("course_id", "course_key")]
    for entry in operation.get("galleries", []):
        if not isinstance(entry, dict):
            continue
        identities.extend(
            course_identity(entry.get(key))
            for key in ("course_id", "course_key", "vertical_locator", "gallery_locator")
        )
    known = [identity for identity in identities if identity]
    return bool(known) and all(identity == course_key for identity in known)


def section_id_matches(section: dict[str, Any], section_id: str) -> bool:
    candidates = [
        section.get("section_id"),
        section.get("id"),
        section.get("block_location"),
        section.get("locator"),
    ]
    if section_id in [str(value or "").strip() for value in candidates]:
        return True
    name = str(section.get("name", "")).strip()
    prefix = re.match(r"^(\d+(?:\.\d+)*)\b", name)
    return bool(prefix and prefix.group(1) == section_id)


def target_course_error(target: dict[str, Any], package: dict[str, Any], manifest: dict[str, Any], course_key: str) -> str:
    identity_fields = (
        "course_id",
        "course_key",
        "target_vertical_block_location",
        "vertical_locator",
        "source_vertical_block_location",
        "gallery_locator",
        "section_id",
        "studio_vertical_url",
        "studio_url",
    )
    identities = [
        identity
        for record in (package, target, manifest)
        for field in identity_fields
        if (identity := course_identity(record.get(field)))
    ]
    if not identities:
        return "Package target has no verifiable course identity"
    if any(identity != course_key for identity in identities):
        return "Package target belongs to a different Studio course"
    return ""


def resolve_package_target(
    target: dict[str, Any],
    manifest: dict[str, Any],
    course_structure: dict[str, Any],
    *,
    course_key: str,
    package: dict[str, Any] | None = None,
) -> dict[str, Any]:
    section_id = str(target.get("section_id") or manifest.get("section_id") or "").strip()
    section_title = str(target.get("section_title") or manifest.get("section_title") or target.get("name") or "").strip()
    identity_error = target_course_error(target, package or {}, manifest, course_key)
    if identity_error:
        return {"status": "unverified", "error": identity_error}

    records = publisher.iter_section_vertical_records(course_structure)
    sections = list(dict.fromkeys(id(record["section"]) for record in records))
    section_objects = {id(record["section"]): record["section"] for record in records}
    matched_section = None
    if section_id:
        id_matches = [section_objects[section_id_key] for section_id_key in sections
                      if section_id_matches(section_objects[section_id_key], section_id)]
        if len(id_matches) > 1:
            return {"status": "unverified", "error": "Section ID matches more than one Studio section"}
        if len(id_matches) == 1:
            matched_section = id_matches[0]
    if matched_section is None and section_title:
        title_matches = [section_objects[section_id_key] for section_id_key in sections
                         if str(section_objects[section_id_key].get("name", "")).strip() == section_title]
        if len(title_matches) > 1:
            return {"status": "unverified", "error": "Section title is not unique in the Studio course"}
        if len(title_matches) == 1:
            matched_section = title_matches[0]

    target_vertical_value = None
    target_vertical_supplied = False
    for key in ("target_vertical_block_location", "vertical_locator"):
        value = target.get(key)
        if value is None or isinstance(value, str) and not value.strip():
            continue
        target_vertical_value = value
        target_vertical_supplied = True
        break
    target_vertical = safe_locator(target_vertical_value)
    if target_vertical_supplied and not target_vertical:
        return {"status": "unverified", "error": "Target vertical locator is invalid"}
    candidates = [record for record in records if matched_section is None or record["section"] is matched_section]
    if target_vertical_supplied:
        if course_identity(target_vertical) != course_key:
            return {"status": "unverified", "error": "Target vertical belongs to a different Studio course"}
        vertical_matches = [record for record in records if safe_locator(record["vertical"].get("block_location")) == target_vertical]
        if len(vertical_matches) != 1:
            return {"status": "unverified", "error": "Target vertical could not be found uniquely in the Studio course"}
        if matched_section is not None and vertical_matches[0]["section"] is not matched_section:
            return {"status": "unverified", "error": "Target vertical is outside the matched Studio section"}
        candidates = vertical_matches
        matched_section = vertical_matches[0]["section"]
    if matched_section is None:
        return {"status": "unverified", "error": "No Studio section matches the package section ID or unique title"}
    if len(candidates) == 0:
        return {"status": "unverified", "error": "No unique target vertical was found in the matched section"}

    if not target_vertical:
        gallery_verticals = [
            record for record in candidates
            if any(publisher.block_category(block) == "imagesgallery" for block in record["vertical"].get("blocks", []) if isinstance(block, dict))
        ]
        if len(gallery_verticals) == 1:
            candidates = gallery_verticals
        elif not gallery_verticals and len(candidates) != 1:
            return {"status": "unverified", "error": "Section has multiple possible verticals and no unique Gallery"}
    if len(candidates) != 1:
        return {"status": "unverified", "error": "Package section maps to multiple Studio verticals"}

    vertical = candidates[0]["vertical"]
    vertical_locator = safe_locator(vertical.get("block_location"))
    if not vertical_locator or course_identity(vertical_locator) != course_key:
        return {"status": "unverified", "error": "Matched Studio vertical locator is invalid"}
    galleries = [
        publisher.block_locator(block)
        for block in vertical.get("blocks", [])
        if isinstance(block, dict) and publisher.block_category(block) == "imagesgallery" and publisher.block_locator(block)
    ]
    if len(galleries) > 1:
        return {"status": "unverified", "error": "Matched section contains multiple Gallery candidates"}
    return {
        "status": "resolved",
        "vertical_locator": vertical_locator,
        "gallery_locator": safe_locator(galleries[0]) if galleries else "",
        "gallery_missing": not galleries,
        "section_id": section_id,
        "section_title": section_title,
        "vertical_snapshot": publisher.retained_vertical_snapshot(vertical, safe_locator(galleries[0])) if galleries else None,
    }


def package_operation(
    package_path: Path,
    package: dict[str, Any],
    targets: list[dict[str, Any]],
    course_structure: dict[str, Any] | None,
    *,
    course_key: str,
) -> dict[str, Any]:
    entries = []
    for index, target in enumerate(targets, start=1):
        manifest_value = str(target.get("manifest", "") or "").strip()
        manifest_path = Path(manifest_value).expanduser()
        if manifest_value and not manifest_path.is_absolute():
            manifest_path = (package_path.parent / manifest_path).resolve()
        manifest_data: dict[str, Any] = {}
        manifest_error = ""
        try:
            raw = publisher.read_json(manifest_path)
            if isinstance(raw, dict):
                manifest_data = raw
            else:
                manifest_error = "Local Gallery manifest is not an object"
        except Exception:
            manifest_error = "Local Gallery manifest could not be read"
        identity_error = target_course_error(target, package, manifest_data, course_key)
        if manifest_error:
            resolution = {"status": "unverified", "error": manifest_error}
        elif identity_error:
            resolution = {"status": "unverified", "error": identity_error}
        elif course_structure is None:
            resolution = {"status": "unverified", "error": "Studio course structure is unavailable"}
        else:
            resolution = resolve_package_target(
                target,
                manifest_data,
                course_structure,
                course_key=course_key,
                package=package,
            )
        gallery_key = str(target.get("gallery_key") or "").strip()
        if not gallery_key:
            identity = str(target.get("section_id") or manifest_data.get("section_id") or target.get("section_title") or manifest_data.get("section_title") or target.get("name") or index)
            gallery_key = f"package-{index}-{re.sub(r'[^A-Za-z0-9_-]+', '-', identity).strip('-')[:48]}"
        package_target = {}
        for key in ("ppt", "pptx", "section_json", "section_list_json", "audio_search_root"):
            value = target.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            source_path = Path(value).expanduser()
            if not source_path.is_absolute():
                source_path = package_path.parent / source_path
            package_target[key] = str(source_path.resolve())
        entry: dict[str, Any] = {
            "gallery_key": gallery_key,
            "manifest": str(manifest_path) if manifest_value else "",
            "section_id": str(target.get("section_id") or manifest_data.get("section_id") or ""),
            "section_title": str(target.get("section_title") or manifest_data.get("section_title") or target.get("name") or ""),
            "package_target": package_target,
            "publish": {},
            "initial_vertical_publish": {"status": "not_started"},
            "repair_history": [],
            "repair_attempts": 0,
            "audio_evidence": {},
            "target_mapping_status": resolution.get("status", "unverified"),
            "target_mapping_error": str(resolution.get("error", ""))[:240],
        }
        if resolution.get("status") == "resolved":
            entry.update({
                "vertical_locator": resolution["vertical_locator"],
                "gallery_locator": resolution["gallery_locator"],
                "gallery_missing": resolution["gallery_missing"],
            })
            if resolution.get("vertical_snapshot"):
                entry["vertical_integrity"] = {"expected": resolution["vertical_snapshot"]}
        else:
            entry["vertical_locator"] = ""
            entry["gallery_locator"] = ""
        entries.append(entry)
    return {"operation_id": uuid.uuid4().hex, "course_id": course_key, "galleries": entries}


def material_repair_script(root_value: str = "") -> Path:
    script_name = Path("scripts") / "repair_imagesgallery_materials.py"
    if root_value:
        candidate = Path(root_value).expanduser().resolve() / script_name
        if candidate.is_file():
            return candidate
        raise FileNotFoundError("PPT_TO_IMAGESGALLERY_ROOT does not contain the targeted repair script")

    roots = []
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        roots.append(Path(codex_home).expanduser() / "skills" / "ppt-to-imagesgallery")
    roots.extend((
        Path.home() / ".codex" / "skills" / "ppt-to-imagesgallery",
        Path.home() / ".agents" / "skills" / "ppt-to-imagesgallery",
        Path(__file__).resolve().parents[2] / "ppt-to-imagesgallery",
    ))
    for root in roots:
        candidate = root / script_name
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("PPT-to-ImagesGallery targeted repair script is unavailable")


def resolve_package_source_path(value: Any, *, targets_file: str, manifest_path: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        base = Path(targets_file).expanduser().resolve().parent if targets_file else manifest_path.parent
        path = base / path
    return path.resolve()


def _section_title(record: dict[str, Any]) -> str:
    for key in ("section_title", "title", "resource_title", "name"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def build_section_payload(
    *,
    section_id: str,
    section_title: str,
    resource_title: Any,
    output_name: Any,
    block_type: Any,
    page_count: int,
    page_content: list[Any],
) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "section_title": section_title,
        "resource_title": str(resource_title or "").strip(),
        "output_name": str(output_name or "").strip(),
        "block_type": str(block_type or "").strip(),
        "page_count": page_count,
        "page_content": page_content,
    }


def section_payload_from_list(
    entry: dict[str, Any],
    manifest: dict[str, Any],
    section_list_path: Path,
) -> dict[str, Any]:
    records = publisher.section_list_records(section_list_path)
    section_id = str(entry.get("section_id") or manifest.get("section_id") or "").strip()
    section_title = str(entry.get("section_title") or manifest.get("section_title") or "").strip()
    id_matches = [record for record in records if section_id and section_id_matches(record, section_id)]
    if len(id_matches) > 1:
        raise ValueError("section-list.json has multiple records for the target section ID")
    matched_by_id = len(id_matches) == 1
    if matched_by_id:
        record = id_matches[0]
    else:
        title_matches = [record for record in records if section_title and _section_title(record) == section_title]
        if len(title_matches) != 1:
            raise ValueError("section-list.json does not uniquely identify the target section")
        record = title_matches[0]

    page_content = record.get("page_content")
    if not isinstance(page_content, list):
        raise ValueError("resolved section record has no page_content list")
    page_count = record.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool):
        page_count = len(page_content)
    resolved_id = str(record.get("section_id") or "").strip()
    if not resolved_id and matched_by_id:
        resolved_id = section_id
    resolved_title = _section_title(record)
    if not resolved_title and matched_by_id:
        resolved_title = section_title
    return build_section_payload(
        section_id=resolved_id,
        section_title=resolved_title,
        resource_title=record.get("resource_title"),
        output_name=record.get("output_name"),
        block_type=record.get("block_type"),
        page_count=page_count,
        page_content=page_content,
    )


def section_payload_from_manifest(entry: dict[str, Any], manifest_path: Path) -> dict[str, Any] | None:
    manifest = publisher.read_json(manifest_path)
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not items:
        return None
    try:
        ordered_items = sorted(items, key=lambda item: int(item["page"]))
    except (KeyError, TypeError, ValueError):
        return None
    if any(not isinstance(item, dict) or not isinstance(item.get("speech"), str) or not item["speech"].strip() for item in ordered_items):
        return None
    return build_section_payload(
        section_id=str(entry.get("section_id") or manifest.get("section_id") or "").strip(),
        section_title=str(entry.get("section_title") or manifest.get("section_title") or "").strip(),
        resource_title=manifest.get("resource_title"),
        output_name=manifest.get("output_name"),
        block_type=manifest.get("block_type"),
        page_count=len(ordered_items),
        page_content=[item["speech"] for item in ordered_items],
    )


def invoke_material_repair(
    entry: dict[str, Any],
    manifest_path: Path,
    *,
    args: argparse.Namespace,
    targets_file: str,
) -> int:
    script = material_repair_script(str(getattr(args, "ppt_to_imagesgallery_root", "") or ""))
    package_target = entry.get("package_target") if isinstance(entry.get("package_target"), dict) else {}
    ppt_path = resolve_package_source_path(
        package_target.get("ppt") or package_target.get("pptx"),
        targets_file=targets_file,
        manifest_path=manifest_path,
    )
    section_json_path = resolve_package_source_path(
        package_target.get("section_json"),
        targets_file=targets_file,
        manifest_path=manifest_path,
    )
    section_list_path = resolve_package_source_path(
        package_target.get("section_list_json"),
        targets_file=targets_file,
        manifest_path=manifest_path,
    )
    audio_search_root = resolve_package_source_path(
        package_target.get("audio_search_root"),
        targets_file=targets_file,
        manifest_path=manifest_path,
    )

    with tempfile.TemporaryDirectory(prefix="imagegallery-local-repair-") as temp_dir:
        if section_json_path is None and section_list_path is not None:
            manifest = publisher.read_json(manifest_path)
            section_payload = section_payload_from_list(entry, manifest, section_list_path)
            section_json_path = Path(temp_dir) / "section.json"
            section_json_path.write_text(json.dumps(section_payload, ensure_ascii=False), encoding="utf-8")
        elif section_json_path is None:
            section_payload = section_payload_from_manifest(entry, manifest_path)
            if section_payload is not None:
                section_json_path = Path(temp_dir) / "section.json"
                section_json_path.write_text(json.dumps(section_payload, ensure_ascii=False), encoding="utf-8")

        command = [sys.executable, str(script), "--manifest", str(manifest_path)]
        if section_json_path is not None:
            command.extend(("--section-json", str(section_json_path)))
        if ppt_path is not None and section_json_path is not None:
            command.extend(("--ppt", str(ppt_path)))
        if audio_search_root is not None:
            command.extend(("--audio-search-root", str(audio_search_root)))
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return int(result.returncode)


def safe_local_asset_name(value: str) -> str:
    filename = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    filename = filename.split("?", 1)[0].split("#", 1)[0].strip()
    return safe_summary_text(filename)[:120] or "未命名文件"


def local_audio_missing_item(manifest: dict[str, Any]) -> str:
    audio = manifest.get("audio") if isinstance(manifest.get("audio"), dict) else {}
    path = audio.get("_full_audio_abs") or audio.get("full_audio")
    return f"音频文件 {safe_local_asset_name(str(path or ''))}"


def diagnose_local_material(manifest_path: Path) -> list[str]:
    try:
        publisher.ensure_manifest(manifest_path)
    except FileNotFoundError as exc:
        message = str(exc)
        for prefix, label in (
            ("图片不存在: ", "图片文件"),
            ("音频不存在: ", "音频文件"),
            ("图片素材不可用: ", "图片文件"),
            ("音频素材不可用: ", "音频文件"),
        ):
            if message.startswith(prefix):
                return [f"{label} {safe_local_asset_name(message[len(prefix):])}"]
        return ["Gallery manifest 文件"]
    except Exception as exc:
        message = str(exc)
        if "manifest.items must contain" in message:
            return ["字幕时间配置（页码、时间边界或字幕文案无效）"]
        if "manifest.audio" in message:
            return ["音频素材配置"]
        if "manifest.items[].image" in message:
            return ["图片素材路径"]
        if "manifest.items" in message:
            return ["Gallery 图片和字幕清单"]
        return ["Gallery manifest 内容或引用素材"]
    return []


def ensure_local_package(
    entry: dict[str, Any],
    manifest_path: Path,
    *,
    args: argparse.Namespace,
    targets_file: str,
    request_id: str,
    allow_local_repair: bool,
    store: Callable[[], None],
) -> dict[str, Any] | None:
    fingerprint_missing_items: list[str] = []
    try:
        manifest = publisher.ensure_manifest(manifest_path)
        _size, _digest, fingerprint_complete = local_audio_fingerprint_for_manifest(manifest, refresh=True)
        if not fingerprint_complete:
            fingerprint_missing_items = [local_audio_missing_item(manifest)]
            raise BatchValidationError("local_audio_fingerprint", "Local audio size and SHA-256 could not be computed")
        entry.pop("local_package_missing_items", None)
        entry["local_package_status"] = "complete"
        return manifest
    except Exception:
        entry["local_package_status"] = "incomplete"
        missing_items = diagnose_local_material(manifest_path)
        entry["local_package_missing_items"] = list(dict.fromkeys([*missing_items, *fingerprint_missing_items]))

    if not args.execute or not allow_local_repair:
        return None
    previous_repair = entry.get("local_material_repair") if isinstance(entry.get("local_material_repair"), dict) else {}
    if previous_repair.get("request_id") == request_id:
        return None

    repair = {
        "status": "running",
        "request_id": request_id,
        "started_at": utc_now(),
    }
    entry["local_material_repair"] = repair
    store()
    try:
        return_code = invoke_material_repair(
            entry,
            manifest_path,
            args=args,
            targets_file=targets_file,
        )
        repair["return_code"] = return_code
        if return_code != 0:
            raise BatchValidationError("local_material_repair", "Targeted local material repair failed")
        try:
            manifest = publisher.ensure_manifest(manifest_path)
        except Exception:
            raise BatchValidationError(
                "local_material_repair",
                "Local Gallery package remains incomplete after targeted repair",
            ) from None
        _size, _digest, fingerprint_complete = local_audio_fingerprint_for_manifest(manifest, refresh=True)
        if not fingerprint_complete:
            fingerprint_missing_items = [local_audio_missing_item(manifest)]
            raise BatchValidationError("local_audio_fingerprint", "Local audio size and SHA-256 could not be computed")
        fingerprint_missing_items = []
    except Exception as exc:
        repair["status"] = "failed"
        repair["failed_step"] = publisher.safe_step_name(getattr(exc, "step", "local_material_repair"))
        repair["error_summary"] = safe_error(exc, repair["failed_step"])
        missing_items = diagnose_local_material(manifest_path)
        repair["missing_items"] = list(dict.fromkeys([*missing_items, *fingerprint_missing_items]))
        repair["finished_at"] = utc_now()
        entry["local_package_missing_items"] = repair["missing_items"]
        entry["local_package_status"] = "incomplete"
        store()
        return None

    repair["status"] = "completed"
    repair.pop("missing_items", None)
    repair["finished_at"] = utc_now()
    entry.pop("local_package_missing_items", None)
    entry["local_package_status"] = "complete"
    store()
    return manifest


def validate_gallery(client: StudioClient, entry: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    locator = safe_locator(entry.get("gallery_locator"))
    expected_count = manifest_image_count(manifest)
    expected_images, expected_asset_ids, checkpoint_status = expected_image_records(entry, manifest)
    referer = publisher.build_container_block_url(client.studio_base, locator)
    observed: dict[str, Any] = {}
    checks: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []
    audio_size, audio_sha256, audio_fingerprint_complete = local_audio_fingerprint_for_manifest(manifest)
    if audio_fingerprint_complete:
        checks["local_audio_fingerprint"] = "pass"
    else:
        checks["local_audio_fingerprint"] = "unverified"
        entry["local_package_status"] = "incomplete"
        entry["local_package_missing_items"] = [local_audio_missing_item(manifest)]
        diagnostics.append({
            "step": "manifest",
            "status": "unverified",
            "error_summary": "Local audio size and SHA-256 could not be verified",
        })

    files, total_count, file_diagnostic = get_files(client, locator)
    if file_diagnostic:
        diagnostics.append(file_diagnostic)
    if files is None:
        checks["images"] = "unverified"
        checks["image_count"] = "unverified"
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
        checks["image_count"] = "unverified" if len(files) != total_count else "pass" if len(files) == expected_count else "mismatch"
        names_available = all(item["image"] for item in observed_images)
        if checks["image_count"] == "unverified" or not names_available:
            checks["images"] = "unverified"
        elif checks["image_count"] == "mismatch":
            checks["images"] = "mismatch"
        else:
            names_match = [item["image"] for item in observed_images] == [item["image"] for item in expected_images]
            asset_ids_match = expected_asset_ids is None or [
                item["asset_id"] for item in observed_images
            ] == expected_asset_ids
            checks["images"] = "pass" if names_match and asset_ids_match else "mismatch"

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
    play_config_unavailable = bool(config_diagnostic and config_diagnostic["status"] != "ok") or not isinstance(play_config, dict)
    play_config_read_failed = play_config_unavailable
    play_config_untrusted = play_config_unavailable
    show_subtitles = play_config.get("show_subtitles") if isinstance(play_config, dict) else None
    show_subtitles_present = isinstance(play_config, dict) and "show_subtitles" in play_config
    show_subtitles_invalid = show_subtitles_present and not isinstance(show_subtitles, bool)
    if isinstance(show_subtitles, bool):
        observed["show_subtitles"] = show_subtitles
    if show_subtitles_invalid:
        play_config_untrusted = True

    if play_config_unavailable:
        checks["play_config"] = "unverified"
        if not (config_diagnostic and config_diagnostic["status"] != "ok"):
            diagnostics.append({"step": "get_play_config", "status": "invalid_response", "error_summary": "Studio play_config.time_config is not a list"})
    elif "time_config" not in play_config:
        observed["time_config_count"] = 0
        observed["time_config_entries_valid"] = False
        checks["play_config"] = "unverified" if play_config_untrusted else "mismatch"
    elif not isinstance(time_config, list):
        play_config_untrusted = True
        checks["play_config"] = "unverified"
        diagnostics.append({"step": "get_play_config", "status": "invalid_response", "error_summary": "Studio play_config.time_config is not a list"})
    else:
        observed["time_config_count"] = len(time_config)
        timing_entries_valid = publisher.valid_time_config_entries(time_config)
        observed["time_config_entries_valid"] = timing_entries_valid
        if play_config_untrusted:
            checks["play_config"] = "unverified"
        else:
            observed["time_config"] = [
                {
                    "page": item.get("page"),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "subtitle": item.get("subtitle"),
                }
                for item in time_config
            ]
            expected_time_config = {
                int(item["page"]): {
                    "start": int(item["start"]),
                    "end": int(item["end"]),
                    "subtitle": item["subtitle"],
                }
                for item in manifest["items"]
            }
            observed_time_config = {
                int(item["page"]): {
                    "start": int(item["start"]),
                    "end": int(item["end"]),
                    "subtitle": item["subtitle"],
                }
                for item in time_config
                if isinstance(item, dict) and isinstance(item.get("page"), int) and not isinstance(item.get("page"), bool)
            } if timing_entries_valid else {}
            checks["play_config"] = (
                "pass"
                if timing_entries_valid and observed_time_config == expected_time_config
                else "mismatch"
            )

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

    upload_evidence = audio_upload_evidence(entry)
    audio_evidence = {
        **upload_evidence,
        "local_size_bytes": audio_size,
        "local_sha256": audio_sha256,
    }
    if isinstance(audios, list) and len(audios) == 1 and not (
        audio_diagnostic and audio_diagnostic.get("status") != "ok"
    ):
        bound_audio_id = audio_binding_id(audios[0])
        if bound_audio_id:
            observed["audio_id"] = bound_audio_id
            audio_evidence["audio_id"] = bound_audio_id
            signed_url, url_diagnostic = audio_download_url(client, locator, bound_audio_id, referer)
            if url_diagnostic:
                diagnostics.append(url_diagnostic)
            if signed_url:
                download_path = urlsplit(signed_url).path
                audio_evidence["download_key_path"] = download_path
                upload_path = audio_evidence.get("upload_key_path")
                if isinstance(upload_path, str) and upload_path:
                    audio_evidence["key_paths_match"] = upload_path.lstrip("/") == download_path.lstrip("/")
                access_status, download_status, object_length = read_audio_range_byte(
                    signed_url,
                    timeout=client.args.timeout,
                )
                audio_evidence["object_accessibility"] = access_status
                if download_status is not None:
                    audio_evidence["download_http_status"] = download_status
                    observed["audio_download_http_status"] = download_status
                if object_length is not None:
                    audio_evidence["object_length_bytes"] = object_length
                    observed["audio_object_length_bytes"] = object_length
                observed["audio_object_accessibility"] = access_status
                checks["audio_object_accessibility"] = (
                    "pass" if access_status == "accessible" else
                    "mismatch" if access_status == "missing" else
                    "unverified"
                )
                if access_status == "unverified":
                    diagnostics.append({
                        "step": "audio_object_range_get",
                        "status": "unverified",
                        "error_summary": "Audio object accessibility could not be confirmed",
                    })
            else:
                checks["audio_object_accessibility"] = "unverified"
                observed["audio_object_accessibility"] = "unverified"
                audio_evidence["object_accessibility"] = "unverified"
        else:
            checks["audio_object_accessibility"] = "unverified"
            observed["audio_object_accessibility"] = "unverified"
            audio_evidence["object_accessibility"] = "unverified"
            diagnostics.append({
                "step": "get_audios",
                "status": "invalid_response",
                "error_summary": "Studio audio binding does not contain an audio ID",
            })
    else:
        checks["audio_object_accessibility"] = "not_checked"
        observed["audio_object_accessibility"] = "not_checked"

    entry["audio_evidence"] = safe_audio_evidence(audio_evidence)

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
    if play_config_untrusted:
        result["play_config_untrusted"] = True
    if play_config_read_failed:
        result["play_config_read_failed"] = True
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


def vertical_publish_started_record() -> dict[str, str]:
    return {"step": "publish_vertical", "status": "started"}


def replace_gallery_from_package(
    operation: dict[str, Any],
    entry: dict[str, Any],
    manifest: dict[str, Any],
    args: argparse.Namespace,
    adapter: dict[str, Any],
    store: Callable[[], None],
    *,
    repair_round: int,
    attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history = entry.setdefault("repair_history", [])
    validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
    checks = validation.get("checks") if isinstance(validation.get("checks"), dict) else {}
    observed = validation.get("observed") if isinstance(validation.get("observed"), dict) else {}
    show_subtitles = observed.get("show_subtitles")
    if not isinstance(show_subtitles, bool):
        show_subtitles = True
    repair_components = [
        name
        for name in ("gallery_presence", "images", "play_config", "audio_bindings", "audio_object_accessibility")
        if checks.get(name) == "mismatch"
    ]
    if play_config_is_untrusted(validation) and "time_config_entries_valid" not in observed and "play_config" not in repair_components:
        repair_components.append("play_config")
    if attempt is None:
        attempt = {
            "attempt": max(len(history), int(entry.get("repair_attempts", 0))) + 1,
            "status": "running",
            "repair_mode": "gallery_replacement",
            "repair_round": repair_round,
            "started_at": utc_now(),
            "trigger_validation": copy.deepcopy(validation),
            "initial_publish": safe_publish_state(entry.get("publish")),
            "replaced_gallery_locator": safe_locator(entry.get("gallery_locator")),
            "repair_components": repair_components,
            "steps": [],
        }
        previous_repair_publish = entry.get("repair_publish")
        if isinstance(previous_repair_publish, dict):
            attempt["previous_repair_publish"] = safe_publish_state(previous_repair_publish)
        previous_repair_locator = safe_locator(entry.get("repair_gallery_locator"))
        if previous_repair_locator:
            attempt["previous_repair_gallery_locator"] = previous_repair_locator
        entry.pop("repair_publish", None)
        entry.pop("repair_gallery_locator", None)
        history.append(attempt)
    else:
        attempt["status"] = "running"
        attempt["repair_mode"] = "gallery_replacement"
        attempt["repair_round"] = repair_round
        attempt["resumed_at"] = utc_now()
        for key in ("finished_at", "failed_step", "error_summary", "http_status"):
            attempt.pop(key, None)

    request_id = operation.get("repair_request_id")
    if isinstance(request_id, str) and request_id:
        attempt["request_id"] = request_id
    entry["repair_attempts"] = max(int(entry.get("repair_attempts", 0)), int(attempt.get("attempt", 1)))
    attempt["vertical_publish"] = vertical_publish_started_record()
    store()

    vertical_locator = safe_locator(entry.get("vertical_locator"))
    if not vertical_locator:
        raise BatchValidationError("target_mapping", "Gallery replacement requires a uniquely matched Studio vertical")
    parsed_studio_url = urlsplit(args.studio_url)
    studio_base = f"{parsed_studio_url.scheme}://{parsed_studio_url.netloc}"
    studio_vertical_url = publisher.build_container_block_url(studio_base, vertical_locator)
    package_target = entry.get("package_target") if isinstance(entry.get("package_target"), dict) else {}
    target = {
        "name": str(entry.get("section_title") or entry.get("gallery_key") or "Gallery"),
        "manifest": str(Path(str(entry.get("manifest", ""))).expanduser().resolve()),
        "gallery_key": str(entry.get("gallery_key", "")),
        "section_id": str(entry.get("section_id", "")),
        "section_title": str(entry.get("section_title", "")),
        "block_type": str(entry.get("block_type") or manifest.get("block_type") or "imagesgallery"),
        "section_list_json": str(package_target.get("section_list_json", "")),
        "source_vertical_block_location": vertical_locator,
        "target_vertical_block_location": vertical_locator,
        "studio_vertical_url": studio_vertical_url,
    }
    publish_args = argparse.Namespace(
        execute=True,
        dry_run=False,
        adapter=str(args.adapter),
        studio_url=studio_vertical_url,
        timeout=int(args.timeout),
        operation_file=str(args.operation_file or ""),
        gallery_key=str(entry.get("gallery_key", "")),
        checkpoint_field="repair_publish",
        courses_base=str(args.courses_base or ""),
        account=str(args.account or ""),
        password=str(args.password or ""),
        cookie=str(args.cookie or ""),
        csrf_token=str(args.csrf_token or ""),
        skip_oss_multipart=bool(args.skip_oss_multipart),
        auth_retry_max_retries=int(args.auth_retry_max_retries),
        auth_retry_delay_seconds=float(args.auth_retry_delay_seconds),
        show_subtitles=show_subtitles,
    )
    result = publisher.process_publish_target(
        target,
        {"chapters": []},
        publish_args,
        adapter,
        previous_result=None,
    )

    operation_path_value = str(args.operation_file or "").strip()
    if operation_path_value:
        refresh_publish_checkpoint(entry, Path(operation_path_value))
    created_locator = safe_locator(result.get("created_block_locator") or result.get("block_locator"))
    if not created_locator:
        created_locator = safe_locator(entry.get("repair_gallery_locator"))
    if created_locator:
        entry["repair_gallery_locator"] = created_locator
        entry["gallery_locator"] = created_locator
        entry["gallery_missing"] = False
        attempt["created_block_locator"] = created_locator

    expected_snapshot = normalize_child_snapshot(result.get("expected_vertical_snapshot_after_delete"))
    if expected_snapshot is not None:
        entry.setdefault("vertical_integrity", {})["expected"] = expected_snapshot

    steps = result.get("steps") if isinstance(result.get("steps"), list) else []
    safe_steps = [publisher.safe_step_record(step) for step in steps if isinstance(step, dict)]
    attempt["steps"] = safe_steps
    attempt["publisher_result"] = {
        key: result[key]
        for key in ("status", "content_status", "failed_step")
        if isinstance(result.get(key), str)
    }
    attempt["publisher_result"]["steps"] = safe_steps
    vertical_steps = [step for step in steps if isinstance(step, dict) and step.get("step") == "publish_vertical"]
    if vertical_steps:
        vertical_step = vertical_steps[-1]
        attempt["vertical_publish"] = {
            "step": "publish_vertical",
            "status": str(vertical_step.get("status", "unverified")),
            **({"reason": safe_summary_text(vertical_step.get("reason"))} if vertical_step.get("reason") else {}),
        }
    repair_publish = entry.get("repair_publish") if isinstance(entry.get("repair_publish"), dict) else {}
    attempt["publish"] = safe_publish_state(repair_publish)
    publisher_status = str(result.get("status", ""))
    content_status = str(result.get("content_status", ""))
    vertical_publish_status = str((attempt.get("vertical_publish") or {}).get("status", ""))
    replacement_completed = bool(
        created_locator
        and publisher_status == "replaced"
        and content_status == "completed"
        and vertical_publish_status == "completed"
    )
    attempt["status"] = "running" if replacement_completed else "failed"
    if not replacement_completed:
        default_failed_step = "create_block" if not created_locator else "publish_vertical" if vertical_publish_status != "completed" else "publish"
        attempt["failed_step"] = publisher.safe_step_name(result.get("failed_step") or default_failed_step)
        default_error = (
            "Studio did not confirm the replacement Gallery locator"
            if not created_locator
            else "Studio did not confirm a complete Gallery replacement and vertical publication"
        )
        attempt["error_summary"] = safe_summary_text(result.get("note") or default_error)
    attempt["finished_at"] = utc_now()
    store()
    if not created_locator:
        raise BatchValidationError("create_block", "Studio did not confirm the replacement Gallery locator")
    return attempt


def repair_gallery(
    client: StudioClient,
    operation: dict[str, Any],
    entry: dict[str, Any],
    manifest: dict[str, Any],
    args: argparse.Namespace,
    adapter: dict[str, Any],
    store,
    attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history = entry.setdefault("repair_history", [])
    validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
    checks = validation.get("checks") if isinstance(validation.get("checks"), dict) else {}
    repair_images = checks.get("images") == "mismatch"
    repair_audio = checks.get("audio_bindings") == "mismatch" or checks.get("audio_object_accessibility") == "mismatch"
    repair_play_config = checks.get("play_config") == "mismatch" or play_config_is_untrusted(validation)
    repair_components = [
        name
        for name, requested in (
            ("images", repair_images),
            ("audio_bindings", repair_audio),
            ("play_config", repair_play_config),
        )
        if requested
    ]
    if attempt is None:
        attempt = {
            "attempt": max(len(history), int(entry.get("repair_attempts", 0))) + 1,
            "status": "running",
            "repair_mode": "play_config",
            "started_at": utc_now(),
            "trigger_validation": copy.deepcopy(entry.get("validation", {})),
            "initial_publish": safe_publish_state(entry.get("publish")),
            "repair_components": repair_components,
            "steps": [],
        }
        history.append(attempt)
    else:
        attempt["status"] = "running"
        attempt["repair_mode"] = "play_config"
        attempt["resumed_at"] = utc_now()
        for key in ("finished_at", "failed_step", "error_summary", "http_status"):
            attempt.pop(key, None)
        attempt["repair_components"] = repair_components
    request_id = operation.get("repair_request_id")
    if isinstance(request_id, str) and request_id:
        attempt["request_id"] = request_id
    entry["repair_attempts"] = max(int(entry.get("repair_attempts", 0)), int(attempt.get("attempt", 1)))
    store()

    locator = safe_locator(entry.get("gallery_locator"))
    referer = publisher.build_container_block_url(client.studio_base, locator)
    attempt_steps = attempt.setdefault("steps", [])
    failures: list[BatchValidationError] = []
    uploaded_images: list[dict[str, Any]] = []

    def recorded_step(step: str, action: Callable[[], dict[str, Any] | None]) -> dict[str, Any]:
        record = {"step": step, "status": "started"}
        attempt_steps.append(record)
        store()
        try:
            details = action()
            if isinstance(details, dict):
                record.update(details)
            record["status"] = "completed"
            store()
            return record
        except Exception as exc:
            record["status"] = "failed"
            record["error_summary"] = safe_error(exc, step)
            status = safe_http_status(exc)
            if status is not None:
                record["http_status"] = status
            store()
            if isinstance(exc, BatchValidationError):
                raise
            raise BatchValidationError(step, record["error_summary"], http_status=status) from None

    if repair_images:
        try:
            files, total_count, diagnostic = get_files(client, locator)
            if files is None:
                raise BatchValidationError(
                    "get_files_before_repair",
                    (diagnostic or {}).get("error_summary", "Gallery files could not be verified before repair"),
                    http_status=(diagnostic or {}).get("http_status"),
                )
            if len(files) != total_count:
                raise BatchValidationError("get_files_before_repair", "Gallery file listing was incomplete before repair")
            removal_keys: list[str] = []
            for item in files:
                asset_key = str(item.get("asset_key") or item.get("id") or "").strip()
                if not asset_key or asset_key in removal_keys:
                    raise BatchValidationError("get_files_before_repair", "Gallery image references could not be identified safely")
                removal_keys.append(asset_key)
            if removal_keys:
                recorded_step(
                    "remove_gallery_files",
                    lambda: {
                        "http_status": client.request_json(
                            "remove_gallery_files",
                            "POST",
                            f"{client.studio_base}/xblock/{locator}/handler/remove_files",
                            body={"assets": removal_keys},
                            referer=referer,
                        )[1]
                    },
                )
                remaining, remaining_total, remaining_diagnostic = get_files(client, locator)
                if remaining is None or len(remaining) != remaining_total or remaining:
                    raise BatchValidationError(
                        "verify_removed_gallery_files",
                        (remaining_diagnostic or {}).get("error_summary", "Gallery image references could not be cleared before replacement"),
                        http_status=(remaining_diagnostic or {}).get("http_status"),
                    )

            uploaded_ids: list[str] = []
            for index, item in enumerate(manifest["items"], start=1):
                image_name = publisher.manifest_image_name(item)
                context = {
                    "studio_base": client.studio_base,
                    "block_url": referer,
                    "block_locator": locator,
                    "vertical_block_id": entry["vertical_locator"],
                    "csrftoken": client.csrftoken,
                    "image_file": item["_image_abs"],
                    "image_rel": item["image"],
                    "page": int(item["page"]),
                    "page_number": int(item["page"]),
                    "index": index,
                    "subtitle": item["subtitle"],
                    "start": int(item["start"]),
                    "end": int(item["end"]),
                }
                step = recorded_step(
                    f"upload_image[{index}]",
                    lambda context=context: _template_details(
                        client,
                        f"upload_image[{index}]",
                        adapter["upload_image"],
                        context,
                    ),
                )
                asset_id = step.get("asset_id")
                if isinstance(asset_id, str) and asset_id:
                    uploaded_ids.append(asset_id)
                    uploaded_images.append({"page": int(item["page"]), "image": image_name, "asset_id": asset_id})
            if len(uploaded_ids) == len(manifest["items"]):
                recorded_step(
                    "change_files_order",
                    lambda: _template_details(
                        client,
                        "change_files_order",
                        adapter["change_files_order"],
                        {
                            "studio_base": client.studio_base,
                            "block_url": referer,
                            "block_locator": locator,
                            "csrftoken": client.csrftoken,
                            "uploaded_asset_ids": uploaded_ids,
                        },
                    ),
                )
            else:
                attempt_steps.append({"step": "change_files_order", "status": "skipped", "reason": "Image asset IDs could not be confirmed"})
                store()
        except BatchValidationError as exc:
            failures.append(exc)

    if repair_audio:
        try:
            audio_path = Path(str(manifest["audio"]["_full_audio_abs"]))
            audio_suffix = audio_path.suffix.lstrip(".")
            context: dict[str, Any] = {
                "studio_base": client.studio_base,
                "block_url": referer,
                "block_locator": locator,
                "vertical_block_id": entry["vertical_locator"],
                "csrftoken": client.csrftoken,
                "audio_file": str(audio_path),
                "audio_file_name": audio_path.name,
                "audio_post_fix": audio_suffix,
                "course_key": client.course_key,
                "courses_json": json.dumps({client.course_key: False}, ensure_ascii=False),
                "total_pages": len(manifest["items"]),
            }
            if "upload_audio_prepare" not in adapter or "upload_audio_register" not in adapter or "save_audio_binding" not in adapter:
                raise BatchValidationError("adapter", "adapter does not define the Gallery audio upload steps")
            recorded_step(
                "upload_audio_prepare",
                lambda: _template_details(client, "upload_audio_prepare", adapter["upload_audio_prepare"], context),
            )
            file_key = str(context.get("file_key") or "").strip()
            document_id = publisher.maybe_extract_uuid(file_key)
            if not file_key or not document_id:
                raise BatchValidationError("upload_audio_prepare", "Studio audio preparation did not return a usable document ID")
            context["edx_document_id"] = document_id
            context["audio_document_id"] = f"{document_id}.{audio_suffix}"
            if args.skip_oss_multipart:
                attempt_steps.append({"step": "upload_audio_multipart", "status": "skipped", "reason": "OSS multipart upload was skipped"})
                store()
            else:
                def upload_audio_multipart() -> dict[str, Any]:
                    result = publisher.sts_multipart_upload_audio(context)
                    return {"parts": result["parts"], "size": result["size"]}

                recorded_step("upload_audio_multipart", upload_audio_multipart)
            recorded_step(
                "upload_audio_register",
                lambda: _template_details(client, "upload_audio_register", adapter["upload_audio_register"], context),
            )
            recorded_step(
                "save_audio_binding",
                lambda: _template_details(client, "save_audio_binding", adapter["save_audio_binding"], context),
            )
        except BatchValidationError as exc:
            failures.append(exc)

    if repair_play_config:
        try:
            time_config = [
                {
                    "page": int(item["page"]),
                    "start": int(item["start"]),
                    "end": int(item["end"]),
                    "subtitle": item["subtitle"],
                }
                for item in manifest["items"]
            ]
            play_config: dict[str, Any] = {"time_config": time_config}
            observed_config = validation.get("observed") if isinstance(validation.get("observed"), dict) else {}
            show_subtitles = observed_config.get("show_subtitles")
            if isinstance(show_subtitles, bool):
                play_config["show_subtitles"] = show_subtitles
            recorded_step(
                "update_play_config",
                lambda: {
                    "http_status": client.request_json(
                        "update_play_config",
                        "POST",
                        f"{client.studio_base}/xblock/{locator}/handler/update_play_config",
                        body={"play_config": play_config},
                        referer=referer,
                    )[1]
                },
            )
        except BatchValidationError as exc:
            failures.append(exc)

    if repair_images or repair_audio or repair_play_config:
        try:
            vertical_url = publisher.build_container_block_url(client.studio_base, entry["vertical_locator"])
            vertical_publish = recorded_step(
                "publish_vertical",
                lambda: _template_details(
                    client,
                    "publish_vertical",
                    adapter["publish_vertical"],
                    {
                        "studio_base": client.studio_base,
                        "vertical_block_id": entry["vertical_locator"],
                        "block_url": vertical_url,
                        "csrftoken": client.csrftoken,
                    },
                ),
            )
            attempt["vertical_publish"] = {
                "step": "publish_vertical",
                "status": "completed",
                **{key: value for key, value in vertical_publish.items() if key not in {"step", "status"}},
            }
        except BatchValidationError as exc:
            attempt["vertical_publish"] = {"step": "publish_vertical", "status": "failed", "error_summary": safe_error(exc, "publish_vertical")}
            failures.append(exc)

    observed_images = validation.get("observed", {}).get("images", []) if isinstance(validation.get("observed"), dict) else []
    image_records = uploaded_images if repair_images else publisher.safe_image_checkpoint_records(observed_images)
    asset_ids = [item["asset_id"] for item in image_records if isinstance(item.get("asset_id"), str)]
    entry["repair_publish"] = {
        "gallery_key": entry["gallery_key"],
        "status": "completed" if not failures else "partial",
        "completed_stage": "saved",
        "active_step": "",
        "failed_step": failures[0].step if failures else "",
        "asset_ids": asset_ids,
        "images": image_records,
        "steps": [publisher.safe_step_record(step) for step in attempt_steps if isinstance(step, dict)],
        "images_started": repair_images,
        "reconciliation_count": 0,
    }
    attempt["publish"] = {
        "status": "completed" if not failures else "partial",
        "completed_stage": "saved",
        "asset_ids": asset_ids,
        "images": image_records,
        "steps": [publisher.safe_step_record(step) for step in attempt_steps if isinstance(step, dict)],
    }
    attempt["finished_at"] = utc_now()
    attempt["status"] = "completed" if not failures else "failed"
    store()
    if failures:
        raise failures[0]
    return attempt


def _template_details(
    client: StudioClient,
    step: str,
    template: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    record, retries = client.request_template(step, template, context)
    details = {
        key: record[key]
        for key in ("asset_id", "http_status")
        if key in record
    }
    if retries:
        details["auth_retry_attempts"] = len(retries)
        details["auth_retry_events"] = retries
    return details


def resumable_repair_attempt(entry: dict[str, Any]) -> dict[str, Any] | None:
    history = entry.get("repair_history")
    attempts = entry.get("repair_attempts")
    if (
        not isinstance(history, list)
        or not history
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
    ):
        return None
    attempt = history[-1]
    if not isinstance(attempt, dict) or attempt.get("attempt") != attempts:
        return None
    if attempt.get("status") not in {"running", "failed"} or "final_validation" in attempt:
        return None
    return attempt


def republish_vertical_for_pending_changes(
    client: StudioClient,
    entry: dict[str, Any],
    args: argparse.Namespace,
    adapter: dict[str, Any],
    store,
    *,
    repair_round: int,
    attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history = entry.setdefault("repair_history", [])
    if attempt is None:
        attempt = {
            "attempt": max(len(history), int(entry.get("repair_attempts", 0))) + 1,
            "status": "running",
            "repair_mode": "publish_vertical",
            "repair_round": repair_round,
            "started_at": utc_now(),
            "trigger_validation": copy.deepcopy(entry.get("validation", {})),
            "initial_publish": safe_publish_state(entry.get("publish")),
            "steps": [],
            "request_id": entry.get("repair_request_id", ""),
        }
        history.append(attempt)
    else:
        attempt["status"] = "running"
        attempt["repair_mode"] = "publish_vertical"
        if not isinstance(attempt.get("repair_round"), int) or isinstance(attempt.get("repair_round"), bool):
            attempt["repair_round"] = repair_round
        attempt["resumed_at"] = utc_now()
        attempt.pop("finished_at", None)
        attempt.pop("failed_step", None)
        attempt.pop("error_summary", None)
        attempt.pop("http_status", None)
    entry["repair_attempts"] = max(int(entry.get("repair_attempts", 0)), int(attempt.get("attempt", 1)))
    store()

    vertical_publish = attempt.get("vertical_publish")
    if not isinstance(vertical_publish, dict) or vertical_publish.get("status") != "completed":
        vertical_publish = vertical_publish_started_record()
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


def vertical_publish_attempted_in_request(entry: dict[str, Any], request_id: str) -> bool:
    history = entry.get("repair_history")
    if not isinstance(history, list):
        return False
    for attempt in history:
        if not isinstance(attempt, dict) or attempt.get("request_id") != request_id:
            continue
        if attempt.get("repair_mode") == "structure" or isinstance(attempt.get("vertical_publish"), dict):
            return True
        publisher_result = attempt.get("publisher_result") if isinstance(attempt.get("publisher_result"), dict) else {}
        steps = publisher_result.get("steps") if isinstance(publisher_result.get("steps"), list) else attempt.get("steps", [])
        if isinstance(steps, list) and any(
            isinstance(step, dict) and step.get("step") == "publish_vertical"
            for step in steps
        ):
            return True
    return False


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


OPERATOR_REPORT_MODES = {"categories", "sections"}


def _operator_report(operation: dict[str, Any], *, studio_base: str = "", report_mode: str = "sections") -> dict[str, Any]:
    if report_mode not in OPERATOR_REPORT_MODES:
        raise ValueError("Unsupported operator report mode")
    course_check = operation.get("course_structure_verification")
    course_check = course_check if isinstance(course_check, dict) else {}
    section_records = course_check.get("subsections") if isinstance(course_check.get("subsections"), list) else []
    sections_by_vertical = {
        safe_locator(item.get("vertical_locator")): item
        for item in section_records
        if isinstance(item, dict) and safe_locator(item.get("vertical_locator"))
    }
    groups_by_key: dict[str, dict[str, Any]] = {}

    def ensure_group(entry: dict[str, Any]) -> dict[str, Any]:
        locator = safe_locator(entry.get("vertical_locator"))
        key = locator or str(entry.get("gallery_key") or f"target-{len(groups_by_key) + 1}")
        if key not in groups_by_key:
            subsection = sections_by_vertical.get(locator, {})
            name = str(
                entry.get("section_title")
                or subsection.get("section_title")
                or subsection.get("vertical_title")
                or "目标小节"
            )
            url = str(subsection.get("studio_url") or "")
            if not url and locator and studio_base:
                url = publisher.build_container_block_url(studio_base, locator)
            groups_by_key[key] = {
                "name": name,
                "locator": locator,
                "studio_url": url,
                "findings": [],
            }
        return groups_by_key[key]

    def add_finding(entry: dict[str, Any], status: str, category: str, message: str) -> None:
        if status not in {"issue", "unverified"}:
            return
        group = ensure_group(entry)
        finding = {"status": status, "category": category, "message": message}
        if finding not in group["findings"]:
            group["findings"].append(finding)

    for entry in operation.get("galleries", []):
        if not isinstance(entry, dict):
            continue
        initial_publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
        initial_vertical = entry.get("initial_vertical_publish") if isinstance(entry.get("initial_vertical_publish"), dict) else {}
        if initial_publish.get("status") != "completed" or initial_vertical.get("status") != "completed":
            no_initial_publish_attempt = (
                bool(operation.get("targets_file"))
                and initial_publish.get("status") in {None, "", "not_started"}
                and initial_vertical.get("status") in {None, "", "not_started", "pending"}
            )
            if not no_initial_publish_attempt:
                add_finding(entry, "issue", "首次发布", "首次内容写入或小节发布未全部完成，请查看发布步骤记录。")
        validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
        checks = validation.get("checks") if isinstance(validation.get("checks"), dict) else {}
        image_mismatch = any(
            checks.get(name) == "mismatch"
            for name in ("images", "image_count", "image_names", "image_asset_ids", "image_order")
        )
        if checks.get("gallery_presence") == "mismatch":
            add_finding(entry, "issue", "ImageGallery 内容", "本地包对应的小节中缺少 ImageGallery。")
        if image_mismatch:
            add_finding(
                entry,
                "issue",
                "图片内容",
                "Studio 中的 Gallery 图片与本地素材存在不一致，请核对页数、名称、顺序和图片对应关系。",
            )
        if checks.get("play_config") == "mismatch":
            add_finding(entry, "issue", "字幕设置", "Studio 保存的字幕未启用、配置页数不符，或页码、时间边界、字幕字段无效。")
        if checks.get("audio_bindings") == "mismatch":
            add_finding(entry, "issue", "音频绑定", "Gallery 的音频绑定数量不是恰好一条。")
        if checks.get("audio_object_accessibility") == "mismatch":
            add_finding(
                entry,
                "issue",
                "音频对象可访问性",
                "音频绑定存在，但对应的 OSS 对象缺失（404/NoSuchKey）。",
            )
        if checks.get("audio_object_accessibility") == "unverified":
            add_finding(
                entry,
                "unverified",
                "音频对象可访问性",
                "音频绑定存在，但对象可访问性暂时无法确认。",
            )
        if checks.get("gallery_owner") == "mismatch":
            add_finding(entry, "issue", "组件结构", "Gallery 没有唯一关联到目标小节，无法确认它属于哪节内容。")
        if validation.get("status") == "mismatch" and not image_mismatch and not any(
            checks.get(name) == "mismatch"
            for name in (
                "gallery_presence",
                "play_config",
                "audio_bindings",
                "audio_object_accessibility",
                "gallery_owner",
                "vertical_structure",
                "vertical_publication",
            )
        ):
            add_finding(entry, "issue", "ImageGallery 内容", "Studio 中保存的 Gallery 内容与本地素材预期不一致。")

        if validation.get("status") in {"unverified", "failed"}:
            failed_step = str(validation.get("failed_step") or "")
            if validation.get("pre_repair_structure_failed_step"):
                add_finding(entry, "unverified", "组件结构", "暂时无法读取 Studio 保存的小节结构，因此无法确认图片页是否位于正确位置。")
            elif failed_step == "get_files":
                add_finding(entry, "unverified", "图片内容", "暂时无法读取 Studio 中的图片信息，因此无法确认图片是否齐全。")
            elif failed_step == "get_play_config":
                add_finding(entry, "unverified", "字幕设置", "Studio 返回的字幕配置无法确认页数或字段完整性。")
            elif failed_step == "get_audios":
                add_finding(entry, "unverified", "音频绑定", "暂时无法读取 Gallery 的音频绑定，因此无法确认绑定数量。")
            elif failed_step == "manifest":
                local_repair = entry.get("local_material_repair") if isinstance(entry.get("local_material_repair"), dict) else {}
                message = (
                    "本地素材恢复失败或仍有缺项，已停止 Studio 写入。"
                    if local_repair.get("status") == "failed"
                    else "本地 Gallery 素材不完整，暂时无法核对图片和字幕。"
                )
                missing_items = entry.get("local_package_missing_items")
                if isinstance(missing_items, list):
                    details = [item for item in missing_items if isinstance(item, str) and item]
                    if details:
                        message += f" 缺项：{'、'.join(details)}。"
                add_finding(entry, "unverified", "本地素材", message)
            elif failed_step == "target_mapping":
                mapping_error = entry.get("target_mapping_error")
                message = (
                    f"本地包目标无法唯一映射到 Studio 小节或 Gallery（{safe_summary_text(mapping_error)}），已停止该目标写入。"
                    if isinstance(mapping_error, str) and mapping_error
                    else "本地包目标无法唯一映射到 Studio 小节或 Gallery，已停止该目标写入。"
                )
                add_finding(entry, "unverified", "目标小节", message)
            elif failed_step == "operation_file":
                add_finding(entry, "unverified", "目标小节", "目标地址或 Gallery 记录不完整，暂时无法确认这节的发布结果。")
            elif failed_step == "load_live_course_structure":
                add_finding(entry, "unverified", "组件结构", "暂时无法读取 Studio 保存的小节结构，因此无法确认图片页是否位于正确位置。")
            else:
                gallery_checks = (
                    ("images", "暂时无法读取 Studio 中的图片信息，因此无法确认图片是否齐全。", "图片内容"),
                    ("play_config", "Studio 返回的字幕配置无法确认页数或字段完整性。", "字幕设置"),
                    ("audio_bindings", "暂时无法读取 Gallery 的音频绑定，因此无法确认绑定数量。", "音频绑定"),
                    ("audio_object_accessibility", "音频绑定存在，但对象可访问性暂时无法确认。", "音频对象可访问性"),
                )
                found_gallery_read = False
                for check_name, message, category in gallery_checks:
                    if checks.get(check_name) == "unverified":
                        add_finding(entry, "unverified", category, message)
                        found_gallery_read = True
                structure_checks = {"vertical_structure", "gallery_owner", "vertical_publication"}
                structure_unverified = any(checks.get(name) == "unverified" for name in structure_checks)
                if not found_gallery_read and not structure_unverified and not sections_by_vertical.get(safe_locator(entry.get("vertical_locator"))):
                    add_finding(entry, "unverified", "ImageGallery 内容", "暂时无法读取 Studio 中这节的保存状态，无法确认 Gallery 是否正确发布。")

        locator = safe_locator(entry.get("vertical_locator"))
        subsection = sections_by_vertical.get(locator)
        if isinstance(subsection, dict):
            if subsection.get("status") == "issue":
                issue_messages = {
                    "structure_mismatch": ("组件结构", "这节的 Gallery 与“本节要点”组件数量或顺序不符合要求。"),
                    "published_false": ("发布状态", "Studio 中这节的当前内容尚未正式发布。"),
                    "unpublished_changes": ("发布状态", "Studio 中这节仍有未发布改动，当前内容尚未同步到学员可见版本。"),
                    "publisher_vertical_integrity_unresolved": ("组件结构", "这节发布前的组件结构无法确认，不能保证 Gallery 替换结果正确。"),
                    "target_vertical_missing": ("目标小节", "Studio 中找不到本次目标小节，无法确认发布结果。"),
                }
                for issue in subsection.get("issues", []) if isinstance(subsection.get("issues"), list) else []:
                    category, message = issue_messages.get(
                        issue,
                        ("组件结构", "Studio 中的小节组件结构与本次发布预期不一致。"),
                    )
                    add_finding(entry, "issue", category, message)
            elif subsection.get("status") == "unverified":
                reasons = subsection.get("unverified_reasons") if isinstance(subsection.get("unverified_reasons"), list) else []
                if any(reason in reasons for reason in ("published_state_missing", "pending_changes_state_missing", "publication_state_missing", "publication_state_ambiguous", "publication_state_read_failed", "visibility_state_invalid")):
                    message = "Studio 未提供足够的发布状态信息，暂时无法确认这节当前内容已发布且没有待发布改动。"
                elif "expected_structure_ambiguous" in reasons or "expected_structure_missing" in reasons:
                    message = "发布记录不足以唯一确认目标组件，暂时无法核对这节的课程结构。"
                else:
                    message = "Studio 返回的小节结构信息不完整，暂时无法确认 Gallery 与“本节要点”的排列是否正确。"
                add_finding(entry, "unverified", "暂时无法确认", message)

    if course_check.get("status") == "unverified" and not section_records:
        for entry in operation.get("galleries", []):
            if isinstance(entry, dict):
                add_finding(
                    entry,
                    "unverified",
                    "暂时无法确认",
                    "暂时无法读取 Studio 中这节已保存的结构和发布状态。",
                )

    section_groups = [group for group in groups_by_key.values() if group["findings"]]
    issue_count = sum(
        finding["status"] == "issue"
        for group in section_groups
        for finding in group["findings"]
    )
    unverified_count = sum(
        finding["status"] == "unverified"
        for group in section_groups
        for finding in group["findings"]
    )
    status = "issue" if issue_count else "unverified" if unverified_count else "pass"
    summary = (
        f"发现 {issue_count} 项需要处理，另有 {unverified_count} 项暂时无法确认。请打开对应小节查看。"
        if status != "pass"
        else ""
    )
    if report_mode == "categories":
        visible_groups: list[dict[str, Any]] = []
        by_category: dict[str, dict[str, Any]] = {}
        for section in section_groups:
            for finding in section["findings"]:
                category = finding["category"]
                grouped = by_category.setdefault(category, {"name": category, "findings": []})
                grouped["findings"].append({
                    **finding,
                    "section_name": section["name"],
                    "studio_url": section["studio_url"],
                })
        visible_groups = list(by_category.values())
    else:
        visible_groups = section_groups

    entries = [entry for entry in operation.get("galleries", []) if isinstance(entry, dict)]
    completed_initial = sum(
        (entry.get("publish") or {}).get("status") == "completed"
        and (entry.get("initial_vertical_publish") or {}).get("status") == "completed"
        for entry in entries
    )
    repaired = sum(bool(entry.get("repair_history")) for entry in entries)
    audit_passed = bool(entries) and course_check.get("status") == "verified" and all(
        (entry.get("validation") or {}).get("status") == "verified" for entry in entries
    )
    lines = [f"首次发布：{completed_initial}/{len(entries)} 个目标的内容写入和小节发布已完成。",
             f"后置审核：{'全部通过' if audit_passed else '仍有问题或无法确认'}；修复涉及 {repaired} 个目标。"]
    if summary:
        lines.append(summary)
    if report_mode == "categories":
        for group in visible_groups:
            lines.extend(("", f"### {group['name']}"))
            for finding in group["findings"]:
                link = f"[{finding['section_name']}]({finding['studio_url']})" if finding["studio_url"] else finding["section_name"]
                label = "需要处理" if finding["status"] == "issue" else "暂时无法确认"
                lines.append(f"- {link}：{label}，{finding['message']}")
    else:
        for group in visible_groups:
            link = f"[{group['name']}]({group['studio_url']})" if group["studio_url"] else group["name"]
            lines.extend(("", f"### {link}"))
            for finding in group["findings"]:
                label = "需要处理" if finding["status"] == "issue" else "暂时无法确认"
                lines.append(f"- {label}：{finding['message']}")

    return {
        "status": status,
        "mode": report_mode,
        "issue_count": issue_count,
        "unverified_count": unverified_count,
        "summary": summary,
        "groups": visible_groups,
        "course_findings": [],
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
    publication_states, _publication_diagnostic = read_saved_publication_states(
        client,
        studio_base=studio_base,
        course_key=course_key,
        studio_url=studio_url,
        target_verticals=target_verticals,
        step="verify_saved_publication_state",
    )
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

        publication = publication_states.get(locator, {
            "status": "unverified",
            "published": None,
            "has_changes": None,
            "issues": [],
            "unverified_reasons": ["publication_state_missing"],
        })
        published = publication.get("published") if isinstance(publication.get("published"), bool) else None
        has_changes = publication.get("has_changes") if isinstance(publication.get("has_changes"), bool) else None
        issues.extend(publication.get("issues", []))
        unverified_reasons.extend(publication.get("unverified_reasons", []))

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
            "has_changes": has_changes,
            "publisher_integrity_status": (
                next(iter(integrity_statuses[locator]))
                if locator in integrity_statuses and len(integrity_statuses[locator]) == 1
                else "ambiguous" if locator in integrity_statuses else "missing"
            ),
            "issues": issues,
            "unverified_reasons": unverified_reasons,
        }
        if isinstance(publication.get("visibility_state"), str):
            subsection["visibility_state"] = publication["visibility_state"]
        for field in ("failed_step", "http_status", "error_summary"):
            if field in publication:
                subsection[field] = publication[field]
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


def summarize_batch(operation: dict[str, Any], *, studio_base: str = "", report_mode: str = "sections") -> dict[str, Any]:
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
        validation_checks = validation.get("checks") if isinstance(validation.get("checks"), dict) else {}
        vertical_locator = safe_locator(entry.get("vertical_locator"))
        vertical = vertical_results.get(vertical_locator, {})
        vertical_ok = (
            vertical.get("status") == "verified"
            and vertical.get("published") is True
            and vertical.get("has_changes") is False
            and vertical.get("visibility_state") != "needs_attention"
        )
        history = entry.get("repair_history") if isinstance(entry.get("repair_history"), list) else []
        publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
        initial_vertical = entry.get("initial_vertical_publish") if isinstance(entry.get("initial_vertical_publish"), dict) else {}
        original_publish = publish
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
        no_initial_publish_attempt = (
            bool(operation.get("targets_file"))
            and original_publish.get("status") in {None, "", "not_started"}
            and initial_vertical.get("status") in {None, "", "not_started", "pending"}
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
        operation_status = "not_applicable" if no_initial_publish_attempt else "completed" if operation_ok else "has_issues"
        if operation_status == "has_issues":
            operation_issue_count += 1
        item = {
            "gallery_key": entry.get("gallery_key", ""),
            "status": validation.get("status", "unverified"),
            "operation_status": operation_status,
            "initial_vertical_publish_status": (entry.get("initial_vertical_publish") or {}).get("status", "missing") if isinstance(entry.get("initial_vertical_publish"), dict) else "missing",
            "expected": validation.get("expected", {}),
            "observed": validation.get("observed", {}),
            "checks": copy.deepcopy(validation_checks),
            "repair_attempts": repair_attempts,
        }
        if isinstance(entry.get("target_mapping_status"), str):
            item["target_mapping_status"] = entry["target_mapping_status"]
        if isinstance(entry.get("target_mapping_error"), str) and entry["target_mapping_error"]:
            item["target_mapping_error"] = entry["target_mapping_error"]
        if isinstance(entry.get("local_package_status"), str):
            item["local_package_status"] = entry["local_package_status"]
        local_missing_items = entry.get("local_package_missing_items")
        if isinstance(local_missing_items, list) and local_missing_items:
            item["local_package_missing_items"] = [
                missing for missing in local_missing_items if isinstance(missing, str) and missing
            ]
        local_repair = entry.get("local_material_repair")
        if isinstance(local_repair, dict):
            item["local_material_repair"] = {
                key: local_repair[key]
                for key in ("status", "return_code", "failed_step", "error_summary")
                if key in local_repair
            }
            if isinstance(local_missing_items, list) and local_missing_items:
                item["local_material_repair"]["missing_items"] = item["local_package_missing_items"]
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
    operator_report = _operator_report(operation, studio_base=studio_base, report_mode=report_mode)
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
    parsed_studio_url = urlsplit(args.studio_url)
    if parsed_studio_url.username or parsed_studio_url.password or parsed_studio_url.scheme not in {"http", "https"}:
        raise BatchValidationError("studio_url", "--studio-url must be an HTTP(S) Studio URL without embedded credentials")
    studio_base = f"{parsed_studio_url.scheme}://{parsed_studio_url.netloc}" if parsed_studio_url.netloc else ""
    vertical_from_url = publisher.parse_vertical_block_id(args.studio_url)
    course_key = publisher.course_key_from_vertical_block_id(vertical_from_url or "")
    if not studio_base or not course_key or not vertical_from_url or "+type@vertical+" not in vertical_from_url:
        raise BatchValidationError("studio_url", "--studio-url must contain a Studio vertical locator")
    args.studio_url = publisher.build_container_block_url(studio_base, vertical_from_url)

    package_path = Path(args.targets_file).expanduser().resolve() if args.targets_file else None
    package: dict[str, Any] | None = None
    package_targets: list[dict[str, Any]] = []
    if package_path is not None:
        package, package_targets = load_package_targets(package_path)
        package_course = course_identity(package.get("course_id") or package.get("course_key"))
        if package_course and package_course != course_key:
            raise BatchValidationError("targets_file", "local package targets belong to a different Studio course")

    operation_path = Path(args.operation_file).expanduser().resolve() if args.operation_file else None
    if operation_path is None and package_path is not None:
        operation_path = package_path.with_name(f"{package_path.stem}.batch-operation.json")
    operation_exists = bool(operation_path and operation_path.is_file())
    if operation_exists:
        operation = load_operation(operation_path)
        if not operation_course_matches(operation, course_key):
            raise BatchValidationError("operation_file", "operation file does not match the requested Studio course")
    elif package_path is not None:
        operation = {"operation_id": uuid.uuid4().hex, "course_id": course_key, "galleries": []}
    else:
        raise BatchValidationError("operation_file", "provide an operation file or local package targets file")
    args.operation_file = str(operation_path) if operation_path is not None else ""
    operation["course_id"] = course_key

    resumable_attempts = []
    for entry in operation["galleries"]:
        attempt = resumable_repair_attempt(entry)
        if attempt is not None:
            resumable_attempts.append(attempt)
    request_id = ""
    if resumable_attempts:
        request_id = next(
            (
                attempt["request_id"]
                for attempt in resumable_attempts
                if isinstance(attempt.get("request_id"), str) and attempt["request_id"]
            ),
            str(operation.get("repair_request_id") or ""),
        )
    if not request_id:
        request_id = uuid.uuid4().hex
    operation["repair_request_id"] = request_id
    for entry in operation["galleries"]:
        resume_attempt = resumable_repair_attempt(entry)
        if resume_attempt and resume_attempt.get("request_id") in {None, request_id}:
            resume_attempt["request_id"] = request_id
            entry["repair_request_id"] = request_id
            if not isinstance(entry.get("repair_attempts_in_request"), int) or isinstance(entry.get("repair_attempts_in_request"), bool):
                matching_attempts = [
                    item
                    for item in entry.get("repair_history", [])
                    if isinstance(item, dict) and item.get("request_id") == request_id
                ]
                entry["repair_attempts_in_request"] = max(1, len(matching_attempts))
        else:
            matching_attempts = [
                item
                for item in entry.get("repair_history", [])
                if isinstance(item, dict) and item.get("request_id") == request_id
            ]
            if matching_attempts:
                entry["repair_request_id"] = request_id
                entry["repair_attempts_in_request"] = len(matching_attempts)
            elif not resumable_attempts:
                entry["repair_request_id"] = request_id
                entry["repair_attempts_in_request"] = 0
    report_mode = args.report_mode or "sections"
    operation["operator_report_mode"] = report_mode
    def store() -> None:
        if operation_path is not None and operation.get("galleries"):
            publisher.atomic_write_json(operation_path, operation)

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

    if package_path is not None and package is not None:
        live_structure, _mapping_diagnostic, _mapping_error = read_live_structure()
        mapped_entries = package_operation(
            package_path,
            package,
            package_targets,
            live_structure,
            course_key=course_key,
        )["galleries"]
        if operation_exists:
            previous_entries = [entry for entry in operation.get("galleries", []) if isinstance(entry, dict)]
            package_entries = []
            for mapped in mapped_entries:
                candidates = [
                    previous for previous in previous_entries
                    if mapped.get("gallery_key") == previous.get("gallery_key")
                    and mapped.get("gallery_key")
                ]
                if not candidates and mapped.get("vertical_locator"):
                    candidates = [
                        previous for previous in previous_entries
                        if safe_locator(previous.get("vertical_locator")) == mapped.get("vertical_locator")
                    ]
                if not candidates and mapped.get("section_id"):
                    candidates = [
                        previous for previous in previous_entries
                        if str(previous.get("section_id", "")).strip() == mapped.get("section_id")
                    ]
                if not candidates and mapped.get("section_title"):
                    candidates = [
                        previous for previous in previous_entries
                        if str(previous.get("section_title", "")).strip() == mapped.get("section_title")
                    ]
                if len(candidates) == 1:
                    previous = candidates[0]
                    current_mapping = mapped
                    mapped = dict(previous)
                    for key in (
                        "manifest", "section_id", "section_title", "package_target",
                        "target_mapping_status", "target_mapping_error", "gallery_missing",
                    ):
                        mapped[key] = current_mapping.get(key)
                    mapped["gallery_locator"] = current_mapping.get("gallery_locator", "")
                    mapped["vertical_locator"] = current_mapping.get("vertical_locator", "")
                    mapped["gallery_key"] = previous.get("gallery_key") or current_mapping["gallery_key"]
                package_entries.append(mapped)
            operation["galleries"] = package_entries
        else:
            operation["galleries"] = mapped_entries
        operation["targets_file"] = str(package_path)
        for entry in operation["galleries"]:
            entry["gallery_locator"] = safe_locator(entry.get("gallery_locator"))
            entry["vertical_locator"] = safe_locator(entry.get("vertical_locator"))
            if entry.get("target_mapping_status") != "resolved" and entry.get("target_mapping_error"):
                entry["validation"] = invalid_gallery_result(
                    entry,
                    None,
                    step="target_mapping",
                    summary=str(entry["target_mapping_error"]),
                )
        store()
    else:
        for entry in operation["galleries"]:
            entry["gallery_locator"] = safe_locator(entry.get("gallery_locator"))
            entry["vertical_locator"] = safe_locator(entry.get("vertical_locator"))
            if not entry["gallery_locator"] or not entry["vertical_locator"]:
                entry["validation"] = invalid_gallery_result(entry, None, step="operation_file", summary="Gallery or vertical locator is invalid")
        store()

    def audit_batch_round(round_number: int) -> dict[str, list[str]]:
        structure, structure_diagnostic, structure_error = read_live_structure()
        owners = map_gallery_owners(structure) if structure is not None else {}
        expected_verticals, missing_verticals, ambiguous_verticals, _ = expected_vertical_snapshots(operation)
        target_verticals = {
            safe_locator(entry.get("vertical_locator"))
            for entry in operation["galleries"]
            if isinstance(entry, dict) and safe_locator(entry.get("vertical_locator"))
        }
        publication_states, publication_diagnostic = read_saved_publication_states(
            client,
            studio_base=studio_base,
            course_key=course_key,
            studio_url=args.studio_url,
            target_verticals=target_verticals,
            step="load_saved_publication_state",
        )
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
            vertical_matches = [
                item for item in vertical_records
                if safe_locator(item.get("block_location")) == vertical_locator
            ]
            owner_matches = owners.get(gallery_locator, [])
            mapped_gallery_missing = (
                entry.get("gallery_missing") is True
                and entry.get("target_mapping_status") == "resolved"
                and bool(vertical_locator)
            )
            safe_local_repair_target = len(vertical_matches) == 1 and (
                mapped_gallery_missing
                or (
                    bool(gallery_locator)
                    and len(owner_matches) == 1
                    and owner_matches[0] == vertical_locator
                )
            )
            manifest_path = Path(str(entry.get("manifest", ""))).expanduser().resolve()
            manifest = None
            if mapped_gallery_missing or gallery_locator and vertical_locator:
                manifest = ensure_local_package(
                    entry,
                    manifest_path,
                    args=args,
                    targets_file=str(operation.get("targets_file") or ""),
                    request_id=request_id,
                    allow_local_repair=safe_local_repair_target,
                    store=store,
                )
            local_package_complete = manifest is not None
            if mapped_gallery_missing:
                if local_package_complete:
                    expected_count = manifest_image_count(manifest)
                    validation = {
                        "status": "mismatch",
                        "expected": {"gallery_present": True, "image_count": expected_count},
                        "observed": {"gallery_present": False},
                        "checks": {"gallery_presence": "mismatch"},
                        "failed_step": "gallery_presence",
                        "error_summary": "No Studio Gallery exists in the uniquely matched section",
                        "checked_at": utc_now(),
                    }
                else:
                    validation = invalid_gallery_result(
                        entry,
                        None,
                        step="manifest",
                        summary="Local Gallery manifest or referenced assets could not be validated",
                    )
                    validation["expected"]["gallery_present"] = True
                    validation["observed"]["gallery_present"] = False
                    validation["checks"]["gallery_presence"] = "mismatch"
                    validation["checks"]["local_package"] = "unverified"
            elif not gallery_locator or not vertical_locator:
                unresolved_mapping = entry.get("target_mapping_status") != "resolved"
                validation = invalid_gallery_result(
                    entry,
                    None,
                    step="target_mapping" if unresolved_mapping else "operation_file",
                    summary=(
                        str(entry.get("target_mapping_error") or "Package target could not be uniquely mapped to a Studio section")
                        if unresolved_mapping
                        else "Gallery or vertical locator is invalid"
                    ),
                )
            else:
                if local_package_complete:
                    validation = validate_gallery(client, entry, manifest)
                else:
                    validation = invalid_gallery_result(entry, None, step="manifest", summary="Local Gallery manifest or package could not be validated")
                    validation["checks"]["local_package"] = "unverified"
            if entry.get("local_package_status") == "incomplete":
                local_package_complete = False

            checks = validation.setdefault("checks", {})
            target_snapshot = None
            if structure_error:
                vertical_structure_status = "unverified"
                owner_status = "unverified"
                publication_status = "unverified"
                validation["pre_repair_structure_failed_step"] = "load_live_course_structure"
                validation["pre_repair_structure_error"] = structure_error[:240]
                if isinstance((structure_diagnostic or {}).get("http_status"), int):
                    validation["pre_repair_structure_http_status"] = structure_diagnostic["http_status"]
            else:
                target_snapshot = publisher.retained_vertical_snapshot(vertical_matches[0], gallery_locator) if len(vertical_matches) == 1 else None
                if target_snapshot is not None:
                    expected_verticals[vertical_locator] = target_snapshot
                    missing_verticals.discard(vertical_locator)
                    entry.setdefault("vertical_integrity", {})["expected"] = target_snapshot
                vertical_structure_status = live_vertical_structure_status(
                    structure,
                    vertical_locator,
                    gallery_locator=gallery_locator,
                    expected=expected_verticals,
                    missing=missing_verticals,
                    ambiguous=ambiguous_verticals,
                ) if len(vertical_matches) == 1 else "mismatch"
                owner_status = (
                    "pass"
                    if mapped_gallery_missing and len(vertical_matches) == 1
                    else "pass" if len(owner_matches) == 1 and owner_matches[0] == vertical_locator else "mismatch"
                )
                publication = publication_states.get(vertical_locator, {
                    "status": "unverified",
                    "published": None,
                    "has_changes": None,
                    "issues": [],
                    "unverified_reasons": ["publication_state_missing"],
                })
                publication_status = publication.get("status", "unverified")
                validation["observed_vertical"] = {
                    **{
                        key: publication[key]
                        for key in ("published", "has_changes", "visibility_state", "issues", "unverified_reasons")
                        if key in publication
                    },
                    "child_snapshot": observed_child_snapshot(vertical_matches[0]) if len(vertical_matches) == 1 else None,
                }
                for field in ("failed_step", "http_status", "error_summary"):
                    if field in publication:
                        validation[f"publication_{field}"] = publication[field]
                if publication_diagnostic and publication_diagnostic.get("status") != "ok":
                    validation["publication_read_failed"] = True
            checks["vertical_structure"] = "pass" if vertical_structure_status == "verified" else vertical_structure_status
            checks["gallery_owner"] = owner_status
            checks["vertical_publication"] = publication_status
            validation["audit_round"] = round_number
            publish = entry.get("publish") if isinstance(entry.get("publish"), dict) else {}
            validation["publish_status"] = publish.get("status", "not_started")
            if publish.get("failed_step"):
                validation["publish_failed_step"] = publish["failed_step"]
            states = list(checks.values())
            safe_missing_gallery = (
                mapped_gallery_missing
                and local_package_complete
                and not structure_error
                and len(vertical_matches) == 1
                and checks.get("gallery_presence") == "mismatch"
            )
            if safe_missing_gallery:
                validation["status"] = "mismatch"
            elif "unverified" in states:
                validation["status"] = "unverified"
            elif "mismatch" in states:
                validation["status"] = "mismatch"
            else:
                validation["status"] = "verified"
            content_mismatches = any(
                checks.get(name) == "mismatch"
                for name in ("gallery_presence", "images", "play_config", "audio_bindings", "audio_object_accessibility")
            )
            publication_mismatch = checks.get("vertical_publication") == "mismatch"
            structure_mismatch = checks.get("vertical_structure") == "mismatch"
            ambiguous_vertical_structure = (
                not structure_error
                and len(vertical_matches) == 1
                and target_snapshot is None
                and owner_status == "pass"
                and vertical_structure_status in {"mismatch", "unverified"}
            )
            play_config_untrusted = play_config_is_untrusted(validation)
            observed_config = validation.get("observed") if isinstance(validation.get("observed"), dict) else {}
            unreadable_timeline = (
                play_config_untrusted
                and checks.get("play_config") == "unverified"
                and "time_config_entries_valid" not in observed_config
            )
            timeline_only_repair = (
                unreadable_timeline
                and not content_mismatches
                and not publication_mismatch
                and not structure_mismatch
            )
            gallery_evidence_unverified = any(
                checks.get(name) == "unverified"
                for name in ("images", "audio_bindings", "audio_object_accessibility")
            )
            has_repair_target = (
                safe_missing_gallery
                or content_mismatches
                or timeline_only_repair
                or publication_mismatch
                or structure_mismatch
                or ambiguous_vertical_structure
            )
            ambiguous_structure_publish_already_attempted = (
                ambiguous_vertical_structure
                and not (safe_missing_gallery or content_mismatches or timeline_only_repair)
                and vertical_publish_attempted_in_request(entry, request_id)
            )
            validation["repairable"] = bool(
                local_package_complete
                and (safe_missing_gallery or not gallery_evidence_unverified)
                and not ambiguous_structure_publish_already_attempted
                and (
                    safe_missing_gallery
                    or (
                        has_repair_target
                        and (
                            checks.get("vertical_structure") in {"pass", "mismatch"}
                            or ambiguous_vertical_structure
                        )
                        and (not structure_mismatch or target_snapshot is not None or ambiguous_vertical_structure)
                        and owner_status == "pass"
                    )
                )
            )
            if validation["repairable"]:
                validation["repair_scope"] = (
                    "gallery" if safe_missing_gallery or content_mismatches or timeline_only_repair else
                    "structure" if structure_mismatch else
                    "publish_vertical"
                )
            if has_repair_target and not validation["repairable"]:
                validation["repair_skip_reason"] = (
                    "The local Gallery package is incomplete"
                    if not local_package_complete
                    else "A publish attempt was already made for this unresolved structure in the current request"
                    if ambiguous_structure_publish_already_attempted
                    else "The current section structure or Gallery ownership is not safe to change automatically"
                )
            entry["validation"] = validation
            history = entry.get("repair_history") if isinstance(entry.get("repair_history"), list) else []
            latest_attempt = history[-1] if history and isinstance(history[-1], dict) else {}
            if (
                latest_attempt.get("repair_round") == round_number
                and "final_validation" not in latest_attempt
            ):
                latest_attempt["final_validation"] = copy.deepcopy(validation)
                publisher_result = latest_attempt.get("publisher_result") if isinstance(latest_attempt.get("publisher_result"), dict) else {}
                vertical_publish = latest_attempt.get("vertical_publish") if isinstance(latest_attempt.get("vertical_publish"), dict) else {}
                repair_failed = (
                    latest_attempt.get("status") == "failed"
                    or publisher_result.get("status") in {"failed", "partial"}
                    or vertical_publish.get("status") == "failed"
                )
                if repair_failed:
                    latest_attempt["status"] = "failed"
                    validation["repair_status"] = "failed"
                elif validation["status"] == "verified" and vertical_publish.get("status") == "completed":
                    latest_attempt["status"] = "completed"
                elif validation["status"] == "unverified":
                    latest_attempt["status"] = "unverified"
                elif validation["status"] == "mismatch":
                    latest_attempt["status"] = "mismatch"
                latest_attempt["finished_at"] = utc_now()
            store()
        return owners

    owners = audit_batch_round(0)
    repair_cycles = range(1, MAX_REPAIR_CYCLES + 1) if args.execute else ()
    for cycle in repair_cycles:
        repair_targets = []
        for entry in operation["galleries"]:
            validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
            if not validation.get("repairable"):
                continue
            attempts_this_request = entry.get("repair_attempts_in_request", 0)
            if not isinstance(attempts_this_request, int) or isinstance(attempts_this_request, bool):
                attempts_this_request = 0
            resume_attempt = resumable_repair_attempt(entry)
            if resume_attempt and resume_attempt.get("request_id") not in {None, request_id}:
                resume_attempt = None
            if resume_attempt is None and attempts_this_request >= MAX_REPAIR_CYCLES:
                validation["repair_limit_reached"] = True
                store()
                continue
            if resume_attempt is None:
                entry["repair_request_id"] = request_id
                entry["repair_attempts_in_request"] = attempts_this_request + 1
                store()
            if validation.get("repair_scope") == "publish_vertical":
                repair_targets.append((entry, "publish_vertical", None, resume_attempt))
                continue
            if validation.get("repair_scope") == "structure":
                repair_targets.append((entry, "structure", None, resume_attempt))
                continue
            try:
                manifest = publisher.ensure_manifest(Path(str(entry.get("manifest", ""))).expanduser().resolve())
            except Exception:
                validation["repairable"] = False
                validation["repair_skip_reason"] = "The local Gallery package could not be checked before repair"
                store()
                continue
            repair_targets.append((entry, "gallery", manifest, resume_attempt))
        if not repair_targets:
            break
        for entry, repair_mode, manifest, resume_attempt in repair_targets:
            validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
            try:
                if repair_mode == "structure":
                    history = entry.setdefault("repair_history", [])
                    attempt = resume_attempt or {
                        "attempt": max(len(history), int(entry.get("repair_attempts", 0))) + 1,
                        "repair_round": cycle,
                        "repair_mode": "structure",
                        "request_id": request_id,
                        "steps": [],
                    }
                    attempt["repair_round"] = cycle
                    attempt["request_id"] = request_id
                    if resume_attempt is None:
                        history.append(attempt)
                    entry["repair_attempts"] = max(entry.get("repair_attempts", 0), attempt["attempt"])
                    attempt["vertical_publish"] = vertical_publish_started_record()
                    store()
                    steps, expected = publisher.cleanup_and_publish_vertical(
                        client.session, studio_base=studio_base, vertical_locator=entry["vertical_locator"],
                        gallery_locator=entry["gallery_locator"], adapter=adapter, csrftoken=client.csrftoken, timeout=args.timeout,
                    )
                    attempt["steps"] = steps
                    attempt["vertical_publish"] = steps[-1]
                    attempt["status"] = "completed" if all(step["status"] == "completed" for step in steps) else "failed"
                    if expected is not None:
                        entry.setdefault("vertical_integrity", {})["expected"] = expected
                    store()
                elif repair_mode == "publish_vertical":
                    attempt = republish_vertical_for_pending_changes(
                        client,
                        entry,
                        args,
                        adapter,
                        store,
                        repair_round=cycle,
                        attempt=resume_attempt,
                    )
                    attempt["repair_round"] = cycle
                    attempt["request_id"] = request_id
                else:
                    attempt = replace_gallery_from_package(
                        operation,
                        entry,
                        manifest,
                        args,
                        adapter,
                        store,
                        repair_round=cycle,
                        attempt=resume_attempt,
                    )
            except Exception as exc:
                history = entry.setdefault("repair_history", [])
                attempt = history[-1] if history and isinstance(history[-1], dict) else {"attempt": len(history) + 1, "steps": []}
                attempt["repair_round"] = cycle
                attempt["request_id"] = request_id
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
    operation["batch_result"] = summarize_batch(operation, studio_base=studio_base, report_mode=report_mode)
    store()
    return operation["batch_result"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and safely repair a published Studio ImageGallery batch")
    parser.add_argument("--studio-url", help="Studio URL for a target vertical in this course")
    parser.add_argument("--operation-file", default="", help="batch-operation.json input/output; package mode defaults to a sibling report")
    parser.add_argument("--targets-file", default="", help="local package targets file for operation-free legacy course mapping")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read Studio and local package state without repairing")
    mode.add_argument("--execute", action="store_true", help="repair confirmed mismatches and recheck Studio")
    parser.add_argument(
        "--report-mode",
        choices=sorted(OPERATOR_REPORT_MODES),
        default=None,
        help="group findings by target subsection (default) or by check category",
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
    parser.add_argument(
        "--ppt-to-imagesgallery-root",
        default=os.environ.get("PPT_TO_IMAGESGALLERY_ROOT", ""),
        help="installed ppt-to-imagesgallery skill root for targeted local material recovery",
    )
    args = parser.parse_args()
    args.cookie = args.cookie or os.environ.get("FIRA_SAAS_OP_COOKIE", "")
    args.csrf_token = args.csrf_token or os.environ.get("FIRA_SAAS_OP_CSRF_TOKEN", "")
    if not args.studio_url:
        parser.error("--studio-url is required")
    if not args.operation_file and not args.targets_file:
        parser.error("either --operation-file or --targets-file is required")
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
