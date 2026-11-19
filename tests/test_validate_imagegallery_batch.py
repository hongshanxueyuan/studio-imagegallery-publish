import importlib
import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
publisher = importlib.import_module("publish_imagegallery_block")
module = importlib.import_module("validate_imagegallery_batch")
ADAPTER_PATH = ROOT / "references" / "adapter.template.json"

VERTICAL_1 = "block-v1:ORG+COURSE+RUN+type@vertical+block@vertical123"
VERTICAL_2 = "block-v1:ORG+COURSE+RUN+type@vertical+block@vertical456"
GALLERY_1 = "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@gallery123"
GALLERY_2 = "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@gallery456"
COURSE_KEY = "course-v1:ORG+COURSE+RUN"


def valid_time_config(page_count):
    return [
        {
            "page": page,
            "start": (page - 1) * 1000,
            "end": page * 1000,
            "subtitle": f"第{page}页",
        }
        for page in range(1, page_count + 1)
    ]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)
        self.payload = payload

    def json(self):
        return self.payload


class FakeRangeResponse:
    def __init__(self, status_code=206, *, headers=None, content=b"x"):
        self.status_code = status_code
        self.headers = headers or {"Content-Range": "bytes 0-0/999", "Content-Length": "1"}
        self.raw = BytesIO(content)

    def close(self):
        self.raw.close()


class FakeCookies:
    def set(self, *_args, **_kwargs):
        return None


class FakeStudio:
    def __init__(self, galleries, *, keep_mismatch_after_repair=False, fail_get_files=None, payload_overrides=None, extra_assets=None, fail_vertical_publish=False, vertical_overrides=None, fail_course_reads=None, fail_publication_reads=None):
        self.galleries = galleries
        self.keep_mismatch_after_repair = keep_mismatch_after_repair
        self.fail_get_files = fail_get_files or {}
        self.payload_overrides = payload_overrides or {}
        self.fail_vertical_publish = fail_vertical_publish
        self.vertical_overrides = vertical_overrides or {}
        self.fail_course_reads = set(fail_course_reads or [])
        self.fail_publication_reads = set(fail_publication_reads or [])
        self.calls = []
        self.path_counts = {}
        self.get_files_counts = {}
        self.course_reads = 0
        self.publication_reads = 0
        self.next_asset = 1
        self.next_gallery = 1
        self.assets = []
        for gallery in galleries.values():
            for index, item in enumerate(gallery["files"], start=1):
                if not any(item.get(key) for key in ("display_name", "name", "filename", "file_name")):
                    item["display_name"] = f"page-{index:03d}.png"
                asset_id = item.get("id") or item.get("asset_key")
                if asset_id and all(asset.get("id") != asset_id for asset in self.assets):
                    self.assets.append({"id": asset_id, "display_name": item.get("display_name", "page.png")})
        for asset in extra_assets or []:
            if isinstance(asset, dict) and isinstance(asset.get("id"), str):
                self.assets.append(dict(asset))
        self.session = self.Session(self)

    class Session:
        def __init__(self, studio):
            self.studio = studio
            self.cookies = FakeCookies()
            self.headers = {}

        def request(self, *, method, url, **kwargs):
            return self.studio.request(method, url, **kwargs)

        def get(self, url, **kwargs):
            return self.studio.request("GET", url, **kwargs)

    def _response(self, method, path, kwargs):
        if method == "GET" and path == f"/course/{COURSE_KEY}":
            is_concise = (kwargs.get("params") or {}).get("format") == "concise"
            if is_concise:
                self.course_reads += 1
                failed = self.course_reads in self.fail_course_reads
            else:
                self.publication_reads += 1
                failed = self.publication_reads in self.fail_publication_reads
            if failed:
                return FakeResponse({"detail": "structure unavailable"}, 503)
            return FakeResponse(make_course_structure(self.galleries, self.vertical_overrides))
        if method == "GET" and path == f"/assets/{COURSE_KEY}/":
            page = int((kwargs.get("params") or {}).get("page", 0))
            return FakeResponse({"assets": self.assets if page == 0 else [], "totalCount": len(self.assets), "page": page, "pageSize": 50})

        if method == "POST" and path == "/xblock/":
            vertical = (kwargs.get("json") or {}).get("parent_locator", "")
            gallery = f"block-v1:ORG+COURSE+RUN+type@imagesgallery+block@created-{self.next_gallery}"
            self.next_gallery += 1
            self.galleries[gallery] = {"files": [], "time_config": [], "audios": []}
            override = self.vertical_overrides.setdefault(vertical, {})
            override["has_changes"] = True
            if not isinstance(override.get("blocks"), list):
                original_gallery = GALLERY_1 if vertical == VERTICAL_1 else GALLERY_2
                summary = vertical.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
                override["blocks"] = []
                if original_gallery in self.galleries:
                    override["blocks"].append({"id": original_gallery, "category": "imagesgallery", "display_name": "有声幻灯片"})
                override["blocks"].append({"id": summary, "category": "html", "display_name": "本节要点"})
            override["blocks"].append({"id": gallery, "category": "imagesgallery", "display_name": "有声幻灯片"})
            return FakeResponse({"locator": gallery})

        if method == "DELETE" and path.startswith("/xblock/") and "+type@vertical+" not in path:
            locator = path[len("/xblock/"):]
            self.galleries.pop(locator, None)
            for override in self.vertical_overrides.values():
                if isinstance(override.get("blocks"), list):
                    original_count = len(override["blocks"])
                    override["blocks"][:] = [block for block in override["blocks"] if block.get("id") != locator]
                    if len(override["blocks"]) != original_count:
                        override["has_changes"] = True
            return FakeResponse({"ok": True})

        if method == "PUT" and path.startswith("/xblock/") and "+type@vertical+" in path:
            vertical = path[len("/xblock/"):]
            override = self.vertical_overrides.setdefault(vertical, {"blocks": []})
            override["has_changes"] = True
            blocks = override.setdefault("blocks", [])
            by_id = {block["id"]: block for block in blocks if isinstance(block, dict) and isinstance(block.get("id"), str)}
            requested = (kwargs.get("json") or {}).get("children", [])
            override["blocks"] = [by_id[locator] for locator in requested if locator in by_id]
            return FakeResponse({"ok": True})

        for gallery_locator, gallery in self.galleries.items():
            preview_prefix = f"/preview/xblock/{gallery_locator}/handler/"
            author_prefix = f"/xblock/{gallery_locator}/handler/"
            if method == "POST" and path == preview_prefix + "get_files":
                occurrence = self.get_files_counts.get(gallery_locator, 0) + 1
                self.get_files_counts[gallery_locator] = occurrence
                failure_at = self.fail_get_files.get(gallery_locator)
                if failure_at == occurrence:
                    return FakeResponse({
                        "detail": "raw-response-secret",
                        "body": "raw-body-secret",
                        "token": "error-token-secret",
                        "Cookie": "error-cookie-secret",
                        "csrftoken": "error-csrf-secret",
                        "Authorization": "Bearer error-auth-secret",
                        "Credentials": {"AccessKeyId": "error-sts-secret", "SecurityToken": "error-sts-secret"},
                    }, 503)
                override = self.payload_overrides.get((gallery_locator, "get_files"))
                if override is not None:
                    return FakeResponse(override)
                return FakeResponse({
                    "files": gallery["files"],
                    "total_count": gallery.get("total_count", len(gallery["files"])),
                    "Credentials": {"AccessKeyId": "success-sts-secret"},
                    "Cookie": "success-cookie-secret",
                    "csrftoken": "success-csrf-secret",
                    "Authorization": "Bearer success-auth-secret",
                    "raw_response": {"token": "success-token-secret"},
                })
            if method == "POST" and path == preview_prefix + "get_play_config":
                override = self.payload_overrides.get((gallery_locator, "get_play_config"))
                play_config = {"time_config": gallery["time_config"]}
                if "show_subtitles" in gallery:
                    play_config["show_subtitles"] = gallery["show_subtitles"]
                return FakeResponse(override if override is not None else {"play_config": play_config})
            if method == "POST" and path == preview_prefix + "get_audios":
                override = self.payload_overrides.get((gallery_locator, "get_audios"))
                audios = gallery["audios"]
                if audios == [{}]:
                    audios = [{"document_id": "doc-1.mp3"}]
                return FakeResponse(override if override is not None else {"audios": audios})
            if method == "POST" and path == preview_prefix + "get_audio_download_url":
                override = self.payload_overrides.get((gallery_locator, "get_audio_download_url"))
                return FakeResponse(override if override is not None else {
                    "download_url": "https://objects.example.com/audio/doc-1.mp3?signature=signed-secret&token=sts-secret",
                })
            if method == "POST" and path == author_prefix + "remove_files":
                requested = set((kwargs.get("json") or {}).get("assets", []))
                gallery["files"] = [
                    item for item in gallery["files"]
                    if (item.get("asset_key") or item.get("id")) not in requested
                ]
                gallery["total_count"] = len(gallery["files"])
                return FakeResponse({"ok": True})
            if method == "POST" and path == author_prefix + "file_upload":
                asset_id = f"asset-new-{self.next_asset:03d}"
                self.next_asset += 1
                file_spec = kwargs.get("files") or {}
                uploaded = next(iter(file_spec.values()), ("page.png", b"", "image/png"))
                display_name = uploaded[0] if isinstance(uploaded, tuple) else "page.png"
                gallery["files"].append({"id": asset_id, "asset_key": asset_id, "display_name": display_name})
                gallery["total_count"] = len(gallery["files"])
                self.assets.append({"id": asset_id, "display_name": display_name})
                return FakeResponse({"id": asset_id})
            if method == "POST" and path == author_prefix + "change_files_order":
                by_id = {item.get("id") or item.get("asset_key"): item for item in gallery["files"]}
                gallery["files"] = [by_id[asset_id] for asset_id in (kwargs.get("json") or {}).get("ids", [])]
                gallery["total_count"] = len(gallery["files"])
                return FakeResponse({"ok": True})
            if method == "POST" and path == author_prefix + "get_upload_info":
                return FakeResponse({
                    "file_key": "12345678-1234-1234-1234-123456789abc",
                    "Credentials": {"AccessKeyId": "sts-secret", "AccessKeySecret": "sts-secret", "SecurityToken": "sts-secret"},
                })
            if method == "POST" and path == author_prefix + "save_audio":
                if not self.keep_mismatch_after_repair:
                    gallery["audios"] = [{"document_id": (kwargs.get("json") or {}).get("document_id", "doc-1")}]
                return FakeResponse({"ok": True, "Authorization": "Bearer raw-secret"})
            if method == "POST" and path == author_prefix + "update_play_config":
                if not self.keep_mismatch_after_repair:
                    config = (kwargs.get("json") or {}).get("play_config", {})
                    gallery["time_config"] = config.get("time_config", [])
                    if "show_subtitles" in config:
                        gallery["show_subtitles"] = config["show_subtitles"]
                return FakeResponse({"ok": True})

        if method == "POST" and path == "/api/edxdal/v1/documents/":
            return FakeResponse({"ok": True})
        if method == "POST" and path.startswith("/xblock/") and "+type@vertical+" in path:
            if self.fail_vertical_publish:
                return FakeResponse({"detail": "vertical publish failed", "Authorization": "Bearer vertical-secret"}, 503)
            for locator in (VERTICAL_1, VERTICAL_2):
                if locator in path:
                    override = self.vertical_overrides.setdefault(locator, {})
                    override.update({"published": True, "has_changes": False, "visibility_state": "live"})
            return FakeResponse({"ok": True})
        if method == "DELETE" and path.startswith(f"/assets/{COURSE_KEY}/"):
            asset_id = path.rsplit("/", 1)[-1]
            self.assets = [asset for asset in self.assets if asset.get("id") != asset_id]
            return FakeResponse({}, 204)
        return FakeResponse({"ok": True})

    def request(self, method, url, **kwargs):
        path = urlparse(url).path
        self.calls.append({"method": method, "path": path, "kwargs": kwargs})
        self.path_counts[path] = self.path_counts.get(path, 0) + 1
        return self._response(method.upper(), path, kwargs)


def make_course_structure(galleries, vertical_overrides=None):
    vertical_overrides = vertical_overrides or {}
    override_gallery_locators = {
        block.get("id")
        for override in vertical_overrides.values()
        if isinstance(override, dict) and isinstance(override.get("blocks"), list)
        for block in override["blocks"]
        if isinstance(block, dict) and block.get("category") == "imagesgallery"
    }
    blocks_by_vertical = {VERTICAL_1: [], VERTICAL_2: []}
    for locator in galleries:
        if locator in override_gallery_locators:
            continue
        vertical = VERTICAL_1 if locator == GALLERY_1 else VERTICAL_2
        blocks_by_vertical[vertical].extend([
            {"id": locator, "category": "imagesgallery", "display_name": "有声幻灯片"},
            {
                "id": vertical.replace("type@vertical", "type@html").replace("block@vertical", "block@summary"),
                "category": "html",
                "display_name": "本节要点",
            },
        ])
    vertical_nodes = []
    for locator in dict.fromkeys([*blocks_by_vertical, *vertical_overrides]):
        override = vertical_overrides.get(locator, {})
        blocks = override.get("blocks", blocks_by_vertical.get(locator, []))
        node = {
            "id": locator,
            "category": "vertical",
            "display_name": "小节",
            "child_info": {"children": blocks},
        }
        if not override.get("omit_published"):
            node["published"] = override.get("published", True)
        if not override.get("omit_has_changes"):
            node["has_changes"] = override.get("has_changes", False)
        if "visibility_state" in override:
            node["visibility_state"] = override["visibility_state"]
        if blocks or override.get("include_empty"):
            vertical_nodes.append(node)
    section = {"id": "block-v1:ORG+COURSE+RUN+type@sequential+block@section", "category": "sequential", "child_info": {"children": vertical_nodes}}
    chapter = {"id": "block-v1:ORG+COURSE+RUN+type@chapter+block@chapter", "category": "chapter", "child_info": {"children": [section]}}
    return {"id": COURSE_KEY, "category": "course", "display_name": "Test Course", "child_info": {"children": [chapter]}}


def make_incomplete_course_structure():
    payload = make_course_structure({GALLERY_1: {}, GALLERY_2: {}})
    section = payload["child_info"]["children"][0]["child_info"]["children"][0]
    section["child_info"]["children"][1].pop("child_info")
    return payload


class ValidateImageGalleryBatchTests(unittest.TestCase):
    def test_play_config_untrusted_signal_reads_legacy_persisted_flag(self):
        self.assertTrue(module.play_config_is_untrusted({"play_config_untrusted": True}))
        self.assertTrue(module.play_config_is_untrusted({"play_config_read_failed": True}))
        self.assertFalse(module.play_config_is_untrusted({}))

    def test_cli_reports_initial_failure_separately_from_verified_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            operation = self._operation(manifest)
            operation["galleries"][0]["publish"]["status"] = "failed"
            path = root / "operation.json"
            self._write_operation_file(path, operation)
            studio = FakeStudio({GALLERY_1: {"files": [{"id": "asset-a"}, {"id": "asset-b"}],
                                              "time_config": valid_time_config(2), "audios": [{}]}})
            code, stdout, stderr = self._run_cli(path, studio)
            self.assertEqual(0, code, stdout + stderr)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(state["batch_result"]["all_sections_verified"])
            report = state["batch_result"]["operator_report"]["text"]
            self.assertIn("首次发布", report)
            self.assertIn("后置审核：全部通过", report)
            self.assertIn(VERTICAL_1, report)
            self.assertEqual("failed", state["galleries"][0]["publish"]["status"])

    def test_cli_checks_bound_audio_object_with_one_byte_range_and_safe_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            operation_path = root / "operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["publish"]["audio_upload_evidence"] = {
                "upload_key_path": "//objects.example.com/audio/course.mp3?signature=upload-secret#fragment-secret",
                "bucket": "course-audio",
                "region": "cn-beijing",
                "multipart_part_count": 2,
                "multipart_completed": True,
                "Credentials": {"AccessKeyId": "sts-secret"},
            }
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"document_id": "doc-1.mp3"}],
            }})
            object_requests = []

            def get_object(url, **kwargs):
                object_requests.append((url, kwargs))
                return FakeRangeResponse()

            code, stdout, stderr = self._run_cli(
                operation_path,
                studio,
                execute=False,
                object_get=get_object,
            )

            state = json.loads(operation_path.read_text(encoding="utf-8"))
            validation = state["galleries"][0]["validation"]
            self.assertEqual(0, code, stdout + stderr)
            self.assertEqual("pass", validation["checks"]["audio_bindings"])
            self.assertEqual("pass", validation["checks"]["audio_object_accessibility"])
            self.assertEqual("accessible", validation["observed"]["audio_object_accessibility"])
            summary_checks = state["batch_result"]["galleries"][0]["checks"]
            self.assertEqual("pass", summary_checks["audio_bindings"])
            self.assertEqual("pass", summary_checks["audio_object_accessibility"])
            self.assertEqual("doc-1.mp3", validation["observed"]["audio_id"])
            self.assertEqual(1, len(object_requests))
            self.assertEqual("bytes=0-0", object_requests[0][1]["headers"]["Range"])
            self.assertTrue(object_requests[0][1]["stream"])
            download_call = next(call for call in studio.calls if call["path"].endswith("/get_audio_download_url"))
            self.assertEqual({"audio_id": "doc-1.mp3"}, download_call["kwargs"]["json"])
            evidence = state["galleries"][0]["audio_evidence"]
            self.assertEqual(206, evidence["download_http_status"])
            self.assertEqual(999, evidence["object_length_bytes"])
            self.assertEqual(10, evidence["local_size_bytes"])
            self.assertEqual("72401f193251f177a310936253acb57e91e29d2aa582093974351e65e267bb32", evidence["local_sha256"])
            self.assertEqual("/audio/course.mp3", evidence["upload_key_path"])
            self.assertEqual("/audio/doc-1.mp3", evidence["download_key_path"])
            self.assertFalse(evidence["key_paths_match"])
            self.assertEqual("course-audio", evidence["bucket"])
            self.assertEqual("cn-beijing", evidence["region"])
            self.assertEqual(2, evidence["multipart_part_count"])
            self.assertTrue(evidence["multipart_completed"])
            self.assertNotIn("signed-secret", json.dumps(state))
            self.assertNotIn("upload-secret", json.dumps(state))
            self.assertNotIn("fragment-secret", json.dumps(state))
            self.assertNotIn("sts-secret", json.dumps(state))
            self.assertNotIn("signed-secret", stdout)
            self.assertNotIn("sts-secret", stdout)

    def test_cli_classifies_missing_and_unconfirmed_audio_objects(self):
        scenarios = (
            (200, b"x", {"Content-Length": "200"}, "accessible", "pass"),
            (206, b"x", {"Content-Range": "bytes 0-0/200", "Content-Length": "1"}, "accessible", "pass"),
            (206, b"", {"Content-Range": "bytes 0-0/200", "Content-Length": "0"}, "unverified", "unverified"),
            (403, b"", {}, "unverified", "unverified"),
            (404, b"", {}, "missing", "mismatch"),
            (400, b"<Error><Code>NoSuchKey</Code></Error>", {}, "missing", "mismatch"),
        )
        for status_code, body, headers, expected_access, expected_check in scenarios:
            with self.subTest(status_code=status_code, body=body):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    manifest = self._write_bundle(root)
                    operation_path = root / "operation.json"
                    self._write_operation_file(operation_path, self._operation(manifest))
                    studio = FakeStudio({GALLERY_1: {
                        "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                        "time_config": valid_time_config(2),
                        "audios": [{"document_id": "doc-1.mp3"}],
                    }})
                    code, stdout, stderr = self._run_cli(
                        operation_path,
                        studio,
                        execute=False,
                        object_get=lambda _url, **_kwargs: FakeRangeResponse(
                            status_code,
                            headers=headers,
                            content=body,
                        ),
                    )
                    state = json.loads(operation_path.read_text(encoding="utf-8"))
                    validation = state["galleries"][0]["validation"]
                    self.assertEqual(0 if expected_check == "pass" else 1, code, stdout + stderr)
                    self.assertEqual(expected_access, validation["observed"]["audio_object_accessibility"])
                    self.assertEqual(expected_check, validation["checks"]["audio_object_accessibility"])

    def test_cli_classifies_audio_object_transport_errors_as_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            operation_path = root / "operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"document_id": "doc-1.mp3"}],
            }})

            def fail_transport(_url, **_kwargs):
                raise publisher.requests.ConnectionError("transport-secret")

            code, stdout, stderr = self._run_cli(
                operation_path,
                studio,
                execute=False,
                object_get=fail_transport,
            )

            self.assertEqual(1, code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            validation = state["galleries"][0]["validation"]
            self.assertEqual("unverified", validation["observed"]["audio_object_accessibility"])
            self.assertEqual("unverified", validation["checks"]["audio_object_accessibility"])
            self.assertEqual("unverified", state["galleries"][0]["audio_evidence"]["object_accessibility"])
            self.assertNotIn("transport-secret", stdout + stderr + json.dumps(state))

    def test_cli_rejects_operation_file_for_another_course_before_studio_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            operation_path = root / "operation.json"
            operation = self._operation(manifest)
            operation["course_id"] = "course-v1:OTHER+COURSE+RUN"
            self._write_operation_file(operation_path, operation)
            original_operation = operation_path.read_bytes()
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"document_id": "doc-1.mp3"}],
            }})

            code, stdout, stderr = self._run_cli(operation_path, studio, execute=False)

            self.assertEqual(2, code)
            self.assertEqual([], studio.calls)
            self.assertEqual(original_operation, operation_path.read_bytes())
            error = json.loads(stdout)
            self.assertEqual("failed", error["status"])
            self.assertIn("operation file does not match", error["error_summary"])
            self.assertEqual("", stderr)

    def test_summary_exposes_audio_binding_and_object_access_states_separately(self):
        scenarios = (
            (404, "missing", "mismatch", "issue", "对象缺失（404/NoSuchKey）"),
            (403, "unverified", "unverified", "unverified", "对象可访问性暂时无法确认"),
        )
        for status_code, expected_access, expected_check, expected_report_status, expected_message in scenarios:
            with self.subTest(expected_access=expected_access), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root)
                operation_path = root / "operation.json"
                self._write_operation_file(operation_path, self._operation(manifest))
                studio = FakeStudio({GALLERY_1: {
                    "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                    "time_config": valid_time_config(2),
                    "audios": [{"document_id": "doc-1.mp3"}],
                }})

                code, stdout, stderr = self._run_cli(
                    operation_path,
                    studio,
                    execute=False,
                    object_get=lambda _url, **_kwargs: FakeRangeResponse(
                        status_code,
                        content=b"" if status_code == 404 else b"",
                    ),
                )

                self.assertEqual(1, code, stdout + stderr)
                batch_result = json.loads(operation_path.read_text(encoding="utf-8"))["batch_result"]
                summary_gallery = batch_result["galleries"][0]
                checks = summary_gallery["checks"]
                self.assertEqual("pass", checks["audio_bindings"])
                self.assertEqual(expected_check, checks["audio_object_accessibility"])
                report = batch_result["operator_report"]
                self.assertEqual(expected_report_status, report["status"])
                audio_findings = [
                    finding
                    for group in report["groups"]
                    for finding in group["findings"]
                    if finding["category"] == "音频对象可访问性"
                ]
                self.assertEqual(1, len(audio_findings))
                self.assertIn("音频绑定存在", audio_findings[0]["message"])
                self.assertIn(expected_message, audio_findings[0]["message"])
                self.assertNotIn("完整", report["text"])

    def test_cli_does_not_request_download_url_when_binding_count_is_not_one(self):
        for audios in ([], [{"document_id": "doc-a.mp3"}, {"document_id": "doc-b.mp3"}]):
            with self.subTest(binding_count=len(audios)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root)
                operation_path = root / "operation.json"
                self._write_operation_file(operation_path, self._operation(manifest))
                studio = FakeStudio({GALLERY_1: {
                    "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                    "time_config": valid_time_config(2),
                    "audios": audios,
                }})
                code, stdout, stderr = self._run_cli(operation_path, studio, execute=False)
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                validation = state["galleries"][0]["validation"]
                self.assertEqual(1, code, stdout + stderr)
                self.assertEqual("mismatch", validation["checks"]["audio_bindings"])
                self.assertEqual("not_checked", validation["checks"]["audio_object_accessibility"])
                result = state["batch_result"]
                self.assertEqual("mismatch", result["galleries"][0]["checks"]["audio_bindings"])
                self.assertEqual("not_checked", result["galleries"][0]["checks"]["audio_object_accessibility"])
                findings = [
                    finding
                    for group in result["operator_report"]["groups"]
                    for finding in group["findings"]
                ]
                self.assertIn("音频绑定", [finding["category"] for finding in findings])
                self.assertNotIn("音频对象可访问性", [finding["category"] for finding in findings])
                self.assertFalse(any(call["path"].endswith("/get_audio_download_url") for call in studio.calls))

    def test_cli_repairs_only_definite_audio_object_missing_and_rechecks_it(self):
        for initial_access, expected_repairs in (("missing", 1), ("unverified", 0)):
            with self.subTest(initial_access=initial_access), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root)
                operation_path = root / "operation.json"
                self._write_operation_file(operation_path, self._operation(manifest))
                studio = FakeStudio({GALLERY_1: {
                    "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                    "time_config": valid_time_config(2),
                    "audios": [{"document_id": "doc-1.mp3"}],
                }})
                object_statuses = [404 if initial_access == "missing" else 403, 206]

                def get_object(_url, **_kwargs):
                    status = object_statuses.pop(0) if object_statuses else 206
                    return FakeRangeResponse(status, content=b"x" if status == 206 else b"")

                code, stdout, stderr = self._run_cli(
                    operation_path,
                    studio,
                    extra_args=("--skip-oss-multipart",),
                    object_get=get_object,
                )
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                entry = state["galleries"][0]
                save_audio_calls = [call for call in studio.calls if call["path"].endswith("/handler/save_audio")]
                self.assertEqual(0 if initial_access == "missing" else 1, code, stdout + stderr)
                self.assertEqual(expected_repairs, entry["repair_attempts"])
                self.assertEqual(expected_repairs, len(save_audio_calls))
                self.assertEqual(
                    "pass" if initial_access == "missing" else "unverified",
                    entry["validation"]["checks"]["audio_object_accessibility"],
                )

    def test_two_target_cli_pass_continues_after_unreadable_and_ambiguous_structure(self):
        class SharedStudio(FakeStudio):
            def __init__(self):
                self.summary1 = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
                self.summary2 = VERTICAL_2.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
                self.extra = VERTICAL_1.replace("type@vertical", "type@problem").replace("block@vertical", "block@extra")
                super().__init__({}, vertical_overrides={
                    VERTICAL_1: {"blocks": [{"id": self.extra, "category": "problem"}, {"id": self.summary1, "category": "html", "display_name": "本节要点"}]},
                    VERTICAL_2: {"blocks": [{"id": self.summary2, "category": "html", "display_name": "本节要点"}, {"id": self.summary2 + "-other", "category": "html", "display_name": "本节要点"}]},
                })
                self.fail_first_structure = True
                self.omit_first_asset_id = True

            def _response(self, method, path, kwargs):
                if method == "POST" and path == "/xblock/":
                    vertical = kwargs["json"]["parent_locator"]
                    gallery = GALLERY_1 if vertical == VERTICAL_1 else GALLERY_2
                    self.galleries[gallery] = {"files": [], "time_config": [], "audios": []}
                    self.vertical_overrides[vertical]["blocks"].append({"id": gallery, "category": "imagesgallery"})
                    return FakeResponse({"locator": gallery})
                if method == "GET" and path == f"/course/{COURSE_KEY}" and self.fail_first_structure:
                    self.fail_first_structure = False
                    return FakeResponse({"secret": "raw-error-secret"}, 503)
                if method == "POST" and path.endswith("/handler/file_upload") and self.omit_first_asset_id:
                    self.omit_first_asset_id = False
                    super()._response(method, path, kwargs)
                    return FakeResponse({"secret": "raw-upload-secret"})
                if method == "DELETE" and path.startswith("/xblock/"):
                    locator = path[len("/xblock/"):]
                    for override in self.vertical_overrides.values():
                        override["blocks"][:] = [block for block in override["blocks"] if block["id"] != locator]
                    return FakeResponse({"ok": True})
                if method == "PUT" and path.startswith("/xblock/"):
                    vertical = path[len("/xblock/"):]
                    blocks = self.vertical_overrides[vertical]["blocks"]
                    by_id = {block["id"]: block for block in blocks}
                    blocks[:] = [by_id[locator] for locator in kwargs["json"]["children"]]
                    return FakeResponse({"ok": True})
                return super()._response(method, path, kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            operation = self._operation(manifest, galleries=(GALLERY_1, GALLERY_2))
            for entry in operation["galleries"]:
                entry["gallery_locator"] = ""
                for key in ("publish", "initial_vertical_publish", "vertical_integrity"):
                    entry.pop(key)
            path = root / "operation.json"
            self._write_operation_file(path, operation)
            studio = SharedStudio()
            targets = root / "targets.json"
            targets.write_text(json.dumps({"targets": [
                {"gallery_key": f"gallery-{index}", "manifest": str(manifest), "target_vertical_block_location": vertical,
                 "studio_vertical_url": f"https://studio.example.com/container/{vertical}"}
                for index, vertical in enumerate((VERTICAL_1, VERTICAL_2), start=1)
            ]}), encoding="utf-8")
            argv = ["publisher", "--targets-file", str(targets), "--report-file", str(root / "report.json"),
                    "--execute", "--cookie", "sessionid=secret; csrftoken=secret", "--skip-oss-multipart",
                    "--operation-file", str(path), "--auth-retry-delay-seconds", "0"]
            out = StringIO()
            with patch("sys.argv", argv), patch.object(publisher.requests, "Session", return_value=studio.session), patch.dict(
                "os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False
            ), redirect_stdout(out):
                self.assertEqual(1, publisher.main())
            first_pass = copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
            boundary = len(studio.calls)
            initial_calls = studio.calls[:boundary]
            self.assertEqual(2, sum(call["path"] in {f"/xblock/{VERTICAL_1}", f"/xblock/{VERTICAL_2}"} and call["method"] == "POST" for call in initial_calls))
            self.assertEqual(4, sum(call["path"].endswith("/handler/file_upload") for call in initial_calls))
            self.assertEqual(1, sum(call["path"].endswith("/handler/get_files") for call in initial_calls))
            self.assertFalse(any(call["method"] == "DELETE" for call in initial_calls))
            code, stdout, stderr = self._run_cli(path, studio, extra_args=("--skip-oss-multipart",))
            self.assertEqual(1, code, stdout + stderr)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([entry["publish"] for entry in first_pass["galleries"]], [entry["publish"] for entry in state["galleries"]])
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("unverified", state["galleries"][1]["validation"]["status"])
            self.assertEqual([GALLERY_1, studio.summary1], [item["id"] for item in studio.vertical_overrides[VERTICAL_1]["blocks"]])
            self.assertEqual(3, len(studio.vertical_overrides[VERTICAL_2]["blocks"]))
            for secret in ("raw-error-secret", "raw-upload-secret", "sessionid=secret", "csrftoken=secret"):
                self.assertNotIn(secret, out.getvalue() + stdout + path.read_text(encoding="utf-8"))

    def test_cli_repairs_identifiable_extra_vertical_children_and_publishes(self):
        class StructuralStudio(FakeStudio):
            def _response(self, method, path, kwargs):
                blocks = self.vertical_overrides[VERTICAL_1]["blocks"]
                if method == "DELETE" and path.startswith("/xblock/"):
                    locator = path[len("/xblock/"):]
                    blocks[:] = [block for block in blocks if block["id"] != locator]
                    return FakeResponse({"ok": True})
                if method == "PUT" and path == f"/xblock/{VERTICAL_1}":
                    by_id = {block["id"]: block for block in blocks}
                    blocks[:] = [by_id[locator] for locator in kwargs["json"]["children"]]
                    return FakeResponse({"ok": True})
                return super()._response(method, path, kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            path = root / "operation.json"
            operation = self._operation(manifest)
            self._write_operation_file(path, operation)
            summary = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
            extra = VERTICAL_1.replace("type@vertical", "type@problem").replace("block@vertical", "block@extra")
            other_html = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@body")
            studio = StructuralStudio({GALLERY_1: {"files": [
                {"id": "asset-a", "display_name": "page-001.png"},
                {"id": "asset-b", "display_name": "page-002.png"},
            ], "time_config": valid_time_config(2), "audios": [{}]}}, vertical_overrides={VERTICAL_1: {"blocks": [
                {"id": summary, "category": "html", "display_name": "本节要点"},
                {"id": extra, "category": "problem"},
                {"id": other_html, "category": "html", "display_name": "关于本节要点的说明"},
                {"id": GALLERY_1, "category": "imagesgallery"},
            ]}})
            code, stdout, stderr = self._run_cli(path, studio, extra_args=("--skip-oss-multipart",))
            self.assertEqual(0, code, stdout + stderr)
            self.assertEqual([GALLERY_1, summary], [block["id"] for block in studio.vertical_overrides[VERTICAL_1]["blocks"]])
            self.assertTrue(any(call["method"] == "POST" and call["path"] == f"/xblock/{VERTICAL_1}" for call in studio.calls))
            self.assertEqual(operation["galleries"][0]["publish"], json.loads(path.read_text(encoding="utf-8"))["galleries"][0]["publish"])

    def test_cli_publishes_ambiguous_vertical_once_without_changing_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            path = root / "operation.json"
            self._write_operation_file(path, self._operation(manifest))
            summary = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
            second_summary = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary-copy")
            initial_blocks = [
                {"id": GALLERY_1, "category": "imagesgallery"},
                {"id": summary, "category": "html", "display_name": "本节要点"},
                {"id": second_summary, "category": "html", "display_name": "本节要点"},
            ]
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }}, vertical_overrides={VERTICAL_1: {
                "blocks": copy.deepcopy(initial_blocks),
                "published": False,
                "has_changes": True,
            }})

            exit_code, stdout, stderr = self._run_cli(path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(path.read_text(encoding="utf-8"))
            entry = state["galleries"][0]
            self.assertEqual("mismatch", entry["validation"]["checks"]["vertical_structure"])
            self.assertEqual("issue", state["course_structure_verification"]["subsections"][0]["status"])
            self.assertEqual("issue", state["batch_result"]["operator_report"]["status"])
            findings = state["batch_result"]["operator_report"]["groups"][0]["findings"]
            self.assertIn("组件结构", [finding["category"] for finding in findings])
            self.assertEqual(initial_blocks, studio.vertical_overrides[VERTICAL_1]["blocks"])
            self.assertEqual(1, sum(
                call["method"] == "POST" and call["path"] == f"/xblock/{VERTICAL_1}"
                for call in studio.calls
            ))
            self.assertFalse(any(
                call["method"] == "DELETE" or call["method"] == "PUT" and call["path"] == f"/xblock/{VERTICAL_1}"
                for call in studio.calls
            ))
            self.assertEqual(1, entry["repair_attempts"])
            self.assertEqual(1, len(entry["repair_history"]))
            self.assertEqual("completed", entry["repair_history"][0]["vertical_publish"]["status"])

    def test_cli_does_not_publish_ambiguous_vertical_when_gallery_evidence_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            path = root / "operation.json"
            self._write_operation_file(path, self._operation(manifest))
            summary = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
            second_summary = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary-copy")
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "wrong-a", "display_name": "page-001.png"},
                    {"id": "wrong-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }}, payload_overrides={(GALLERY_1, "get_audios"): {"audios": "unreadable"}}, vertical_overrides={VERTICAL_1: {
                "blocks": [
                    {"id": GALLERY_1, "category": "imagesgallery"},
                    {"id": summary, "category": "html", "display_name": "本节要点"},
                    {"id": second_summary, "category": "html", "display_name": "本节要点"},
                ],
            }})

            exit_code, stdout, stderr = self._run_cli(path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(path.read_text(encoding="utf-8"))
            entry = state["galleries"][0]
            self.assertEqual("unverified", entry["validation"]["checks"]["audio_bindings"])
            self.assertEqual("mismatch", entry["validation"]["checks"]["vertical_structure"])
            self.assertFalse(entry["validation"]["repairable"])
            self.assertEqual(0, entry["repair_attempts"])
            self.assertFalse(any(
                call["path"] == f"/xblock/{VERTICAL_1}" and call["method"] == "POST"
                for call in studio.calls
            ))

    def test_cli_publishes_ambiguous_vertical_without_a_saved_snapshot_once(self):
        for integrity_shape in ("missing", "no_snapshot"):
            with self.subTest(integrity_shape=integrity_shape), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root)
                operation = self._operation(manifest)
                if integrity_shape == "missing":
                    operation["galleries"][0].pop("vertical_integrity")
                else:
                    operation["galleries"][0]["vertical_integrity"] = {"status": "unresolved"}
                path = root / "operation.json"
                self._write_operation_file(path, operation)
                summary = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
                second_summary = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary-copy")
                studio = FakeStudio({GALLERY_1: {
                    "files": [
                        {"id": "asset-a", "display_name": "page-001.png"},
                        {"id": "asset-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                }}, vertical_overrides={VERTICAL_1: {
                    "blocks": [
                        {"id": GALLERY_1, "category": "imagesgallery"},
                        {"id": summary, "category": "html", "display_name": "本节要点"},
                        {"id": second_summary, "category": "html", "display_name": "本节要点"},
                    ],
                }})

                exit_code, stdout, stderr = self._run_cli(path, studio)

                self.assertEqual(1, exit_code, stdout + stderr)
                state = json.loads(path.read_text(encoding="utf-8"))
                entry = state["galleries"][0]
                self.assertEqual("unverified", entry["validation"]["checks"]["vertical_structure"])
                self.assertEqual("unverified", state["course_structure_verification"]["subsections"][0]["structure_status"])
                self.assertEqual("unverified", state["batch_result"]["operator_report"]["status"])
                self.assertIn("发布记录不足以唯一确认目标组件", state["batch_result"]["operator_report"]["text"])
                self.assertEqual(1, entry["repair_attempts"])
                self.assertEqual("publish_vertical", entry["repair_history"][0]["repair_mode"])
                self.assertEqual("publish_vertical", entry["repair_history"][0]["trigger_validation"]["repair_scope"])
                self.assertEqual("completed", entry["repair_history"][0]["vertical_publish"]["status"])
                self.assertEqual(1, sum(
                    call["method"] == "POST" and call["path"] == f"/xblock/{VERTICAL_1}"
                    for call in studio.calls
                ))
                self.assertFalse(any(
                    call["method"] == "DELETE" or call["method"] == "PUT" and call["path"] == f"/xblock/{VERTICAL_1}"
                    for call in studio.calls
                ))

    def test_publisher_then_validator_cli_complete_first_pass_before_batch_audit(self):
        class SharedStudio(FakeStudio):
            def __init__(self):
                summary = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
                extra = VERTICAL_1.replace("type@vertical", "type@problem").replace("block@vertical", "block@extra")
                super().__init__({}, vertical_overrides={VERTICAL_1: {"blocks": [
                    {"id": extra, "category": "problem", "display_name": "Extra"},
                    {"id": summary, "category": "html", "display_name": "本节要点"},
                ]}})

            def _response(self, method, path, kwargs):
                if method == "POST" and path == "/xblock/":
                    self.galleries[GALLERY_1] = {"files": [], "time_config": [], "audios": []}
                    self.vertical_overrides[VERTICAL_1]["blocks"].append({"id": GALLERY_1, "category": "imagesgallery"})
                    return FakeResponse({"locator": GALLERY_1})
                if method == "DELETE" and path.startswith("/xblock/"):
                    locator = path[len("/xblock/"):]
                    blocks = self.vertical_overrides[VERTICAL_1]["blocks"]
                    blocks[:] = [block for block in blocks if block["id"] != locator]
                    return FakeResponse({"ok": True})
                if method == "PUT" and path == f"/xblock/{VERTICAL_1}":
                    blocks = self.vertical_overrides[VERTICAL_1]["blocks"]
                    by_id = {block["id"]: block for block in blocks}
                    blocks[:] = [by_id[locator] for locator in kwargs["json"]["children"]]
                    return FakeResponse({"ok": True})
                return super()._response(method, path, kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest)
            operation["galleries"][0]["gallery_locator"] = ""
            operation["galleries"][0].pop("publish")
            operation["galleries"][0].pop("initial_vertical_publish")
            operation["galleries"][0].pop("vertical_integrity")
            operation_path = root / "operation.json"
            self._write_operation_file(operation_path, operation)
            studio = SharedStudio()
            course_path = root / "course.json"
            course_path.write_text(json.dumps(make_course_structure({}, studio.vertical_overrides)), encoding="utf-8")
            argv = ["publisher", "--studio-url", f"https://studio.example.com/container/{VERTICAL_1}",
                    "--manifest", str(manifest), "--course-structure-json", str(course_path), "--execute",
                    "--cookie", "sessionid=secret; csrftoken=secret", "--skip-oss-multipart",
                    "--operation-file", str(operation_path), "--gallery-key", "gallery-1", "--auth-retry-delay-seconds", "0"]
            output = StringIO()
            with patch("sys.argv", argv), patch.object(publisher.requests, "Session", return_value=studio.session), patch.dict(
                "os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False
            ), redirect_stdout(output):
                publish_exit = publisher.main()
            self.assertEqual(0, publish_exit, output.getvalue())
            before = json.loads(operation_path.read_text(encoding="utf-8"))["galleries"][0]["publish"]
            boundary = len(studio.calls)
            validate_exit, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))
            self.assertEqual(0, validate_exit, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual(before, state["galleries"][0]["publish"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])
            calls = studio.calls
            self.assertTrue(any(call["method"] == "POST" and call["path"] == f"/xblock/{VERTICAL_1}" for call in calls[:boundary]))
            self.assertTrue(any(call["path"].endswith("/handler/get_files") for call in calls[boundary:]))
            self.assertEqual([GALLERY_1, VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")],
                             [block["id"] for block in studio.vertical_overrides[VERTICAL_1]["blocks"]])

    def test_cli_repairs_empty_gallery_without_changing_initial_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["publish"]["asset_ids"] = []
            operation["galleries"][0]["publish"]["images"] = []
            initial_publish = copy.deepcopy(operation["galleries"][0]["publish"])
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {"files": [], "time_config": valid_time_config(2), "audios": [{}]}})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual(initial_publish, gallery["publish"])
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertEqual(1, gallery["repair_attempts"])
            repaired_gallery = studio.galleries[gallery["gallery_locator"]]
            self.assertNotEqual(GALLERY_1, gallery["gallery_locator"])
            self.assertEqual(["page-001.png", "page-002.png"], [item["display_name"] for item in repaired_gallery["files"]])

    def test_false_show_subtitles_is_preserved_without_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "show_subtitles": False,
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertTrue(state["batch_result"]["all_sections_verified"])
            self.assertEqual("pass", state["galleries"][0]["validation"]["checks"]["play_config"])
            self.assertFalse(studio.galleries[GALLERY_1]["show_subtitles"])
            self.assertFalse(any(call["path"].endswith("/handler/update_play_config") for call in studio.calls))
            self.assertFalse(any(call["path"].endswith("/handler/remove_files") for call in studio.calls))

    def test_missing_audio_binding_replaces_the_gallery_from_the_local_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "show_subtitles": False,
                "audios": [],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery_state = state["galleries"][0]
            repaired_locator = gallery_state["gallery_locator"]
            self.assertNotEqual(GALLERY_1, repaired_locator)
            self.assertEqual(2, len(studio.galleries[repaired_locator]["files"]))
            self.assertEqual(1, len(studio.galleries[repaired_locator]["audios"]))
            self.assertFalse(studio.galleries[repaired_locator]["show_subtitles"])
            self.assertEqual("gallery_replacement", gallery_state["repair_history"][0]["repair_mode"])
            paths = [call["path"] for call in studio.calls]
            self.assertTrue(any(path.endswith("/handler/save_audio") for path in paths))
            self.assertTrue(any(path.endswith("/handler/file_upload") for path in paths))
            self.assertTrue(any(path.endswith("/handler/update_play_config") for path in paths))
            self.assertFalse(any(path.endswith("/handler/remove_files") for path in paths))
            deleted_galleries = [
                call["path"][len("/xblock/"):]
                for call in studio.calls
                if call["method"] == "DELETE" and call["path"].startswith("/xblock/")
            ]
            self.assertEqual([GALLERY_1], deleted_galleries)

    def test_missing_subtitle_time_config_replaces_gallery_and_preserves_disabled_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": [],
                "show_subtitles": False,
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr)
            paths = [call["path"] for call in studio.calls]
            self.assertTrue(any(path.endswith("/handler/update_play_config") for path in paths))
            self.assertFalse(any(path.endswith("/handler/remove_files") for path in paths))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            repaired_locator = state["galleries"][0]["gallery_locator"]
            self.assertNotEqual(GALLERY_1, repaired_locator)
            self.assertTrue(any(path.endswith("/handler/file_upload") for path in paths))
            self.assertTrue(any(path.endswith("/handler/save_audio") for path in paths))
            self.assertEqual(valid_time_config(2), studio.galleries[repaired_locator]["time_config"])
            self.assertFalse(studio.galleries[repaired_locator]["show_subtitles"])

    def test_malformed_timeline_repairs_from_manifest_and_preserves_disabled_display(self):
        class FirstMalformedPlayConfigStudio(FakeStudio):
            def __init__(self, initial_play_config):
                super().__init__({GALLERY_1: {
                    "files": [
                        {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                        {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "show_subtitles": False,
                    "audios": [{}],
                }})
                self.initial_play_config = initial_play_config
                self.initial_play_config_reads = 0

            def _response(self, method, path, kwargs):
                if method == "POST" and path.endswith("/handler/get_play_config") and self.initial_play_config_reads == 0:
                    self.initial_play_config_reads += 1
                    return FakeResponse({"play_config": self.initial_play_config})
                return super()._response(method, path, kwargs)

        malformed_configs = (
            ("missing_time_config", {"show_subtitles": False}, "mismatch"),
            ("non_list_time_config", {"time_config": "not-a-list", "show_subtitles": False}, "unverified"),
        )
        for case, initial_play_config, expected_initial_status in malformed_configs:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                operation_path = root / "batch-operation.json"
                self._write_operation_file(operation_path, self._operation(manifest))
                studio = FirstMalformedPlayConfigStudio(initial_play_config)

                exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

                self.assertEqual(0, exit_code, stdout + stderr)
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                gallery_state = state["galleries"][0]
                self.assertEqual("verified", gallery_state["validation"]["status"])
                self.assertEqual(1, gallery_state["repair_attempts"])
                self.assertEqual(
                    expected_initial_status,
                    gallery_state["repair_history"][0]["trigger_validation"]["status"],
                )
                trigger_validation = gallery_state["repair_history"][0]["trigger_validation"]
                if case == "non_list_time_config":
                    self.assertTrue(trigger_validation["play_config_untrusted"])
                    self.assertNotIn("play_config_read_failed", trigger_validation)
                self.assertEqual("gallery_replacement", gallery_state["repair_history"][0]["repair_mode"])
                repaired_locator = gallery_state["gallery_locator"]
                self.assertNotEqual(GALLERY_1, repaired_locator)
                updates = [
                    call for call in studio.calls
                    if call["path"].endswith("/handler/update_play_config")
                ]
                self.assertEqual(1, len(updates))
                repaired_config = updates[0]["kwargs"]["json"]["play_config"]
                self.assertEqual(valid_time_config(2), repaired_config["time_config"])
                self.assertFalse(repaired_config["show_subtitles"])
                self.assertEqual(valid_time_config(2), studio.galleries[repaired_locator]["time_config"])
                self.assertFalse(studio.galleries[repaired_locator]["show_subtitles"])
                self.assertTrue(any(call["path"].endswith("/handler/file_upload") for call in studio.calls))
                self.assertTrue(any(call["path"].endswith("/handler/save_audio") for call in studio.calls))
                self.assertIn(GALLERY_1, [
                    call["path"][len("/xblock/"):]
                    for call in studio.calls
                    if call["method"] == "DELETE" and call["path"].startswith("/xblock/")
                ])

    def test_invalid_subtitle_visibility_remains_unverified_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "show_subtitles": False,
                "audios": [{}],
            }}, payload_overrides={(GALLERY_1, "get_play_config"): {
                "play_config": {"time_config": valid_time_config(2), "show_subtitles": "false"},
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery_state = state["galleries"][0]
            self.assertEqual("unverified", gallery_state["validation"]["status"])
            self.assertFalse(gallery_state["validation"]["repairable"])
            self.assertTrue(gallery_state["validation"]["play_config_untrusted"])
            self.assertNotIn("play_config_read_failed", gallery_state["validation"])
            self.assertEqual(0, gallery_state["repair_attempts"])
            self.assertEqual([], gallery_state["repair_history"])
            updates = [
                call for call in studio.calls
                if call["path"].endswith("/handler/update_play_config")
            ]
            self.assertEqual([], updates)
            self.assertFalse(any(call["method"] == "DELETE" for call in studio.calls))

    def test_unreadable_play_config_replaces_gallery_with_local_timeline_but_stays_unverified(self):
        class UnreadablePlayConfigStudio(FakeStudio):
            def _response(self, method, path, kwargs):
                if method == "POST" and path.endswith("/handler/get_play_config"):
                    return FakeResponse({"play_config": "unreadable"})
                return super()._response(method, path, kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = UnreadablePlayConfigStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": [],
                "show_subtitles": False,
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery_state = state["galleries"][0]
            self.assertEqual("unverified", gallery_state["validation"]["status"])
            self.assertTrue(gallery_state["validation"]["play_config_untrusted"])
            self.assertTrue(gallery_state["validation"]["play_config_read_failed"])
            self.assertEqual(2, gallery_state["repair_attempts"])
            self.assertTrue(all(
                attempt["repair_mode"] == "gallery_replacement"
                for attempt in gallery_state["repair_history"]
            ))
            self.assertNotEqual(GALLERY_1, gallery_state["gallery_locator"])
            self.assertEqual(valid_time_config(2), studio.galleries[gallery_state["gallery_locator"]]["time_config"])
            updates = [
                call for call in studio.calls
                if call["path"].endswith("/handler/update_play_config")
            ]
            self.assertEqual(2, len(updates))
            self.assertTrue(all(
                call["kwargs"]["json"]["play_config"]["time_config"] == valid_time_config(2)
                for call in updates
            ))
            self.assertFalse(state["batch_result"]["all_sections_verified"])
            self.assertTrue(studio.galleries[gallery_state["gallery_locator"]]["show_subtitles"])

    def test_image_count_mismatch_replaces_the_entire_gallery_without_deleting_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-old", "asset_key": "asset-old", "display_name": "old-page.png"}],
                "time_config": valid_time_config(2),
                "show_subtitles": False,
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery_state = state["galleries"][0]
            repaired_locator = gallery_state["gallery_locator"]
            self.assertNotEqual(GALLERY_1, repaired_locator)
            repaired_gallery = studio.galleries[repaired_locator]
            self.assertEqual(2, len(repaired_gallery["files"]))
            self.assertEqual(3, len(studio.assets))
            self.assertFalse(repaired_gallery["show_subtitles"])
            self.assertEqual(0, sum(call["path"].endswith("/handler/remove_files") for call in studio.calls))
            self.assertEqual(2, sum(call["path"].endswith("/handler/file_upload") for call in studio.calls))
            self.assertEqual([GALLERY_1], [
                call["path"][len("/xblock/"):]
                for call in studio.calls
                if call["method"] == "DELETE" and call["path"].startswith("/xblock/")
            ])
            self.assertTrue(any(call["path"].endswith("/handler/save_audio") for call in studio.calls))
            self.assertTrue(any(call["path"].endswith("/handler/update_play_config") for call in studio.calls))
            self.assertEqual("gallery_replacement", gallery_state["repair_history"][0]["repair_mode"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_ordered_image_filenames_match_when_expected_asset_ids_are_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["publish"].pop("asset_ids")
            operation["galleries"][0]["publish"].pop("images")
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-other-a", "asset_key": "asset-other-a", "display_name": "page-001.png"},
                    {"id": "asset-other-b", "asset_key": "asset-other-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, execute=False)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("pass", state["galleries"][0]["validation"]["checks"]["images"])
            self.assertFalse(any(call["path"].endswith("/handler/remove_files") for call in studio.calls))
            self.assertFalse(any(call["path"].endswith("/handler/file_upload") for call in studio.calls))

    def test_reordered_and_wrong_image_references_are_mismatch_and_repairable(self):
        scenarios = (
            (
                "reordered",
                [
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                ],
            ),
            (
                "wrong reference",
                [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-wrong", "asset_key": "asset-wrong", "display_name": "other-page.png"},
                ],
            ),
        )
        for label, files in scenarios:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                operation_path = root / "batch-operation.json"
                self._write_operation_file(operation_path, self._operation(manifest))
                studio = FakeStudio({GALLERY_1: {
                    "files": files,
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                }})

                exit_code, stdout, stderr = self._run_cli(operation_path, studio, execute=False)

                self.assertEqual(1, exit_code, stdout + stderr)
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                validation = state["galleries"][0]["validation"]
                self.assertEqual("mismatch", validation["status"])
                self.assertEqual("mismatch", validation["checks"]["images"])
                self.assertTrue(validation["repairable"])
                self.assertEqual(0, state["galleries"][0]["repair_attempts"])
                self.assertFalse(any(call["path"].endswith("/handler/remove_files") for call in studio.calls))
                self.assertFalse(any(call["path"].endswith("/handler/file_upload") for call in studio.calls))

    def test_known_image_reference_and_subtitle_differences_replace_the_whole_gallery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            wrong_time_config = valid_time_config(2)
            wrong_time_config[0]["subtitle"] = "旧字幕文本"
            wrong_time_config[1]["start"] = 1050
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "stale-asset-a", "asset_key": "stale-asset-a", "display_name": "page-001.png"},
                    {"id": "stale-asset-b", "asset_key": "stale-asset-b", "display_name": "page-002.png"},
                ],
                "time_config": wrong_time_config,
                "audios": [{"document_id": "doc-1.mp3"}],
            }})
            replacement_gallery = "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@replacement-gallery"
            summary_locator = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
            publish_calls = []

            def replace_target(target, _course_structure, publish_args, _adapter, *, previous_result=None):
                publish_calls.append((target, publish_args, previous_result))
                self.assertEqual("repair_publish", publish_args.checkpoint_field)
                self.assertTrue(publish_args.execute)
                self.assertEqual(VERTICAL_1, target["target_vertical_block_location"])
                persisted = json.loads(operation_path.read_text(encoding="utf-8"))
                entry = persisted["galleries"][0]
                entry["repair_publish"] = {
                    "status": "completed",
                    "completed_stage": "saved",
                    "asset_ids": ["replacement-a", "replacement-b"],
                    "images": [
                        {"page": 1, "image": "page-001.png", "asset_id": "replacement-a"},
                        {"page": 2, "image": "page-002.png", "asset_id": "replacement-b"},
                    ],
                    "audio_upload_evidence": {
                        "upload_key_path": "audio/repaired.mp3?signature=repair-secret",
                        "bucket": "course-audio",
                        "region": "cn-beijing",
                        "multipart_part_count": 3,
                        "multipart_completed": True,
                        "local_size_bytes": 1234,
                        "local_sha256": "a" * 64,
                        "Credentials": {"AccessKeySecret": "credential-secret"},
                    },
                }
                entry["repair_gallery_locator"] = replacement_gallery
                publisher.atomic_write_json(operation_path, persisted)
                studio.galleries.pop(GALLERY_1)
                studio.galleries[replacement_gallery] = {
                    "files": [
                        {"id": "replacement-a", "asset_key": "replacement-a", "display_name": "page-001.png"},
                        {"id": "replacement-b", "asset_key": "replacement-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{"document_id": "doc-1.mp3"}],
                }
                studio.vertical_overrides[VERTICAL_1] = {
                    "blocks": [
                        {"id": replacement_gallery, "category": "imagesgallery", "display_name": "有声幻灯片"},
                        {"id": summary_locator, "category": "html", "display_name": "本节要点"},
                    ],
                    "published": True,
                    "has_changes": False,
                    "visibility_state": "live",
                }
                studio.vertical_overrides[VERTICAL_2] = {"blocks": []}
                return {
                    "status": "replaced",
                    "content_status": "completed",
                    "created_block_locator": replacement_gallery,
                    "expected_vertical_snapshot_after_delete": {
                        "direct_child_count": 2,
                        "children": [
                            {"locator": replacement_gallery, "type": "imagesgallery", "order": 1},
                            {"locator": summary_locator, "type": "html", "order": 2},
                        ],
                    },
                    "steps": [
                        {"step": "create_block", "status": "completed", "block_locator": replacement_gallery},
                        {"step": "publish_vertical", "status": "completed"},
                    ],
                }

            with patch.object(publisher, "process_publish_target", side_effect=replace_target) as process_target:
                exit_code, stdout, stderr = self._run_cli(
                    operation_path,
                    studio,
                    extra_args=("--skip-oss-multipart",),
                    object_get=lambda _url, **_kwargs: FakeRangeResponse(),
                )

            self.assertEqual(0, exit_code, stdout + stderr)
            self.assertEqual(1, process_target.call_count)
            self.assertEqual(1, len(publish_calls))
            self.assertIsNone(publish_calls[0][2])
            self.assertFalse(any(
                call["path"].endswith(("/handler/remove_files", "/handler/update_play_config", "/handler/save_audio"))
                for call in studio.calls
            ))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            repair = gallery["repair_history"][0]
            self.assertEqual("gallery_replacement", repair["repair_mode"])
            self.assertEqual({
                "upload_key_path": "audio/repaired.mp3",
                "bucket": "course-audio",
                "region": "cn-beijing",
                "multipart_part_count": 3,
                "multipart_completed": True,
                "local_size_bytes": 1234,
                "local_sha256": "a" * 64,
            }, repair["publish"]["audio_upload_evidence"])
            self.assertNotIn("repair-secret", json.dumps(state))
            self.assertNotIn("credential-secret", json.dumps(state))
            self.assertEqual("mismatch", repair["trigger_validation"]["checks"]["images"])
            self.assertEqual("mismatch", repair["trigger_validation"]["checks"]["play_config"])
            self.assertIn("images", repair["repair_components"])
            self.assertIn("play_config", repair["repair_components"])
            self.assertEqual(replacement_gallery, gallery["gallery_locator"])
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertEqual("pass", gallery["validation"]["checks"]["images"])
            self.assertEqual("pass", gallery["validation"]["checks"]["play_config"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_new_repair_request_reaudits_live_state_and_gets_two_fresh_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "show_subtitles": False,
                "audios": [],
            }}, keep_mismatch_after_repair=True)

            first_exit, first_stdout, first_stderr = self._run_cli(
                operation_path, studio, extra_args=("--skip-oss-multipart",)
            )
            self.assertEqual(1, first_exit, first_stdout + first_stderr)
            first_audit_count = sum(call["path"].endswith("/handler/get_audios") for call in studio.calls)
            self.assertEqual(2, json.loads(operation_path.read_text(encoding="utf-8"))["galleries"][0]["repair_attempts"])

            studio.keep_mismatch_after_repair = False
            second_exit, second_stdout, second_stderr = self._run_cli(
                operation_path, studio, extra_args=("--skip-oss-multipart",)
            )

            self.assertEqual(0, second_exit, second_stdout + second_stderr)
            self.assertGreater(sum(call["path"].endswith("/handler/get_audios") for call in studio.calls), first_audit_count)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertEqual(3, gallery["repair_attempts"])
            self.assertEqual(1, gallery["repair_attempts_in_request"])

    def test_valid_time_config_entries_rejects_malformed_or_duplicate_pages(self):
        self.assertTrue(publisher.valid_time_config_entries(valid_time_config(2)))
        invalid_configs = (
            [{"page": True, "start": 0, "end": 1, "subtitle": "第一页"}],
            [{"page": 1, "start": -1, "end": 1, "subtitle": "第一页"}],
            [{"page": 1, "start": 2, "end": 1, "subtitle": "第一页"}],
            [{"page": 1, "start": 0, "end": 1, "subtitle": "  "}],
            [
                {"page": 1, "start": 0, "end": 1, "subtitle": "第一页"},
                {"page": 1, "start": 1, "end": 2, "subtitle": "重复页"},
            ],
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                self.assertFalse(publisher.valid_time_config_entries(config))

    def _write_bundle(self, root, page_count=2):
        items = []
        for page in range(1, page_count + 1):
            image = root / f"page-{page:03d}.png"
            image.write_bytes(f"image-{page}".encode())
            items.append({"page": page, "image": image.name, "subtitle": f"第{page}页", "start": (page - 1) * 1000, "end": page * 1000})
        audio = root / "full_speech.mp3"
        audio.write_bytes(b"fake audio")
        manifest = root / "imagesgallery.json"
        manifest.write_text(json.dumps({"audio": {"full_audio": str(audio)}, "items": items}, ensure_ascii=False), encoding="utf-8")
        return manifest

    def _write_repair_targets(self, root, manifest, **source_fields):
        section_id = "block-v1:ORG+COURSE+RUN+type@sequential+block@section"
        section_title = "1.1 Local repair section"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["section_id"] = section_id
        data["section_title"] = section_title
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        targets_path = root / "legacy-targets.json"
        target = {
            "gallery_key": "legacy-gallery-1",
            "manifest": manifest.name,
            "section_id": section_id,
            "section_title": section_title,
            **source_fields,
        }
        targets_path.write_text(json.dumps({"course_id": COURSE_KEY, "targets": [target]}, ensure_ascii=False), encoding="utf-8")
        return targets_path, section_id, section_title

    def _operation(self, manifest, *, galleries=(GALLERY_1,), initial_vertical_status="completed"):
        entries = []
        for index, locator in enumerate(galleries):
            vertical = VERTICAL_1 if locator == GALLERY_1 else VERTICAL_2
            entries.append({
                "gallery_key": f"gallery-{index + 1}",
                "manifest": str(manifest),
                "vertical_locator": vertical,
                "gallery_locator": locator,
                "publish": {
                    "status": "completed",
                    "completed_stage": "saved",
                    "asset_ids": ["asset-a", "asset-b"],
                    "images": [
                        {"page": page, "image": f"page-{page:03d}.png", "asset_id": asset_id}
                        for page, asset_id in enumerate(("asset-a", "asset-b"), start=1)
                    ],
                    "steps": [],
                },
                "initial_vertical_publish": {"status": initial_vertical_status},
                "vertical_integrity": {
                    "expected": {
                        "direct_child_count": 2,
                        "children": [
                            {"locator": locator, "type": "imagesgallery", "order": 1},
                            {
                                "locator": vertical.replace("type@vertical", "type@html").replace("block@vertical", "block@summary"),
                                "type": "html",
                                "order": 2,
                            },
                        ],
                    }
                },
            })
        return {"operation_id": "operation-1", "galleries": entries}

    def _run_cli(self, operation_path, studio, *, execute=True, extra_args=(), object_get=None):
        args = [
            "validate_imagegallery_batch.py",
            "--studio-url",
            f"https://studio.example.com/container/{VERTICAL_1}",
            "--operation-file",
            str(operation_path),
            "--cookie",
            "sessionid=session-secret; csrftoken=csrf-secret",
            "--auth-retry-delay-seconds",
            "0",
        ]
        args.append("--execute" if execute else "--check")
        args.extend(extra_args)
        stdout = StringIO()
        stderr = StringIO()
        object_get = object_get or (lambda _url, **_kwargs: FakeRangeResponse())
        with patch("sys.argv", args), patch.object(publisher.requests, "Session", return_value=studio.session), patch.object(
            publisher.requests, "get", side_effect=object_get
        ), patch.dict(
            "os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = module.main()
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _run_package_cli(self, targets_path, studio, *, execute=False, extra_args=(), object_get=None):
        args = [
            "validate_imagegallery_batch.py",
            "--studio-url",
            f"https://studio.example.com/container/{VERTICAL_1}",
            "--targets-file",
            str(targets_path),
            "--cookie",
            "sessionid=session-secret; csrftoken=csrf-secret",
            "--auth-retry-delay-seconds",
            "0",
            "--execute" if execute else "--check",
            *extra_args,
        ]
        stdout = StringIO()
        stderr = StringIO()
        object_get = object_get or (lambda _url, **_kwargs: FakeRangeResponse())
        with patch("sys.argv", args), patch.object(publisher.requests, "Session", return_value=studio.session), patch.object(
            publisher.requests, "get", side_effect=object_get
        ), patch.dict(
            "os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = module.main()
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _write_operation_file(self, path, operation):
        path.write_text(json.dumps(operation, ensure_ascii=False), encoding="utf-8")

    def test_cli_repairs_all_confirmed_batch_mismatches_and_reaudits_every_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest, galleries=(GALLERY_1, GALLERY_2))
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({
                GALLERY_1: {
                    "files": [
                        {"id": "wrong-a", "asset_key": "wrong-a", "display_name": "first-wrong.png"},
                        {"id": "wrong-b", "asset_key": "wrong-b", "display_name": "second-wrong.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "show_subtitles": False,
                    "audios": [{}],
                },
                GALLERY_2: {
                    "files": [
                        {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                        {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [],
                },
            })

            exit_code, stdout, stderr = self._run_cli(
                operation_path,
                studio,
                extra_args=("--skip-oss-multipart",),

            )

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual([1, 1], [item["repair_attempts"] for item in state["galleries"]])
            self.assertEqual(["verified", "verified"], [item["validation"]["status"] for item in state["galleries"]])
            repaired_gallery = state["galleries"][0]["gallery_locator"]
            self.assertFalse(studio.galleries[repaired_gallery]["show_subtitles"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_cli_allows_two_batch_repair_cycles_before_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [],
            }}, keep_mismatch_after_repair=True)

            exit_code, stdout, stderr = self._run_cli(
                operation_path,
                studio,
                extra_args=("--skip-oss-multipart",),

            )

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual(2, gallery["repair_attempts"])
            self.assertEqual([1, 2], [item["attempt"] for item in gallery["repair_history"]])
            self.assertEqual("mismatch", gallery["validation"]["status"])

    def test_cli_resumes_an_incomplete_second_repair_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            gallery = operation["galleries"][0]
            gallery["repair_attempts"] = 2
            gallery["repair_history"] = [
                {"attempt": 1, "status": "mismatch", "final_validation": {"status": "mismatch"}},
                {"attempt": 2, "status": "failed", "initial_publish": dict(gallery["publish"]), "steps": []},
            ]
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [],
            }})

            exit_code, stdout, stderr = self._run_cli(
                operation_path,
                studio,
                extra_args=("--skip-oss-multipart",),
            )

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual(2, gallery["repair_attempts"])
            self.assertEqual([1, 2], [item["attempt"] for item in gallery["repair_history"]])
            self.assertEqual("completed", gallery["repair_history"][1]["status"])
            self.assertIn("resumed_at", gallery["repair_history"][1])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_cli_repairs_safe_targets_while_leaving_unverified_targets_unwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest, galleries=(GALLERY_1, GALLERY_2)))
            studio = FakeStudio({
                GALLERY_1: {
                    "files": [{"id": "old-a", "asset_key": "old-a", "display_name": "wrong-1.png"}],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                },
                GALLERY_2: {
                    "files": [
                        {"id": "peer-a", "asset_key": "peer-a", "display_name": "page-001.png"},
                        {"id": "peer-b", "asset_key": "peer-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                },
            }, payload_overrides={(GALLERY_2, "get_audios"): {"audios": "unreadable"}})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertEqual(1, state["galleries"][0]["repair_attempts"])
            self.assertEqual("unverified", state["galleries"][1]["validation"]["status"])
            self.assertEqual(0, state["galleries"][1]["repair_attempts"])
            self.assertFalse(any(
                call["path"].startswith(f"/xblock/{GALLERY_2}/") and call["method"] != "GET"
                for call in studio.calls
            ))

    def test_cli_checks_only_target_subsections_in_the_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            unrelated_vertical = "block-v1:ORG+COURSE+RUN+type@vertical+block@outside-batch"
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }}, vertical_overrides={unrelated_vertical: {
                "published": False,
                "blocks": [{
                    "id": "block-v1:ORG+COURSE+RUN+type@html+block@outside-html",
                    "category": "html",
                    "display_name": "未发布的其他小节",
                }],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            verification = state["course_structure_verification"]
            self.assertEqual(1, verification["subsection_count"])
            self.assertEqual([VERTICAL_1], [item["vertical_locator"] for item in verification["subsections"]])

    def test_cli_uses_live_retained_components_when_prior_expectation_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["vertical_integrity"]["expected"] = {
                "direct_child_count": 3,
                "children": [
                    {"locator": GALLERY_1, "type": "imagesgallery", "order": 1},
                    {"locator": "block-v1:ORG+COURSE+RUN+type@html+block@summary", "type": "html", "order": 2},
                    {"locator": "block-v1:ORG+COURSE+RUN+type@problem+block@extra", "type": "problem", "order": 3},
                ],
            }
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertFalse(state["galleries"][0]["validation"]["repairable"])
            self.assertEqual(0, state["galleries"][0]["repair_attempts"])
            self.assertFalse(any(call["path"].startswith(f"/xblock/{GALLERY_1}/") for call in studio.calls))

    def test_observed_child_snapshot_rejects_missing_malformed_and_duplicate_children(self):
        locator = "block-v1:ORG+COURSE+RUN+type@html+block@body"
        self.assertIsNone(module.observed_child_snapshot({}))
        self.assertIsNone(module.observed_child_snapshot({"blocks": [{"category": "html"}]}))
        self.assertIsNone(module.observed_child_snapshot({"blocks": [{"block_location": locator}]}))
        self.assertIsNone(module.observed_child_snapshot({
            "blocks": [
                {"block_location": locator, "category": "html"},
                {"block_location": locator, "category": "html"},
            ],
        }))

    def test_cli_verifies_ordered_filenames_without_scanning_course_asset_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["repair_attempts"] = 2
            operation["skill_provenance"] = {"commit": "fixture-commit", "installation_path": "fixture-skill"}
            operation["provenance"] = {"repository_commit": "fallback-commit"}
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{"id": "audio-binding"}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr + operation_path.read_text(encoding="utf-8"))
            self.assertEqual("", stderr)
            self.assertEqual(2, studio.course_reads)
            self.assertFalse(any(call["path"].startswith(f"/assets/{COURSE_KEY}/") for call in studio.calls))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual(operation["skill_provenance"], state["skill_provenance"])
            self.assertEqual(operation["skill_provenance"], state["batch_result"]["skill_provenance"])
            self.assertEqual(operation["provenance"], state["batch_result"]["provenance"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])
            gallery = state["galleries"][0]
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertEqual(2, gallery["validation"]["observed"]["image_file_count"])
            self.assertEqual("pass", gallery["validation"]["checks"]["images"])
            self.assertNotIn("image_names", gallery["validation"]["checks"])
            self.assertNotIn("image_asset_ids", gallery["validation"]["checks"])
            all_output = stdout + stderr + json.dumps(state)
            for secret in ("success-sts-secret", "success-cookie-secret", "success-csrf-secret", "success-auth-secret", "success-token-secret", "session-secret", "csrf-secret"):
                self.assertNotIn(secret, all_output)
            self.assertNotIn("raw_response", all_output)

    def test_final_course_check_ignores_unpublished_untargeted_vertical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"id": "audio-binding"}],
            }}, vertical_overrides={VERTICAL_2: {
                "blocks": [{"id": "block-v1:ORG+COURSE+RUN+type@html+block@untargeted", "category": "html"}],
                "published": False,
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            course_check = state["course_structure_verification"]
            self.assertEqual(1, course_check["subsection_count"])
            self.assertEqual([VERTICAL_1], [item["vertical_locator"] for item in course_check["subsections"]])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_final_course_check_ignores_published_untargeted_structure_without_expectation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"id": "audio-binding"}],
            }}, vertical_overrides={VERTICAL_2: {
                "blocks": [{
                    "id": "block-v1:ORG+COURSE+RUN+type@html+block@untargeted",
                    "category": "html",
                }],
                "published": True,
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual(1, state["course_structure_verification"]["subsection_count"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_final_course_check_ignores_missing_publication_state_outside_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"id": "audio-binding"}],
            }}, vertical_overrides={VERTICAL_2: {
                "blocks": [{"id": "block-v1:ORG+COURSE+RUN+type@html+block@untargeted", "category": "html"}],
                "omit_published": True,
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            course_check = state["course_structure_verification"]
            self.assertEqual(1, course_check["subsection_count"])
            self.assertEqual([VERTICAL_1], [item["vertical_locator"] for item in course_check["subsections"]])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_final_course_read_failure_is_unverified_after_gallery_validation_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"id": "audio-binding"}],
            }}, fail_course_reads={2})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            self.assertEqual(2, studio.course_reads)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("unverified", state["course_structure_verification"]["status"])
            self.assertEqual(1, state["course_structure_verification"]["unverified_count"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])

    def test_initial_course_read_failure_keeps_result_unverified_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"id": "audio-binding"}],
            }}, fail_course_reads={1})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("unverified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("verified", state["course_structure_verification"]["status"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])
            self.assertEqual(0, state["galleries"][0]["repair_attempts"])
            self.assertFalse(any(call["method"] in {"DELETE", "PUT"} for call in studio.calls))
            report = state["batch_result"]["operator_report"]
            self.assertEqual("unverified", report["status"])
            self.assertIn("https://studio.example.com/container/" + VERTICAL_1, report["text"])
            self.assertIn("暂时无法读取", report["text"])

    def test_final_course_check_uses_identifiable_live_retained_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest)
            operation["galleries"][0]["vertical_integrity"]["expected"]["children"][0]["type"] = "html"
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"id": "audio-binding"}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            target = next(item for item in state["course_structure_verification"]["subsections"] if item["vertical_locator"] == VERTICAL_1)
            self.assertEqual("verified", target["structure_status"])
            self.assertEqual([], target["issues"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_final_course_check_uses_current_structure_after_unresolved_write_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest)
            operation["galleries"][0]["vertical_integrity"]["status"] = "unresolved"
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{"id": "audio-binding"}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            target = next(item for item in state["course_structure_verification"]["subsections"] if item["vertical_locator"] == VERTICAL_1)
            self.assertEqual("verified", target["status"])
            self.assertNotIn("publisher_vertical_integrity_unresolved", target["issues"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_cli_continues_after_one_gallery_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest, galleries=(GALLERY_1, GALLERY_2))
            operation["galleries"][1]["publish"]["asset_ids"] = ["b1", "b2"]
            operation["galleries"][1]["publish"]["images"] = [
                {"page": page, "image": f"page-{page:03d}.png", "asset_id": asset_id}
                for page, asset_id in enumerate(("b1", "b2"), start=1)
            ]
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({
                GALLERY_1: {"files": [], "time_config": valid_time_config(2), "audios": [{}]},
                GALLERY_2: {"files": [{"id": "b1"}, {"id": "b2"}], "time_config": valid_time_config(2), "audios": [{}]},
            }, payload_overrides={(GALLERY_1, "get_files"): {"files": "unreadable", "total_count": 0}})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("unverified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("pass", state["galleries"][0]["validation"]["checks"]["play_config"])
            self.assertEqual(0, state["galleries"][0]["repair_attempts"])
            self.assertEqual([], state["galleries"][0]["repair_history"])
            self.assertEqual("verified", state["galleries"][1]["validation"]["status"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])
            self.assertFalse(any(call["method"] == "DELETE" for call in studio.calls))
            self.assertFalse(any(call["path"].endswith(("/handler/remove_files", "/handler/file_upload", "/handler/save_audio", "/handler/update_play_config")) for call in studio.calls))
            self.assertFalse(any(call["path"].startswith(f"/xblock/{GALLERY_2}/") for call in studio.calls))
            all_output = stdout + stderr + json.dumps(state)
            for secret in (
                "raw-response-secret",
                "raw-body-secret",
                "error-token-secret",
                "error-cookie-secret",
                "error-csrf-secret",
                "error-auth-secret",
                "error-sts-secret",
                "success-sts-secret",
                "success-cookie-secret",
                "success-csrf-secret",
                "success-auth-secret",
                "success-token-secret",
                "session-secret",
                "csrf-secret",
            ):
                self.assertNotIn(secret, all_output)
            self.assertNotIn("raw_response", all_output)

    def test_unverified_audio_evidence_blocks_replacement_despite_known_image_mismatch(self):
        scenarios = (
            ("binding_unreadable", {(GALLERY_1, "get_audios"): {"audios": "unreadable"}}, None),
            ("object_unverified", {}, FakeRangeResponse(403, content=b"")),
        )
        for label, payload_overrides, range_response in scenarios:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                operation_path = root / "batch-operation.json"
                operation = self._operation(manifest)
                operation["galleries"][0]["publish"]["asset_ids"] = ["expected-a", "expected-b"]
                operation["galleries"][0]["publish"]["images"] = [
                    {"page": 1, "image": "page-001.png", "asset_id": "expected-a"},
                    {"page": 2, "image": "page-002.png", "asset_id": "expected-b"},
                ]
                self._write_operation_file(operation_path, operation)
                studio = FakeStudio({GALLERY_1: {
                    "files": [
                        {"id": "wrong-a", "asset_key": "wrong-a", "display_name": "page-001.png"},
                        {"id": "wrong-b", "asset_key": "wrong-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                }}, payload_overrides=payload_overrides)
                range_requests = []

                def get_audio_object(url, **kwargs):
                    range_requests.append((url, kwargs))
                    return range_response or FakeRangeResponse()

                exit_code, stdout, stderr = self._run_cli(
                    operation_path,
                    studio,
                    extra_args=("--skip-oss-multipart",),
                    object_get=get_audio_object,
                )

                self.assertEqual(1, exit_code, stdout + stderr)
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                gallery = state["galleries"][0]
                self.assertEqual("unverified", gallery["validation"]["status"])
                self.assertEqual("mismatch", gallery["validation"]["checks"]["images"])
                self.assertFalse(gallery["validation"]["repairable"])
                self.assertEqual(0, gallery["repair_attempts"])
                self.assertEqual([], gallery["repair_history"])
                self.assertEqual([GALLERY_1], list(studio.galleries))
                self.assertFalse(any(call["method"] == "DELETE" for call in studio.calls))
                self.assertFalse(any(call["path"].endswith((
                    "/handler/remove_files",
                    "/handler/file_upload",
                    "/handler/save_audio",
                    "/handler/update_play_config",
                )) for call in studio.calls))
                self.assertFalse(any(call["path"] == "/xblock/" or call["path"] == f"/xblock/{VERTICAL_1}" for call in studio.calls))
                self.assertEqual(0 if label == "binding_unreadable" else 1, len(range_requests))

    def test_cli_repairs_confirmed_image_count_mismatch_without_deleting_assets_and_rechecks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["publish"]["asset_ids"] = ["asset-checkpoint-orphan"]
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-old", "asset_key": "asset-old", "display_name": "old-page.png"}],
                "time_config": valid_time_config(1),
                "audios": [{"id": "old-binding"}],
            }}, extra_assets=[{"id": "asset-checkpoint-orphan", "display_name": "unreferenced-old.png"}])

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr)
            self.assertEqual("", stderr)
            methods_and_paths = [(call["method"], call["path"]) for call in studio.calls]
            gallery_delete_indexes = [
                i for i, (method, path) in enumerate(methods_and_paths)
                if method == "DELETE" and path.startswith("/xblock/")
            ]
            upload_indexes = [i for i, (method, path) in enumerate(methods_and_paths) if method == "POST" and path.endswith("/handler/file_upload")]
            publish_index = next(i for i, (method, path) in enumerate(methods_and_paths) if method == "POST" and path == f"/xblock/{VERTICAL_1}")
            self.assertEqual(1, len(gallery_delete_indexes))
            self.assertEqual(f"/xblock/{GALLERY_1}", methods_and_paths[gallery_delete_indexes[0]][1])
            self.assertLess(max(upload_indexes), gallery_delete_indexes[0])
            self.assertLess(gallery_delete_indexes[0], publish_index)
            self.assertFalse(any(path.endswith("/handler/remove_files") for _, path in methods_and_paths))
            self.assertTrue(any(asset["id"] == "asset-old" for asset in studio.assets))
            self.assertTrue(any(asset["id"] == "asset-checkpoint-orphan" for asset in studio.assets))
            self.assertFalse(any(call["path"].startswith(f"/assets/{COURSE_KEY}/") for call in studio.calls))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertNotEqual(GALLERY_1, gallery["gallery_locator"])
            self.assertEqual(1, gallery["repair_attempts"])
            self.assertEqual("completed", gallery["repair_history"][0]["status"])
            self.assertEqual("completed", gallery["repair_history"][0]["vertical_publish"]["status"])
            self.assertEqual(["asset-checkpoint-orphan"], gallery["publish"]["asset_ids"])
            self.assertEqual(["asset-new-001", "asset-new-002"], gallery["repair_publish"]["asset_ids"])
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_cli_stops_repair_after_two_cycles_when_final_gallery_still_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-old", "asset_key": "asset-old", "display_name": "old-page.png"}],
                "time_config": valid_time_config(1),
                "audios": [{"id": "old-binding"}],
            }}, keep_mismatch_after_repair=True)

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual(2, gallery["repair_attempts"])
            self.assertEqual("mismatch", gallery["validation"]["status"])
            self.assertEqual([1, 2], [item["attempt"] for item in gallery["repair_history"]])
            self.assertEqual("mismatch", gallery["repair_history"][1]["status"])
            self.assertTrue(any(call["path"].endswith("/handler/file_upload") for call in studio.calls))
            self.assertTrue(any(call["path"] == f"/xblock/{VERTICAL_1}" for call in studio.calls))

    def test_failed_repair_publication_is_reported_after_live_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-old", "asset_key": "asset-old"}],
                "time_config": valid_time_config(1),
                "audios": [{}],
            }}, fail_vertical_publish=True)

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("mismatch", gallery["validation"]["status"])
            self.assertEqual("mismatch", gallery["validation"]["checks"]["vertical_publication"])
            self.assertEqual(2, gallery["repair_attempts"])
            self.assertEqual("failed", gallery["repair_history"][-1]["status"])
            self.assertEqual("failed", gallery["repair_history"][-1]["vertical_publish"]["status"])
            self.assertEqual("failed", state["batch_result"]["galleries"][0]["repair_status"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])
            report = state["batch_result"]["operator_report"]
            self.assertEqual("issue", report["status"])
            self.assertIn("未发布改动", report["text"])
            self.assertIn(f"https://studio.example.com/container/{VERTICAL_1}", report["text"])

    def test_cli_repairs_play_config_and_zero_or_multiple_audio_binding_mismatches(self):
        cases = (
            ("play_config", {"time_config": valid_time_config(1), "audios": [{"id": "audio-1"}]}),
            ("invalid_time_entry", {"time_config": [
                {"page": 1, "start": 10, "end": 5, "subtitle": "第一页"},
                {"page": 2, "start": 1000, "end": 2000, "subtitle": "第二页"},
            ], "audios": [{"id": "audio-1"}]}),
            ("zero_audios", {"time_config": valid_time_config(2), "audios": []}),
            ("multiple_audios", {"time_config": valid_time_config(2), "audios": [{"id": "a1"}, {"id": "a2"}]}),
        )
        for label, state in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                operation_path = root / "batch-operation.json"
                operation = self._operation(manifest)
                operation["galleries"][0]["publish"]["asset_ids"] = ["asset-1", "asset-2"]
                operation["galleries"][0]["publish"]["images"] = [
                    {"page": 1, "image": "page-001.png", "asset_id": "asset-1"},
                    {"page": 2, "image": "page-002.png", "asset_id": "asset-2"},
                ]
                self._write_operation_file(operation_path, operation)
                gallery_state = {
                    "files": [{"id": "asset-1"}, {"id": "asset-2"}],
                    "time_config": state["time_config"],
                    "audios": state["audios"],
                }
                if "show_subtitles" in state:
                    gallery_state["show_subtitles"] = state["show_subtitles"]
                studio = FakeStudio({GALLERY_1: gallery_state})

                exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

                self.assertEqual(0, exit_code, stdout + stderr)
                operation = json.loads(operation_path.read_text(encoding="utf-8"))
                gallery = operation["galleries"][0]
                self.assertEqual(1, gallery["repair_attempts"])
                self.assertEqual("verified", gallery["validation"]["status"])
                self.assertTrue(operation["batch_result"]["all_sections_verified"])

    def test_audio_binding_mismatch_replaces_gallery_without_deleting_course_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["publish"]["asset_ids"] = ["asset-shared", "asset-orphan"]
            operation["galleries"][0]["publish"]["images"] = [
                {"page": 1, "image": "page-001.png", "asset_id": "asset-shared"},
                {"page": 2, "image": "page-002.png", "asset_id": "asset-orphan"},
            ]
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({
                GALLERY_1: {
                    "files": [
                        {"id": "asset-shared", "asset_key": "asset-shared"},
                        {"id": "asset-orphan", "asset_key": "asset-orphan"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [],
                },
                GALLERY_2: {
                    "files": [{"id": "asset-shared", "asset_key": "asset-shared"}],
                    "time_config": valid_time_config(1),
                    "audios": [{}],
                },
            })

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery_state = state["galleries"][0]
            repaired_locator = gallery_state["gallery_locator"]
            self.assertNotEqual(GALLERY_1, repaired_locator)
            repaired_gallery = studio.galleries[repaired_locator]
            self.assertEqual(2, len(repaired_gallery["files"]))
            self.assertEqual(1, len(repaired_gallery["audios"]))
            attempt = gallery_state["repair_history"][0]
            self.assertEqual(["audio_bindings"], attempt["repair_components"])
            self.assertFalse(any(call["path"].endswith("/handler/remove_files") for call in studio.calls))
            self.assertTrue(any(call["path"].endswith("/handler/file_upload") for call in studio.calls))
            self.assertTrue({"asset-shared", "asset-orphan"}.issubset({asset["id"] for asset in studio.assets}))
            self.assertEqual([GALLERY_1], [
                call["path"][len("/xblock/"):]
                for call in studio.calls
                if call["method"] == "DELETE" and call["path"].startswith("/xblock/")
            ])
            self.assertFalse(any(call["path"].startswith(f"/assets/{COURSE_KEY}/") for call in studio.calls))
            self.assertEqual("verified", gallery_state["validation"]["status"])
            self.assertEqual("verified", state["course_structure_verification"]["status"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_image_repair_does_not_scan_other_galleries_or_delete_course_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({
                GALLERY_1: {
                    "files": [{"id": "asset-old", "asset_key": "asset-old"}],
                    "time_config": valid_time_config(1),
                    "audios": [{}],
                },
                GALLERY_2: {
                    "files": [{"id": "peer-asset", "asset_key": "peer-asset"}],
                    "time_config": valid_time_config(1),
                    "audios": [{}],
                },
            })

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr + operation_path.read_text(encoding="utf-8"))
            self.assertEqual([GALLERY_1], [
                call["path"][len("/xblock/"):]
                for call in studio.calls
                if call["method"] == "DELETE" and call["path"].startswith("/xblock/")
            ])
            self.assertFalse(any(call["path"].startswith(f"/assets/{COURSE_KEY}/") for call in studio.calls))
            self.assertFalse(any(call["path"].startswith(f"/preview/xblock/{GALLERY_2}/") for call in studio.calls))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])

    def test_cli_blocks_unverified_gallery_evidence_and_replaces_an_unreadable_timeline(self):
        cases = (
            ("get_files", {"files": [{"name": "no-id"}], "total_count": 1}),
            ("get_play_config", {"play_config": {"time_config": "not-a-list"}}),
            ("get_play_config", {"play_config": {"time_config": valid_time_config(2), "show_subtitles": "true"}}),
            ("get_audios", {"audios": [{"id": "audio"}, None]}),
        )
        for handler, response in cases:
            with self.subTest(handler=handler), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                operation_path = root / "batch-operation.json"
                self._write_operation_file(operation_path, self._operation(manifest))
                studio = FakeStudio({GALLERY_1: {
                    "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                }}, payload_overrides={(GALLERY_1, handler): response})

                exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

                expected_exit = 0 if handler == "get_play_config" and not isinstance(response["play_config"].get("time_config"), list) else 1
                self.assertEqual(expected_exit, exit_code, stdout + stderr)
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                gallery = state["galleries"][0]
                if handler == "get_play_config" and not isinstance(response["play_config"].get("time_config"), list):
                    self.assertEqual("verified", gallery["validation"]["status"])
                    self.assertEqual(1, gallery["repair_attempts"])
                    trigger_validation = gallery["repair_history"][0]["trigger_validation"]
                    self.assertEqual("unverified", trigger_validation["status"])
                    self.assertTrue(trigger_validation["play_config_untrusted"])
                    self.assertNotIn("play_config_read_failed", trigger_validation)
                    self.assertEqual("gallery_replacement", gallery["repair_history"][0]["repair_mode"])
                    repaired = studio.galleries[gallery["gallery_locator"]]
                    self.assertEqual(valid_time_config(2), repaired["time_config"])
                    updates = [
                        call for call in studio.calls
                        if call["path"].endswith("/handler/update_play_config")
                    ]
                    self.assertTrue(updates)
                    self.assertEqual(valid_time_config(2), updates[-1]["kwargs"]["json"]["play_config"]["time_config"])
                else:
                    self.assertEqual("unverified", gallery["validation"]["status"])
                    self.assertEqual(0, gallery["repair_attempts"])
                    self.assertFalse(any(
                        call["method"] != "GET" and "/preview/xblock/" not in call["path"]
                        for call in studio.calls
                    ))

    def test_cli_uses_live_published_state_instead_of_initial_write_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest, initial_vertical_status="failed"))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_cli_does_not_pass_published_vertical_with_unpublished_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }}, vertical_overrides={VERTICAL_1: {
                "has_changes": True,
                "visibility_state": "needs_attention",
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertTrue(state["batch_result"]["all_sections_verified"])
            self.assertEqual("verified", state["course_structure_verification"]["subsections"][0]["status"])
            self.assertFalse(state["course_structure_verification"]["subsections"][0]["has_changes"])
            self.assertEqual(1, state["galleries"][0]["repair_attempts"])
            self.assertTrue(any(call["method"] == "POST" and call["path"] == f"/xblock/{VERTICAL_1}" for call in studio.calls))
            self.assertFalse(any(call["path"].endswith(("remove_files", "file_upload", "save_audio", "update_play_config")) for call in studio.calls))

    def test_cli_marks_pending_visibility_state_as_issue_even_when_has_changes_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }}, vertical_overrides={VERTICAL_1: {
                "has_changes": False,
                "visibility_state": "needs_attention",
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertTrue(state["batch_result"]["all_sections_verified"])
            self.assertFalse(state["course_structure_verification"]["subsections"][0]["has_changes"])
            self.assertEqual("live", state["course_structure_verification"]["subsections"][0]["visibility_state"])
            self.assertEqual(1, state["galleries"][0]["repair_attempts"])
            self.assertEqual(1, sum(call["method"] == "POST" and call["path"].startswith("/xblock/") and "+type@vertical+" in call["path"] for call in studio.calls))
            self.assertFalse(any(call["path"].endswith(("remove_files", "file_upload", "save_audio", "update_play_config")) for call in studio.calls))

    def test_cli_passes_target_published_without_pending_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            subsection = state["course_structure_verification"]["subsections"][0]
            self.assertTrue(state["batch_result"]["all_sections_verified"])
            self.assertTrue(subsection["published"])
            self.assertFalse(subsection["has_changes"])
            self.assertNotIn("visibility_state", subsection)
            self.assertFalse(any(call["method"] == "POST" and call["path"] == f"/xblock/{VERTICAL_1}" for call in studio.calls))

    def test_cli_does_not_write_when_saved_publication_evidence_is_missing_or_invalid(self):
        cases = (
            ({"omit_published": True, "has_changes": False}, "unverified"),
            ({"published": "true", "has_changes": False}, "unverified"),
            ({"omit_has_changes": True}, "unverified"),
            # A confirmed unpublished section remains an issue even when its
            # pending-change evidence is also missing.
            ({"published": False, "omit_has_changes": True}, "issue"),
            ({"has_changes": "false"}, "unverified"),
            ({"has_changes": False, "visibility_state": 1}, "unverified"),
        )
        for override, expected_section_status in cases:
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                operation_path = root / "batch-operation.json"
                self._write_operation_file(operation_path, self._operation(manifest))
                studio = FakeStudio({GALLERY_1: {
                    "files": [
                        {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                        {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                }}, vertical_overrides={VERTICAL_1: override})

                exit_code, stdout, stderr = self._run_cli(operation_path, studio)

                self.assertEqual(1, exit_code, stdout + stderr)
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                self.assertFalse(state["batch_result"]["all_sections_verified"])
                self.assertEqual("unverified", state["galleries"][0]["validation"]["status"])
                self.assertEqual(expected_section_status, state["course_structure_verification"]["subsections"][0]["status"])
                self.assertFalse(any(
                    call["method"] == "DELETE"
                    or call["path"].endswith(("remove_files", "file_upload", "change_files_order", "save_audio", "update_play_config"))
                    or (call["method"] == "POST" and call["path"].startswith("/xblock/") and "+type@vertical+" in call["path"])
                    for call in studio.calls
                ))

    def test_cli_marks_saved_publication_read_failure_unverified_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }}, fail_publication_reads={1, 2})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("unverified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("unverified", state["course_structure_verification"]["status"])
            self.assertFalse(any(
                call["method"] == "DELETE"
                or call["path"].endswith(("remove_files", "file_upload", "change_files_order", "save_audio", "update_play_config"))
                or (call["method"] == "POST" and call["path"].startswith("/xblock/") and "+type@vertical+" in call["path"])
                for call in studio.calls
            ))

    def test_batch_report_keeps_safe_publisher_and_vertical_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest)
            operation["galleries"][0]["publish"].update({
                "status": "failed",
                "completed_stage": "images_ordered",
                "failed_step": "upload_audio_prepare",
                "failure_http_status": 502,
                "error_summary": "Studio request failed (HTTP 502) during upload_audio_prepare",
            })
            operation["galleries"][0]["initial_vertical_publish"] = {
                "status": "failed",
                "failed_step": "publish_vertical",
                "http_status": 403,
                "error_summary": "Studio request failed (HTTP 403) during publish_vertical; Cookie=vertical-secret",
            }
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            result = state["batch_result"]["galleries"][0]
            self.assertEqual("upload_audio_prepare", result["publish"]["failed_step"])
            self.assertEqual(502, result["publish"]["failure_http_status"])
            self.assertEqual("publish_vertical", result["initial_vertical_publish"]["failed_step"])
            self.assertEqual(403, result["initial_vertical_publish"]["http_status"])
            self.assertNotIn("vertical-secret", stdout + stderr + json.dumps(state))

    def test_validation_uses_environment_cookie_and_csrf_without_persisting_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }})
            args = [
                "validate_imagegallery_batch.py",
                "--studio-url", f"https://studio.example.com/container/{VERTICAL_1}",
                "--operation-file", str(operation_path),
                "--execute",
                "--auth-retry-delay-seconds", "0",
            ]
            stdout = StringIO()
            env = {
                "FIRA_SAAS_OP_ACCOUNT": "",
                "FIRA_SAAS_OP_PASSWORD": "",
                "FIRA_SAAS_OP_COOKIE": "sessionid=session-secret",
                "FIRA_SAAS_OP_CSRF_TOKEN": "csrf-secret",
            }

            with patch("sys.argv", args), patch.object(publisher.requests, "Session", return_value=studio.session), patch.object(
                publisher.requests, "get", return_value=FakeRangeResponse()
            ), patch.dict("os.environ", env, clear=False), redirect_stdout(stdout):
                exit_code = module.main()

            self.assertEqual(0, exit_code, stdout.getvalue())
            self.assertTrue(any(call["path"].endswith("get_files") for call in studio.calls))
            self.assertEqual("csrf-secret", studio.calls[0]["kwargs"]["headers"]["X-CSRFToken"])
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertNotIn("session-secret", json.dumps(state))
            self.assertNotIn("csrf-secret", json.dumps(state))

    def test_all_clear_operator_report_is_short_and_has_no_problem_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": valid_time_config(2),
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            report = json.loads(operation_path.read_text(encoding="utf-8"))["batch_result"]["operator_report"]
            self.assertEqual("pass", report["status"])
            self.assertEqual("sections", report["mode"])
            self.assertEqual([], report["groups"])
            self.assertEqual("", report["summary"])
            self.assertIn("首次发布", report["text"])
            self.assertIn("后置审核", report["text"])

    def test_operator_report_shows_only_live_findings_with_subsection_links(self):
        class PersistentlyUnreadableSecondGalleryStudio(FakeStudio):
            def _response(self, method, path, kwargs):
                if method == "POST" and path == f"/preview/xblock/{GALLERY_2}/handler/get_files":
                    return FakeResponse({"files": "unreadable", "total_count": 0})
                return super()._response(method, path, kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest, galleries=(GALLERY_1, GALLERY_2))
            first = operation["galleries"][0]
            first["repair_attempts"] = 2
            first["publish"].update({
                "status": "failed",
                "failed_step": "upload_audio_prepare",
                "failure_http_status": 502,
                "error_summary": "Studio request failed (HTTP 502); token=operation-secret",
                "steps": [{"step": "upload_audio_prepare", "status": "failed"}],
            })
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, operation)
            studio = PersistentlyUnreadableSecondGalleryStudio({
                GALLERY_1: {
                    "files": [
                        {"id": "wrong-a", "asset_key": "wrong-a", "display_name": "wrong-a.png"},
                        {"id": "wrong-b", "asset_key": "wrong-b", "display_name": "wrong-b.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                },
                GALLERY_2: {
                    "files": [
                        {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                        {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                },
            })

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            report = state["batch_result"]["operator_report"]
            self.assertEqual("issue", report["status"])
            self.assertIn("https://studio.example.com/container/" + VERTICAL_1, report["text"])
            self.assertIn("https://studio.example.com/container/" + VERTICAL_2, report["text"])
            self.assertIn("图片", report["text"])
            self.assertIn("暂时无法", report["text"])
            for internal_detail in ("upload_audio_prepare", "准备音频上传失败", "HTTP 502", "operation-secret", "raw-response-secret", "成功"):
                self.assertNotIn(internal_detail, report["text"])
            self.assertEqual("upload_audio_prepare", state["batch_result"]["galleries"][0]["publish"]["failed_step"])
            self.assertEqual(502, state["batch_result"]["galleries"][0]["publish"]["failure_http_status"])
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("unverified", state["galleries"][1]["validation"]["status"])
            self.assertNotIn("operation-secret", json.dumps(state))

    def test_course_structure_completeness_rejects_a_missing_vertical_child_list(self):
        self.assertFalse(module.course_structure_payload_is_complete(make_incomplete_course_structure()))

    def test_package_target_does_not_fall_back_when_a_supplied_vertical_is_invalid(self):
        other_vertical = "block-v1:ORG+COURSE+RUN+type@vertical+block@other-vertical"
        other_gallery = "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@other-gallery"
        missing_vertical = "block-v1:ORG+COURSE+RUN+type@vertical+block@missing-vertical"
        course_structure = {
            "chapters": [{
                "sections": [
                    {
                        "name": "1.1 First section",
                        "block_location": "block-v1:ORG+COURSE+RUN+type@sequential+block@section-one",
                        "verticals": [{
                            "name": "First vertical",
                            "block_location": VERTICAL_1,
                            "blocks": [{"category": "imagesgallery", "block_location": GALLERY_1}],
                        }],
                    },
                    {
                        "name": "1.2 Other section",
                        "block_location": "block-v1:ORG+COURSE+RUN+type@sequential+block@section-two",
                        "verticals": [{
                            "name": "Other vertical",
                            "block_location": other_vertical,
                            "blocks": [{"category": "imagesgallery", "block_location": other_gallery}],
                        }],
                    },
                ],
            }],
        }
        cases = (
            ("target_vertical_block_location", missing_vertical, "missing target"),
            ("target_vertical_block_location", other_vertical, "outside matched section"),
            ("vertical_locator", missing_vertical, "legacy missing target"),
            ("vertical_locator", other_vertical, "legacy target outside matched section"),
            ("target_vertical_block_location", "not-a-locator", "malformed target"),
        )

        for locator_key, locator, label in cases:
            with self.subTest(label=label):
                resolution = module.resolve_package_target(
                    {"section_id": "1.1", locator_key: locator},
                    {"section_id": "1.1", "section_title": "1.1 First section"},
                    course_structure,
                    course_key=COURSE_KEY,
                )

                self.assertEqual("unverified", resolution["status"])
                self.assertNotEqual(VERTICAL_1, resolution.get("vertical_locator"))

    def test_package_cli_stops_ambiguous_section_id_mapping_and_reports_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root)
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_data.update({"section_id": "1.1", "section_title": "1.1 Duplicate section A"})
            manifest.write_text(json.dumps(manifest_data, ensure_ascii=False), encoding="utf-8")
            targets_path = root / "ambiguous-targets.json"
            targets_path.write_text(json.dumps({
                "course_id": COURSE_KEY,
                "targets": [{
                    "gallery_key": "ambiguous-gallery",
                    "manifest": manifest.name,
                    "section_id": "1.1",
                    "section_title": "1.1 Duplicate section A",
                }],
            }, ensure_ascii=False), encoding="utf-8")
            course_structure = {
                "course_id": COURSE_KEY,
                "chapters": [{
                    "name": "Chapter",
                    "sections": [
                        {
                            "name": "1.1 Duplicate section A",
                            "block_location": "block-v1:ORG+COURSE+RUN+type@sequential+block@section-a",
                            "verticals": [{
                                "name": "Vertical A",
                                "block_location": VERTICAL_1,
                                "published": True,
                                "has_changes": False,
                                "blocks": [
                                    {"category": "imagesgallery", "block_location": GALLERY_1},
                                    {
                                        "category": "html",
                                        "block_location": VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary"),
                                        "display_name": "本节要点",
                                    },
                                ],
                            }],
                        },
                        {
                            "name": "1.1 Duplicate section B",
                            "block_location": "block-v1:ORG+COURSE+RUN+type@sequential+block@section-b",
                            "verticals": [{
                                "name": "Vertical B",
                                "block_location": VERTICAL_2,
                                "published": True,
                                "has_changes": False,
                                "blocks": [
                                    {"category": "imagesgallery", "block_location": GALLERY_2},
                                    {
                                        "category": "html",
                                        "block_location": VERTICAL_2.replace("type@vertical", "type@html").replace("block@vertical", "block@summary"),
                                        "display_name": "本节要点",
                                    },
                                ],
                            }],
                        },
                    ],
                }],
            }

            class AmbiguousSectionStudio(FakeStudio):
                def _response(self, method, path, kwargs):
                    if method == "GET" and path == f"/course/{COURSE_KEY}":
                        if (kwargs.get("params") or {}).get("format") == "concise":
                            self.course_reads += 1
                        else:
                            self.publication_reads += 1
                        return FakeResponse(course_structure)
                    return super()._response(method, path, kwargs)

            studio = AmbiguousSectionStudio({})

            code, stdout, stderr = self._run_package_cli(targets_path, studio, execute=False)

            self.assertEqual(1, code, stdout + stderr)
            self.assertEqual("", stderr)
            self.assertFalse(any(call["path"].startswith("/xblock/") for call in studio.calls))
            state = json.loads((root / "ambiguous-targets.batch-operation.json").read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("unverified", gallery["target_mapping_status"])
            self.assertIn("more than one", gallery["target_mapping_error"])
            self.assertEqual("target_mapping", gallery["validation"]["failed_step"])
            self.assertEqual("unverified", state["batch_result"]["galleries"][0]["target_mapping_status"])
            self.assertIn("more than one", state["batch_result"]["galleries"][0]["target_mapping_error"])
            self.assertIn("无法唯一映射", state["batch_result"]["operator_report"]["text"])

    def test_package_cli_requires_a_matching_course_identity_for_each_target(self):
        section_locator = "block-v1:ORG+COURSE+RUN+type@sequential+block@section"
        scenarios = (
            ("missing identity", None, None, "1.1", "unverified", "verifiable course identity", 1),
            ("conflicting target identity", COURSE_KEY, "course-v1:OTHER+COURSE+RUN", section_locator, "unverified", "different Studio course", 1),
            ("section locator identifies course", None, None, section_locator, "resolved", "", 0),
        )
        for label, package_course, target_course, section_id, expected_mapping, expected_error, expected_code in scenarios:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
                manifest_data.update({"section_id": section_id, "section_title": "1.1 Local repair section"})
                manifest.write_text(json.dumps(manifest_data, ensure_ascii=False), encoding="utf-8")
                target = {
                    "gallery_key": "legacy-gallery-1",
                    "manifest": manifest.name,
                    "section_id": section_id,
                    "section_title": "1.1 Local repair section",
                }
                if target_course:
                    target["course_id"] = target_course
                targets = {"targets": [target]}
                if package_course:
                    targets["course_id"] = package_course
                targets_path = root / "legacy-targets.json"
                targets_path.write_text(json.dumps(targets, ensure_ascii=False), encoding="utf-8")
                studio = FakeStudio({GALLERY_1: {
                    "files": [
                        {"id": "asset-a", "display_name": "page-001.png"},
                        {"id": "asset-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{"document_id": "doc-1.mp3"}],
                }})

                code, stdout, stderr = self._run_package_cli(targets_path, studio, execute=True)

                self.assertEqual(expected_code, code, stdout + stderr)
                state = json.loads((root / "legacy-targets.batch-operation.json").read_text(encoding="utf-8"))
                gallery = state["galleries"][0]
                self.assertEqual(expected_mapping, gallery["target_mapping_status"])
                if expected_error:
                    self.assertIn(expected_error, gallery["target_mapping_error"])
                    self.assertEqual("target_mapping", gallery["validation"]["failed_step"])
                    self.assertIn(expected_error, state["batch_result"]["operator_report"]["text"])
                self.assertFalse(any(
                    call["method"] in {"POST", "PUT", "DELETE"} and call["path"].startswith("/xblock/")
                    for call in studio.calls
                ))

    def test_repair_publish_checkpoint_keeps_the_source_gallery_locator_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "operation.json"
            self._write_operation_file(operation_path, {
                "galleries": [{
                    "gallery_key": "gallery-1",
                    "manifest": str(manifest),
                    "vertical_locator": VERTICAL_1,
                    "gallery_locator": GALLERY_1,
                    "repair_publish": {},
                }],
            })
            replacement = "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@replacement"

            checkpoint = publisher.PublishCheckpoint.load(
                str(operation_path),
                "gallery-1",
                manifest_path=manifest,
                vertical_block_id=VERTICAL_1,
                field="repair_publish",
            )
            self.assertEqual("", checkpoint.gallery_locator)
            checkpoint.record_success(
                {"step": "create_block", "status": "ok", "block_locator": replacement},
                stage="block_created",
            )

            persisted = json.loads(operation_path.read_text(encoding="utf-8"))["galleries"][0]
            self.assertEqual(GALLERY_1, persisted["gallery_locator"])
            self.assertEqual(replacement, persisted["repair_gallery_locator"])

    def test_check_mode_does_not_rebuild_incomplete_local_materials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            targets_path, _section_id, _section_title = self._write_repair_targets(root, manifest)
            (root / "page-002.png").unlink()
            original_manifest = manifest.read_bytes()
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{"document_id": "doc-1.mp3"}],
            }})
            generator_root = root / "generator"
            (generator_root / "scripts").mkdir(parents=True)
            (generator_root / "scripts" / "repair_imagesgallery_materials.py").write_text("", encoding="utf-8")

            with patch.object(module.subprocess, "run") as run_generator:
                exit_code, stdout, stderr = self._run_package_cli(
                    targets_path,
                    studio,
                    execute=False,
                    extra_args=("--ppt-to-imagesgallery-root", str(generator_root)),
                )

            self.assertEqual(1, exit_code, stdout + stderr)
            run_generator.assert_not_called()
            self.assertFalse((root / "page-002.png").exists())
            self.assertEqual(original_manifest, manifest.read_bytes())
            state = json.loads((root / "legacy-targets.batch-operation.json").read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("incomplete", gallery["local_package_status"])
            self.assertEqual(["图片文件 page-002.png"], gallery["local_package_missing_items"])
            self.assertIn("图片文件 page-002.png", state["batch_result"]["operator_report"]["text"])
            self.assertNotIn("local_material_repair", gallery)
            self.assertFalse(any(call["path"].startswith("/xblock/") for call in studio.calls))

    def test_execute_repairs_local_materials_before_auditing_and_resolves_relative_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            targets_path, section_id, section_title = self._write_repair_targets(
                root,
                manifest,
                ppt="source.pptx",
                section_json="section.json",
                audio_search_root="audio-assets",
            )
            source_ppt = root / "source.pptx"
            source_ppt.write_bytes(b"fake deck")
            section_json = root / "section.json"
            section_json.write_text(json.dumps({
                "section_id": section_id,
                "section_title": section_title,
                "page_count": 2,
                "page_content": ["Lesson page one", "Lesson page two"],
            }), encoding="utf-8")
            audio_search_root = root / "audio-assets"
            audio_search_root.mkdir()
            missing_image = root / "page-002.png"
            missing_image.unlink()
            studio = FakeStudio({GALLERY_1: {
                "files": [
                    {"id": "asset-a", "display_name": "page-001.png"},
                    {"id": "asset-b", "display_name": "page-002.png"},
                ],
                "time_config": valid_time_config(2),
                "audios": [{"document_id": "doc-1.mp3"}],
            }})
            generator_root = root / "generator"
            repair_script = generator_root / "scripts" / "repair_imagesgallery_materials.py"
            repair_script.parent.mkdir(parents=True)
            repair_script.write_text("", encoding="utf-8")
            calls = []

            def repair_materials(command, **kwargs):
                calls.append((command, kwargs))
                missing_image.write_bytes(b"restored image")
                return SimpleNamespace(returncode=0, stdout="OK", stderr="")

            with patch.object(module.subprocess, "run", side_effect=repair_materials):
                exit_code, stdout, stderr = self._run_package_cli(
                    targets_path,
                    studio,
                    execute=True,
                    extra_args=("--ppt-to-imagesgallery-root", str(generator_root)),
                )

            self.assertEqual(0, exit_code, stdout + stderr)
            self.assertEqual(1, len(calls))
            command = calls[0][0]
            self.assertIsInstance(command, list)
            self.assertEqual(str(repair_script.resolve()), command[1])
            for flag, expected_path in (
                ("--manifest", manifest.resolve()),
                ("--ppt", source_ppt.resolve()),
                ("--section-json", section_json.resolve()),
                ("--audio-search-root", audio_search_root.resolve()),
            ):
                self.assertEqual(str(expected_path), command[command.index(flag) + 1])
            self.assertTrue(calls[0][1]["capture_output"])
            self.assertEqual("utf-8", calls[0][1]["encoding"])
            self.assertTrue(missing_image.is_file())
            state = json.loads((root / "legacy-targets.batch-operation.json").read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("complete", gallery["local_package_status"])
            self.assertEqual("completed", gallery["local_material_repair"]["status"])
            self.assertEqual("complete", state["batch_result"]["galleries"][0]["local_package_status"])
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertFalse(any(call["path"].startswith("/xblock/") for call in studio.calls))

    def test_failed_or_incomplete_local_repair_blocks_studio_writes(self):
        scenarios = (
            ("nonzero exit with a missing image", 1, "image", "图片文件 page-002.png"),
            ("incomplete after successful exit", 0, "audio", "音频文件 full_speech.mp3"),
        )
        for label, return_code, missing_kind, expected_missing_item in scenarios:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                targets_path, _section_id, _section_title = self._write_repair_targets(root, manifest)
                missing_path = root / ("page-002.png" if missing_kind == "image" else "full_speech.mp3")
                missing_path.unlink()
                studio = FakeStudio({GALLERY_1: {
                    "files": [
                        {"id": "asset-a", "display_name": "page-001.png"},
                        {"id": "asset-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{"document_id": "doc-1.mp3"}],
                }})
                generator_root = root / "generator"
                repair_script = generator_root / "scripts" / "repair_imagesgallery_materials.py"
                repair_script.parent.mkdir(parents=True)
                repair_script.write_text("", encoding="utf-8")

                with patch.object(
                    module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=return_code, stdout="token=output-secret", stderr="signature=signed-secret"),
                ):
                    exit_code, stdout, stderr = self._run_package_cli(
                        targets_path,
                        studio,
                        execute=True,
                        extra_args=("--ppt-to-imagesgallery-root", str(generator_root)),
                    )

                self.assertEqual(1, exit_code, stdout + stderr)
                self.assertFalse(missing_path.exists())
                self.assertFalse(any(call["path"].startswith("/xblock/") for call in studio.calls))
                state = json.loads((root / "legacy-targets.batch-operation.json").read_text(encoding="utf-8"))
                gallery = state["galleries"][0]
                self.assertEqual("incomplete", gallery["local_package_status"])
                self.assertEqual("failed", gallery["local_material_repair"]["status"])
                self.assertEqual(return_code, gallery["local_material_repair"]["return_code"])
                self.assertEqual([expected_missing_item], gallery["local_material_repair"]["missing_items"])
                self.assertEqual([expected_missing_item], gallery["local_package_missing_items"])
                self.assertFalse(gallery["validation"]["repairable"])
                report = state["batch_result"]["operator_report"]["text"]
                self.assertIn("本地素材恢复失败", report)
                self.assertIn(expected_missing_item, report)
                persisted = stdout + stderr + json.dumps(state)
                self.assertNotIn(str(missing_path), persisted)
                self.assertNotIn("output-secret", persisted)
                self.assertNotIn("signed-secret", persisted)

    def test_legacy_package_check_persists_a_read_only_missing_gallery_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            original_manifest = manifest.read_bytes()
            section_locator = "block-v1:ORG+COURSE+RUN+type@sequential+block@section"
            targets_path = root / "legacy-targets.json"
            targets_path.write_text(json.dumps({
                "course_id": COURSE_KEY,
                "targets": [{
                    "gallery_key": "legacy-gallery-1",
                    "manifest": str(manifest),
                    "section_id": section_locator,
                    "section_title": "1.1 Legacy section",
                }],
            }), encoding="utf-8")
            summary_locator = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
            studio = FakeStudio({}, vertical_overrides={VERTICAL_1: {
                "blocks": [{"id": summary_locator, "category": "html", "display_name": "本节要点"}],
            }})

            exit_code, stdout, stderr = self._run_package_cli(targets_path, studio, execute=False)

            self.assertEqual(1, exit_code, stdout + stderr)
            operation_path = root / "legacy-targets.batch-operation.json"
            self.assertTrue(operation_path.is_file())
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("resolved", gallery["target_mapping_status"])
            self.assertEqual("mismatch", gallery["validation"]["status"])
            self.assertEqual("mismatch", gallery["validation"]["checks"]["gallery_presence"])
            self.assertTrue(gallery["validation"]["repairable"])
            self.assertIn("缺少 ImageGallery", state["batch_result"]["operator_report"]["text"])
            self.assertEqual(original_manifest, manifest.read_bytes())
            self.assertFalse(any(call["path"].startswith("/xblock/") for call in studio.calls))

    def test_execute_replaces_a_missing_legacy_gallery_and_rechecks_the_returned_locator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            section_locator = "block-v1:ORG+COURSE+RUN+type@sequential+block@section"
            targets_path = root / "legacy-targets.json"
            targets_path.write_text(json.dumps({
                "course_id": COURSE_KEY,
                "targets": [{
                    "gallery_key": "legacy-gallery-1",
                    "manifest": str(manifest),
                    "section_id": section_locator,
                    "section_title": "1.1 Legacy section",
                }],
            }), encoding="utf-8")
            summary_locator = VERTICAL_1.replace("type@vertical", "type@html").replace("block@vertical", "block@summary")
            studio = FakeStudio({}, vertical_overrides={VERTICAL_1: {
                "blocks": [{"id": summary_locator, "category": "html", "display_name": "本节要点"}],
            }})
            new_gallery = GALLERY_1
            expected_snapshot = {
                "direct_child_count": 2,
                "children": [
                    {"locator": new_gallery, "type": "imagesgallery", "order": 1},
                    {"locator": summary_locator, "type": "html", "order": 2},
                ],
            }
            publish_calls = []

            def fake_publish_target(target, _course_structure, publish_args, _adapter, *, previous_result=None):
                publish_calls.append((target, publish_args, previous_result))
                self.assertEqual("repair_publish", publish_args.checkpoint_field)
                self.assertTrue(publish_args.execute)
                operation_path = Path(publish_args.operation_file)
                operation = json.loads(operation_path.read_text(encoding="utf-8"))
                entry = next(item for item in operation["galleries"] if item["gallery_key"] == "legacy-gallery-1")
                entry["repair_publish"] = {
                    "status": "completed",
                    "completed_stage": "saved",
                    "asset_ids": ["asset-a", "asset-b"],
                    "images": [
                        {"page": 1, "image": "page-001.png", "asset_id": "asset-a"},
                        {"page": 2, "image": "page-002.png", "asset_id": "asset-b"},
                    ],
                }
                entry["repair_gallery_locator"] = new_gallery
                entry["repair_vertical_publish"] = {"status": "completed"}
                publisher.atomic_write_json(operation_path, operation)
                studio.galleries[new_gallery] = {
                    "files": [
                        {"id": "asset-a", "asset_key": "asset-a", "display_name": "page-001.png"},
                        {"id": "asset-b", "asset_key": "asset-b", "display_name": "page-002.png"},
                    ],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                }
                studio.vertical_overrides[VERTICAL_1] = {
                    "blocks": [
                        {"id": new_gallery, "category": "imagesgallery", "display_name": "有声幻灯片"},
                        {"id": summary_locator, "category": "html", "display_name": "本节要点"},
                    ],
                }
                return {
                    "status": "replaced",
                    "content_status": "completed",
                    "created_block_locator": new_gallery,
                    "expected_vertical_snapshot_after_delete": expected_snapshot,
                    "steps": [
                        {"step": "create_block", "status": "completed", "block_locator": new_gallery},
                        {"step": "publish_vertical", "status": "completed"},
                    ],
                }

            with patch.object(publisher, "process_publish_target", side_effect=fake_publish_target):
                exit_code, stdout, stderr = self._run_package_cli(targets_path, studio, execute=True)

            self.assertEqual(0, exit_code, stdout + stderr)
            self.assertEqual(1, len(publish_calls))
            self.assertEqual(VERTICAL_1, publish_calls[0][0]["target_vertical_block_location"])
            operation_path = root / "legacy-targets.batch-operation.json"
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual(new_gallery, gallery["gallery_locator"])
            self.assertEqual(new_gallery, gallery["repair_gallery_locator"])
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertEqual("verified", state["batch_result"]["status"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])
            self.assertGreater(studio.path_counts[f"/preview/xblock/{new_gallery}/handler/get_files"], 0)
            self.assertEqual("not_applicable", state["batch_result"]["galleries"][0]["operation_status"])
            self.assertFalse(any(
                finding["category"] == "首次发布"
                for group in state["batch_result"]["operator_report"]["groups"]
                for finding in group["findings"]
            ))

    def test_final_course_structure_verification_rejects_a_partial_tree(self):
        payload = make_incomplete_course_structure()
        with patch.object(module, "read_json_call", return_value=(payload, {"status": "ok"})):
            result = module.verify_final_course_structure(
                object(),
                studio_base="https://studio.example.com",
                course_key=COURSE_KEY,
                studio_url="https://studio.example.com/container/" + VERTICAL_1,
                operation={"galleries": []},
            )

        self.assertEqual("unverified", result["status"])
        self.assertEqual(0, result["subsection_count"])
        self.assertIn("incomplete", result["error_summary"].lower())

if __name__ == "__main__":
    unittest.main()
