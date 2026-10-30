#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from json import JSONDecodeError
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, urlsplit, urlunsplit

import requests


DEFAULT_AUTH_RETRY_MAX_RETRIES = 4
DEFAULT_AUTH_RETRY_DELAY_SECONDS = 3.0
ATOMIC_WRITE_REPLACE_RETRIES = 2
ATOMIC_WRITE_REPLACE_RETRY_DELAY_SECONDS = 0.05
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
HTML_SUMMARY_HEADING = "本节要点"


class VisibleTextParser(HTMLParser):
    BLOCK_TAGS = {"address", "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section"}
    HIDDEN_TAGS = {"audio", "iframe", "object", "script", "style", "svg", "video"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.HIDDEN_TAGS:
            self.hidden_depth += 1
        if tag in self.BLOCK_TAGS and self.hidden_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
        if tag in self.BLOCK_TAGS and self.hidden_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(html.unescape(f"&#{name};"))


def visible_text(value: Any) -> str:
    raw = str(value or "")
    raw = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw)
    raw = re.sub(r"!\[[^\]]*\]\[[^]]*\]", " ", raw)
    parser = VisibleTextParser()
    try:
        parser.feed(raw)
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]*>", " ", raw)
    return html.unescape(text)


def first_visible_line(value: Any) -> str:
    for line in visible_text(value).splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^#{1,6}\s*", "", line).strip()
        if line:
            return re.sub(r"\s+", " ", line)
    return ""


def is_summary_html_block(block: dict[str, Any]) -> bool:
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    for source in (block, metadata):
        for key in ("name", "display_name", "title"):
            value = source.get(key)
            if isinstance(value, str) and first_visible_line(value) == HTML_SUMMARY_HEADING:
                return True
        for key in ("html", "text", "body", "content", "definition"):
            value = source.get(key)
            if not isinstance(value, str):
                continue
            visible_prefix = re.sub(r"\s+", " ", visible_text(value)).strip()[:20]
            if HTML_SUMMARY_HEADING in visible_prefix:
                return True
    return False


def manifest_summary_leaks(manifest: dict[str, Any]) -> list[int]:
    leaked_pages: list[int] = []
    items = manifest.get("items") if isinstance(manifest.get("items"), list) else []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        page = item.get("page", index)
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            page_number = index
        if any(
            isinstance(item.get(field), str)
            and first_visible_line(item[field]) == HTML_SUMMARY_HEADING
            for field in ("subtitle", "speech", "text", "content")
        ):
            leaked_pages.append(page_number)
    return leaked_pages


def summary_html_blocks(vertical: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for block in vertical.get("blocks", [])
        if isinstance(block, dict)
        and block_category(block) == "html"
        and block_locator(block)
        and is_summary_html_block(block)
    ]


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class TemplateCallError(RuntimeError):
    def __init__(self, step_name: str, status_code: int, payload: Any):
        self.step_name = step_name
        self.status_code = int(status_code)
        self.payload = payload
        super().__init__(f"{step_name} failed (HTTP {status_code})")

    def is_auth_related(self) -> bool:
        return is_auth_related_failure(self.status_code, self.payload)


class PublishFailure(RuntimeError):
    def __init__(self, step_name: str, message: str, *, payload: Any = None):
        self.step_name = step_name
        self.payload = payload
        self.steps: list[dict[str, Any]] = []
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def skill_provenance(*, input_paths: list[str] | None = None, output_path: str = "") -> dict[str, Any]:
    script_path = Path(__file__).resolve()
    skill_path = script_path.parent.parent
    provenance: dict[str, Any] = {
        "active_skill_path": str(skill_path),
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "installed_at": "unknown",
        "repository": "unknown",
        "ref": "unknown",
        "commit": "unknown",
        "base_commit": "unknown",
        "working_tree_state": "unknown",
        "content_sha256": "unknown",
        "inputs": [],
        "output": {"path": output_path or "unknown", "identity": Path(output_path).name if output_path else "unknown"},
    }
    for git_args, field in ((["rev-parse", "--show-toplevel"], "repository_root"), (["rev-parse", "--verify", "HEAD"], "base_commit"), (["branch", "--show-current"], "ref"), (["config", "--get", "remote.origin.url"], "repository")):
        try:
            result = subprocess.run(
                ["git", "-C", str(skill_path), *git_args],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            value = result.stdout.strip()
            if result.returncode == 0 and value:
                if field == "repository":
                    parsed = urlsplit(value)
                    if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname and not parsed.password:
                        value = urlunsplit((parsed.scheme, parsed.hostname + (f":{parsed.port}" if parsed.port else ""), parsed.path, "", ""))
                    elif value.startswith("git@") and " " not in value:
                        value = value
                    else:
                        value = "unknown"
                provenance[field] = value
        except (OSError, subprocess.SubprocessError):
            pass
    content_files = [skill_path / "SKILL.md", script_path]
    validator_path = script_path.parent / "validate_imagegallery_batch.py"
    if validator_path.is_file():
        content_files.append(validator_path)
    try:
        relevant_paths = [str(path.relative_to(skill_path)) for path in content_files]
        status = subprocess.run(
            ["git", "-C", str(skill_path), "status", "--short", "--untracked-files=all", "--", *relevant_paths],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if status.returncode == 0:
            source_is_dirty = bool(status.stdout.strip())
            provenance["working_tree_state"] = "dirty" if source_is_dirty else "clean"
            provenance["commit"] = "unknown" if source_is_dirty else provenance["base_commit"]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    content_digest = hashlib.sha256()
    found_content = False
    for path in content_files:
        if path.is_file():
            found_content = True
            content_digest.update(path.name.encode("utf-8"))
            content_digest.update(path.read_bytes())
    if found_content:
        provenance["content_sha256"] = content_digest.hexdigest()
    inputs = []
    for raw_path in input_paths or []:
        path = Path(raw_path).expanduser()
        identity: dict[str, str] = {"path": str(path), "name": path.name or "unknown", "sha256": "unknown"}
        if path.is_file():
            try:
                identity["sha256"] = file_sha256(path)
            except OSError:
                pass
        inputs.append(identity)
    provenance["inputs"] = inputs
    return provenance


def section_list_provenance_input(manifest_path: str, explicit_path: str = "") -> str:
    if explicit_path:
        return str(Path(explicit_path).expanduser().resolve())
    if not manifest_path:
        return ""
    candidate = Path(manifest_path).expanduser().resolve().parent / "section-list.json"
    return str(candidate) if candidate.is_file() else ""


def unique_provenance_inputs(paths: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in paths:
        raw = str(value or "").strip()
        if not raw:
            continue
        resolved = str(Path(raw).expanduser().resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def section_list_records(section_list_path: Path) -> list[dict[str, Any]]:
    data = read_json(section_list_path)
    if isinstance(data, list):
        return [record for record in data if isinstance(record, dict)]
    if not isinstance(data, dict):
        return []
    records = data.get("sections")
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    nested: list[dict[str, Any]] = []
    for chapter in data.get("chapters", []):
        if isinstance(chapter, dict):
            nested.extend(
                section for section in chapter.get("sections", [])
                if isinstance(section, dict)
            )
    return nested


def find_section_list_path(manifest_path: Path, explicit_path: str = "") -> Path | None:
    if explicit_path:
        candidate = Path(explicit_path).expanduser().resolve()
        return candidate if candidate.is_file() else None
    candidate = manifest_path.parent / "section-list.json"
    return candidate if candidate.is_file() else None


def summarize_steps(steps: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "completed_steps": [
            step.get("step", "") for step in steps
            if step.get("status") in {"ok", "completed", "reconciled", "retried", "deleted", "already_missing"}
        ],
        "failed_steps": [
            step.get("step", "") for step in steps
            if step.get("status") in {"failed", "blocked"}
        ],
        "skipped_steps": [
            step.get("step", "") for step in steps
            if step.get("status") == "skipped"
        ],
    }


def failed_target_steps(steps: Any, *, failed_step: str = "", reason: str = "") -> list[dict[str, Any]]:
    records = [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []
    safe_failure_step = safe_step_name(failed_step) if failed_step else "target_preflight"
    if not any(safe_step_name(step.get("step")) == safe_failure_step for step in records):
        records.append({"step": safe_failure_step, "status": "failed", "reason": reason})
    if not any(safe_step_name(step.get("step")) == "publish_vertical" for step in records):
        publish_status = "failed" if safe_failure_step == "publish_vertical" else "skipped"
        records.append({
            "step": "publish_vertical",
            "status": publish_status,
            "reason": reason or "target failed before vertical publication",
        })
    return records


def resolve_source_block_type(
    manifest: dict[str, Any],
    target: dict[str, Any],
    *,
    manifest_path: Path,
    section_list_json: str = "",
) -> dict[str, str]:
    manifest_type = str(manifest.get("block_type", "") or "").strip().lower()
    target_type = str(target.get("block_type", target.get("source_block_type", "")) or "").strip().lower()
    manifest_id = str(manifest.get("section_id", "") or "").strip()
    target_id = str(target.get("section_id", "") or "").strip()
    manifest_title = str(manifest.get("section_title", "") or "").strip()
    target_title = str(target.get("section_title", target.get("section_name", "")) or "").strip()

    if manifest_id and target_id and manifest_id != target_id:
        return {"status": "conflict", "block_type": "", "reason": "manifest and publish target have conflicting section_id values"}
    if manifest_title and target_title and manifest_title != target_title:
        return {"status": "conflict", "block_type": "", "reason": "manifest and publish target have conflicting section titles"}
    local_types = [
        ("gallery manifest", manifest_type),
        ("publish target", target_type),
    ]
    provided_types = [(source, block_type) for source, block_type in local_types if block_type]
    if len({block_type for _source, block_type in provided_types}) > 1:
        return {"status": "conflict", "block_type": "", "reason": "local sources have conflicting block_type values"}

    section_path = find_section_list_path(manifest_path, section_list_json)
    if section_path is not None:
        try:
            records = section_list_records(section_path)
        except Exception:
            return {"status": "missing", "block_type": "", "reason": "section-list.json could not be read"}

        section_id = manifest_id or target_id
        section_title = manifest_title or target_title
        if section_id:
            matches = [record for record in records if str(record.get("section_id", "") or "").strip() == section_id]
            if not matches:
                return {"status": "missing", "block_type": "", "reason": f"section-list.json has no section_id {section_id}"}
            if len(matches) != 1:
                return {"status": "ambiguous", "block_type": "", "reason": f"section-list.json has multiple records for section_id {section_id}"}
        elif section_title:
            matches = [
                record for record in records
                if any(
                    str(record.get(key, "") or "").strip() == section_title
                    for key in ("section_title", "title", "resource_title")
                )
            ]
            if not matches:
                return {"status": "missing", "block_type": "", "reason": f"section-list.json has no section titled {section_title}"}
            if len(matches) != 1:
                return {"status": "ambiguous", "block_type": "", "reason": f"section-list.json has multiple records titled {section_title}"}
        else:
            return {"status": "missing", "block_type": "", "reason": "section metadata has neither section_id nor title"}

        section_type = str(matches[0].get("block_type", "") or "").strip().lower()
        if section_type:
            provided_types.append(("section-list.json", section_type))

    if len({block_type for _source, block_type in provided_types}) > 1:
        return {"status": "conflict", "block_type": "", "reason": "local sources have conflicting block_type values"}
    if not provided_types:
        return {"status": "missing", "block_type": "", "reason": "no local block_type value is available"}
    resolved_type = provided_types[0][1]
    if resolved_type not in {"html", "imagesgallery"}:
        return {"status": "unsupported", "block_type": "", "reason": f"unsupported local block_type: {resolved_type}"}
    return {"status": "resolved", "block_type": resolved_type, "reason": f"local block_type sources agree ({len(provided_types)} source(s))"}


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
        "metadata": metadata,
        "content": node.get("content", metadata.get("content", "")),
        "play_config": node.get("play_config", metadata.get("play_config", {})),
        "time_config": node.get("time_config", metadata.get("time_config", [])),
        "items": node.get("items", metadata.get("items", [])),
        "data": node.get("data", metadata.get("data", {})),
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
    return galleries[0] if len(galleries) == 1 else None


def pick_lesson_html_candidate(
    vertical: dict[str, Any],
    *,
    excluded_block_locators: set[str] | None = None,
) -> dict[str, Any] | None:
    excluded = excluded_block_locators or set()
    return next(
        (
            block for block in vertical.get("blocks", [])
            if isinstance(block, dict)
            and block_category(block) == "html"
            and block_locator(block)
            and block_locator(block) not in excluded
            and str(block.get("name", "")).strip() == "文字讲解"
        ),
        None,
    )


def vertical_child_snapshot(
    vertical: dict[str, Any],
    *,
    source_vertical_block_id: str = "",
    target_vertical_block_id: str = "",
) -> dict[str, Any]:
    children = []
    for index, block in enumerate(vertical.get("blocks", []), start=1):
        if not isinstance(block, dict):
            continue
        locator = block_locator(block)
        if source_vertical_block_id and target_vertical_block_id:
            locator = remap_block_locator_course(locator, source_vertical_block_id, target_vertical_block_id)
        children.append({"locator": locator, "type": block_category(block), "order": index})
    return {"direct_child_count": len(children), "children": children}


def vertical_snapshot_for_course(
    course_structure: dict[str, Any],
    vertical_locator: str,
) -> tuple[dict[str, Any], Any]:
    matches = [
        record["vertical"] for record in iter_section_vertical_records(course_structure)
        if str(record["vertical"].get("block_location", "")).strip() == vertical_locator
    ]
    if len(matches) != 1:
        raise PublishFailure("verify_vertical_structure", "target vertical is missing or ambiguous in Studio course structure")
    vertical = matches[0]
    return vertical_child_snapshot(vertical), vertical.get("published")


def reconcile_created_gallery_snapshot(
    before: dict[str, Any],
    observed: dict[str, Any],
) -> dict[str, Any]:
    before_children = before.get("children") if isinstance(before.get("children"), list) else []
    observed_children = observed.get("children") if isinstance(observed.get("children"), list) else []
    if (
        before.get("direct_child_count") != len(before_children)
        or observed.get("direct_child_count") != len(observed_children)
        or any(not isinstance(child, dict) or not child.get("locator") for child in [*before_children, *observed_children])
    ):
        return {"status": "unverified", "reason": "vertical snapshot is incomplete"}

    before_locators = [str(child["locator"]) for child in before_children]
    observed_locators = [str(child["locator"]) for child in observed_children]
    if len(set(before_locators)) != len(before_locators) or len(set(observed_locators)) != len(observed_locators):
        return {"status": "ambiguous", "reason": "vertical snapshot contains duplicate component locators"}
    if observed == before:
        return {"status": "not_applied", "observed_snapshot": observed}

    added = [child for child in observed_children if str(child["locator"]) not in set(before_locators)]
    preserved = [child for child in observed_children if str(child["locator"]) in set(before_locators)]
    before_identity_order = [(str(child["locator"]), str(child.get("type", "")).lower()) for child in before_children]
    preserved_identity_order = [(str(child["locator"]), str(child.get("type", "")).lower()) for child in preserved]
    if (
        observed.get("direct_child_count") == before.get("direct_child_count", 0) + 1
        and len(added) == 1
        and str(added[0].get("type", "")).lower() == "imagesgallery"
        and preserved_identity_order == before_identity_order
    ):
        return {
            "status": "applied",
            "block_locator": str(added[0]["locator"]),
            "observed_snapshot": observed,
        }
    return {
        "status": "ambiguous",
        "reason": "Studio structure is not the original vertical plus exactly one Gallery",
        "observed_snapshot": observed,
    }


def fetch_vertical_state(
    session: requests.Session,
    *,
    studio_base: str,
    vertical_locator: str,
    timeout: int,
) -> tuple[dict[str, Any], Any]:
    course_key = course_key_from_vertical_block_id(vertical_locator)
    if not course_key:
        raise PublishFailure("verify_vertical_structure", "target vertical locator does not identify a course")
    structure = fetch_live_course_structure(
        session,
        studio_base=studio_base,
        course_key=course_key,
        timeout=timeout,
    )
    return vertical_snapshot_for_course(structure, vertical_locator)


def replace_snapshot_child(
    snapshot: dict[str, Any],
    *,
    old_locator: str,
    new_locator: str,
) -> dict[str, Any]:
    children = [dict(child) for child in snapshot.get("children", [])]
    old_index = next((index for index, child in enumerate(children) if child.get("locator") == old_locator), None)
    if old_index is None:
        raise PublishFailure("replacement_validation", "replacement target is absent from the original vertical snapshot")
    new_child = next((dict(child) for child in children if child.get("locator") == new_locator), None)
    if new_child is None:
        new_child = {"locator": new_locator, "type": "imagesgallery", "order": old_index + 1}
    children = [child for child in children if child.get("locator") != new_locator]
    old_index = next(index for index, child in enumerate(children) if child.get("locator") == old_locator)
    children.insert(old_index, new_child)
    for index, child in enumerate(children, start=1):
        child["order"] = index
    return {"direct_child_count": len(children), "children": children}


def replacement_after_delete_snapshot(
    snapshot: dict[str, Any],
    *,
    old_locator: str,
    new_locator: str,
) -> dict[str, Any]:
    children = [
        dict(child) for child in snapshot.get("children", [])
        if child.get("locator") != old_locator
    ]
    if sum(child.get("locator") == new_locator for child in children) != 1:
        raise PublishFailure("replacement_validation", "new Gallery is missing or ambiguous after replacement")
    for index, child in enumerate(children, start=1):
        child["order"] = index
    return {"direct_child_count": len(children), "children": children}


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
    block_type: str = "",
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

    source_type = str(block_type or "").strip().lower()
    course_kind = (
        "html_based" if source_type == "html" else
        "imagesgallery_based" if source_type == "imagesgallery" else
        classify_replacement_kind(lookup_record, excluded_block_locators=excluded)
    )
    target_record = lookup_record
    summary_matches = [
        block
        for vertical in target_record["section"].get("verticals", [])
        if isinstance(vertical, dict)
        for block in summary_html_blocks(vertical)
    ]
    summary_match_locators = {
        block_locator(block)
        for block in summary_matches
        if block_locator(block)
    }
    excluded_for_body = excluded | summary_match_locators
    matched_block: dict[str, Any] | None = None
    working_record = target_record
    match_scope = "fallback"
    result_type = "fallback_inserted"
    planning_issue: dict[str, str] | None = None

    if course_kind == "imagesgallery_based":
        galleries = [
            block for block in target_record["vertical"].get("blocks", [])
            if isinstance(block, dict)
            and block_category(block) == "imagesgallery"
            and block_locator(block)
            and block_locator(block) not in excluded
        ]
        matched_block = galleries[0] if len(galleries) == 1 else None
        if source_type == "imagesgallery" and len(galleries) != 1:
            planning_issue = {
                "code": "ambiguous_gallery_replacement_target" if galleries else "missing_gallery_replacement_target",
                "reason": "Gallery replacement requires exactly one existing Gallery in the target vertical",
            }
        elif matched_block is not None:
            match_scope = "target_vertical"
            result_type = "replaced"
        elif not source_type:
            matched_block = pick_existing_gallery(
                target_record["vertical"],
                excluded_block_locators=excluded,
            )
            if matched_block is not None:
                match_scope = "target_vertical"
                result_type = "replaced"
    else:
        matched_block = (
            pick_lesson_html_candidate(target_record["vertical"], excluded_block_locators=excluded_for_body)
            if source_type == "html"
            else pick_best_html_candidate(target_record["vertical"], excluded_block_locators=excluded_for_body)
        )
        if matched_block is not None:
            match_scope = "target_vertical"
            result_type = "replaced"
        elif source_type == "html":
            planning_issue = {
                "code": "missing_html_replacement_target",
                "reason": "target vertical has no direct HTML child titled 文字讲解",
            }
        else:
            best_section_match: tuple[tuple[int, int], dict[str, Any], dict[str, Any]] | None = None
            for vertical in target_record["section"].get("verticals", []):
                if not isinstance(vertical, dict):
                    continue
                candidate = pick_best_html_candidate(
                    vertical,
                    excluded_block_locators=excluded_for_body,
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
        "initial_snapshot": vertical_child_snapshot(
            working_record["vertical"],
            source_vertical_block_id=source_vertical_block_id,
            target_vertical_block_id=target_vertical_block_id,
        ),
        "resolved_block_type": source_type,
        "planning_issue": planning_issue,
        "matched_source_block_locator": matched_source_block_locator,
        "matched_source_block_category": matched_source_block_category,
        "summary_cleanup_status": "preserved" if summary_matches else "not_present",
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
        error = PublishFailure("load_live_course_structure", f"Studio course structure request failed (HTTP {resp.status_code})", payload=payload)
        error.safe_summary = f"Studio course structure request failed (HTTP {resp.status_code})"
        raise error
    if parse_warning:
        error = PublishFailure(
            "load_live_course_structure",
            "Studio course structure response was not valid JSON",
            payload=payload,
        )
        error.safe_summary = "Studio course structure response was not valid JSON"
        raise error
    try:
        return normalize_course_structure_payload(payload)
    except Exception as exc:
        error = PublishFailure(
            "load_live_course_structure",
            "Studio course structure could not be normalized",
            payload=payload,
        )
        error.safe_summary = "Studio course structure could not be normalized"
        raise error from exc


def maybe_fetch_live_replacement_plan(
    *,
    studio_url: str,
    source_vertical_block_id: str,
    target_vertical_block_id: str,
    block_type: str = "",
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
                block_type=block_type,
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
            "note": safe_error_summary(exc),
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


def redact_sensitive_fields(value: Any) -> Any:
    sensitive_hints = (
        "authorization",
        "cookie",
        "credential",
        "csrf",
        "password",
        "secret",
        "session",
        "token",
    )
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if any(hint in str(key).lower() for hint in sensitive_hints)
            else redact_sensitive_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_fields(item) for item in value]
    return value


def safe_step_name(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.\[\]-]+", "_", str(value or "step"))[:120]


def safe_error_summary(exc: Exception, *, step_name: str = "") -> str:
    explicit_summary = getattr(exc, "safe_summary", "")
    if isinstance(explicit_summary, str) and explicit_summary:
        return explicit_summary[:240]
    step = safe_step_name(step_name or getattr(exc, "step_name", "publish"))
    if isinstance(exc, TemplateCallError):
        return f"Studio request failed (HTTP {exc.status_code}) during {step}"
    if isinstance(exc, ValueError):
        return str(exc)[:240]
    return f"Publishing failed during {step}"


def safe_auth_retry_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "retry_index": event.get("retry_index"),
            "reason": "auth_error",
            "step": safe_step_name(event.get("step")),
            "http_status": event.get("status_code", event.get("http_status")),
        }
        for event in value
        if isinstance(event, dict)
    ]


def safe_image_checkpoint_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    images = []
    for item in value:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        image = item.get("image")
        asset_id = item.get("asset_id")
        if isinstance(page, int) and isinstance(image, str) and isinstance(asset_id, str) and asset_id:
            images.append({"page": page, "image": Path(image.replace("\\", "/")).name, "asset_id": asset_id})
    return images


def safe_step_record(record: dict[str, Any], *, page_image: str = "") -> dict[str, Any]:
    safe: dict[str, Any] = {
        "step": safe_step_name(record.get("step")),
        "status": safe_step_name(record.get("status", "ok")),
    }
    status_code = record.get("response_status")
    if isinstance(status_code, int):
        safe["http_status"] = status_code
    for key in ("asset_id", "block_locator"):
        value = record.get(key)
        if isinstance(value, str) and value:
            safe[key] = value
    if page_image:
        safe["page_image"] = Path(page_image.replace("\\", "/")).name
    retry_attempts = record.get("auth_retry_attempts")
    if isinstance(retry_attempts, int) and retry_attempts > 0:
        safe["auth_retry_attempts"] = retry_attempts
    retry_events = record.get("auth_retry_events")
    if isinstance(retry_events, list):
        safe["auth_retry_events"] = safe_auth_retry_events(retry_events)
    attempts = record.get("attempts")
    if isinstance(attempts, list):
        safe["attempts"] = [
            {
                key: event[key]
                for key in ("attempt", "status", "http_status")
                if isinstance(event, dict) and isinstance(event.get(key), (str, int))
            }
            for event in attempts if isinstance(event, dict)
        ]
    return safe


def safe_preview_url(url: str) -> str:
    parsed = urlsplit(url)
    safe_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if any(hint in key.lower() for hint in ("auth", "cookie", "csrf", "credential", "password", "secret", "session", "token")):
            value = "<redacted>"
        safe_query.append((key, value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(safe_query), ""))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        temp_file.write(encoded)
        temp_file.flush()
        os.fsync(temp_file.fileno())
    try:
        for attempt in range(ATOMIC_WRITE_REPLACE_RETRIES + 1):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                if attempt >= ATOMIC_WRITE_REPLACE_RETRIES:
                    raise
                time.sleep(ATOMIC_WRITE_REPLACE_RETRY_DELAY_SECONDS)
    finally:
        if temp_path.exists():
            temp_path.unlink()


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
    }

    if not execute:
        record["url"] = safe_preview_url(url)
        record["request"] = {
            "headers": redact_sensitive_fields(headers),
            "params": redact_sensitive_fields(params),
            "json": redact_sensitive_fields(json_body),
            "data": redact_sensitive_fields(data_body),
            "has_files": bool(files_spec),
        }
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
    if parse_warning:
        record["response_parse_warning"] = "Studio returned invalid JSON"

    if resp.status_code >= 400:
        raise TemplateCallError(name, resp.status_code, payload)

    extract = rendered.get("extract") or {}
    if parse_warning and isinstance(extract, dict) and extract:
        raise PublishFailure(
            name,
            f"{name} succeeded with an invalid response",
            payload=payload,
        )
    if isinstance(extract, dict):
        for k, p in extract.items():
            ctx[k] = get_by_path(payload, str(p))
        if extract:
            if name == "create_block":
                locator = ctx.get("block_locator")
                if isinstance(locator, str) and locator:
                    record["block_locator"] = locator

    if name == "upload_audio_prepare" and isinstance(payload, dict):
        for key in ("Credentials", "region", "bucket", "file_key"):
            if key in payload:
                ctx[key] = payload[key]

    if name.startswith("upload_image"):
        asset_id = ""
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            asset_id = str(payload[0].get("id") or payload[0].get("asset_key") or "").strip()
        elif isinstance(payload, dict):
            asset_id = str(payload.get("id") or payload.get("asset_key") or "").strip()
        if asset_id:
            record["asset_id"] = asset_id

    return record


class PublishCheckpoint:
    def __init__(self, path: Path, operation: dict[str, Any], gallery_index: int, gallery_key: str):
        self.path = path.expanduser().resolve()
        self.operation = operation
        self.gallery_index = gallery_index
        self.gallery_key = gallery_key
        self.gallery = operation["galleries"][gallery_index]
        previous = self.gallery.get("publish")
        previous = previous if isinstance(previous, dict) else {}
        reconciliation_count = previous.get("reconciliation_count", 0)
        if not isinstance(reconciliation_count, int) or reconciliation_count < 0:
            reconciliation_count = 0
        self.state: dict[str, Any] = {
            "gallery_key": gallery_key,
            "status": previous.get("status") if previous.get("status") in {"in_progress", "failed", "completed"} else "not_started",
            "completed_stage": previous.get("completed_stage") if isinstance(previous.get("completed_stage"), str) else "",
            "active_step": safe_step_name(previous.get("active_step")) if previous.get("active_step") else "",
            "failed_step": safe_step_name(previous.get("failed_step")) if previous.get("failed_step") else "",
            "asset_ids": [str(value) for value in previous.get("asset_ids", []) if isinstance(value, str) and value],
            "images": safe_image_checkpoint_records(previous.get("images")),
            "steps": self._safe_steps(previous.get("steps")),
            "images_started": bool(previous.get("images_started", False)),
            "reconciliation_count": reconciliation_count,
            "create_attempts": max(0, int(previous.get("create_attempts", 0))) if isinstance(previous.get("create_attempts", 0), int) and not isinstance(previous.get("create_attempts", 0), bool) else 0,
        }
        error_summary = previous.get("error_summary")
        if isinstance(error_summary, str) and error_summary:
            self.state["error_summary"] = "Previous publishing attempt failed; review the recorded safe step diagnostics"
        failed_page_image = previous.get("failed_page_image")
        if isinstance(failed_page_image, str) and failed_page_image:
            self.state["failed_page_image"] = Path(failed_page_image.replace("\\", "/")).name
        audio_document_id = previous.get("audio_document_id")
        if isinstance(audio_document_id, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", audio_document_id):
            self.state["audio_document_id"] = audio_document_id
        self.gallery_locator = str(self.gallery.get("gallery_locator") or previous.get("gallery_locator") or "").strip()
        self.resume = bool(self.state["completed_stage"] or self.state["active_step"] or self.state["status"] in {"in_progress", "failed"})

    @staticmethod
    def _safe_steps(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        steps = []
        for item in value:
            if not isinstance(item, dict):
                continue
            record = {
                "step": safe_step_name(item.get("step")),
                "status": safe_step_name(item.get("status", "ok")),
            }
            for key in ("http_status", "asset_id", "block_locator", "page_image", "auth_retry_attempts"):
                if isinstance(item.get(key), (str, int)):
                    record[key] = item[key]
            attempts = item.get("attempts")
            if isinstance(attempts, list):
                record["attempts"] = [
                    {
                        key: event[key]
                        for key in ("attempt", "status", "http_status")
                        if isinstance(event, dict) and isinstance(event.get(key), (str, int))
                    }
                    for event in attempts if isinstance(event, dict)
                ]
            retry_events = item.get("auth_retry_events")
            if isinstance(retry_events, list):
                record["auth_retry_events"] = safe_auth_retry_events(retry_events)
            steps.append(record)
        return steps

    @classmethod
    def load(cls, path_value: str, gallery_key: str, *, manifest_path: Path, vertical_block_id: str) -> "PublishCheckpoint":
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            raise ValueError("operation file must already exist and be created by the batch caller")
        try:
            operation = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, JSONDecodeError) as exc:
            raise ValueError("operation file is not readable JSON") from exc
        galleries = operation.get("galleries") if isinstance(operation, dict) else None
        if not isinstance(galleries, list):
            raise ValueError("operation file must contain a galleries list")
        matches = [index for index, gallery in enumerate(galleries) if isinstance(gallery, dict) and gallery.get("gallery_key") == gallery_key]
        if len(matches) != 1:
            raise ValueError("operation file must contain exactly one matching gallery_key")
        gallery = galleries[matches[0]]
        recorded_manifest = gallery.get("manifest")
        if recorded_manifest and Path(str(recorded_manifest)).expanduser().resolve() != manifest_path:
            raise ValueError("operation gallery manifest does not match --manifest")
        recorded_vertical = str(gallery.get("vertical_locator") or "").strip()
        if recorded_vertical and recorded_vertical != vertical_block_id:
            raise ValueError("operation gallery vertical_locator does not match --studio-url")
        return cls(path, operation, matches[0], gallery_key)

    def persist(self) -> None:
        if self.gallery_locator:
            self.gallery["gallery_locator"] = self.gallery_locator
        self.gallery["publish"] = self.state
        atomic_write_json(self.path, self.operation)

    def mark_started(self, step: str, *, page_image: str = "") -> None:
        self.state["status"] = "in_progress"
        self.state["active_step"] = safe_step_name(step)
        self.state.pop("error_summary", None)
        self.state["failed_step"] = ""
        if safe_step_name(step) == "create_block":
            self.state["create_attempts"] = int(self.state.get("create_attempts", 0)) + 1
        if page_image:
            self.state["active_page_image"] = Path(page_image.replace("\\", "/")).name
        if safe_step_name(step).startswith("upload_image["):
            self.state["images_started"] = True
        self.persist()

    def record_success(
        self,
        record: dict[str, Any],
        *,
        stage: str = "",
        page_image: str = "",
        images: list[dict[str, Any]] | None = None,
        audio_document_id: str = "",
    ) -> None:
        safe = safe_step_record(record, page_image=page_image)
        identity = (safe["step"], safe.get("page_image", ""))
        self.state["steps"] = [
            item for item in self.state["steps"]
            if (item["step"], item.get("page_image", "")) != identity
        ] + [safe]
        self.state["active_step"] = ""
        self.state.pop("active_page_image", None)
        if safe.get("block_locator"):
            self.gallery_locator = safe["block_locator"]
        if images is not None:
            self.state["images"] = safe_image_checkpoint_records(images)
            self.state["asset_ids"] = [item["asset_id"] for item in self.state["images"]]
        if audio_document_id and re.fullmatch(r"[0-9a-fA-F-]{36}", audio_document_id):
            self.state["audio_document_id"] = audio_document_id
        if stage:
            self.state["completed_stage"] = stage
        self.state["status"] = "in_progress"
        self.persist()

    def set_stage(self, stage: str) -> None:
        self.state["completed_stage"] = stage
        self.state["active_step"] = ""
        self.state.pop("active_page_image", None)
        self.state["status"] = "in_progress"
        self.persist()

    def fail(self, exc: Exception, step_name: str, *, page_image: str = "") -> None:
        self.state["status"] = "failed"
        self.state["failed_step"] = safe_step_name(step_name)
        self.state["active_step"] = ""
        self.state.pop("active_page_image", None)
        self.state["error_summary"] = str(getattr(exc, "safe_summary", "") or safe_error_summary(exc, step_name=step_name))[:240]
        if isinstance(exc, TemplateCallError):
            self.state["failure_http_status"] = exc.status_code
        failed_record: dict[str, Any] = {"step": safe_step_name(step_name), "status": "failed"}
        if isinstance(exc, TemplateCallError):
            failed_record["http_status"] = exc.status_code
        if page_image:
            failed_image = Path(page_image.replace("\\", "/")).name
            self.state["failed_page_image"] = failed_image
            failed_record["page_image"] = failed_image
        identity = (failed_record["step"], failed_record.get("page_image", ""))
        self.state["steps"] = [
            item for item in self.state["steps"]
            if (item["step"], item.get("page_image", "")) != identity
        ] + [failed_record]
        self.persist()

    def complete(self) -> None:
        self.state["status"] = "completed"
        self.state["completed_stage"] = "saved"
        self.state["active_step"] = ""
        self.state["failed_step"] = ""
        self.state.pop("active_page_image", None)
        self.state.pop("failed_page_image", None)
        self.state.pop("error_summary", None)
        self.persist()


def manifest_image_name(item: dict[str, Any]) -> str:
    return Path(str(item.get("image", "")).replace("\\", "/")).name


def request_gallery_files(
    session: requests.Session,
    *,
    studio_base: str,
    gallery_locator: str,
    block_url: str,
    context: dict[str, Any],
    timeout: int,
    step: str,
    invalid_json_summary: str,
    refresh_auth_fn: Any = None,
    auth_retry_max_retries: int = DEFAULT_AUTH_RETRY_MAX_RETRIES,
    auth_retry_delay_seconds: float = DEFAULT_AUTH_RETRY_DELAY_SECONDS,
) -> tuple[Any, int, list[dict[str, Any]]]:
    url = f"{studio_base.rstrip('/')}/preview/xblock/{gallery_locator}/handler/get_files"

    def get_files() -> tuple[Any, int]:
        response = session.request(
            method="POST",
            url=url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "X-CSRFToken": str(context.get("csrftoken", "")),
                "X-Requested-With": "XMLHttpRequest",
                "Referer": block_url,
            },
            json={"current_page": 0, "page_size": 100},
            timeout=timeout,
        )
        payload, parse_warning = decode_response_payload(response)
        if response.status_code >= 400:
            raise TemplateCallError(step, response.status_code, payload)
        if parse_warning:
            error = PublishFailure(step, "Studio returned invalid get_files JSON", payload=payload)
            error.safe_summary = invalid_json_summary
            raise error
        return payload, int(response.status_code)

    (payload, status_code), retry_events = call_with_auth_retry(
        get_files,
        refresh_auth_fn,
        max_retries=auth_retry_max_retries,
        delay_seconds=auth_retry_delay_seconds,
    )
    return payload, status_code, retry_events


def reconcile_gallery_images(
    session: requests.Session,
    *,
    studio_base: str,
    gallery_locator: str,
    block_url: str,
    context: dict[str, Any],
    manifest_items: list[dict[str, Any]],
    timeout: int,
    refresh_auth_fn: Any = None,
    auth_retry_max_retries: int = DEFAULT_AUTH_RETRY_MAX_RETRIES,
    auth_retry_delay_seconds: float = DEFAULT_AUTH_RETRY_DELAY_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload, status_code, retry_events = request_gallery_files(
        session,
        studio_base=studio_base,
        gallery_locator=gallery_locator,
        block_url=block_url,
        context=context,
        timeout=timeout,
        step="get_files_reconciliation",
        invalid_json_summary="Studio get_files response could not be read",
        refresh_auth_fn=refresh_auth_fn,
        auth_retry_max_retries=auth_retry_max_retries,
        auth_retry_delay_seconds=auth_retry_delay_seconds,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        error = PublishFailure("get_files_reconciliation", "Studio returned an invalid get_files shape", payload=payload)
        error.safe_summary = "Studio get_files response did not contain a valid files list"
        raise error
    files = payload["files"]
    total_count = payload.get("total_count")
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count != len(files) or len(files) != len(manifest_items):
        error = PublishFailure("get_files_reconciliation", "Studio get_files count did not match the manifest")
        error.safe_summary = f"Studio get_files returned {len(files)} file records for {len(manifest_items)} manifest pages"
        raise error

    expected_names = [manifest_image_name(item) for item in manifest_items]
    if len(set(expected_names)) != len(expected_names):
        error = PublishFailure("get_files_reconciliation", "manifest filenames are ambiguous")
        error.safe_summary = "Manifest page filenames do not uniquely identify uploaded files"
        raise error

    matches: list[dict[str, Any]] = []
    used_asset_ids: set[str] = set()
    for index, (file_record, manifest_item) in enumerate(zip(files, manifest_items), start=1):
        if not isinstance(file_record, dict):
            raise PublishFailure("get_files_reconciliation", "Studio get_files contained an invalid file record")
        remote_name = str(
            file_record.get("display_name")
            or file_record.get("name")
            or file_record.get("filename")
            or file_record.get("file_name")
            or ""
        ).replace("\\", "/").rsplit("/", 1)[-1]
        expected_name = expected_names[index - 1]
        asset_id = str(file_record.get("id") or file_record.get("asset_key") or "").strip()
        if remote_name != expected_name or not asset_id or asset_id in used_asset_ids:
            error = PublishFailure("get_files_reconciliation", "Studio files did not uniquely match manifest order")
            error.safe_summary = f"Studio get_files did not uniquely match manifest page {index} ({expected_name})"
            raise error
        used_asset_ids.add(asset_id)
        matches.append({"page": int(manifest_item["page"]), "image": expected_name, "asset_id": asset_id})
    return matches, {
        "step": "get_files_reconciliation",
        "status": "ok",
        "response_status": status_code,
        "auth_retry_attempts": len(retry_events),
        "auth_retry_events": retry_events,
    }


def read_gallery_asset_order(
    session: requests.Session,
    *,
    studio_base: str,
    gallery_locator: str,
    block_url: str,
    context: dict[str, Any],
    expected_count: int,
    timeout: int,
    refresh_auth_fn: Any = None,
    auth_retry_max_retries: int = DEFAULT_AUTH_RETRY_MAX_RETRIES,
    auth_retry_delay_seconds: float = DEFAULT_AUTH_RETRY_DELAY_SECONDS,
) -> tuple[list[str], dict[str, Any]]:
    payload, status_code, retry_events = request_gallery_files(
        session,
        studio_base=studio_base,
        gallery_locator=gallery_locator,
        block_url=block_url,
        context=context,
        timeout=timeout,
        step="change_files_order_reconciliation",
        invalid_json_summary="Studio get_files response could not be read before retrying image ordering",
        refresh_auth_fn=refresh_auth_fn,
        auth_retry_max_retries=auth_retry_max_retries,
        auth_retry_delay_seconds=auth_retry_delay_seconds,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        error = PublishFailure("change_files_order_reconciliation", "Studio returned an invalid get_files shape", payload=payload)
        error.safe_summary = "Studio get_files response did not contain a valid files list before retrying image ordering"
        raise error
    files = payload["files"]
    total_count = payload.get("total_count")
    if (
        not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count != len(files)
        or len(files) != expected_count
    ):
        error = PublishFailure("change_files_order_reconciliation", "Studio get_files count did not match saved images")
        error.safe_summary = f"Studio get_files returned {len(files)} file records for {expected_count} saved images"
        raise error

    asset_ids: list[str] = []
    seen_asset_ids: set[str] = set()
    for file_record in files:
        if not isinstance(file_record, dict):
            raise PublishFailure("change_files_order_reconciliation", "Studio get_files contained an invalid file record")
        asset_id = str(file_record.get("id") or file_record.get("asset_key") or "").strip()
        if not asset_id or asset_id in seen_asset_ids:
            raise PublishFailure("change_files_order_reconciliation", "Studio get_files contained duplicate or unidentified assets")
        seen_asset_ids.add(asset_id)
        asset_ids.append(asset_id)

    return asset_ids, {
        "step": "change_files_order_reconciliation",
        "status": "ok",
        "response_status": status_code,
        "auth_retry_attempts": len(retry_events),
        "auth_retry_events": retry_events,
    }


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
    unsupported = [result for result in results if result.get("status") == "unsupported"]
    failed = [result for result in results if result.get("status") == "failed"]
    manual_followup_failed = [result for result in failed if bool(result.get("requires_manual_followup", True))]
    unexpected_failed = [result for result in failed if not bool(result.get("requires_manual_followup", True))]
    return {
        "replaced": replaced,
        "fallback_inserted": fallback_inserted,
        "unsupported": unsupported,
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
        "unsupported_count": len(grouped["unsupported"]),
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
        "url": safe_preview_url(url),
        "request": {
            "headers": redact_sensitive_fields(headers),
            "params": redact_sensitive_fields(params),
            "json": redact_sensitive_fields(json_body),
            "data": redact_sensitive_fields(data_body),
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
    if parse_warning:
        record["response_parse_warning"] = "Studio returned invalid JSON"

    if resp.status_code in {200, 202, 204}:
        record["status"] = "deleted"
        return record
    if resp.status_code == 404:
        record["status"] = "already_missing"
        return record

    error = PublishFailure(
        step_name,
        f"{step_name} failed (HTTP {resp.status_code})",
        payload=payload,
    )
    error.safe_summary = f"Studio request failed (HTTP {resp.status_code}) during {safe_step_name(step_name)}"
    raise error


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
        operation_file=str(getattr(base_args, "operation_file", "") or ""),
        gallery_key=str(getattr(base_args, "gallery_key", "") or ""),
    )


def operator_vertical_urls(studio_base: str, replacement_plan: dict[str, Any], fallback_url: str) -> tuple[str, str]:
    requested_vertical_url = build_container_block_url(studio_base, str(replacement_plan.get("target_vertical_block_id", ""))) or fallback_url
    working_vertical_url = build_container_block_url(studio_base, str(replacement_plan.get("working_vertical_block_id", ""))) or requested_vertical_url
    return working_vertical_url, requested_vertical_url


def unsupported_cleanup_only_result(
    *,
    target_vertical_block_id: str = "",
    target_vertical_url: str = "",
) -> dict[str, Any]:
    return {
        "status": "unsupported",
        "cleanup_only": True,
        "target_vertical_block_id": target_vertical_block_id,
        "target_vertical_url": target_vertical_url,
        "failed_step": "cleanup_only_unsupported",
        "failure_bucket": "unsupported",
        "requires_manual_followup": False,
        "note": "cleanup-only is unsupported; HTML summary components are preserved",
        "summary_cleanup": {"status": "preserved"},
        "steps": [],
    }


def run_single_publish(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = ensure_manifest(manifest_path)
    adapter = read_json(Path(args.adapter).expanduser().resolve())
    studio_url = args.studio_url.strip()
    parsed_url = urlparse(studio_url)
    studio_base = f"{parsed_url.scheme}://{parsed_url.netloc}" if parsed_url.scheme and parsed_url.netloc else ""
    if not studio_base:
        raise ValueError("studio_url is invalid")

    vertical_block_id = args.vertical_block_id or parse_vertical_block_id(studio_url)
    if not vertical_block_id:
        raise ValueError("studio_url does not contain a vertical_block_id")

    operation_file = str(getattr(args, "operation_file", "") or "").strip()
    gallery_key = str(getattr(args, "gallery_key", "") or "").strip()
    if bool(operation_file) != bool(gallery_key):
        raise ValueError("--operation-file and --gallery-key must be supplied together")
    if operation_file and not args.execute:
        raise ValueError("--operation-file is available only with --execute")
    checkpoint = (
        PublishCheckpoint.load(operation_file, gallery_key, manifest_path=manifest_path, vertical_block_id=vertical_block_id)
        if operation_file
        else None
    )
    if checkpoint and checkpoint.state["status"] == "completed" and checkpoint.state["completed_stage"] == "saved" and checkpoint.gallery_locator:
        locator = checkpoint.gallery_locator
        gallery_url = build_container_block_url(studio_base, locator)
        return {
            "mode": "execute" if args.execute else "dry-run",
            "studio_base": studio_base,
            "vertical_block_id": vertical_block_id,
            "target_vertical_url": build_container_block_url(studio_base, vertical_block_id),
            "block_locator": locator,
            "created_imagegallery_block_url": gallery_url,
            "created_block_url": gallery_url,
            "asset_ids": list(checkpoint.state["asset_ids"]),
            "steps": list(checkpoint.state["steps"]),
            "resumed": True,
        }

    execute = bool(args.execute)
    session = requests.Session()
    csrftoken = (args.csrf_token or "") if execute else ""
    account = (args.account or os.environ.get("FIRA_SAAS_OP_ACCOUNT", "")).strip()
    password = (args.password or os.environ.get("FIRA_SAAS_OP_PASSWORD", "")).strip()
    courses_base = args.courses_base or derive_courses_base_from_studio_base(studio_base)
    if execute and not args.cookie and (not account or not password):
        raise ValueError("execute mode requires Studio credentials or --cookie")
    if execute and account and password and courses_base:
        session, login_csrf = login_and_get_studio_session(
            account=account,
            password=password,
            courses_base=courses_base,
            studio_csrf_url=args.block_url or studio_url,
        )
        if not csrftoken:
            csrftoken = login_csrf
    if execute and args.cookie:
        cookies = parse_cookie_header(args.cookie)
        for key, value in cookies.items():
            session.cookies.set(key, value, domain=urlparse(studio_base).hostname or "")
        if not csrftoken:
            csrftoken = cookies.get("csrftoken", "")

    items_obj = [
        {"page": int(item["page"]), "image": item["image"], "subtitle": item["subtitle"], "start": int(item["start"]), "end": int(item["end"])}
        for item in manifest["items"]
    ]
    time_config_obj = [
        {"page": int(item["page"]), "start": int(item["start"]), "end": int(item["end"]), "subtitle": item["subtitle"]}
        for item in manifest["items"]
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
    if checkpoint and checkpoint.state.get("audio_document_id"):
        ctx["edx_document_id"] = checkpoint.state["audio_document_id"]
        ctx["audio_document_id"] = f"{ctx['edx_document_id']}.{ctx['audio_post_fix']}"

    logs = list(checkpoint.state["steps"]) if checkpoint else []
    gallery_create_attempts = max(
        int(checkpoint.state.get("create_attempts", 0)) if checkpoint else 0,
        int(getattr(args, "create_attempts", 0) or 0),
    )

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

    def start_and_call(name: str, tmpl: dict[str, Any], *, page_image: str = "") -> dict[str, Any]:
        if checkpoint:
            checkpoint.mark_started(name, page_image=page_image)
        try:
            record, retry_events = call_with_auth_retry(
                lambda: call_template(session, name, tmpl, ctx, execute, args.timeout),
                refresh_auth_session if can_refresh_auth else None,
                max_retries=normalize_auth_retry_max_retries(getattr(args, "auth_retry_max_retries", DEFAULT_AUTH_RETRY_MAX_RETRIES)),
                delay_seconds=max(0.0, float(args.auth_retry_delay_seconds)),
            )
        except Exception as exc:
            if not getattr(exc, "step_name", None):
                setattr(exc, "step_name", name)
            mark_failure(exc, name, page_image=page_image)
            raise
        if execute and "status" not in record:
            record["status"] = "ok"
        if retry_events:
            record["auth_retry_attempts"] = len(retry_events)
            record["auth_retry_events"] = retry_events
        return record

    def accept_step(
        record: dict[str, Any],
        *,
        stage: str = "",
        page_image: str = "",
        images: list[dict[str, Any]] | None = None,
        audio_document_id: str = "",
    ) -> None:
        logs.append(record)
        if checkpoint:
            checkpoint.record_success(
                record,
                stage=stage,
                page_image=page_image,
                images=images,
                audio_document_id=audio_document_id,
            )

    def mark_failure(exc: Exception, step: str, *, page_image: str = "") -> None:
        diagnostics: dict[str, Any] = {
            "completed_stage": checkpoint.state["completed_stage"] if checkpoint else "",
            "asset_ids": list(ctx.get("uploaded_asset_ids", [])),
            "created_block_locator": str(ctx.get("block_locator", "")),
            "create_attempts": max(
                gallery_create_attempts,
                int(checkpoint.state.get("create_attempts", 0)) if checkpoint else 0,
            ),
            "steps": [safe_step_record(record) for record in logs],
        }
        if page_image:
            diagnostics["failed_page_image"] = Path(page_image.replace("\\", "/")).name
        setattr(exc, "safe_diagnostics", diagnostics)
        if checkpoint and checkpoint.state["status"] != "failed":
            checkpoint.fail(exc, step, page_image=page_image)

    def run_reconciliation(failed_step: str, *, page_image: str = "") -> list[dict[str, Any]]:
        if checkpoint and checkpoint.state["reconciliation_count"] >= 1:
            error = PublishFailure(failed_step, "image recovery already attempted")
            error.safe_summary = "Studio image recovery was already attempted; no images were uploaded again"
            checkpoint.fail(error, failed_step, page_image=page_image)
            raise error
        if checkpoint:
            checkpoint.state["reconciliation_count"] += 1
            checkpoint.mark_started("get_files_reconciliation", page_image=page_image)
            checkpoint.state["reconciliation_count"] += 0
            checkpoint.persist()
        try:
            images, record = reconcile_gallery_images(
                session,
                studio_base=studio_base,
                gallery_locator=str(ctx.get("block_locator", "")),
                block_url=canonical_block_url,
                context=ctx,
                manifest_items=list(manifest["items"]),
                timeout=args.timeout,
                refresh_auth_fn=refresh_auth_session if can_refresh_auth else None,
                auth_retry_max_retries=normalize_auth_retry_max_retries(getattr(args, "auth_retry_max_retries", DEFAULT_AUTH_RETRY_MAX_RETRIES)),
                auth_retry_delay_seconds=max(0.0, float(args.auth_retry_delay_seconds)),
            )
        except Exception as exc:
            failed_reconciliation = {
                "step": "get_files_reconciliation",
                "status": "failed",
                "response_status": exc.status_code if isinstance(exc, TemplateCallError) else None,
            }
            logs.append(failed_reconciliation)
            safe_exc = exc if isinstance(exc, Exception) else Exception("reconciliation failed")
            mark_failure(safe_exc, failed_step, page_image=page_image)
            if isinstance(exc, Exception):
                exc.step_name = failed_step
            raise
        logs.append(record)
        if checkpoint:
            checkpoint.record_success(record, images=images)
        ctx["uploaded_asset_ids"] = [image["asset_id"] for image in images]
        return images

    for key in ("create_block", "upload_image", "save_block"):
        if key not in adapter:
            raise ValueError(f"adapter missing step {key}")

    def query_create_reconciliation() -> dict[str, Any]:
        callback = getattr(args, "reconcile_create_block", None)
        if not callable(callback):
            return {"status": "unverified", "reason": "no vertical snapshot reconciler is available"}
        try:
            result = callback()
        except Exception:
            return {"status": "unverified", "reason": "Studio vertical could not be reread after Gallery creation"}
        return result if isinstance(result, dict) else {"status": "unverified", "reason": "Studio vertical reconciliation returned no evidence"}

    def reconciled_create_record(evidence: dict[str, Any], attempts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        locator = str(evidence.get("block_locator", "") or "").strip()
        if not locator:
            raise PublishFailure("create_block", "Gallery create reconciliation did not identify one locator")
        ctx["block_locator"] = locator
        if checkpoint:
            checkpoint.gallery_locator = locator
            if (
                checkpoint.state.get("active_step") == "create_block"
                or checkpoint.state.get("failed_step") == "create_block"
            ):
                checkpoint.mark_started("create_block_reconciliation")
        record = {
            "step": "create_block",
            "status": "reconciled",
            "block_locator": locator,
            "observed_snapshot": evidence.get("observed_snapshot"),
            "attempts": attempts or [{"attempt": max(1, gallery_create_attempts), "status": "reconciled_after_reread"}],
        }
        accept_step(record, stage="block_created")
        return record

    seeded_locator = str(getattr(args, "reconciled_created_locator", "") or "").strip()
    checkpoint_pending_create = bool(
        checkpoint
        and not checkpoint.gallery_locator
        and (
            checkpoint.state.get("active_step") == "create_block"
            or checkpoint.state.get("failed_step") == "create_block"
        )
    )
    if checkpoint_pending_create and not seeded_locator:
        reconciliation = query_create_reconciliation()
        if reconciliation.get("status") == "applied":
            seeded_locator = str(reconciliation.get("block_locator", "") or "").strip()
        elif reconciliation.get("status") != "not_applied" or gallery_create_attempts >= 2:
            error = PublishFailure("create_block", str(reconciliation.get("reason") or "previous Gallery create is unresolved"))
            error.safe_summary = "The previous Gallery create is unresolved; vertical publish is skipped"
            error.create_reconciliation = reconciliation
            error.safe_diagnostics = {
                "completed_stage": checkpoint.state.get("completed_stage", ""),
                "asset_ids": list(checkpoint.state.get("asset_ids", [])),
                "created_block_locator": "",
                "create_attempts": gallery_create_attempts,
                "steps": list(checkpoint.state.get("steps", [])),
            }
            raise error

    if seeded_locator:
        ctx["block_locator"] = seeded_locator
        if checkpoint:
            checkpoint.gallery_locator = seeded_locator
        reconciled_create_record(
            {"block_locator": seeded_locator, "observed_snapshot": getattr(args, "reconciled_create_snapshot", None)},
            attempts=getattr(args, "reconciled_create_attempts", None),
        )
    elif checkpoint and checkpoint.gallery_locator:
        ctx["block_locator"] = checkpoint.gallery_locator
    elif checkpoint and checkpoint.resume and checkpoint.state["completed_stage"]:
        error = PublishFailure("create_block", "checkpoint is missing its gallery locator")
        error.safe_summary = "The saved publish stage is missing its Gallery locator"
        checkpoint.fail(error, "create_block")
        raise error
    else:
        def call_create_once() -> dict[str, Any]:
            nonlocal gallery_create_attempts
            # Do not let a locator from an earlier, unverified response make a
            # later response look usable when it omitted its own locator.
            ctx.pop("block_locator", None)
            if checkpoint:
                try:
                    record = start_and_call("create_block", adapter["create_block"])
                finally:
                    gallery_create_attempts = max(gallery_create_attempts, int(checkpoint.state.get("create_attempts", 0)))
            else:
                gallery_create_attempts += 1
                record = start_and_call("create_block", adapter["create_block"])
            locator = record.get("block_locator") or ctx.get("block_locator")
            if execute and (not isinstance(locator, str) or not locator.strip()):
                error = PublishFailure("create_block", "Studio did not return a usable block locator")
                error.safe_summary = "Studio did not return a usable Gallery locator"
                mark_failure(error, "create_block")
                raise error
            return record

        create_attempt_history: list[dict[str, Any]] = []
        create_step: dict[str, Any] | None = None
        create_reconciler_available = callable(getattr(args, "reconcile_create_block", None))
        for attempt_number in range(1, 3):
            if gallery_create_attempts >= 2:
                error = PublishFailure("create_block", "Gallery create retry limit has already been reached")
                error.create_attempt_history = create_attempt_history
                mark_failure(error, "create_block")
                raise error
            try:
                response_step = call_create_once()
            except Exception as create_error:
                reconciliation = query_create_reconciliation()
                attempt = {"attempt": max(attempt_number, gallery_create_attempts), "status": "response_uncertain"}
                if isinstance(create_error, TemplateCallError):
                    attempt["http_status"] = create_error.status_code
                create_error.create_reconciliation = reconciliation
                if reconciliation.get("status") == "applied":
                    attempt["status"] = "reconciled_after_reread"
                    create_step = reconciled_create_record(reconciliation, [*create_attempt_history, attempt])
                    break
                if reconciliation.get("status") == "not_applied" and gallery_create_attempts < 2:
                    attempt["status"] = "not_applied_after_reconciliation"
                    create_attempt_history.append(attempt)
                    continue
                if reconciliation.get("status") == "not_applied":
                    attempt["status"] = "not_applied_after_reconciliation"
                else:
                    attempt["status"] = "unresolved_after_reconciliation"
                create_attempt_history.append(attempt)
                create_error.create_attempt_history = create_attempt_history
                raise create_error

            if execute and create_reconciler_available:
                reconciliation = query_create_reconciliation()
                response_locator = str(response_step.get("block_locator") or ctx.get("block_locator") or "").strip()
                attempt = {
                    "attempt": gallery_create_attempts,
                    "status": "response_ok",
                    "postcondition_status": reconciliation.get("status", "unverified"),
                }
                if isinstance(reconciliation.get("observed_snapshot"), dict):
                    attempt["observed_snapshot"] = reconciliation["observed_snapshot"]
                if reconciliation.get("status") == "applied":
                    observed_locator = str(reconciliation.get("block_locator", "") or "").strip()
                    if not response_locator or response_locator != observed_locator:
                        attempt["status"] = "locator_mismatch_after_reconciliation"
                        create_attempt_history.append(attempt)
                        error = PublishFailure(
                            "create_block",
                            "successful Gallery create response did not match the unique Gallery observed in the vertical",
                        )
                        error.create_reconciliation = reconciliation
                        error.create_attempt_history = create_attempt_history
                        mark_failure(error, "create_block")
                        raise error
                    create_attempt_history.append(attempt)
                    response_step["block_locator"] = response_locator
                    response_step["observed_snapshot"] = reconciliation.get("observed_snapshot")
                    response_step["status"] = "completed" if gallery_create_attempts == 1 else "retried"
                    response_step["attempts"] = create_attempt_history
                    create_step = response_step
                    break
                if reconciliation.get("status") == "not_applied" and gallery_create_attempts < 2:
                    attempt["status"] = "postcondition_missing_after_response"
                    create_attempt_history.append(attempt)
                    continue
                attempt["status"] = "unresolved_after_reconciliation"
                create_attempt_history.append(attempt)
                error = PublishFailure(
                    "create_block",
                    str(reconciliation.get("reason") or "successful Gallery create response could not be verified in the vertical"),
                )
                error.create_reconciliation = reconciliation
                error.create_attempt_history = create_attempt_history
                mark_failure(error, "create_block")
                raise error

            if gallery_create_attempts > 1:
                create_attempt_history.append({"attempt": max(attempt_number, gallery_create_attempts), "status": "response_ok"})
                response_step["status"] = "retried"
                response_step["attempts"] = create_attempt_history
            create_step = response_step
            break

        if create_step is None:
            error = PublishFailure("create_block", "Gallery create attempts ended without a verified result")
            error.create_attempt_history = create_attempt_history
            mark_failure(error, "create_block")
            raise error
        locator = create_step.get("block_locator") or ctx.get("block_locator")
        if create_step.get("status") != "reconciled":
            accept_step(create_step, stage="block_created")
        if isinstance(locator, str) and locator.strip():
            ctx["block_locator"] = locator.strip()
        elif not execute:
            ctx["block_locator"] = "__dry_run_created_imagesgallery__"
        if checkpoint and isinstance(locator, str) and locator.strip():
            checkpoint.gallery_locator = locator.strip()
            checkpoint.persist()

    stage_order = {
        "": 0,
        "block_created": 1,
        "images_uploaded": 2,
        "images_ordered": 3,
        "audio_prepared": 4,
        "audio_uploaded": 5,
        "audio_registered": 6,
        "audio_binding_saved": 7,
        "audio_bound": 8,
        "saved": 9,
    }

    def completed_rank() -> int:
        stage = checkpoint.state.get("completed_stage", "") if checkpoint else ""
        return stage_order.get(stage, 0)

    def valid_saved_images(value: Any) -> list[dict[str, Any]] | None:
        if not isinstance(value, list) or len(value) != len(manifest["items"]):
            return None
        saved: list[dict[str, Any]] = []
        ids: set[str] = set()
        for item, record in zip(manifest["items"], value):
            if not isinstance(record, dict):
                return None
            if record.get("page") != int(item["page"]) or record.get("image") != manifest_image_name(item):
                return None
            asset_id = record.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id or asset_id in ids:
                return None
            ids.add(asset_id)
            saved.append({"page": int(item["page"]), "image": manifest_image_name(item), "asset_id": asset_id})
        return saved

    interrupted_order = bool(
        checkpoint
        and (
            checkpoint.state.get("active_step") == "change_files_order"
            or checkpoint.state.get("failed_step") == "change_files_order"
        )
    )
    checkpoint_images = valid_saved_images(checkpoint.state.get("images")) if checkpoint else None
    image_started = bool(checkpoint and (checkpoint.state["images_started"] or checkpoint.state["images"]))
    images = checkpoint_images or []
    if checkpoint and image_started and checkpoint_images is None:
        images = run_reconciliation("resume_image_reconciliation")
        if checkpoint:
            checkpoint.set_stage("images_ordered" if interrupted_order else "images_uploaded")
    elif checkpoint and checkpoint_images and completed_rank() < stage_order["images_uploaded"]:
        checkpoint.set_stage("images_uploaded")

    if checkpoint and checkpoint.state["status"] == "completed" and checkpoint.state["completed_stage"] == "saved":
        images = checkpoint_images or []
    else:
        if not images:
            for idx, item in enumerate(manifest["items"], start=1):
                page_image = manifest_image_name(item)
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
                image_step = start_and_call(f"upload_image[{idx}]", adapter["upload_image"], page_image=page_image)
                asset_id = str(image_step.get("asset_id", "")).strip()
                if not execute:
                    accept_step(image_step, page_image=page_image)
                    continue
                if not asset_id:
                    accept_step(image_step, page_image=page_image)
                    images = run_reconciliation(f"upload_image[{idx}]", page_image=page_image)
                    break
                if any(saved["asset_id"] == asset_id for saved in images):
                    error = PublishFailure(f"upload_image[{idx}]", "Studio returned a duplicate image asset ID")
                    error.safe_summary = f"Studio returned a duplicate asset ID for {page_image}"
                    mark_failure(error, f"upload_image[{idx}]", page_image=page_image)
                    raise error
                images.append({"page": int(item["page"]), "image": page_image, "asset_id": asset_id})
                accept_step(image_step, page_image=page_image, images=images)
            if execute and len(images) != len(manifest["items"]):
                error = PublishFailure("upload_image", "not every manifest image was confirmed")
                error.safe_summary = "Not every manifest image has a confirmed Studio asset ID"
                mark_failure(error, "upload_image")
                raise error
            if checkpoint and completed_rank() < stage_order["images_uploaded"]:
                checkpoint.set_stage("images_uploaded")

    ctx["uploaded_asset_ids"] = [image["asset_id"] for image in images]
    if not checkpoint or completed_rank() < stage_order["images_ordered"]:
        if "change_files_order" in adapter:
            if interrupted_order and execute:
                try:
                    remote_asset_ids, reconciliation_step = read_gallery_asset_order(
                        session,
                        studio_base=studio_base,
                        gallery_locator=str(ctx.get("block_locator", "")),
                        block_url=canonical_block_url,
                        context=ctx,
                        expected_count=len(images),
                        timeout=args.timeout,
                        refresh_auth_fn=refresh_auth_session if can_refresh_auth else None,
                        auth_retry_max_retries=normalize_auth_retry_max_retries(
                            getattr(args, "auth_retry_max_retries", DEFAULT_AUTH_RETRY_MAX_RETRIES)
                        ),
                        auth_retry_delay_seconds=max(0.0, float(args.auth_retry_delay_seconds)),
                    )
                    saved_asset_ids = [image["asset_id"] for image in images]
                    if set(remote_asset_ids) != set(saved_asset_ids):
                        error = PublishFailure(
                            "change_files_order_reconciliation",
                            "Studio get_files assets did not match the saved image IDs",
                        )
                        error.safe_summary = "Studio get_files did not confirm the saved Gallery images; image ordering was not retried"
                        raise error
                except Exception as exc:
                    logs.append(
                        {
                            "step": "change_files_order_reconciliation",
                            "status": "failed",
                            "http_status": exc.status_code if isinstance(exc, TemplateCallError) else None,
                        }
                    )
                    mark_failure(exc, "change_files_order")
                    if isinstance(exc, Exception):
                        exc.step_name = "change_files_order"
                    raise

                if remote_asset_ids == saved_asset_ids:
                    accept_step(reconciliation_step, stage="images_ordered")
                else:
                    order_step = start_and_call("change_files_order", adapter["change_files_order"])
                    accept_step(order_step, stage="images_ordered")
            else:
                order_step = start_and_call("change_files_order", adapter["change_files_order"])
                accept_step(order_step, stage="images_ordered")
        elif checkpoint:
            checkpoint.set_stage("images_ordered")

    rank = completed_rank()
    if "upload_audio_prepare" in adapter and rank < stage_order["audio_uploaded"]:
        prepare_step = start_and_call("upload_audio_prepare", adapter["upload_audio_prepare"])
        accept_step(prepare_step)
        file_key = ctx.get("file_key")
        if execute and (not isinstance(file_key, str) or not file_key.strip()):
            error = PublishFailure("upload_audio_prepare", "Studio returned no usable file_key")
            error.safe_summary = "Studio audio preparation did not return a usable file_key"
            mark_failure(error, "upload_audio_prepare")
            raise error
        if execute and not ctx.get("edx_document_id"):
            ctx["edx_document_id"] = maybe_extract_uuid(file_key)
        if ctx.get("edx_document_id"):
            ctx["audio_document_id"] = f"{ctx['edx_document_id']}.{ctx['audio_post_fix']}"
        accept_step(prepare_step, stage="audio_prepared", audio_document_id=str(ctx.get("edx_document_id", "")))

        if execute and not args.skip_oss_multipart:
            if checkpoint:
                checkpoint.mark_started("upload_audio_multipart")
            try:
                multipart_result = sts_multipart_upload_audio(ctx)
            except Exception as exc:
                mark_failure(exc, "upload_audio_multipart")
                raise
            multipart_step = {"step": "upload_audio_multipart", "status": "ok", "result": {"parts": multipart_result["parts"], "size": multipart_result["size"]}}
            logs.append(multipart_step)
            if checkpoint:
                checkpoint.record_success(multipart_step, stage="audio_uploaded", audio_document_id=str(ctx.get("edx_document_id", "")))
            ctx["audio_object_key"] = multipart_result["object_key"]
        elif not execute:
            logs.append({"step": "upload_audio_multipart", "status": "dry-run", "note": "execute mode uploads audio through OSS multipart"})
        else:
            skipped_step = {"step": "upload_audio_multipart", "status": "skipped", "note": "OSS multipart upload was skipped"}
            logs.append(skipped_step)
            if checkpoint:
                checkpoint.record_success(skipped_step, stage="audio_uploaded", audio_document_id=str(ctx.get("edx_document_id", "")))

    if checkpoint and completed_rank() >= stage_order["audio_uploaded"] and checkpoint.state.get("audio_document_id"):
        ctx["edx_document_id"] = checkpoint.state["audio_document_id"]
        ctx["audio_document_id"] = f"{ctx['edx_document_id']}.{ctx['audio_post_fix']}"

    if "upload_audio" in adapter and completed_rank() < stage_order["audio_uploaded"]:
        audio_step = start_and_call("upload_audio", adapter["upload_audio"])
        accept_step(audio_step, stage="audio_uploaded", audio_document_id=str(ctx.get("edx_document_id", "")))

    if "upload_audio_register" in adapter and completed_rank() < stage_order["audio_registered"]:
        register_step = start_and_call("upload_audio_register", adapter["upload_audio_register"])
        accept_step(register_step, stage="audio_registered", audio_document_id=str(ctx.get("edx_document_id", "")))

    if "save_audio_binding" in adapter and completed_rank() < stage_order["audio_binding_saved"]:
        binding_step = start_and_call("save_audio_binding", adapter["save_audio_binding"])
        accept_step(binding_step, stage="audio_binding_saved", audio_document_id=str(ctx.get("edx_document_id", "")))

    if "submit_studio_edits" in adapter and completed_rank() < stage_order["audio_bound"]:
        edits_step = start_and_call("submit_studio_edits", adapter["submit_studio_edits"])
        accept_step(edits_step, stage="audio_bound", audio_document_id=str(ctx.get("edx_document_id", "")))

    if completed_rank() < stage_order["saved"]:
        save_step = start_and_call("save_block", adapter["save_block"])
        accept_step(save_step, stage="saved", audio_document_id=str(ctx.get("edx_document_id", "")))
        if checkpoint:
            checkpoint.complete()

    target_vertical_url = build_container_block_url(studio_base, vertical_block_id)
    result_block_locator = str(ctx.get("block_locator", "")) if execute else ""
    created_imagegallery_block_url = build_container_block_url(studio_base, result_block_locator) if result_block_locator else ""
    result = {
        "mode": "execute" if execute else "dry-run",
        "studio_base": studio_base,
        "courses_base": courses_base,
        "vertical_block_id": vertical_block_id,
        "target_vertical_url": target_vertical_url,
        "block_locator": result_block_locator,
        "created_imagegallery_block_url": created_imagegallery_block_url,
        "created_block_url": created_imagegallery_block_url,
        "asset_ids": list(ctx.get("uploaded_asset_ids", [])),
        "create_attempts": gallery_create_attempts,
        "auth_retry_policy": {
            "enabled": can_refresh_auth,
            "max_retries": normalize_auth_retry_max_retries(getattr(args, "auth_retry_max_retries", DEFAULT_AUTH_RETRY_MAX_RETRIES)),
            "delay_seconds": max(0.0, float(args.auth_retry_delay_seconds)),
        },
        "steps": logs,
    }
    if checkpoint:
        result["gallery_key"] = gallery_key
        result["completed_stage"] = checkpoint.state["completed_stage"]
        result["resumed"] = checkpoint.resume
    return result


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
    original_snapshot = replacement_plan.get("initial_snapshot")

    requested_vertical_url = build_container_block_url(studio_base, str(replacement_plan.get("target_vertical_block_id", ""))) or studio_url
    live_plan, live_step = maybe_fetch_live_replacement_plan(
        studio_url=requested_vertical_url,
        source_vertical_block_id=str(replacement_plan.get("source_vertical_block_id", "")),
        target_vertical_block_id=str(replacement_plan.get("target_vertical_block_id", "")),
        block_type=str(replacement_plan.get("resolved_block_type", "") or ""),
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
        if publish_only:
            live_plan["result_type"] = replacement_plan.get("result_type", live_plan.get("result_type", "replaced"))
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
    baseline_snapshot = original_snapshot
    if not isinstance(baseline_snapshot, dict) and not execute:
        dry_run_children = [str(child).strip() for child in current_children if str(child).strip()]
        baseline_snapshot = {
            "direct_child_count": len(dry_run_children),
            "children": [
                {
                    "locator": locator,
                    "type": (re.search(r"\+type@([^+]+)\+block@", locator).group(1) if re.search(r"\+type@([^+]+)\+block@", locator) else "unknown"),
                    "order": index,
                }
                for index, locator in enumerate(dry_run_children, start=1)
            ],
        }
    if not isinstance(baseline_snapshot, dict):
        raise PublishFailure("replacement_validation", "original vertical snapshot is missing; refusing to publish without a baseline")
    baseline_children = [dict(child) for child in baseline_snapshot.get("children", []) if isinstance(child, dict)]
    post_create_snapshot: dict[str, Any] = {}
    move_snapshot: dict[str, Any] = {}
    final_snapshot: dict[str, Any] = {}

    def vertical_evidence() -> dict[str, Any]:
        return {
            "block_type": replacement_plan.get("resolved_block_type", ""),
            "planned_result_type": replacement_plan.get("result_type", ""),
            "vertical_snapshot_before": baseline_snapshot,
            "vertical_snapshot_after_create": post_create_snapshot,
            "expected_vertical_snapshot_after_move": move_snapshot,
            "expected_vertical_snapshot_after_delete": final_snapshot,
        }

    if publish_only:
        post_create_snapshot = (
            live_plan.get("initial_snapshot")
            if isinstance(live_plan, dict)
            else replacement_plan.get("vertical_snapshot_after_create")
        )
        if not isinstance(post_create_snapshot, dict):
            raise PublishFailure("replacement_validation", "saved vertical snapshot is missing for publish recovery")
        desired_children = [str(child.get("locator", "")) for child in post_create_snapshot.get("children", [])]
        move_snapshot = post_create_snapshot
        final_snapshot = post_create_snapshot
    elif execute:
        expected_create_count = int(baseline_snapshot.get("direct_child_count", len(baseline_children))) + 1
        post_create_snapshot = live_plan.get("initial_snapshot") if isinstance(live_plan, dict) else None
        created_rows = [
            child for child in (post_create_snapshot or {}).get("children", [])
            if isinstance(child, dict) and child.get("locator") == created_block_locator
        ]
        unchanged_original_rows = [
            dict(child) for child in (post_create_snapshot or {}).get("children", [])
            if isinstance(child, dict) and child.get("locator") != created_block_locator
        ]
        baseline_identity_order = [
            (str(child.get("locator", "")), str(child.get("type", "")).lower())
            for child in baseline_children
        ]
        observed_identity_order = [
            (str(child.get("locator", "")), str(child.get("type", "")).lower())
            for child in unchanged_original_rows
        ]
        if (
            not isinstance(post_create_snapshot, dict)
            or post_create_snapshot.get("direct_child_count") != expected_create_count
            or len(created_rows) != 1
            or created_rows[0].get("type") != "imagesgallery"
            or observed_identity_order != baseline_identity_order
        ):
            steps.append({
                "step": "verify_vertical_after_create",
                "status": "failed",
                "expected_snapshot": baseline_snapshot,
                "observed_snapshot": post_create_snapshot,
            })
            steps.append({"step": "publish_vertical", "status": "skipped", "reason": "create structure did not match the original snapshot plus one Gallery"})
            failure = PublishFailure("verify_vertical_after_create", "Studio vertical structure did not match the original snapshot plus the created Gallery")
            failure.steps = steps.copy()
            failure.vertical_evidence = vertical_evidence()
            raise failure
        steps.append({
            "step": "verify_vertical_after_create",
            "status": "completed",
            "expected_direct_child_count": expected_create_count,
            "observed_snapshot": post_create_snapshot,
        })
        try:
            move_snapshot = replace_snapshot_child(
                baseline_snapshot,
                old_locator=matched_source_block_locator,
                new_locator=created_block_locator,
            )
            final_snapshot = replacement_after_delete_snapshot(
                move_snapshot,
                old_locator=matched_source_block_locator,
                new_locator=created_block_locator,
            )
        except PublishFailure as exc:
            steps.append({"step": "verify_vertical_after_create", "status": "failed", "reason": safe_error_summary(exc)})
            steps.append({"step": "publish_vertical", "status": "skipped", "reason": "replacement snapshot could not be planned"})
            exc.steps = steps.copy()
            exc.vertical_evidence = vertical_evidence()
            raise
        desired_children = [str(child.get("locator", "")) for child in move_snapshot["children"]]
    else:
        post_create_snapshot = {
            "direct_child_count": len(baseline_children) + 1,
            "children": [*baseline_children, {"locator": created_block_locator, "type": "imagesgallery", "order": len(baseline_children) + 1}],
        }
        move_snapshot = replace_snapshot_child(
            baseline_snapshot,
            old_locator=matched_source_block_locator,
            new_locator=created_block_locator,
        )
        final_snapshot = replacement_after_delete_snapshot(
            move_snapshot,
            old_locator=matched_source_block_locator,
            new_locator=created_block_locator,
        )
        desired_children = [str(child.get("locator", "")) for child in move_snapshot["children"]]

    ctx["vertical_children_obj"] = desired_children
    operator_vertical_url, requested_vertical_url = operator_vertical_urls(studio_base, replacement_plan, studio_url)

    def read_current_state() -> tuple[dict[str, Any], Any]:
        try:
            return fetch_vertical_state(
                session,
                studio_base=studio_base,
                vertical_locator=working_vertical_block_id,
                timeout=timeout,
            )
        except PublishFailure:
            raise
        except Exception as exc:
            raise PublishFailure("verify_vertical_structure", "Studio vertical structure could not be reread") from exc

    def read_before_mutation(step_name: str) -> tuple[dict[str, Any], Any]:
        try:
            return read_current_state()
        except PublishFailure as exc:
            steps.append({"step": step_name, "status": "failed", "reason": safe_error_summary(exc)})
            steps.append({"step": "publish_vertical", "status": "skipped", "reason": "vertical structure could not be verified before publishing"})
            exc.steps = steps.copy()
            exc.vertical_evidence = vertical_evidence()
            raise

    def call_optional_step(
        step_name: str,
        *,
        before_snapshot: dict[str, Any],
        expected_snapshot: dict[str, Any],
        delete_locator: str = "",
        require_published: bool = False,
    ) -> dict[str, Any]:
        tmpl = adapter.get(step_name)
        if not tmpl:
            exc = PublishFailure(step_name, f"adapter missing required step: {step_name}")
            exc.steps = steps.copy()
            raise exc
        if delete_locator:
            ctx["delete_block_locator"] = delete_locator
        attempts: list[dict[str, Any]] = []
        current_expected = before_snapshot
        for attempt_number in range(1, 3):
            write_error: TemplateCallError | None = None
            try:
                record = call_template(session, step_name, tmpl, ctx, execute, timeout)
            except TemplateCallError as exc:
                write_error = exc
                record = {"step": step_name, "reason": safe_error_summary(exc)}
            if not execute:
                record["status"] = "ok"
                steps.append(record)
                return expected_snapshot
            try:
                observed_snapshot, published_state = read_current_state()
            except PublishFailure as exc:
                action = {
                    "step": step_name,
                    "status": "blocked",
                    "attempt": attempt_number,
                    "reason": safe_error_summary(exc),
                    "expected_snapshot": expected_snapshot,
                }
                attempts.append(action)
                break
            postcondition = (
                published_state is True
                if require_published
                else observed_snapshot == expected_snapshot
            )
            if postcondition:
                status = "completed" if write_error is None and attempt_number == 1 else ("reconciled" if write_error is not None and attempt_number == 1 else "retried")
                action = {
                    "step": step_name,
                    "status": status,
                    "attempts": attempts + [{"attempt": attempt_number, "status": "response_uncertain" if write_error else "response_ok"}],
                    "expected_snapshot": expected_snapshot,
                    "observed_snapshot": observed_snapshot,
                }
                if require_published:
                    action["published"] = published_state
                if delete_locator:
                    action["block_locator"] = delete_locator
                steps.append(action)
                return observed_snapshot
            state_unchanged = observed_snapshot == current_expected
            if state_unchanged and attempt_number == 1:
                attempts.append({
                    "attempt": attempt_number,
                    "status": "not_applied_after_reconciliation" if write_error is not None else "postcondition_missing_after_response",
                    "observed_snapshot": observed_snapshot,
                })
                current_expected = observed_snapshot
                continue
            status = "failed" if state_unchanged else "blocked"
            action = {
                "step": step_name,
                "status": status,
                "attempts": attempts + [{"attempt": attempt_number, "status": "response_uncertain" if write_error else "response_ok"}],
                "reason": safe_error_summary(write_error) if write_error else "Studio structure did not match the planned postcondition",
                "expected_snapshot": expected_snapshot,
                "observed_snapshot": observed_snapshot,
            }
            if require_published:
                action["published"] = published_state
            if delete_locator:
                action["block_locator"] = delete_locator
            steps.append(action)
            break
        if step_name != "publish_vertical":
            steps.append({"step": "publish_vertical", "status": "skipped", "reason": f"{step_name} did not reach its verified postcondition"})
        failure = PublishFailure(step_name, f"{step_name} did not reach its verified postcondition")
        failure.steps = steps.copy()
        failure.vertical_evidence = vertical_evidence()
        raise failure

    if execute:
        if not publish_only and [str(child.get("locator", "")) for child in post_create_snapshot["children"]] != desired_children:
            current_snapshot, _published = read_before_mutation("verify_vertical_before_move")
            call_optional_step(
                "reorder_children",
                before_snapshot=current_snapshot,
                expected_snapshot=move_snapshot,
            )
            steps.append({"step": "verify_vertical_after_move", "status": "completed", "expected_snapshot": move_snapshot})
        elif not publish_only:
            steps.append({"step": "verify_vertical_after_move", "status": "completed", "expected_snapshot": move_snapshot})
        if not publish_only and replacement_plan.get("result_type") == "replaced" and matched_source_block_locator:
            current_snapshot, _published = read_before_mutation("verify_vertical_before_delete")
            call_optional_step(
                "delete_block",
                before_snapshot=current_snapshot,
                expected_snapshot=final_snapshot,
                delete_locator=matched_source_block_locator,
            )
            steps.append({"step": "verify_vertical_after_delete", "status": "completed", "expected_snapshot": final_snapshot})
        current_snapshot, _published = read_before_mutation("verify_vertical_before_publish")
        if current_snapshot != final_snapshot:
            steps.append({"step": "verify_vertical_before_publish", "status": "failed", "expected_snapshot": final_snapshot, "observed_snapshot": current_snapshot})
            steps.append({"step": "publish_vertical", "status": "skipped", "reason": "pre-publish vertical structure did not match the planned replacement"})
            failure = PublishFailure("verify_vertical_before_publish", "pre-publish vertical structure did not match the planned replacement")
            failure.steps = steps.copy()
            failure.vertical_evidence = vertical_evidence()
            raise failure
        steps.append({"step": "verify_vertical_before_publish", "status": "completed", "expected_snapshot": final_snapshot, "observed_snapshot": current_snapshot})
        call_optional_step(
            "publish_vertical",
            before_snapshot=current_snapshot,
            expected_snapshot=final_snapshot,
            require_published=True,
        )
    else:
        if not publish_only and desired_children != [*current_children, created_block_locator]:
            call_optional_step("reorder_children", before_snapshot={}, expected_snapshot={})

        if not publish_only and replacement_plan.get("result_type") == "replaced" and matched_source_block_locator:
            call_optional_step("delete_block", before_snapshot={}, expected_snapshot={}, delete_locator=matched_source_block_locator)

        call_optional_step("publish_vertical", before_snapshot={}, expected_snapshot={})

    summary_cleanup = {"status": "preserved"}

    result = {
        "status": str(replacement_plan.get("result_type", "fallback_inserted")),
        "target_vertical_block_id": str(replacement_plan.get("target_vertical_block_id", "")),
        "working_vertical_block_id": working_vertical_block_id,
        "target_vertical_url": operator_vertical_url,
        "requested_target_vertical_url": requested_vertical_url,
        "working_vertical_url": working_vertical_url,
        "created_block_locator": created_block_locator,
        "create_attempts": single_publish_result.get("create_attempts", 0),
        "created_imagegallery_block_url": build_container_block_url(studio_base, created_block_locator),
        "existing_gallery_block_locator": str(replacement_plan.get("existing_gallery_block_locator", "")),
        "existing_gallery_block_url": build_container_block_url(studio_base, str(replacement_plan.get("existing_gallery_block_locator", ""))),
        "matched_source_block_locator": matched_source_block_locator,
        "matched_source_block_url": build_container_block_url(studio_base, matched_source_block_locator),
        "matched_summary_block_locator": "",
        "matched_summary_block_url": "",
        "matched_summary_component_name": "",
        "summary_cleanup": summary_cleanup,
        "note": (
            "replaced existing source block in place" if replacement_plan.get("result_type") == "replaced" else
            "inserted new imagesgallery at the top of the target vertical"
        ),
        "course_kind": replacement_plan.get("course_kind", ""),
        "match_scope": replacement_plan.get("match_scope", ""),
        "block_type": replacement_plan.get("resolved_block_type", ""),
        "vertical_snapshot_before": baseline_snapshot,
        "vertical_snapshot_after_create": post_create_snapshot,
        "expected_vertical_snapshot_after_move": move_snapshot,
        "expected_vertical_snapshot_after_delete": final_snapshot,
        "vertical_published": True if execute else None,
        "plan_source": replacement_plan.get("plan_source", ""),
        "requires_manual_followup": False,
        "steps": steps,
        **summarize_steps(steps),
    }
    if publish_only:
        result["note"] = "reused the previously created imagesgallery block and retried the final vertical publish"
    return result


def _process_publish_target_impl(
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
    if bool(target.get("cleanup_only", False)) or bool(getattr(args, "cleanup_only", False)):
        return unsupported_cleanup_only_result(
            target_vertical_block_id=target_vertical_block_id,
            target_vertical_url=studio_vertical_url,
        )

    summary_cleanup_info = {"status": "preserved"}
    manifest_path = Path(str(target.get("manifest", ""))).expanduser().resolve()
    try:
        source_metadata = read_json(manifest_path)
        if not isinstance(source_metadata, dict):
            raise ValueError("gallery manifest must be an object")
    except Exception:
        return {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "target_vertical_url": studio_vertical_url,
            "failed_step": "resolve_source_block_type",
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": "Gallery manifest source metadata could not be read",
            "steps": [{"step": "resolve_source_block_type", "status": "failed", "reason": "Gallery manifest source metadata could not be read"}],
        }
    leaking_summary_pages = manifest_summary_leaks(source_metadata)
    if leaking_summary_pages:
        reason = "Gallery page or speech input contains a standalone 本节要点 summary"
        return {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "target_vertical_url": studio_vertical_url,
            "failed_step": "stage_b_output_contract",
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": reason,
            "leaking_summary_pages": leaking_summary_pages,
            "steps": [
                {"step": "stage_b_output_contract", "status": "failed", "leaking_summary_pages": leaking_summary_pages, "reason": reason},
                {"step": "publish_vertical", "status": "skipped", "reason": "summary content must be removed before publishing"},
            ],
            **summarize_steps([
                {"step": "stage_b_output_contract", "status": "failed"},
                {"step": "publish_vertical", "status": "skipped"},
            ]),
        }
    type_resolution = resolve_source_block_type(
        source_metadata,
        target,
        manifest_path=manifest_path,
        section_list_json=str(target.get("section_list_json", "") or ""),
    )
    if type_resolution["status"] != "resolved":
        step = {
            "step": "resolve_source_block_type",
            "status": "failed",
            "source_type_status": type_resolution["status"],
            "reason": type_resolution["reason"],
        }
        return {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "target_vertical_url": studio_vertical_url,
            "failed_step": "resolve_source_block_type",
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": type_resolution["reason"],
            "block_type_resolution": {"status": type_resolution["status"]},
            "steps": [step],
        }
    resolved_block_type = type_resolution["block_type"]
    target["block_type"] = resolved_block_type
    operation_record = operation_gallery_checkpoint(args)
    operation_create_pending = pending_create_checkpoint(args)
    previous_create_pending = bool(
        isinstance(previous_result, dict)
        and str(previous_result.get("failed_step", "")) == "create_block"
        and not str(previous_result.get("created_block_locator", "") or "").strip()
    )
    create_is_pending = previous_create_pending or operation_create_pending is not None
    saved_create_snapshot = (
        (previous_result or {}).get("vertical_snapshot_before")
        if previous_create_pending
        else (operation_create_pending or {}).get("before")
    )
    saved_integrity_baseline: dict[str, Any] | None = None
    saved_integrity_match = ""
    if operation_record is not None:
        _operation_path, _operation, operation_gallery = operation_record
        saved_integrity = operation_gallery.get("vertical_integrity") if isinstance(operation_gallery.get("vertical_integrity"), dict) else {}
        candidate_baseline = saved_integrity.get("before") or saved_integrity.get("vertical_snapshot_before")
        if isinstance(candidate_baseline, dict) and saved_integrity.get("status") != "verified":
            saved_integrity_baseline = candidate_baseline
            saved_integrity_match = str(saved_integrity.get("matched_source_block_locator", "") or "").strip()
    if not isinstance(saved_create_snapshot, dict) and saved_integrity_baseline is not None:
        saved_create_snapshot = saved_integrity_baseline
    create_attempts = 0
    if previous_create_pending:
        value = (previous_result or {}).get("create_attempts", 0)
        create_attempts = value if isinstance(value, int) and not isinstance(value, bool) else 0
    elif operation_create_pending is not None:
        create_attempts = int(operation_create_pending.get("create_attempts", 0))
    saved_matched_source = str(
        (previous_result or {}).get("matched_source_block_locator", "")
        or (operation_create_pending or {}).get("matched_source_block_locator", "")
        or saved_integrity_match
    ).strip()

    replacement_plan = mark_replacement_plan_source(
        plan_publish_replacement(
            course_structure,
            source_vertical_block_id=source_vertical_block_id,
            target_vertical_block_id=target_vertical_block_id,
            block_type=resolved_block_type,
        ),
        "course_structure_json",
    )
    live_plan, live_step = maybe_fetch_live_replacement_plan(
        studio_url=studio_vertical_url,
        source_vertical_block_id=source_vertical_block_id,
        target_vertical_block_id=target_vertical_block_id,
        block_type=resolved_block_type,
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
    replacement_plan["resolved_block_type"] = resolved_block_type
    original_replacement_plan = dict(replacement_plan)
    creation_baseline = (
        saved_create_snapshot
        if isinstance(saved_create_snapshot, dict) and (create_is_pending or saved_integrity_baseline is not None)
        else replacement_plan.get("initial_snapshot")
    )
    reconciled_create_locator = ""
    reconciled_create_snapshot: dict[str, Any] | None = None
    if create_is_pending:
        observed_snapshot = replacement_plan.get("initial_snapshot")
        if not isinstance(creation_baseline, dict) or not isinstance(observed_snapshot, dict):
            reconciliation = {"status": "unverified", "reason": "saved or observed pre-create vertical snapshot is unavailable"}
        else:
            reconciliation = reconcile_created_gallery_snapshot(creation_baseline, observed_snapshot)
        if reconciliation["status"] == "applied":
            reconciled_create_locator = str(reconciliation.get("block_locator", ""))
            reconciled_create_snapshot = observed_snapshot
            replacement_plan["initial_snapshot"] = creation_baseline
            replacement_plan["planning_issue"] = None
            replacement_plan["matched_source_block_locator"] = saved_matched_source or original_replacement_plan.get("matched_source_block_locator", "")
            replacement_plan["matched_source_block_category"] = "imagesgallery" if resolved_block_type == "imagesgallery" else "html"
            replacement_plan["result_type"] = str(
                (previous_result or {}).get("planned_result_type")
                or (
                    (previous_result or {}).get("status")
                    if (previous_result or {}).get("status") in {"replaced", "fallback_inserted"}
                    else "replaced"
                )
                or "replaced"
            )
            replacement_plan["current_children"] = [
                str(child.get("locator", "")) for child in observed_snapshot.get("children", [])
            ]
            create_attempts = max(1, create_attempts)
        elif reconciliation["status"] == "not_applied" and create_attempts < 2:
            replacement_plan["initial_snapshot"] = creation_baseline
        else:
            reason = reconciliation.get("reason") or "the uncertain Gallery create could not be reconciled safely"
            blocked_steps = [
                {
                    "step": "create_block",
                    "status": "blocked",
                    "create_attempts": create_attempts,
                    "reason": reason,
                    "expected_snapshot": creation_baseline,
                    "observed_snapshot": observed_snapshot,
                },
                {"step": "publish_vertical", "status": "skipped", "reason": "create outcome is unresolved"},
            ]
            return {
                "status": "failed",
                "target_vertical_block_id": target_vertical_block_id,
                "target_vertical_url": studio_vertical_url,
                "working_vertical_block_id": str(replacement_plan.get("working_vertical_block_id", "")),
                "failed_step": "create_block",
                "failure_bucket": "manual_followup",
                "requires_manual_followup": True,
                "note": reason,
                "block_type": resolved_block_type,
                "planned_result_type": str((previous_result or {}).get("planned_result_type") or "replaced"),
                "matched_source_block_locator": saved_matched_source,
                "vertical_snapshot_before": creation_baseline,
                "vertical_snapshot_after_create": observed_snapshot,
                "vertical_published": None,
                "create_attempts": create_attempts,
                "steps": blocked_steps,
                **summarize_steps(blocked_steps),
            }
    else:
        creation_baseline = replacement_plan.get("initial_snapshot")
    rerun_action = resolve_rerun_action(
        previous_result,
        current_structure_block_ids=list(replacement_plan.get("all_structure_block_ids", [])),
    )
    if rerun_action["action"] == "resume_publish_only":
        replacement_plan["planning_issue"] = None
        replacement_plan["matched_source_block_locator"] = ""
        replacement_plan["result_type"] = str((previous_result or {}).get("planned_result_type") or "replaced")
        if isinstance((previous_result or {}).get("vertical_snapshot_before"), dict):
            replacement_plan["initial_snapshot"] = previous_result["vertical_snapshot_before"]
        replacement_plan["vertical_snapshot_after_create"] = (previous_result or {}).get("vertical_snapshot_after_create")
    if replacement_plan.get("planning_issue") and rerun_action["action"] != "resume_publish_only":
        issue = replacement_plan["planning_issue"]
        step = {
            "step": "resolve_replacement_target",
            "status": "failed",
            "block_type": resolved_block_type,
            "reason": issue.get("reason", "replacement target could not be uniquely resolved"),
        }
        return {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "target_vertical_url": studio_vertical_url,
            "failed_step": "resolve_replacement_target",
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": step["reason"],
            "block_type_resolution": {"status": "resolved", "block_type": resolved_block_type},
            "steps": [step],
        }
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
                "requires_manual_followup": bool((previous_result or {}).get("requires_manual_followup", False)),
                "note": rerun_action["reason"],
                "matched_summary_block_locator": "",
                "matched_summary_block_url": "",
                "matched_summary_component_name": "",
                "summary_cleanup": summary_cleanup_info,
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
                "note": safe_error_summary(exc),
                "steps": preflight_steps,
            }

    if rerun_action["action"] == "resume_publish_only":
        saved_snapshot = (previous_result or {}).get("vertical_snapshot_before")
        if isinstance(saved_snapshot, dict):
            replacement_plan["initial_snapshot"] = saved_snapshot
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
                "result_type": str(
                    (previous_result or {}).get("planned_result_type")
                    or (previous_result or {}).get("status")
                    or replacement_plan.get("result_type", "replaced")
                ),
            },
            studio_url=studio_vertical_url,
            execute=bool(getattr(args, "execute", False)),
            timeout=int(getattr(args, "timeout", 30)),
            args=args,
            publish_only=True,
        )
        resumed["resumed_from_previous_run"] = True
        resumed["summary_cleanup"] = summary_cleanup_info
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
    if not isinstance(creation_baseline, dict):
        return {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "target_vertical_url": studio_vertical_url,
            "failed_step": "create_block",
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": "original vertical snapshot is missing; Gallery creation is blocked",
            "block_type": resolved_block_type,
            "matched_source_block_locator": str(replacement_plan.get("matched_source_block_locator", "")),
            "steps": [
                {"step": "create_block", "status": "blocked", "reason": "original vertical snapshot is missing"},
                {"step": "publish_vertical", "status": "skipped", "reason": "pre-create vertical baseline is unavailable"},
            ],
        }
    persist_vertical_integrity_baseline(
        args,
        block_type=resolved_block_type,
        snapshot=creation_baseline,
        matched_source_block_locator=str(replacement_plan.get("matched_source_block_locator", "")),
    )

    def reconcile_create_block() -> dict[str, Any]:
        observed_plan, _read_step = maybe_fetch_live_replacement_plan(
            studio_url=studio_vertical_url,
            source_vertical_block_id=source_vertical_block_id,
            target_vertical_block_id=target_vertical_block_id,
            block_type=resolved_block_type,
            args=args,
            timeout=int(getattr(args, "timeout", 30)),
        )
        if observed_plan is None or not isinstance(observed_plan.get("initial_snapshot"), dict):
            return {"status": "unverified", "reason": "Studio vertical could not be reread after Gallery creation"}
        return reconcile_created_gallery_snapshot(creation_baseline, observed_plan["initial_snapshot"])

    publish_args.reconcile_create_block = reconcile_create_block
    publish_args.create_attempts = create_attempts
    publish_args.reconciled_created_locator = reconciled_create_locator
    if reconciled_create_locator:
        publish_args.reconciled_create_snapshot = reconciled_create_snapshot
    try:
        single_publish_result = run_single_publish(publish_args)
    except Exception as exc:
        diagnostics = getattr(exc, "safe_diagnostics", None)
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        reconciliation = getattr(exc, "create_reconciliation", None)
        reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
        create_attempt_history = getattr(exc, "create_attempt_history", None)
        create_attempt_history = create_attempt_history if isinstance(create_attempt_history, list) else []
        failure_steps = list(diagnostics.get("steps", []))
        failure_steps.append(
            {
                "step": str(getattr(exc, "step_name", "create_block")),
                "status": "failed" if str(getattr(exc, "step_name", "create_block")) == "create_block" else "blocked",
                "create_attempts": diagnostics.get("create_attempts", create_attempts),
                "attempts": create_attempt_history,
                "observed_snapshot": reconciliation.get("observed_snapshot"),
                "reason": safe_error_summary(exc),
            }
        )
        failure_snapshot = reconciliation.get("observed_snapshot")
        created_locator = str(diagnostics.get("created_block_locator", "") or "")
        if not isinstance(failure_snapshot, dict) and created_locator:
            post_create_reconciliation = reconcile_create_block()
            if post_create_reconciliation.get("status") == "applied" and post_create_reconciliation.get("block_locator") == created_locator:
                failure_snapshot = post_create_reconciliation.get("observed_snapshot")
                failure_steps.append({
                    "step": "verify_vertical_after_create",
                    "status": "completed",
                    "observed_snapshot": failure_snapshot,
                })
            else:
                failure_steps.append({
                    "step": "verify_vertical_after_create",
                    "status": "blocked",
                    "observed_snapshot": post_create_reconciliation.get("observed_snapshot"),
                    "reason": post_create_reconciliation.get("reason", "created Gallery could not be identified in the vertical"),
                })
        failure_steps.append({"step": "publish_vertical", "status": "skipped", "reason": "Gallery creation or content preparation did not complete"})
        return {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "working_vertical_block_id": working_vertical_block_id,
            "target_vertical_url": operator_vertical_url,
            "requested_target_vertical_url": requested_vertical_url,
            "created_block_locator": created_locator,
            "planned_result_type": str(replacement_plan.get("result_type", "replaced")),
            "matched_source_block_locator": str(replacement_plan.get("matched_source_block_locator", "")),
            "failed_step": str(getattr(exc, "step_name", "create_block")),
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": safe_error_summary(exc),
            "block_type": resolved_block_type,
            "create_attempts": diagnostics.get("create_attempts", create_attempts),
            "vertical_snapshot_before": creation_baseline,
            "vertical_snapshot_after_create": failure_snapshot,
            "expected_vertical_snapshot_after_delete": None,
            "vertical_published": None,
            "steps": failure_steps,
            **summarize_steps(failure_steps),
        }
    if preflight_steps:
        single_publish_result["steps"] = [*preflight_steps, *list(single_publish_result.get("steps", []))]
    try:
        result = apply_in_place_publish_actions(
            single_publish_result,
            adapter=adapter,
            replacement_plan=replacement_plan,
            studio_url=studio_vertical_url,
            execute=bool(getattr(args, "execute", False)),
            timeout=int(getattr(args, "timeout", 30)),
            args=args,
        )
        result["summary_cleanup"] = summary_cleanup_info
        return result
    except PublishFailure as exc:
        operator_vertical_url, requested_vertical_url = operator_vertical_urls(studio_base, replacement_plan, studio_vertical_url)
        failure_steps = list(exc.steps or single_publish_result.get("steps", []))
        failure_summary_cleanup = dict(summary_cleanup_info)
        failed = {
            "status": "failed",
            "target_vertical_block_id": target_vertical_block_id,
            "working_vertical_block_id": working_vertical_block_id,
            "target_vertical_url": operator_vertical_url,
            "requested_target_vertical_url": requested_vertical_url,
            "created_block_locator": str(single_publish_result.get("block_locator", "")),
            "created_imagegallery_block_url": str(single_publish_result.get("created_imagegallery_block_url", "")),
            "planned_result_type": str(replacement_plan.get("result_type", "fallback_inserted")),
            "matched_source_block_locator": str(replacement_plan.get("matched_source_block_locator", "")),
            "matched_source_block_url": build_container_block_url(studio_base, str(replacement_plan.get("matched_source_block_locator", ""))),
            "matched_summary_block_locator": "",
            "matched_summary_block_url": "",
            "matched_summary_component_name": "",
            "failed_step": exc.step_name,
            "failure_bucket": "manual_followup",
            "requires_manual_followup": True,
            "note": safe_error_summary(exc),
            "summary_cleanup": failure_summary_cleanup,
            "course_kind": replacement_plan.get("course_kind", ""),
            "match_scope": replacement_plan.get("match_scope", ""),
            "steps": failure_steps,
            **summarize_steps(failure_steps),
            **(exc.vertical_evidence if isinstance(getattr(exc, "vertical_evidence", None), dict) else {}),
        }
        return failed


def process_publish_target(
    target: dict[str, Any],
    course_structure: dict[str, Any],
    args: argparse.Namespace,
    adapter: dict[str, Any],
    *,
    previous_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        result = _process_publish_target_impl(
            target,
            course_structure,
            args,
            adapter,
            previous_result=previous_result,
        )
    except Exception as exc:
        failed_step = safe_step_name(getattr(exc, "step_name", getattr(exc, "step", "target_preflight")))
        exc.steps = failed_target_steps(
            getattr(exc, "steps", []),
            failed_step=failed_step,
            reason=safe_error_summary(exc),
        )
        raise
    if result.get("status") == "failed":
        result["steps"] = failed_target_steps(
            result.get("steps", []),
            failed_step=str(result.get("failed_step", "target_preflight")),
            reason=str(result.get("note", "") or "target failed before vertical publication"),
        )
        result.update(summarize_steps(result["steps"]))
    return result


def run_in_place_publish(args: argparse.Namespace) -> dict[str, Any]:
    if bool(getattr(args, "cleanup_only", False)):
        return unsupported_cleanup_only_result(
            target_vertical_block_id=str(getattr(args, "vertical_block_id", "") or ""),
            target_vertical_url=normalize_block_url(str(getattr(args, "studio_url", "") or "")),
        )
    course_structure = load_course_structure_snapshot(Path(args.course_structure_json).expanduser().resolve())
    adapter = load_adapter(args.adapter)
    target_vertical_block_id = str(args.vertical_block_id or parse_vertical_block_id(args.studio_url))
    target = {
        "name": Path(str(getattr(args, "manifest", "") or "cleanup-only")).stem,
        "manifest": args.manifest,
        "section_id": str(getattr(args, "section_id", "") or ""),
        "section_title": str(getattr(args, "section_title", "") or ""),
        "block_type": str(getattr(args, "block_type", "") or ""),
        "section_list_json": str(getattr(args, "section_list_json", "") or ""),
        "source_vertical_block_location": str(args.source_vertical_block_id or target_vertical_block_id),
        "target_vertical_block_location": target_vertical_block_id,
        "studio_vertical_url": normalize_block_url(args.studio_url),
    }
    result = process_publish_target(target, course_structure, args, adapter)
    result["provenance"] = skill_provenance(
        input_paths=unique_provenance_inputs([
            str(getattr(args, "manifest", "")),
            str(getattr(args, "course_structure_json", "")),
            section_list_provenance_input(
                str(getattr(args, "manifest", "")),
                str(getattr(args, "section_list_json", "")),
            ),
        ]),
        output_path=str(getattr(args, "report_file", "") or getattr(args, "operation_file", "")),
    )
    persist_vertical_integrity_evidence(args, result)
    return result


def persist_vertical_integrity_evidence(args: argparse.Namespace, result: dict[str, Any]) -> None:
    operation_file = str(getattr(args, "operation_file", "") or "").strip()
    gallery_key = str(getattr(args, "gallery_key", "") or "").strip()
    if not operation_file or not gallery_key or not bool(getattr(args, "execute", False)):
        return
    path = Path(operation_file).expanduser().resolve()
    try:
        operation = read_json(path)
    except (OSError, JSONDecodeError):
        return
    galleries = operation.get("galleries") if isinstance(operation, dict) else None
    if not isinstance(galleries, list):
        return
    matches = [entry for entry in galleries if isinstance(entry, dict) and entry.get("gallery_key") == gallery_key]
    if len(matches) != 1:
        return
    provenance = result.get("provenance")
    if isinstance(provenance, dict):
        operation["skill_provenance"] = provenance
    evidence = {
        "status": "verified" if result.get("status") in {"replaced", "fallback_inserted"} and result.get("vertical_published") is True else "unresolved",
        "block_type": result.get("block_type", "unknown"),
        "matched_source_block_locator": result.get("matched_source_block_locator", ""),
        "before": result.get("vertical_snapshot_before"),
        "expected": result.get("expected_vertical_snapshot_after_delete"),
        "published": result.get("vertical_published"),
        "vertical_snapshot_before": result.get("vertical_snapshot_before"),
        "vertical_snapshot_after_create": result.get("vertical_snapshot_after_create"),
        "expected_vertical_snapshot_after_delete": result.get("expected_vertical_snapshot_after_delete"),
        "vertical_published": result.get("vertical_published"),
        "provenance": provenance if isinstance(provenance, dict) else {},
        "steps": [
            {
                "step": safe_step_name(step.get("step")),
                "status": safe_step_name(step.get("status")),
                **({"attempts": step["attempts"]} if isinstance(step.get("attempts"), list) else {}),
            }
            for step in result.get("steps", []) if isinstance(step, dict)
        ],
    }
    matches[0]["vertical_integrity"] = evidence
    atomic_write_json(path, operation)


def operation_gallery_checkpoint(args: argparse.Namespace) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    operation_file = str(getattr(args, "operation_file", "") or "").strip()
    gallery_key = str(getattr(args, "gallery_key", "") or "").strip()
    if not operation_file or not gallery_key:
        return None
    path = Path(operation_file).expanduser().resolve()
    try:
        operation = read_json(path)
    except (OSError, JSONDecodeError):
        return None
    galleries = operation.get("galleries") if isinstance(operation, dict) else None
    if not isinstance(galleries, list):
        return None
    matches = [entry for entry in galleries if isinstance(entry, dict) and entry.get("gallery_key") == gallery_key]
    if len(matches) != 1:
        return None
    return path, operation, matches[0]


def persist_vertical_integrity_baseline(
    args: argparse.Namespace,
    *,
    block_type: str,
    snapshot: dict[str, Any],
    matched_source_block_locator: str = "",
) -> None:
    record = operation_gallery_checkpoint(args)
    if record is None or not bool(getattr(args, "execute", False)):
        return
    path, operation, gallery = record
    integrity = gallery.get("vertical_integrity") if isinstance(gallery.get("vertical_integrity"), dict) else {}
    before = integrity.get("before") or integrity.get("vertical_snapshot_before")
    if isinstance(before, dict):
        return
    gallery["vertical_integrity"] = {
        "status": "in_progress",
        "block_type": block_type,
        "before": snapshot,
        "expected": None,
        "published": None,
        "vertical_snapshot_before": snapshot,
        "matched_source_block_locator": matched_source_block_locator,
        "steps": [],
    }
    atomic_write_json(path, operation)


def pending_create_checkpoint(args: argparse.Namespace) -> dict[str, Any] | None:
    record = operation_gallery_checkpoint(args)
    if record is None:
        return None
    _path, _operation, gallery = record
    publish = gallery.get("publish") if isinstance(gallery.get("publish"), dict) else {}
    gallery_locator = str(gallery.get("gallery_locator") or publish.get("gallery_locator") or "").strip()
    active = str(publish.get("active_step", "") or "").strip()
    failed = str(publish.get("failed_step", "") or "").strip()
    if gallery_locator or active != "create_block" and failed != "create_block":
        return None
    integrity = gallery.get("vertical_integrity") if isinstance(gallery.get("vertical_integrity"), dict) else {}
    before = integrity.get("before") or integrity.get("vertical_snapshot_before")
    matched_source = str(integrity.get("matched_source_block_locator", "") or "").strip()
    attempts = publish.get("create_attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        attempts = 0
    return {
        "before": before if isinstance(before, dict) else None,
        "matched_source_block_locator": matched_source,
        "create_attempts": attempts,
    }


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
    section_list_json = str(payload.get("section_list_json", "") or getattr(args, "section_list_json", "")).strip()

    run_target = publish_target_fn or process_publish_target
    results: list[dict[str, Any]] = []
    targets = list(payload.get("targets") or [])
    provenance_inputs = [str(targets_path), str(course_structure_path)]
    for target in targets:
        if not isinstance(target, dict):
            continue
        manifest = str(target.get("manifest", "") or "")
        provenance_inputs.append(manifest)
        provenance_inputs.append(
            section_list_provenance_input(
                manifest,
                str(target.get("section_list_json", "") or section_list_json),
            )
        )
    provenance = skill_provenance(
        input_paths=unique_provenance_inputs(provenance_inputs),
        output_path=str(report_path),
    )
    for raw_target in targets:
        target = dict(raw_target or {})
        if section_list_json:
            target.setdefault("section_list_json", section_list_json)
        target_key = batch_target_key(target)
        previous_result = previous_results_by_key.get(target_key)
        course_structure = load_course_structure_snapshot(course_structure_path)
        try:
            result = run_target(target, course_structure, args, adapter, previous_result=previous_result)
        except PublishFailure as exc:
            failure_steps = failed_target_steps(
                getattr(exc, "steps", []),
                failed_step=exc.step_name,
                reason=safe_error_summary(exc),
            )
            result = {
                "status": "failed",
                "failed_step": exc.step_name,
                "failure_bucket": "manual_followup",
                "requires_manual_followup": True,
                "note": safe_error_summary(exc),
                "target_vertical_url": str(target.get("studio_vertical_url", "")),
                "steps": failure_steps,
                **summarize_steps(failure_steps),
            }
        except Exception as exc:
            failed_step = safe_step_name(getattr(exc, "step_name", "unexpected_error"))
            failure_steps = failed_target_steps(
                getattr(exc, "steps", []),
                failed_step=failed_step,
                reason=safe_error_summary(exc),
            )
            result = {
                "status": "failed",
                "failed_step": failed_step,
                "failure_bucket": "manual_followup",
                "requires_manual_followup": True,
                "note": safe_error_summary(exc),
                "target_vertical_url": str(target.get("studio_vertical_url", "")),
                "steps": failure_steps,
                **summarize_steps(failure_steps),
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
            "provenance": provenance,
        }
        write_batch_report(report_path, partial_payload)

    grouped = group_batch_results(results)
    final_payload = {
        "targets_file": str(targets_path),
        "report_file": str(report_path),
        "results": results,
        "replaced": grouped["replaced"],
        "fallback_inserted": grouped["fallback_inserted"],
        "unsupported": grouped["unsupported"],
        "manual_followup_failed": grouped["manual_followup_failed"],
        "failed": grouped["failed"],
        "summary": build_batch_summary(results),
        "provenance": provenance,
    }
    write_batch_report(report_path, final_payload)
    return final_payload


def run(args: argparse.Namespace) -> dict[str, Any]:
    if bool(getattr(args, "cleanup_only", False)):
        return unsupported_cleanup_only_result(
            target_vertical_block_id=str(getattr(args, "vertical_block_id", "") or ""),
            target_vertical_url=normalize_block_url(str(getattr(args, "studio_url", "") or "")),
        )
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
    p.add_argument("--section-id", default="", help="source section ID for section-list metadata fallback")
    p.add_argument("--section-title", default="", help="source section title used only when section ID is unavailable")
    p.add_argument("--block-type", default="", help="source block_type metadata override")
    p.add_argument("--section-list-json", default="", help="section-list.json used to recover gallery source metadata")
    p.add_argument("--cleanup-only", action="store_true", help="legacy option; returns unsupported without contacting Studio")
    p.add_argument("--course-structure-json", default="")
    p.add_argument("--targets-file", default="")
    p.add_argument("--report-file", default="")
    p.add_argument("--operation-file", default="", help="shared batch-operation.json file owned by the batch caller")
    p.add_argument("--gallery-key", default="", help="stable gallery_key identifying this Gallery in --operation-file")

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
    args = p.parse_args()
    if bool(args.operation_file) != bool(args.gallery_key):
        p.error("--operation-file and --gallery-key must be supplied together")
    return args


def main() -> int:
    args = None
    try:
        args = parse_args()
        if args.dry_run:
            args.execute = False
        if args.cleanup_only:
            print(json.dumps(unsupported_cleanup_only_result(), ensure_ascii=False, indent=2))
            return 2
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
    except SystemExit:
        raise
    except Exception as exc:
        failure: dict[str, Any] = {
            "status": "failed",
            "failed_step": safe_step_name(getattr(exc, "step_name", "publish")),
            "error_summary": safe_error_summary(exc),
        }
        if isinstance(exc, TemplateCallError):
            failure["http_status"] = exc.status_code
        diagnostics = getattr(exc, "safe_diagnostics", None)
        if isinstance(diagnostics, dict):
            for key in ("completed_stage", "asset_ids", "steps", "failed_page_image"):
                if key in diagnostics:
                    failure[key] = diagnostics[key]
        if args and args.operation_file and args.manifest and args.studio_url:
            try:
                vertical_id = args.vertical_block_id or parse_vertical_block_id(args.studio_url)
                if vertical_id:
                    checkpoint = PublishCheckpoint.load(
                        args.operation_file,
                        args.gallery_key,
                        manifest_path=Path(args.manifest).expanduser().resolve(),
                        vertical_block_id=vertical_id,
                    )
                    failure["completed_stage"] = checkpoint.state["completed_stage"]
                    failure["asset_ids"] = list(checkpoint.state["asset_ids"])
                    failure["steps"] = list(checkpoint.state["steps"])
                    if checkpoint.gallery_locator:
                        failure["gallery_locator"] = checkpoint.gallery_locator
                    if checkpoint.state.get("failed_page_image"):
                        failure["failed_page_image"] = checkpoint.state["failed_page_image"]
            except Exception:
                pass
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if out.get("status") == "unsupported" or any(
        result.get("status") == "unsupported"
        for result in out.get("results", [])
        if isinstance(result, dict)
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
