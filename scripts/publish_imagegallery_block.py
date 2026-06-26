#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def parse_vertical_block_id(url_input: str) -> str | None:
    m = re.search(r"block-v1:[^/?#]+", url_input)
    return m.group(0) if m else None


def normalize_block_url(url_input: str) -> str:
    parsed = urlparse(url_input.strip())
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def course_key_from_vertical_block_id(vertical_block_id: str) -> str:
    parts = vertical_block_id.split("+")
    if len(parts) < 3 or ":" not in parts[0]:
        return ""
    org = parts[0].split(":", 1)[1]
    return f"course-v1:{org}+{parts[1]}+{parts[2]}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
    content_type = resp.headers.get("content-type", "")

    if "application/json" in content_type:
        payload = resp.json()
    else:
        try:
            payload = resp.json()
        except Exception:
            payload = {"text": resp.text[:2000]}
    record["response"] = payload

    if resp.status_code >= 400:
        raise RuntimeError(f"{name} 失败: HTTP {resp.status_code}, body={payload}")

    extract = rendered.get("extract") or {}
    if isinstance(extract, dict):
        for k, p in extract.items():
            ctx[k] = get_by_path(payload, str(p))
        if extract:
            record["extracted"] = {k: ctx.get(k) for k in extract}

    return record


def run(args: argparse.Namespace) -> dict[str, Any]:
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

    for key in ("create_block", "upload_image", "save_block"):
        if key not in adapter:
            raise ValueError(f"adapter 缺失步骤: {key}")

    logs.append(call_template(session, "create_block", adapter["create_block"], ctx, execute, args.timeout))

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
        image_step = call_template(session, f"upload_image[{idx}]", adapter["upload_image"], ctx, execute, args.timeout)
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
            call_template(
                session,
                "change_files_order",
                adapter["change_files_order"],
                ctx,
                execute,
                args.timeout,
            )
        )

    if "upload_audio_prepare" in adapter:
        prepare_step = call_template(
            session,
            "upload_audio_prepare",
            adapter["upload_audio_prepare"],
            ctx,
            execute,
            args.timeout,
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
        logs.append(call_template(session, "upload_audio", adapter["upload_audio"], ctx, execute, args.timeout))

    if "upload_audio_register" in adapter:
        logs.append(
            call_template(
                session,
                "upload_audio_register",
                adapter["upload_audio_register"],
                ctx,
                execute,
                args.timeout,
            )
        )

    if "save_audio_binding" in adapter:
        logs.append(
            call_template(
                session,
                "save_audio_binding",
                adapter["save_audio_binding"],
                ctx,
                execute,
                args.timeout,
            )
        )

    if "submit_studio_edits" in adapter:
        logs.append(
            call_template(
                session,
                "submit_studio_edits",
                adapter["submit_studio_edits"],
                ctx,
                execute,
                args.timeout,
            )
        )
    logs.append(call_template(session, "save_block", adapter["save_block"], ctx, execute, args.timeout))

    return {
        "mode": "execute" if execute else "dry-run",
        "studio_base": studio_base,
        "courses_base": courses_base,
        "vertical_block_id": vertical_block_id,
        "block_locator": ctx.get("block_locator", ""),
        "steps": logs,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish local imagesgallery package to Studio")
    p.add_argument("--studio-url", required=True, help="Studio container URL")
    p.add_argument("--manifest", required=True, help="path to imagesgallery.json")
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

    p.add_argument("--courses-base", default="")
    p.add_argument("--account", default="")
    p.add_argument("--password", default="")
    p.add_argument("--cookie", default="")
    p.add_argument("--csrf-token", default="")
    p.add_argument("--skip-oss-multipart", action="store_true", help="skip OSS multipart upload after get_upload_info")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        args.execute = False
    out = run(args)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
