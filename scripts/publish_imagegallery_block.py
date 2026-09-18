#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests


DEFAULT_AUTH_RETRY_MAX_RETRIES = 4
DEFAULT_AUTH_RETRY_DELAY_SECONDS = 3.0
DEFAULT_BATCH_REPORT_FILENAME = "imagegallery-publish-report.json"
AUTH_ERROR_HINTS = (
    "not login",
    "login yet",
    "unauthorized",
    "authorization",
    "authentication",
    "authenticated",
    "csrf",
    "session",
    "login required",
    "登录",
    "认证",
    "会话",
)
BLOCK_LOCATOR_PREFIX_RE = re.compile(r"^(block-v1:)([^+]+\+[^+]+\+[^+]+)")
HTML_MAIN_BODY_HINTS = ("正文", "文字讲解", "课程内容", "main", "body", "content")


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class TemplateCallError(RuntimeError):
    def __init__(self, step_name: str, status_code: int, payload: Any):
        self.step_name = step_name
        self.status_code = int(status_code)
        self.payload = payload
        payload_text = payload_to_text(payload)
        self.payload_text = payload_text[:500]
        super().__init__(f"{step_name} 失败: HTTP {status_code}, body={payload}")

    def is_auth_related(self) -> bool:
        return is_auth_related_failure(self.status_code, self.payload)


class PublishFailure(RuntimeError):
    def __init__(self, step_name: str, message: str, *, payload: Any = None):
        self.step_name = step_name
        self.payload = payload
        super().__init__(message)


def parse_vertical_block_id(url_input: str) -> str | None:
    m = re.search(r"block-v1:[^/?#]+", url_input)
    return m.group(0) if m else None


def normalize_block_url(url_input: str) -> str:
    parsed = urlparse(url_input.strip())
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def studio_base_from_url(url_input: str) -> str:
    parsed = urlparse(str(url_input or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def build_container_block_url(studio_base: str, block_locator: str) -> str:
    base = (studio_base or "").rstrip("/")
    locator = (block_locator or "").strip()
    if not base or not locator:
        return ""
    return f"{base}/container/{locator}"


def course_key_from_vertical_block_id(vertical_block_id: str) -> str:
    parts = vertical_block_id.split("+")
    if len(parts) < 3 or ":" not in parts[0]:
        return ""
    org = parts[0].split(":", 1)[1]
    return f"course-v1:{org}+{parts[1]}+{parts[2]}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_with_diagnostics(path: Path, *, step_name: str) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except JSONDecodeError as exc:
        start = max(0, exc.pos - 120)
        end = min(len(text), exc.pos + 120)
        snippet = text[start:end].replace("\r", "\\r").replace("\n", "\\n")
        raise PublishFailure(
            step_name,
            (
                f"{step_name} 失败: invalid json file {path} "
                f"(line {exc.lineno}, column {exc.colno}): {exc.msg}; snippet={snippet}"
            ),
        ) from exc


def concise_node_name(node: dict[str, Any]) -> str:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    for key in ("display_name", "name", "title"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def concise_node_locator(node: dict[str, Any]) -> str:
    for key in ("id", "block_location", "locator"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def concise_child_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    child_info = node.get("child_info") if isinstance(node.get("child_info"), dict) else {}
    candidates = child_info.get("children")
    if isinstance(candidates, list):
        return [child for child in candidates if isinstance(child, dict)]
    fallback = node.get("children")
    if isinstance(fallback, list) and fallback and all(isinstance(child, dict) for child in fallback):
        return list(fallback)
    return []


def format_block_order(*parts: int) -> str:
    return ".".join(f"{int(part):03d}" for part in parts)


def normalize_concise_block(node: dict[str, Any], *, block_order: str) -> dict[str, Any]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    text_value = ""
    for key in ("html", "text", "body", "content"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            text_value = value
            break
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            text_value = value
            break
    return {
        "name": concise_node_name(node),
        "category": str(node.get("category", "")).strip(),
        "block_location": concise_node_locator(node),
        "block_order": block_order,
        "html": text_value,
        "text": text_value,
        "published": node.get("published"),
    }


def normalize_course_structure_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("course structure json must be an object")
    chapters = data.get("chapters")
    if isinstance(chapters, list):
        return data

    root_category = str(data.get("category", "")).strip().lower()
    root_locator = concise_node_locator(data)
    root_children = concise_child_nodes(data)
    if root_category and root_children:
        normalized_chapters: list[dict[str, Any]] = []
        chapter_nodes = root_children if root_category == "course" else [data]
        for chapter_index, chapter_node in enumerate(chapter_nodes, start=1):
            if not isinstance(chapter_node, dict):
                continue
            section_nodes = concise_child_nodes(chapter_node)
            normalized_sections: list[dict[str, Any]] = []
            for section_index, section_node in enumerate(section_nodes, start=1):
                if not isinstance(section_node, dict):
                    continue
                vertical_nodes = concise_child_nodes(section_node)
                normalized_verticals: list[dict[str, Any]] = []
                for vertical_index, vertical_node in enumerate(vertical_nodes, start=1):
                    if not isinstance(vertical_node, dict):
                        continue
                    block_nodes = concise_child_nodes(vertical_node)
                    normalized_blocks = [
                        normalize_concise_block(
                            block_node,
                            block_order=format_block_order(chapter_index, section_index, vertical_index, block_index),
                        )
                        for block_index, block_node in enumerate(block_nodes, start=1)
                        if isinstance(block_node, dict)
                    ]
                    normalized_verticals.append(
                        {
                            "name": concise_node_name(vertical_node),
                            "category": str(vertical_node.get("category", "")).strip(),
                            "block_location": concise_node_locator(vertical_node),
                            "block_order": format_block_order(chapter_index, section_index, vertical_index),
                            "published": vertical_node.get("published"),
                            "blocks": normalized_blocks,
                        }
                    )
                normalized_sections.append(
                    {
                        "name": concise_node_name(section_node),
                        "category": str(section_node.get("category", "")).strip(),
                        "block_location": concise_node_locator(section_node),
                        "block_order": format_block_order(chapter_index, section_index),
                        "published": section_node.get("published"),
                        "verticals": normalized_verticals,
                    }
                )
            normalized_chapters.append(
                {
                    "name": concise_node_name(chapter_node),
                    "category": str(chapter_node.get("category", "")).strip(),
                    "block_location": concise_node_locator(chapter_node),
                    "block_order": format_block_order(chapter_index),
                    "published": chapter_node.get("published"),
                    "sections": normalized_sections,
                }
            )
        return {
            "course_id": root_locator,
            "name": concise_node_name(data),
            "category": root_category,
            "chapters": normalized_chapters,
        }

    return data


def mark_replacement_plan_source(replacement_plan: dict[str, Any], plan_source: str) -> dict[str, Any]:
    tagged = dict(replacement_plan)
    tagged["plan_source"] = str(plan_source or "").strip()
    return tagged


def block_locator_course_prefix(block_locator: str) -> str:
    match = BLOCK_LOCATOR_PREFIX_RE.match(str(block_locator or "").strip())
    return match.group(2) if match else ""


def remap_block_locator_course(block_locator: str, source_vertical_block_id: str, target_vertical_block_id: str) -> str:
    raw = str(block_locator or "").strip()
    source_prefix = block_locator_course_prefix(source_vertical_block_id)
    target_prefix = block_locator_course_prefix(target_vertical_block_id)
    if not raw or not source_prefix or not target_prefix or source_prefix == target_prefix:
        return raw
    prefix = f"block-v1:{source_prefix}"
    if raw.startswith(prefix):
        return f"block-v1:{target_prefix}{raw[len(prefix):]}"
    return raw


def iter_course_sections(course_structure: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for chapter in course_structure.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        for section in chapter.get("sections", []):
            if isinstance(section, dict):
                sections.append(section)
    return sections


def iter_section_vertical_records(course_structure: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for section in iter_course_sections(course_structure):
        for vertical in section.get("verticals", []):
            if isinstance(vertical, dict):
                records.append({"section": section, "vertical": vertical})
    return records


def block_category(block: dict[str, Any]) -> str:
    return str(block.get("category", "")).strip().lower()


def block_locator(block: dict[str, Any]) -> str:
    return str(block.get("block_location", "")).strip()


def collect_structure_block_ids(course_structure: dict[str, Any]) -> list[str]:
    block_ids: list[str] = []
    for record in iter_section_vertical_records(course_structure):
        vertical = record["vertical"]
        vertical_locator = str(vertical.get("block_location", "")).strip()
        if vertical_locator:
            block_ids.append(vertical_locator)
        for block in vertical.get("blocks", []):
            if isinstance(block, dict):
                locator = block_locator(block)
                if locator:
                    block_ids.append(locator)
    return block_ids


def normalize_auth_retry_max_retries(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_AUTH_RETRY_MAX_RETRIES
    return min(DEFAULT_AUTH_RETRY_MAX_RETRIES, max(0, parsed))


def classify_replacement_kind(
    target_record: dict[str, Any],
    *,
    excluded_block_locators: set[str] | None = None,
) -> str:
    excluded = excluded_block_locators or set()
    vertical = target_record.get("vertical") if isinstance(target_record, dict) else None
    if isinstance(vertical, dict):
        for block in vertical.get("blocks", []):
            if (
                isinstance(block, dict)
                and block_category(block) == "imagesgallery"
                and block_locator(block)
                and block_locator(block) not in excluded
            ):
                return "imagesgallery_based"
    return "html_based"


def html_text_length(block: dict[str, Any]) -> int:
    for key in ("html", "text", "body", "content"):
        value = block.get(key)
        if isinstance(value, str):
            return len(value.strip())
    return len(payload_to_text(block))


def pick_best_html_candidate(
    vertical: dict[str, Any],
    *,
    excluded_block_locators: set[str] | None = None,
) -> dict[str, Any] | None:
    excluded = excluded_block_locators or set()
    html_blocks = [
        block
        for block in vertical.get("blocks", [])
        if (
            isinstance(block, dict)
            and block_category(block) == "html"
            and block_locator(block)
            and block_locator(block) not in excluded
        )
    ]
    if not html_blocks:
        return None

    def score(block: dict[str, Any]) -> tuple[int, int]:
        name = str(block.get("name", "")).lower()
        main_body_bonus = 1 if any(hint in name for hint in HTML_MAIN_BODY_HINTS) else 0
        return (main_body_bonus, html_text_length(block))

    return max(html_blocks, key=score)


def pick_existing_gallery(
    vertical: dict[str, Any],
    *,
    excluded_block_locators: set[str] | None = None,
) -> dict[str, Any] | None:
    excluded = excluded_block_locators or set()
    galleries = [
        block
        for block in vertical.get("blocks", [])
        if (
            isinstance(block, dict)
            and block_category(block) == "imagesgallery"
            and block_locator(block)
            and block_locator(block) not in excluded
        )
    ]
    if not galleries:
        return None
    return max(
        galleries,
        key=lambda block: (
            1 if "有声幻灯片" in str(block.get("name", "")) else 0,
            len(str(block.get("name", ""))),
        ),
    )


def build_vertical_children(
    children: list[str],
    *,
    new_block_locator: str,
    replace_block_locator: str = "",
) -> list[str]:
    ordered = [str(child).strip() for child in children if str(child).strip() and str(child).strip() != new_block_locator]
    if replace_block_locator and replace_block_locator in ordered:
        insert_at = ordered.index(replace_block_locator)
        ordered.insert(insert_at, new_block_locator)
        return ordered
    return [new_block_locator, *ordered]


def plan_publish_replacement(
    course_structure: dict[str, Any],
    *,
    source_vertical_block_id: str,
    target_vertical_block_id: str,
    excluded_block_locators: list[str] | None = None,
) -> dict[str, Any]:
    source_vertical_block_id = str(source_vertical_block_id or "").strip()
    target_vertical_block_id = str(target_vertical_block_id or source_vertical_block_id).strip()
    excluded = {str(locator).strip() for locator in (excluded_block_locators or []) if str(locator).strip()}
    records = iter_section_vertical_records(course_structure)
    record_by_source_vertical = {
        str(record["vertical"].get("block_location", "")).strip(): record
        for record in records
        if str(record["vertical"].get("block_location", "")).strip()
    }
    lookup_record = record_by_source_vertical.get(source_vertical_block_id) or record_by_source_vertical.get(target_vertical_block_id)
    if lookup_record is None:
        raise PublishFailure("course_structure_lookup", f"source vertical not found in course structure: {source_vertical_block_id or target_vertical_block_id}")

    course_kind = classify_replacement_kind(
        lookup_record,
        excluded_block_locators=excluded,
    )
    target_record = lookup_record
    matched_block: dict[str, Any] | None = None
    working_record = target_record
    match_scope = "fallback"
    result_type = "fallback_inserted"

    if course_kind == "imagesgallery_based":
        matched_block = pick_existing_gallery(
            target_record["vertical"],
            excluded_block_locators=excluded,
        )
        if matched_block is not None:
            match_scope = "target_vertical"
            result_type = "replaced"
    else:
        matched_block = pick_best_html_candidate(
            target_record["vertical"],
            excluded_block_locators=excluded,
        )
        if matched_block is not None:
            match_scope = "target_vertical"
            result_type = "replaced"
        else:
            best_section_match: tuple[tuple[int, int], dict[str, Any], dict[str, Any]] | None = None
            for vertical in target_record["section"].get("verticals", []):
                if not isinstance(vertical, dict):
                    continue
                candidate = pick_best_html_candidate(
                    vertical,
                    excluded_block_locators=excluded,
                )
                if candidate is None:
                    continue
                score = (
                    1 if any(hint in str(candidate.get("name", "")).lower() for hint in HTML_MAIN_BODY_HINTS) else 0,
                    html_text_length(candidate),
                )
                if best_section_match is None or score > best_section_match[0]:
                    best_section_match = (score, {"section": target_record["section"], "vertical": vertical}, candidate)
            if best_section_match is not None:
                _score, working_record, matched_block = best_section_match
                match_scope = "section"
                result_type = "replaced"

    working_source_vertical_id = str(working_record["vertical"].get("block_location", "")).strip()
    current_children = [
        remap_block_locator_course(block_locator(block), source_vertical_block_id, target_vertical_block_id)
        for block in working_record["vertical"].get("blocks", [])
        if isinstance(block, dict) and block_locator(block)
    ]
    matched_source_block_locator = ""
    matched_source_block_category = ""
    if matched_block is not None:
        matched_source_block_locator = remap_block_locator_course(
            block_locator(matched_block),
            source_vertical_block_id,
            target_vertical_block_id,
        )
        matched_source_block_category = block_category(matched_block)

    return {
        "course_kind": course_kind,
        "result_type": result_type,
        "match_scope": match_scope,
        "source_vertical_block_id": source_vertical_block_id,
        "target_vertical_block_id": target_vertical_block_id,
        "working_vertical_block_id": remap_block_locator_course(
            working_source_vertical_id,
            source_vertical_block_id,
            target_vertical_block_id,
        ),
        "working_vertical_name": str(working_record["vertical"].get("name", "")),
        "current_children": current_children,
        "matched_source_block_locator": matched_source_block_locator,
        "matched_source_block_category": matched_source_block_category,
        "all_structure_block_ids": [
            remap_block_locator_course(block_id, source_vertical_block_id, target_vertical_block_id)
            for block_id in collect_structure_block_ids(course_structure)
        ],
    }


def resolve_rerun_action(previous_result: dict[str, Any] | None, *, current_structure_block_ids: list[str]) -> dict[str, str]:
    if not previous_result:
        return {"action": "fresh", "reason": "no previous result"}

    status = str(previous_result.get("status", "")).strip()
    if status in {"replaced", "fallback_inserted"}:
        return {"action": "skip", "reason": "target already succeeded in a previous batch report"}

    created_block_locator = str(previous_result.get("created_block_locator", "")).strip()
    if not created_block_locator:
        return {"action": "fresh", "reason": "no reusable created block recorded"}
    if created_block_locator not in current_structure_block_ids:
        return {
            "action": "cleanup_orphaned_created_block",
            "reason": "previously created block is missing from refreshed course structure; try orphan cleanup before a fresh rerun",
        }

    failed_step = str(previous_result.get("failed_step", "")).strip()
    if failed_step == "publish_vertical":
        return {"action": "resume_publish_only", "reason": "created block still exists; retrying final vertical publish only"}
    return {
        "action": "manual_followup",
        "reason": "partial publish requires manual follow-up before another automatic rerun",
    }


def ensure_manifest(manifest_path: Path) -> dict[str, Any]:
    data = read_json(manifest_path)
    if not isinstance(data.get("items"), list) or not data["items"]:
        raise ValueError("manifest.items 不能为空")
    if not isinstance(data.get("audio"), dict):
        raise ValueError("manifest.audio 缺失（需要 stage-B schema）")
    audio = data["audio"]
    if not audio.get("full_audio"):
        raise ValueError("manifest.audio.full_audio 缺失")

    root = manifest_path.parent
    for item in data["items"]:
        page = item.get("page")
        if page is None:
            raise ValueError("manifest.items[].page 缺失")
        for key in ("image", "subtitle", "start", "end"):
            if key not in item:
                raise ValueError(f"manifest.items[] 缺失字段: {key}")
        img = Path(str(item["image"]))
        if not img.is_absolute():
            img = (root / img).resolve()
        if not img.exists():
            raise FileNotFoundError(f"图片不存在: {img}")
        item["_image_abs"] = str(img)

    full_audio = Path(str(audio["full_audio"]))
    if not full_audio.is_absolute():
        full_audio = (root / full_audio).resolve()
    if not full_audio.exists():
        raise FileNotFoundError(f"音频不存在: {full_audio}")
    audio["_full_audio_abs"] = str(full_audio)

    data["items"] = sorted(data["items"], key=lambda x: int(x["page"]))
    return data


def render_value(v: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(v, str):
        m = re.fullmatch(r"\{([a-zA-Z0-9_]+)\}", v.strip())
        if m and m.group(1) in ctx:
            return ctx[m.group(1)]
        return v.format_map(SafeDict({k: str(x) for k, x in ctx.items()}))
    if isinstance(v, list):
        return [render_value(x, ctx) for x in v]
    if isinstance(v, dict):
        return {k: render_value(x, ctx) for k, x in v.items()}
    return v


def get_by_path(data: Any, path: str) -> Any:
    cur = data
    for seg in path.split("."):
        if seg == "":
            continue
        if isinstance(cur, list) and seg.isdigit():
            idx = int(seg)
            cur = cur[idx]
            continue
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            raise KeyError(f"extract path 不存在: {path}")
    return cur


def maybe_extract_uuid(text: str) -> str:
    m = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        text,
    )
    return m.group(0) if m else ""


def payload_to_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def is_auth_related_failure(status_code: int, payload: Any) -> bool:
    text = payload_to_text(payload).lower()
    if int(status_code) == 401:
        return True
    if int(status_code) == 403 and any(hint in text for hint in AUTH_ERROR_HINTS):
        return True
    return any(hint in text for hint in AUTH_ERROR_HINTS)


def call_with_auth_retry(
    call_fn: Any,
    refresh_auth_fn: Any = None,
    *,
    max_retries: int = DEFAULT_AUTH_RETRY_MAX_RETRIES,
    delay_seconds: float = DEFAULT_AUTH_RETRY_DELAY_SECONDS,
) -> tuple[Any, list[dict[str, Any]]]:
    retry_events: list[dict[str, Any]] = []
    while True:
        try:
            result = call_fn()
            return result, retry_events
        except TemplateCallError as exc:
            if refresh_auth_fn is None or not exc.is_auth_related() or len(retry_events) >= max_retries:
                raise
            retry_index = len(retry_events) + 1
            retry_events.append(
                {
                    "retry_index": retry_index,
                    "reason": "auth_error",
                    "step": exc.step_name,
                    "status_code": exc.status_code,
                    "message": exc.payload_text,
                }
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            refresh_auth_fn()


def ensure_oss2():
    try:
        import oss2  # type: ignore
        return oss2
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "oss2"])
        import oss2  # type: ignore
        return oss2


def derive_courses_base_from_studio_base(studio_base: str) -> str:
    u = urlparse(studio_base)
    host = u.hostname or ""
    if host.startswith("studio."):
        new_host = "courses." + host[len("studio."):]
        return f"{u.scheme}://{new_host}"
    return ""


def sts_multipart_upload_audio(ctx: dict[str, Any]) -> dict[str, Any]:
    oss2 = ensure_oss2()

    creds = ctx.get("Credentials") or {}
    access_key_id = str(creds.get("AccessKeyId", "")).strip()
    access_key_secret = str(creds.get("AccessKeySecret", "")).strip()
    security_token = str(creds.get("SecurityToken", "")).strip()
    region = str(ctx.get("region", "")).strip()
    bucket_name = str(ctx.get("bucket", "")).strip()
    file_key = str(ctx.get("file_key", "")).strip()
    audio_file = Path(str(ctx.get("audio_file", "")))
    suffix = str(ctx.get("audio_post_fix", "")).strip()

    if not all([access_key_id, access_key_secret, security_token, region, bucket_name, file_key, suffix]):
        raise ValueError("OSS multipart 上传缺少必要字段（Credentials/region/bucket/file_key/suffix）")
    if not audio_file.exists():
        raise FileNotFoundError(f"音频文件不存在: {audio_file}")

    object_key = f"{file_key}.{suffix}"
    endpoint = f"https://{region}.aliyuncs.com"
    auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)

    total_size = audio_file.stat().st_size
    part_size = oss2.determine_part_size(total_size, preferred_size=5 * 1024 * 1024)
    init_res = bucket.init_multipart_upload(object_key)
    upload_id = init_res.upload_id
    parts = []
    uploaded_parts = 0

    try:
        with audio_file.open("rb") as fp:
            part_number = 1
            offset = 0
            while offset < total_size:
                num_to_upload = min(part_size, total_size - offset)
                result = bucket.upload_part(
                    object_key,
                    upload_id,
                    part_number,
                    oss2.SizedFileAdapter(fp, num_to_upload),
                )
                parts.append(oss2.models.PartInfo(part_number, result.etag))
                offset += num_to_upload
                part_number += 1
                uploaded_parts += 1

        bucket.complete_multipart_upload(object_key, upload_id, parts)
    except Exception:
        try:
            bucket.abort_multipart_upload(object_key, upload_id)
        except Exception:
            pass
        raise

    return {
        "object_key": object_key,
        "endpoint": endpoint,
        "bucket": bucket_name,
        "upload_id": upload_id,
        "parts": uploaded_parts,
        "size": total_size,
    }


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in cookie_header.split(";"):
        p = part.strip()
        if not p or "=" not in p:
            continue
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def login_and_get_studio_session(
    account: str,
    password: str,
    courses_base: str,
    studio_csrf_url: str,
) -> tuple[requests.Session, str]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
    )

    login_url = f"{courses_base.rstrip('/')}/openapi/v1/auth/login/"
    resp = session.post(login_url, data={"account": account, "password": password}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    jwt = data.get("meta", {}).get("jwt")
    if not jwt:
        raise ValueError("登录成功但未返回 jwt")
    session.headers["Authorization"] = f"Bearer {jwt}"

    page_resp = session.get(studio_csrf_url, headers={"Accept": "text/html,*/*"}, timeout=30)
    page_resp.raise_for_status()

    studio_host = urlparse(studio_csrf_url).hostname or ""
    csrftoken = ""
    for c in session.cookies:
        if c.name == "csrftoken" and studio_host and studio_host in (c.domain or ""):
            csrftoken = c.value
            break
    if not csrftoken:
        for c in session.cookies:
            if c.name == "csrftoken":
                csrftoken = c.value
                break
    if not csrftoken:
        raise ValueError("未获取到 csrftoken")
    return session, csrftoken


def build_studio_session_from_args(
    *,
    studio_base: str,
    studio_url: str,
    args: argparse.Namespace,
    allow_login: bool,
) -> tuple[requests.Session, str, bool]:
    session = requests.Session()
    csrftoken = str(getattr(args, "csrf_token", "") or "").strip()
    auth_ready = False

    cookie_header = str(getattr(args, "cookie", "") or "").strip()
    if cookie_header:
        cookies = parse_cookie_header(cookie_header)
        for key, value in cookies.items():
            session.cookies.set(key, value, domain=urlparse(studio_base).hostname or "")
        auth_ready = bool(cookies)
        if not csrftoken:
            csrftoken = cookies.get("csrftoken", "")

    account = str(getattr(args, "account", "") or os.environ.get("FIRA_SAAS_OP_ACCOUNT", "")).strip()
    password = str(getattr(args, "password", "") or os.environ.get("FIRA_SAAS_OP_PASSWORD", "")).strip()
    courses_base = str(getattr(args, "courses_base", "") or derive_courses_base_from_studio_base(studio_base)).strip()
    if allow_login and not auth_ready and account and password and courses_base:
        session, login_csrf = login_and_get_studio_session(
            account=account,
            password=password,
            courses_base=courses_base,
            studio_csrf_url=studio_url,
        )
        auth_ready = True
        if not csrftoken:
            csrftoken = login_csrf

    return session, csrftoken, auth_ready


def decode_response_payload(resp: requests.Response) -> tuple[Any, str | None]:
    content_type = resp.headers.get("content-type", "")
    body_text = resp.text or ""
    if not body_text.strip():
        return {}, None

    should_attempt_json = "application/json" in content_type.lower() or body_text.lstrip().startswith(("{", "["))
    if should_attempt_json:
        try:
            return resp.json(), None
        except (JSONDecodeError, ValueError) as exc:
            return (
                {
                    "text": body_text[:2000],
                    "_json_parse_error": str(exc),
                    "_content_type": content_type,
                },
                str(exc),
            )

    return {"text": body_text[:2000], "_content_type": content_type}, None


def fetch_live_course_structure(
    session: requests.Session,
    *,
    studio_base: str,
    course_key: str,
    timeout: int,
) -> dict[str, Any]:
    url = f"{studio_base.rstrip('/')}/course/{course_key}"
    resp = session.get(
        url,
        params={"format": "concise"},
        headers={"Accept": "application/json, text/plain, */*"},
        timeout=timeout,
    )
    payload, parse_warning = decode_response_payload(resp)
    if resp.status_code >= 400:
        raise PublishFailure(
            "load_live_course_structure",
            f"load_live_course_structure 失败: HTTP {resp.status_code}, url={url}, body={payload_to_text(payload)[:500]}",
            payload=payload,
        )
    if parse_warning:
        raise PublishFailure(
            "load_live_course_structure",
            f"load_live_course_structure 返回了无法解析的 JSON: url={url}, error={parse_warning}, body={payload_to_text(payload)[:500]}",
            payload=payload,
        )
    try:
        return normalize_course_structure_payload(payload)
    except Exception as exc:
        raise PublishFailure(
            "load_live_course_structure",
            f"load_live_course_structure 返回的课程结构无法标准化: url={url}, error={exc}",
            payload=payload,
        ) from exc


def maybe_fetch_live_replacement_plan(
    *,
    studio_url: str,
    source_vertical_block_id: str,
    target_vertical_block_id: str,
    excluded_block_locators: list[str] | None = None,
    args: argparse.Namespace,
    timeout: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not bool(getattr(args, "execute", False)):
        return None, None

    studio_base = studio_base_from_url(studio_url)
    course_key = course_key_from_vertical_block_id(target_vertical_block_id)
    if not studio_base or not course_key:
        return None, None

    session, _csrftoken, auth_ready = build_studio_session_from_args(
        studio_base=studio_base,
        studio_url=studio_url,
        args=args,
        allow_login=bool(getattr(args, "execute", False)),
    )
    if not auth_ready:
        return None, None

    try:
        live_structure = fetch_live_course_structure(
            session,
            studio_base=studio_base,
            course_key=course_key,
            timeout=timeout,
        )
        live_plan = mark_replacement_plan_source(
            plan_publish_replacement(
                live_structure,
                source_vertical_block_id=source_vertical_block_id,
                target_vertical_block_id=target_vertical_block_id,
                excluded_block_locators=excluded_block_locators,
            ),
            "live_target",
        )
        return live_plan, {
            "step": "load_live_target_structure",
            "status": "ok",
            "course_key": course_key,
            "working_vertical_block_id": live_plan.get("working_vertical_block_id", ""),
            "matched_source_block_locator": live_plan.get("matched_source_block_locator", ""),
        }
    except PublishFailure as exc:
        return None, {
            "step": "load_live_target_structure",
            "status": "warning",
            "course_key": course_key,
            "note": str(exc),
        }


def validate_replacement_plan(
    replacement_plan: dict[str, Any],
    *,
    created_block_locator: str,
    publish_only: bool,
) -> None:
    current_children = [str(child).strip() for child in replacement_plan.get("current_children", []) if str(child).strip()]
    matched_source_block_locator = str(replacement_plan.get("matched_source_block_locator", "")).strip()
    working_vertical_block_id = str(replacement_plan.get("working_vertical_block_id", "")).strip()
    if not working_vertical_block_id:
        raise PublishFailure("replacement_validation", "replacement plan missing working vertical block id")

    if matched_source_block_locator and replacement_plan.get("result_type") == "replaced" and matched_source_block_locator not in current_children:
        raise PublishFailure(
            "replacement_validation",
            f"matched source block is not present in live vertical children: vertical={working_vertical_block_id}, block={matched_source_block_locator}",
        )

    if not publish_only and created_block_locator and matched_source_block_locator == created_block_locator:
        raise PublishFailure(
            "replacement_validation",
            f"live replacement plan matched the newly created imagesgallery block; refusing to delete it: vertical={working_vertical_block_id}, block={created_block_locator}",
        )

    if publish_only and created_block_locator and created_block_locator not in current_children:
        raise PublishFailure(
            "replacement_validation",
            f"previously created imagesgallery block is no longer present in the working vertical children: vertical={working_vertical_block_id}, block={created_block_locator}",
        )


def build_files(files_spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, spec in files_spec.items():
        if isinstance(spec, str):
            p = Path(render_value(spec, ctx))
            mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            out[field] = (p.name, p.read_bytes(), mime)
        elif isinstance(spec, dict):
            p = Path(render_value(spec.get("path", ""), ctx))
            filename = render_value(spec.get("filename", p.name), ctx)
            content_type = render_value(spec.get("content_type", mimetypes.guess_type(str(p))[0] or "application/octet-stream"), ctx)
            out[field] = (filename, p.read_bytes(), content_type)
        else:
            raise ValueError(f"files.{field} 配置无效")
    return out


def call_template(
    session: requests.Session,
    name: str,
    tmpl: dict[str, Any],
    ctx: dict[str, Any],
    execute: bool,
    timeout: int,
) -> dict[str, Any]:
    rendered = render_value(tmpl, ctx)
    method = str(rendered.get("method", "POST")).upper()
    url = str(rendered.get("url", "")).strip()
    headers = dict(rendered.get("headers") or {})
    params = rendered.get("params")
    json_body = rendered.get("json")
    data_body = rendered.get("data")
    files_spec = rendered.get("files")

    record: dict[str, Any] = {
        "step": name,
        "method": method,
        "url": url,
        "request": {
            "headers": headers,
            "params": params,
            "json": json_body,
            "data": data_body,
            "has_files": bool(files_spec),
        },
    }

    if not execute:
        record["status"] = "dry-run"
        return record

    files = build_files(files_spec, ctx) if isinstance(files_spec, dict) else None
    resp = session.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json_body,
        data=data_body,
        files=files,
        timeout=timeout,
    )
    record["response_status"] = resp.status_code
    payload, parse_warning = decode_response_payload(resp)
    record["response"] = payload
    if parse_warning:
        record["response_parse_warning"] = parse_warning

    if resp.status_code >= 400:
        raise TemplateCallError(name, resp.status_code, payload)

    extract = rendered.get("extract") or {}
    if parse_warning and isinstance(extract, dict) and extract:
        raise PublishFailure(
            name,
            f"{name} 成功返回但响应不是可提取的 JSON: url={url}, error={parse_warning}, body={payload_to_text(payload)[:500]}",
            payload=payload,
        )
    if isinstance(extract, dict):
        for k, p in extract.items():
            ctx[k] = get_by_path(payload, str(p))
        if extract:
            record["extracted"] = {k: ctx.get(k) for k in extract}

    return record


def load_adapter(adapter_path: str) -> dict[str, Any]:
    return read_json(Path(adapter_path).expanduser().resolve())


def default_batch_report_path(targets_file: str) -> Path:
    return Path(targets_file).expanduser().resolve().with_name(DEFAULT_BATCH_REPORT_FILENAME)


def batch_target_key(target: dict[str, Any]) -> str:
    manifest = str(target.get("manifest", "")).strip()
    vertical = str(target.get("target_vertical_block_location", "")).strip()
    return f"{vertical}|{manifest}"


def load_previous_batch_results(report_path: Path) -> dict[str, dict[str, Any]]:
    if not report_path.exists():
        return {}
    data = read_json_with_diagnostics(report_path, step_name="load_previous_batch_results")
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        target_key = str(result.get("target_key", "")).strip()
        if target_key:
            out[target_key] = result
    return out


def load_course_structure_snapshot(course_structure_path: Path) -> dict[str, Any]:
    return normalize_course_structure_payload(read_json(course_structure_path.expanduser().resolve()))


def group_batch_results(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    replaced = [result for result in results if result.get("status") == "replaced"]
    fallback_inserted = [result for result in results if result.get("status") == "fallback_inserted"]
    failed = [result for result in results if result.get("status") == "failed"]
    manual_followup_failed = [result for result in failed if bool(result.get("requires_manual_followup", True))]
    unexpected_failed = [result for result in failed if not bool(result.get("requires_manual_followup", True))]
    return {
        "replaced": replaced,
        "fallback_inserted": fallback_inserted,
        "failed": failed,
        "manual_followup_failed": manual_followup_failed,
        "unexpected_failed": unexpected_failed,
    }


def build_batch_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = group_batch_results(results)
    return {
        "total": len(results),
        "replaced_count": len(grouped["replaced"]),
        "fallback_inserted_count": len(grouped["fallback_inserted"]),
        "failed_count": len(grouped["failed"]),
        "manual_followup_failed_count": len(grouped["manual_followup_failed"]),
        "unexpected_failed_count": len(grouped["unexpected_failed"]),
    }


def write_batch_report(report_path: Path, payload: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_orphaned_created_block(
    *,
    created_block_locator: str,
    studio_url: str,
    adapter: dict[str, Any],
    args: argparse.Namespace,
    timeout: int,
) -> dict[str, Any]:
    step_name = "cleanup_orphaned_created_block"
    block_locator = str(created_block_locator or "").strip()
    if not block_locator:
        raise PublishFailure(step_name, "missing created block locator for orphan cleanup")
    if not bool(getattr(args, "execute", False)):
        raise PublishFailure(step_name, "cannot clean orphaned created block during dry-run")

    delete_template = adapter.get("delete_block")
    if not isinstance(delete_template, dict):
        raise PublishFailure(step_name, "adapter missing delete_block step required for orphan cleanup")

    studio_base = studio_base_from_url(studio_url)
    session, csrftoken, auth_ready = build_studio_session_from_args(
        studio_base=studio_base,
        studio_url=studio_url,
        args=args,
        allow_login=True,
    )
    if not auth_ready:
        raise PublishFailure(step_name, "cannot clean orphaned created block without Studio authentication")

    ctx = {
        "studio_base": studio_base,
        "block_url": studio_url,
        "vertical_block_id": parse_vertical_block_id(studio_url) or "",
        "csrftoken": csrftoken,
        "delete_block_locator": block_locator,
    }
    rendered = render_value(delete_template, ctx)
    method = str(rendered.get("method", "DELETE")).upper()
    url = str(rendered.get("url", "")).strip()
    headers = dict(rendered.get("headers") or {})
    params = rendered.get("params")
    json_body = rendered.get("json")
    data_body = rendered.get("data")

    record = {
        "step": step_name,
        "cleanup_target_block_locator": block_locator,
        "method": method,
        "url": url,
        "request": {
            "headers": headers,
            "params": params,
            "json": json_body,
            "data": data_body,
        },
    }

    resp = session.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json_body,
        data=data_body,
        timeout=timeout,
    )
    payload, parse_warning = decode_response_payload(resp)
    record["response_status"] = resp.status_code
    record["response"] = payload
    if parse_warning:
        record["response_parse_warning"] = parse_warning

    if resp.status_code in {200, 202, 204}:
        record["status"] = "deleted"
        return record
    if resp.status_code == 404:
        record["status"] = "already_missing"
        return record

    raise PublishFailure(
        step_name,
        f"{step_name} 失败: HTTP {resp.status_code}, url={url}, body={payload_to_text(payload)[:500]}",
        payload=payload,
    )


def build_publish_args_for_target(
    base_args: argparse.Namespace,
    *,
    manifest: str,
    studio_url: str,
    vertical_block_id: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        studio_url=studio_url,
        manifest=manifest,
        adapter=base_args.adapter,
        execute=bool(getattr(base_args, "execute", False)),
        dry_run=bool(getattr(base_args, "dry_run", False)),
        timeout=int(getattr(base_args, "timeout", 30)),
        vertical_block_id=vertical_block_id,
        block_url=studio_url,
        courses_base=getattr(base_args, "courses_base", ""),
        account=getattr(base_args, "account", ""),
        password=getattr(base_args, "password", ""),
        cookie=getattr(base_args, "cookie", ""),
        csrf_token=getattr(base_args, "csrf_token", ""),
        skip_oss_multipart=bool(getattr(base_args, "skip_oss_multipart", False)),
        auth_retry_max_retries=int(getattr(base_args, "auth_retry_max_retries", DEFAULT_AUTH_RETRY_MAX_RETRIES)),
        auth_retry_delay_seconds=float(getattr(base_args, "auth_retry_delay_seconds", DEFAULT_AUTH_RETRY_DELAY_SECONDS)),
    )


def operator_vertical_urls(studio_base: str, replacement_plan: dict[str, Any], fallback_url: str) -> tuple[str, str]:
    requested_vertical_url = build_container_block_url(studio_base, str(replacement_plan.get("target_vertical_block_id", ""))) or fallback_url
    working_vertical_url = build_container_block_url(studio_base, str(replacement_plan.get("working_vertical_block_id", ""))) or requested_vertical_url
    return working_vertical_url, requested_vertical_url


def run_single_publish(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest).expanduser().resolve()
    adapter_path = Path(args.adapter).expanduser().resolve()
    manifest = ensure_manifest(manifest_path)
    adapter = read_json(adapter_path)

    studio_url = args.studio_url.strip()
    u = urlparse(studio_url)
    studio_base = f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""
    if not studio_base:
        raise ValueError("studio_url 无效")

    vertical_block_id = args.vertical_block_id or parse_vertical_block_id(studio_url)
    if not vertical_block_id:
        raise ValueError("无法从 studio_url 解析 vertical_block_id，请显式传 --vertical-block-id")

    session = requests.Session()
    csrftoken = args.csrf_token or ""

    account = (args.account or os.environ.get("FIRA_SAAS_OP_ACCOUNT", "")).strip()
    password = (args.password or os.environ.get("FIRA_SAAS_OP_PASSWORD", "")).strip()
    courses_base = args.courses_base
    if not courses_base:
        courses_base = derive_courses_base_from_studio_base(studio_base)

    if not args.cookie and (not account or not password):
        raise ValueError(
            "缺少登录凭证。请提供 --account/--password，或配置环境变量 "
            "FIRA_SAAS_OP_ACCOUNT / FIRA_SAAS_OP_PASSWORD。"
        )

    if account and password and courses_base:
        session, login_csrf = login_and_get_studio_session(
            account=account,
            password=password,
            courses_base=courses_base,
            studio_csrf_url=args.block_url or studio_url,
        )
        if not csrftoken:
            csrftoken = login_csrf

    if args.cookie:
        cookies = parse_cookie_header(args.cookie)
        for k, v in cookies.items():
            session.cookies.set(k, v, domain=urlparse(studio_base).hostname or "")
        if not csrftoken:
            csrftoken = cookies.get("csrftoken", "")

    items_obj = [
        {
            "page": int(i["page"]),
            "image": i["image"],
            "subtitle": i["subtitle"],
            "start": int(i["start"]),
            "end": int(i["end"]),
        }
        for i in manifest["items"]
    ]
    time_config_obj = [
        {
            "page": int(i["page"]),
            "start": int(i["start"]),
            "end": int(i["end"]),
            "subtitle": i["subtitle"],
        }
        for i in manifest["items"]
    ]

    canonical_block_url = normalize_block_url(args.block_url or studio_url)

    ctx: dict[str, Any] = {
        "studio_url": studio_url,
        "studio_base": studio_base,
        "block_url": canonical_block_url,
        "vertical_block_id": vertical_block_id,
        "manifest": str(manifest_path),
        "manifest_json": json.dumps(manifest, ensure_ascii=False),
        "imagegallery_category": adapter.get("imagegallery_category", "imagesgallery"),
        "csrftoken": csrftoken,
        "total_pages": len(manifest["items"]),
        "audio_file": manifest["audio"]["_full_audio_abs"],
        "audio_file_name": Path(manifest["audio"]["_full_audio_abs"]).name,
        "audio_post_fix": Path(manifest["audio"]["_full_audio_abs"]).suffix.lstrip("."),
        "audio_document_id": "",
        "course_key": course_key_from_vertical_block_id(vertical_block_id),
        "courses_json": "",
        "items_json": json.dumps(items_obj, ensure_ascii=False),
        "items_obj": items_obj,
        "time_config_json": json.dumps(time_config_obj, ensure_ascii=False),
        "time_config_obj": time_config_obj,
        "show_subtitles": True,
        "uploaded_asset_ids": [],
    }
    if ctx["course_key"]:
        ctx["courses_json"] = json.dumps({ctx["course_key"]: False}, ensure_ascii=False)

    logs: list[dict[str, Any]] = []
    execute = bool(args.execute)
    can_refresh_auth = bool(execute and account and password and courses_base)

    def refresh_auth_session() -> None:
        nonlocal session, csrftoken
        session, login_csrf = login_and_get_studio_session(
            account=account,
            password=password,
            courses_base=courses_base,
            studio_csrf_url=args.block_url or studio_url,
        )
        csrftoken = login_csrf
        ctx["csrftoken"] = csrftoken

    def call_step(name: str, tmpl: dict[str, Any]) -> dict[str, Any]:
        record, retry_events = call_with_auth_retry(
            lambda: call_template(session, name, tmpl, ctx, execute, args.timeout),
            refresh_auth_session if can_refresh_auth else None,
            max_retries=normalize_auth_retry_max_retries(getattr(args, "auth_retry_max_retries", DEFAULT_AUTH_RETRY_MAX_RETRIES)),
            delay_seconds=max(0.0, float(args.auth_retry_delay_seconds)),
        )
        if retry_events:
            record["auth_retry_attempts"] = len(retry_events)
            record["auth_retry_events"] = retry_events
        return record

    for key in ("create_block", "upload_image", "save_block"):
        if key not in adapter:
            raise ValueError(f"adapter 缺失步骤: {key}")

    logs.append(call_step("create_block", adapter["create_block"]))

    for idx, item in enumerate(manifest["items"], start=1):
        ctx.update(
            {
                "index": idx,
                "page": int(item["page"]),
                "page_number": int(item["page"]),
                "image_file": item["_image_abs"],
                "image_rel": item["image"],
                "subtitle": item["subtitle"],
                "start": int(item["start"]),
                "end": int(item["end"]),
            }
        )
        image_step = call_step(f"upload_image[{idx}]", adapter["upload_image"])
        logs.append(image_step)
        resp = image_step.get("response")
        asset_id = ""
        if isinstance(resp, list) and resp and isinstance(resp[0], dict):
            asset_id = str(resp[0].get("id", "")).strip()
        elif isinstance(resp, dict):
            asset_id = str(resp.get("id", "")).strip()
        if asset_id:
            ctx["uploaded_asset_ids"] = [*ctx.get("uploaded_asset_ids", []), asset_id]

    if "change_files_order" in adapter:
        logs.append(
            call_step(
                "change_files_order",
                adapter["change_files_order"],
            )
        )

    if "upload_audio_prepare" in adapter:
        prepare_step = call_step(
            "upload_audio_prepare",
            adapter["upload_audio_prepare"],
        )
        logs.append(prepare_step)
        if isinstance(prepare_step.get("response"), dict):
            resp_obj = prepare_step["response"]
            for key in ("Credentials", "region", "bucket", "file_key"):
                if key in resp_obj:
                    ctx[key] = resp_obj[key]
        if execute and not args.skip_oss_multipart:
            multipart_result = sts_multipart_upload_audio(ctx)
            logs.append(
                {
                    "step": "upload_audio_multipart",
                    "status": "ok",
                    "result": multipart_result,
                }
            )
            ctx["audio_object_key"] = multipart_result["object_key"]
        elif not execute:
            logs.append(
                {
                    "step": "upload_audio_multipart",
                    "status": "dry-run",
                    "note": "execute 模式下会执行 OSS multipart 上传；缺少 oss2 时会自动安装",
                }
            )
        else:
            logs.append(
                {
                    "step": "upload_audio_multipart",
                    "status": "skipped",
                    "note": "--skip-oss-multipart 已启用，跳过 OSS multipart 上传",
                }
            )
        if not ctx.get("edx_document_id") and ctx.get("file_key"):
            maybe_id = maybe_extract_uuid(str(ctx["file_key"]))
            if maybe_id:
                ctx["edx_document_id"] = maybe_id
        if ctx.get("edx_document_id"):
            ctx["audio_document_id"] = f"{ctx['edx_document_id']}.{ctx['audio_post_fix']}"

    if "upload_audio" in adapter:
        logs.append(call_step("upload_audio", adapter["upload_audio"]))

    if "upload_audio_register" in adapter:
        logs.append(
            call_step(
                "upload_audio_register",
                adapter["upload_audio_register"],
            )
        )

    if "save_audio_binding" in adapter:
        logs.append(
            call_step(
                "save_audio_binding",
                adapter["save_audio_binding"],
            )
        )

    if "submit_studio_edits" in adapter:
        logs.append(
            call_step(
                "submit_studio_edits",
                adapter["submit_studio_edits"],
            )
        )
    logs.append(call_step("save_block", adapter["save_block"]))

    target_vertical_url = build_container_block_url(studio_base, vertical_block_id)
    created_imagegallery_block_url = build_container_block_url(studio_base, str(ctx.get("block_locator", "")))

    return {
        "mode": "execute" if execute else "dry-run",
        "studio_base": studio_base,
        "courses_base": courses_base,
        "vertical_block_id": vertical_block_id,
        "target_vertical_url": target_vertical_url,
        "block_locator": ctx.get("block_locator", ""),
        "created_imagegallery_block_url": created_imagegallery_block_url,
        "created_block_url": created_imagegallery_block_url,
        "auth_retry_policy": {
            "enabled": can_refresh_auth,
            "max_retries": normalize_auth_retry_max_retries(getattr(args, "auth_retry_max_retries", DEFAULT_AUTH_RETRY_MAX_RETRIES)),
            "delay_seconds": max(0.0, float(args.auth_retry_delay_seconds)),
        },
        "steps": logs,
    }


def apply_in_place_publish_actions(
    single_publish_result: dict[str, Any],
    *,
    adapter: dict[str, Any],
    replacement_plan: dict[str, Any],
    studio_url: str,
    execute: bool,
    timeout: int,
    args: argparse.Namespace,
    publish_only: bool = False,
) -> dict[str, Any]:
    studio_base = str(single_publish_result.get("studio_base", "")).strip()
    if not studio_base:
        raise PublishFailure("studio_base", "missing studio_base for in-place publish actions")

    steps = list(single_publish_result.get("steps", []))
    replacement_plan = dict(replacement_plan)
    created_block_locator = str(single_publish_result.get("block_locator", "") or single_publish_result.get("created_block_locator", "")).strip()

    if not publish_only and not created_block_locator:
        if execute:
            raise PublishFailure("create_block", "missing created imagesgallery block locator")
        created_block_locator = "__dry_run_created_imagesgallery__"

    requested_vertical_url = build_container_block_url(studio_base, str(replacement_plan.get("target_vertical_block_id", ""))) or studio_url
    live_plan, live_step = maybe_fetch_live_replacement_plan(
        studio_url=requested_vertical_url,
        source_vertical_block_id=str(replacement_plan.get("source_vertical_block_id", "")),
        target_vertical_block_id=str(replacement_plan.get("target_vertical_block_id", "")),
        excluded_block_locators=[created_block_locator] if created_block_locator else [],
        args=args,
        timeout=timeout,
    )
    if live_step:
        steps.append(live_step)
    if execute and not publish_only and live_plan is None:
        raise PublishFailure(
            "load_live_target_structure",
            str((live_step or {}).get("note") or "execute mode requires a fresh live target structure before in-place replacement"),
        )
    if live_plan is not None:
        replacement_plan = live_plan

    working_vertical_block_id = str(replacement_plan.get("working_vertical_block_id", "")).strip()
    matched_source_block_locator = str(replacement_plan.get("matched_source_block_locator", "")).strip()
    working_vertical_url = build_container_block_url(studio_base, working_vertical_block_id) or studio_url

    session, csrftoken, _auth_ready = build_studio_session_from_args(
        studio_base=studio_base,
        studio_url=working_vertical_url,
        args=args,
        allow_login=True,
    )

    ctx = {
        "studio_base": studio_base,
        "block_url": working_vertical_url,
        "vertical_block_id": working_vertical_block_id,
        "csrftoken": csrftoken,
        "delete_block_locator": matched_source_block_locator,
        "vertical_children_obj": [],
        "created_block_locator": created_block_locator,
    }

    current_children = list(replacement_plan.get("current_children", []))
    validate_replacement_plan(
        replacement_plan,
        created_block_locator=created_block_locator,
        publish_only=publish_only,
    )
    if publish_only:
        desired_children = current_children
    else:
        desired_children = build_vertical_children(
            [*current_children, created_block_locator],
            new_block_locator=created_block_locator,
            replace_block_locator=matched_source_block_locator,
        )
        stale_children = [
            child
            for child in desired_children
            if child != created_block_locator and child not in current_children
        ]
        if stale_children:
            raise PublishFailure(
                "replacement_validation",
                f"desired child order contains stale child locators that are not present in the live vertical children: {stale_children}",
            )
    ctx["vertical_children_obj"] = desired_children
    operator_vertical_url, requested_vertical_url = operator_vertical_urls(studio_base, replacement_plan, studio_url)

    def call_optional_step(step_name: str) -> None:
        tmpl = adapter.get(step_name)
        if not tmpl:
            raise PublishFailure(step_name, f"adapter missing required step: {step_name}")
        try:
            steps.append(call_template(session, step_name, tmpl, ctx, execute, timeout))
        except TemplateCallError as exc:
            raise PublishFailure(step_name, str(exc), payload=exc.payload) from exc

    if not publish_only and desired_children != [*current_children, created_block_locator]:
        call_optional_step("reorder_children")

    if not publish_only and replacement_plan.get("result_type") == "replaced" and matched_source_block_locator:
        call_optional_step("delete_block")

    call_optional_step("publish_vertical")

    result = {
        "status": str(replacement_plan.get("result_type", "fallback_inserted")),
        "target_vertical_block_id": str(replacement_plan.get("target_vertical_block_id", "")),
        "working_vertical_block_id": working_vertical_block_id,
        "target_vertical_url": operator_vertical_url,
        "requested_target_vertical_url": requested_vertical_url,
        "working_vertical_url": working_vertical_url,
        "created_block_locator": created_block_locator,
        "created_imagegallery_block_url": build_container_block_url(studio_base, created_block_locator),
        "matched_source_block_locator": matched_source_block_locator,
        "matched_source_block_url": build_container_block_url(studio_base, matched_source_block_locator),
        "note": "replaced existing source block in place" if replacement_plan.get("result_type") == "replaced" else "inserted new imagesgallery at the top of the target vertical",
        "course_kind": replacement_plan.get("course_kind", ""),
        "match_scope": replacement_plan.get("match_scope", ""),
        "plan_source": replacement_plan.get("plan_source", ""),
        "requires_manual_followup": False,
        "steps": steps,
    }
    if publish_only:
        result["note"] = "reused the previously created imagesgallery block and retried the final vertical publish"
    return result


def process_publish_target(
    target: dict[str, Any],
    course_structure: dict[str, Any],
    args: argparse.Namespace,
    adapter: dict[str, Any],
    *,
    previous_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_vertical_block_id = str(target.get("target_vertical_block_location", "")).strip()
    source_vertical_block_id = str(target.get("source_vertical_block_location", "") or target_vertical_block_id).strip()
    studio_vertical_url = str(target.get("studio_vertical_url", "")).strip()
    if not studio_vertical_url:
        studio_vertical_url = build_container_block_url(
            studio_base_from_url(str(target.get("studio_vertical_url", "") or "")),
            target_vertical_block_id,
        )
    if not target_vertical_block_id:
        raise PublishFailure("target_vertical", f"target missing target_vertical_block_location: {target}")
    if not studio_vertical_url:
        raise PublishFailure("target_vertical", f"target missing studio_vertical_url: {target}")

    replacement_plan = mark_replacement_plan_source(
        plan_publish_replacement(
            course_structure,
            source_vertical_block_id=source_vertical_block_id,
            target_vertical_block_id=target_vertical_block_id,
        ),
        "course_structure_json",
    )
    live_plan, live_step = maybe_fetch_live_replacement_plan(
        studio_url=studio_vertical_url,
        source_vertical_block_id=source_vertical_block_id,
        target_vertical_block_id=target_vertical_block_id,
        args=args,
        timeout=int(getattr(args, "timeout", 30)),
    )
    if bool(getattr(args, "execute", False)) and live_plan is None:
        operator_vertical_url, requested_vertical_url = operator_vertical_urls(
            studio_base_from_url(studio_vertical_url),
            replacement_plan,
            studio_vertical_url,
        )
        return {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "target_vertical_url": operator_vertical_url,
            "requested_target_vertical_url": requested_vertical_url,
            "working_vertical_block_id": str(replacement_plan.get("working_vertical_block_id", "")),
            "matched_source_block_locator": str(replacement_plan.get("matched_source_block_locator", "")),
            "failed_step": "load_live_target_structure",
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": str((live_step or {}).get("note") or "execute mode requires a fresh live target structure before in-place replacement"),
            "steps": [live_step] if live_step else [],
        }
    if live_plan is not None:
        replacement_plan = live_plan
    rerun_action = resolve_rerun_action(
        previous_result,
        current_structure_block_ids=list(replacement_plan.get("all_structure_block_ids", [])),
    )
    preflight_steps: list[dict[str, Any]] = []

    if rerun_action["action"] == "skip":
        skipped = dict(previous_result or {})
        operator_vertical_url, requested_vertical_url = operator_vertical_urls(
            studio_base_from_url(studio_vertical_url),
            replacement_plan,
            studio_vertical_url,
        )
        skipped.update(
            {
                "status": str(previous_result.get("status", "replaced")) if previous_result else "replaced",
                "target_vertical_block_id": target_vertical_block_id,
                "target_vertical_url": operator_vertical_url,
                "requested_target_vertical_url": requested_vertical_url,
                "requires_manual_followup": False,
                "note": rerun_action["reason"],
            }
        )
        return skipped

    if rerun_action["action"] == "manual_followup":
        operator_vertical_url, requested_vertical_url = operator_vertical_urls(
            studio_base_from_url(studio_vertical_url),
            replacement_plan,
            studio_vertical_url,
        )
        return {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "target_vertical_url": operator_vertical_url,
            "requested_target_vertical_url": requested_vertical_url,
            "working_vertical_block_id": str(replacement_plan.get("working_vertical_block_id", "")),
            "matched_source_block_locator": str(replacement_plan.get("matched_source_block_locator", "")),
            "created_block_locator": str((previous_result or {}).get("created_block_locator", "")),
            "failed_step": str((previous_result or {}).get("failed_step", "rerun_guard")),
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": rerun_action["reason"],
        }

    if rerun_action["action"] == "cleanup_orphaned_created_block":
        try:
            cleanup_step = cleanup_orphaned_created_block(
                created_block_locator=str((previous_result or {}).get("created_block_locator", "")),
                studio_url=studio_vertical_url,
                adapter=adapter,
                args=args,
                timeout=int(getattr(args, "timeout", 30)),
            )
            preflight_steps.append(cleanup_step)
        except PublishFailure as exc:
            operator_vertical_url, requested_vertical_url = operator_vertical_urls(
                studio_base_from_url(studio_vertical_url),
                replacement_plan,
                studio_vertical_url,
            )
            return {
                "status": "failed",
                "target_vertical_block_id": target_vertical_block_id,
                "target_vertical_url": operator_vertical_url,
                "requested_target_vertical_url": requested_vertical_url,
                "working_vertical_block_id": str(replacement_plan.get("working_vertical_block_id", "")),
                "matched_source_block_locator": str(replacement_plan.get("matched_source_block_locator", "")),
                "created_block_locator": str((previous_result or {}).get("created_block_locator", "")),
                "failed_step": exc.step_name,
                "failure_bucket": "manual_followup",
                "requires_manual_followup": True,
                "note": str(exc),
                "steps": preflight_steps,
            }

    if rerun_action["action"] == "resume_publish_only":
        resume_result = {
            "studio_base": studio_base_from_url(studio_vertical_url),
            "block_locator": str((previous_result or {}).get("created_block_locator", "")),
            "steps": preflight_steps,
        }
        resumed = apply_in_place_publish_actions(
            resume_result,
            adapter=adapter,
            replacement_plan={
                **replacement_plan,
                "result_type": str((previous_result or {}).get("status") or replacement_plan.get("result_type", "replaced")),
            },
            studio_url=studio_vertical_url,
            execute=bool(getattr(args, "execute", False)),
            timeout=int(getattr(args, "timeout", 30)),
            args=args,
            publish_only=True,
        )
        resumed["resumed_from_previous_run"] = True
        return resumed

    working_vertical_block_id = str(replacement_plan.get("working_vertical_block_id", "")).strip()
    studio_base = studio_base_from_url(studio_vertical_url)
    working_vertical_url = build_container_block_url(studio_base, working_vertical_block_id) or studio_vertical_url

    publish_args = build_publish_args_for_target(
        args,
        manifest=str(target.get("manifest", "")),
        studio_url=working_vertical_url,
        vertical_block_id=working_vertical_block_id,
    )
    single_publish_result = run_single_publish(publish_args)
    if preflight_steps:
        single_publish_result["steps"] = [*preflight_steps, *list(single_publish_result.get("steps", []))]
    try:
        return apply_in_place_publish_actions(
            single_publish_result,
            adapter=adapter,
            replacement_plan=replacement_plan,
            studio_url=studio_vertical_url,
            execute=bool(getattr(args, "execute", False)),
            timeout=int(getattr(args, "timeout", 30)),
            args=args,
        )
    except PublishFailure as exc:
        operator_vertical_url, requested_vertical_url = operator_vertical_urls(studio_base, replacement_plan, studio_vertical_url)
        failed = {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "working_vertical_block_id": working_vertical_block_id,
            "target_vertical_url": operator_vertical_url,
            "requested_target_vertical_url": requested_vertical_url,
            "created_block_locator": str(single_publish_result.get("block_locator", "")),
            "created_imagegallery_block_url": str(single_publish_result.get("created_imagegallery_block_url", "")),
            "matched_source_block_locator": str(replacement_plan.get("matched_source_block_locator", "")),
            "failed_step": exc.step_name,
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": str(exc),
            "course_kind": replacement_plan.get("course_kind", ""),
            "match_scope": replacement_plan.get("match_scope", ""),
            "steps": list(single_publish_result.get("steps", [])),
        }
        return failed


def run_in_place_publish(args: argparse.Namespace) -> dict[str, Any]:
    course_structure = load_course_structure_snapshot(Path(args.course_structure_json).expanduser().resolve())
    adapter = load_adapter(args.adapter)
    target_vertical_block_id = str(args.vertical_block_id or parse_vertical_block_id(args.studio_url))
    target = {
        "name": Path(args.manifest).stem,
        "manifest": args.manifest,
        "source_vertical_block_location": str(args.source_vertical_block_id or target_vertical_block_id),
        "target_vertical_block_location": target_vertical_block_id,
        "studio_vertical_url": normalize_block_url(args.studio_url),
    }
    return process_publish_target(target, course_structure, args, adapter)


def run_batch_publish(
    args: argparse.Namespace,
    *,
    publish_target_fn: Any = None,
) -> dict[str, Any]:
    targets_path = Path(args.targets_file).expanduser().resolve()
    payload = read_json_with_diagnostics(targets_path, step_name="load_batch_targets")
    adapter = load_adapter(getattr(args, "adapter", (Path(__file__).resolve().parent.parent / "references" / "adapter.template.json").as_posix()))
    course_structure_path = Path(str(payload.get("course_structure_json", "") or getattr(args, "course_structure_json", "")).strip()).expanduser().resolve()
    if not course_structure_path.exists():
        raise PublishFailure("course_structure_json", f"course structure json not found: {course_structure_path}")

    report_path = (
        Path(args.report_file).expanduser().resolve()
        if getattr(args, "report_file", "")
        else default_batch_report_path(str(targets_path))
    )
    previous_results_by_key = load_previous_batch_results(report_path)

    run_target = publish_target_fn or process_publish_target
    results: list[dict[str, Any]] = []
    targets = list(payload.get("targets") or [])
    for raw_target in targets:
        target = dict(raw_target or {})
        target_key = batch_target_key(target)
        previous_result = previous_results_by_key.get(target_key)
        course_structure = load_course_structure_snapshot(course_structure_path)
        try:
            result = run_target(target, course_structure, args, adapter, previous_result=previous_result)
        except PublishFailure as exc:
            result = {
                "status": "failed",
                "failed_step": exc.step_name,
                "failure_bucket": "manual_followup",
                "requires_manual_followup": True,
                "note": str(exc),
                "target_vertical_url": str(target.get("studio_vertical_url", "")),
            }
        except Exception as exc:
            result = {
                "status": "failed",
                "failed_step": getattr(exc, "step_name", "unexpected_error"),
                "failure_bucket": "manual_followup",
                "requires_manual_followup": True,
                "note": str(exc),
                "target_vertical_url": str(target.get("studio_vertical_url", "")),
            }

        result.setdefault("status", "failed")
        result.setdefault("target_vertical_url", str(target.get("studio_vertical_url", "")))
        result.setdefault("requires_manual_followup", result.get("status") == "failed")
        if result.get("status") == "failed":
            result.setdefault("failure_bucket", "manual_followup" if result.get("requires_manual_followup", True) else "unexpected")
        result["name"] = str(target.get("name", ""))
        result["manifest"] = str(target.get("manifest", ""))
        result["target_key"] = target_key
        results.append(result)

        partial_payload = {
            "targets_file": str(targets_path),
            "report_file": str(report_path),
            "results": results,
            "summary": build_batch_summary(results),
        }
        write_batch_report(report_path, partial_payload)

    grouped = group_batch_results(results)
    final_payload = {
        "targets_file": str(targets_path),
        "report_file": str(report_path),
        "results": results,
        "replaced": grouped["replaced"],
        "fallback_inserted": grouped["fallback_inserted"],
        "manual_followup_failed": grouped["manual_followup_failed"],
        "failed": grouped["failed"],
        "summary": build_batch_summary(results),
    }
    write_batch_report(report_path, final_payload)
    return final_payload


def run(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "targets_file", ""):
        return run_batch_publish(args)
    if getattr(args, "course_structure_json", ""):
        return run_in_place_publish(args)
    return run_single_publish(args)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish local imagesgallery package to Studio")
    p.add_argument("--studio-url", default="", help="Studio container URL")
    p.add_argument("--manifest", help="path to imagesgallery.json")
    default_adapter = (Path(__file__).resolve().parent.parent / "references" / "adapter.template.json").as_posix()
    p.add_argument(
        "--adapter",
        default=default_adapter,
        help=f"path to adapter json (default: {default_adapter})",
    )
    p.add_argument("--execute", action="store_true", help="execute real requests")
    p.add_argument("--dry-run", action="store_true", help="explicit dry-run flag")
    p.add_argument("--timeout", type=int, default=30)

    p.add_argument("--vertical-block-id", default="")
    p.add_argument("--block-url", default="")
    p.add_argument("--source-vertical-block-id", default="")
    p.add_argument("--course-structure-json", default="")
    p.add_argument("--targets-file", default="")
    p.add_argument("--report-file", default="")

    p.add_argument("--courses-base", default="")
    p.add_argument("--account", default="")
    p.add_argument("--password", default="")
    p.add_argument("--cookie", default="")
    p.add_argument("--csrf-token", default="")
    p.add_argument("--skip-oss-multipart", action="store_true", help="skip OSS multipart upload after get_upload_info")
    p.add_argument(
        "--auth-retry-max-retries",
        type=int,
        default=DEFAULT_AUTH_RETRY_MAX_RETRIES,
        help=f"max retries for auth/login/session related publish errors (default: {DEFAULT_AUTH_RETRY_MAX_RETRIES})",
    )
    p.add_argument(
        "--auth-retry-delay-seconds",
        type=float,
        default=DEFAULT_AUTH_RETRY_DELAY_SECONDS,
        help=f"delay between auth-error retries in seconds (default: {DEFAULT_AUTH_RETRY_DELAY_SECONDS})",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        args.execute = False
    if args.targets_file:
        out = run_batch_publish(args)
    elif args.course_structure_json:
        if not args.manifest:
            raise SystemExit("--manifest is required when --course-structure-json is set")
        if not args.studio_url:
            raise SystemExit("--studio-url is required when --course-structure-json is set")
        out = run_in_place_publish(args)
    else:
        if not args.manifest:
            raise SystemExit("--manifest is required unless --targets-file is used")
        if not args.studio_url:
            raise SystemExit("--studio-url is required unless --targets-file is used")
        out = run_single_publish(args)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
