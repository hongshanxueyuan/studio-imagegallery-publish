import importlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
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


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)
        self.payload = payload

    def json(self):
        return self.payload


class FakeCookies:
    def set(self, *_args, **_kwargs):
        return None


class FakeStudio:
    def __init__(self, galleries, *, keep_mismatch_after_repair=False, fail_get_files=None, payload_overrides=None, extra_assets=None, fail_vertical_publish=False, vertical_overrides=None, fail_course_reads=None):
        self.galleries = galleries
        self.keep_mismatch_after_repair = keep_mismatch_after_repair
        self.fail_get_files = fail_get_files or {}
        self.payload_overrides = payload_overrides or {}
        self.fail_vertical_publish = fail_vertical_publish
        self.vertical_overrides = vertical_overrides or {}
        self.fail_course_reads = set(fail_course_reads or [])
        self.calls = []
        self.path_counts = {}
        self.get_files_counts = {}
        self.course_reads = 0
        self.next_asset = 1
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

    def _response(self, method, path, kwargs):
        if method == "GET" and path == f"/course/{COURSE_KEY}":
            self.course_reads += 1
            if self.course_reads in self.fail_course_reads:
                return FakeResponse({"detail": "structure unavailable"}, 503)
            return FakeResponse(make_course_structure(self.galleries, self.vertical_overrides))
        if method == "GET" and path == f"/assets/{COURSE_KEY}/":
            page = int((kwargs.get("params") or {}).get("page", 0))
            return FakeResponse({"assets": self.assets if page == 0 else [], "totalCount": len(self.assets), "page": page, "pageSize": 50})

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
                return FakeResponse(override if override is not None else {"play_config": {"time_config": gallery["time_config"]}})
            if method == "POST" and path == preview_prefix + "get_audios":
                override = self.payload_overrides.get((gallery_locator, "get_audios"))
                return FakeResponse(override if override is not None else {"audios": gallery["audios"]})
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
                    gallery["time_config"] = (kwargs.get("json") or {}).get("play_config", {}).get("time_config", [])
                return FakeResponse({"ok": True})

        if method == "POST" and path == "/api/edxdal/v1/documents/":
            return FakeResponse({"ok": True})
        if method == "POST" and path.startswith("/xblock/") and "+type@vertical+" in path:
            if self.fail_vertical_publish:
                return FakeResponse({"detail": "vertical publish failed", "Authorization": "Bearer vertical-secret"}, 503)
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
    blocks_by_vertical = {VERTICAL_1: [], VERTICAL_2: []}
    for locator in galleries:
        vertical = VERTICAL_1 if locator == GALLERY_1 else VERTICAL_2
        blocks_by_vertical[vertical].append({"id": locator, "category": "imagesgallery", "display_name": "有声幻灯片"})
    vertical_overrides = vertical_overrides or {}
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
                        "direct_child_count": 1,
                        "children": [{"locator": locator, "type": "imagesgallery", "order": 1}],
                    }
                },
            })
        return {"operation_id": "operation-1", "galleries": entries}

    def _run_cli(self, operation_path, studio, *, extra_args=(), audit_report=None, finalize_audit=True):
        args = [
            "validate_imagegallery_batch.py",
            "--studio-url",
            f"https://studio.example.com/container/{VERTICAL_1}",
            "--operation-file",
            str(operation_path),
            "--execute",
            "--cookie",
            "sessionid=session-secret; csrftoken=csrf-secret",
            "--auth-retry-delay-seconds",
            "0",
            *extra_args,
        ]
        stdout = StringIO()
        stderr = StringIO()
        default_audit_report = {
            "course_id": COURSE_KEY,
            "status": "pass",
            "pass_count": 1,
            "issue_count": 0,
            "unverified_count": 0,
            "sequential_sections": [{
                "name": "1.2",
                "locator": "block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                "studio_url": "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                "checks": [{"check_id": "NLMCA-013", "status": "pass"}],
            }],
            "target_sections": [],
            "course_findings": [],
        }
        if audit_report is None:
            audit_report = default_audit_report
        with patch("sys.argv", args), patch.object(publisher.requests, "Session", return_value=studio.session), patch.dict(
            "os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = module.main()
        if not finalize_audit:
            return exit_code, stdout.getvalue(), stderr.getvalue()

        request = json.loads(operation_path.read_text(encoding="utf-8"))["course_audit_request"]
        report = json.loads(json.dumps(audit_report))
        report.setdefault("course_id", request["course_id"])
        report_path = Path(request["report_path"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        finalize_stdout = StringIO()
        finalize_stderr = StringIO()
        finalize_args = [
            "validate_imagegallery_batch.py",
            "--operation-file", str(operation_path),
            "--finalize-audit-report", str(report_path),
        ]
        with patch("sys.argv", finalize_args), redirect_stdout(finalize_stdout), redirect_stderr(finalize_stderr):
            exit_code = module.main()
        return exit_code, finalize_stdout.getvalue(), finalize_stderr.getvalue()

    def _write_operation_file(self, path, operation):
        path.write_text(json.dumps(operation, ensure_ascii=False), encoding="utf-8")

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

    def test_cli_reports_filename_mismatch_without_scanning_course_asset_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["repair_attempts"] = 1
            operation["skill_provenance"] = {"commit": "fixture-commit", "installation_path": "fixture-skill"}
            operation["provenance"] = {"repository_commit": "fallback-commit"}
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a", "display_name": "renamed-a.png"}, {"id": "asset-b", "display_name": "renamed-b.png"}],
                "time_config": [{"page": 1}, {"page": 2}],
                "audios": [{"id": "audio-binding"}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr + operation_path.read_text(encoding="utf-8"))
            self.assertEqual("", stderr)
            self.assertEqual(2, studio.course_reads)
            self.assertFalse(any(call["path"].startswith(f"/assets/{COURSE_KEY}/") for call in studio.calls))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual(operation["skill_provenance"], state["skill_provenance"])
            self.assertEqual(operation["skill_provenance"], state["batch_result"]["skill_provenance"])
            self.assertEqual(operation["provenance"], state["batch_result"]["provenance"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])
            gallery = state["galleries"][0]
            self.assertEqual("mismatch", gallery["validation"]["status"])
            self.assertEqual(2, gallery["validation"]["observed"]["image_file_count"])
            self.assertEqual("mismatch", gallery["validation"]["checks"]["image_names"])
            self.assertEqual("pass", gallery["validation"]["checks"]["image_asset_ids"])
            all_output = stdout + stderr + json.dumps(state)
            for secret in ("success-sts-secret", "success-cookie-secret", "success-csrf-secret", "success-auth-secret", "success-token-secret", "session-secret", "csrf-secret"):
                self.assertNotIn(secret, all_output)
            self.assertNotIn("raw_response", all_output)

    def test_final_course_check_includes_unpublished_untargeted_vertical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{"page": 1}, {"page": 2}],
                "audios": [{"id": "audio-binding"}],
            }}, vertical_overrides={VERTICAL_2: {
                "blocks": [{"id": "block-v1:ORG+COURSE+RUN+type@html+block@untargeted", "category": "html"}],
                "published": False,
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            course_check = state["course_structure_verification"]
            untargeted = next(item for item in course_check["subsections"] if item["vertical_locator"] == VERTICAL_2)
            self.assertEqual("issue", untargeted["status"])
            self.assertEqual(False, untargeted["published"])
            self.assertIn("published_false", untargeted["issues"])
            self.assertEqual(1, course_check["issue_count"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])

    def test_final_course_check_marks_published_untargeted_structure_unverified_without_expectation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{"page": 1}, {"page": 2}],
                "audios": [{"id": "audio-binding"}],
            }}, vertical_overrides={VERTICAL_2: {
                "blocks": [{
                    "id": "block-v1:ORG+COURSE+RUN+type@html+block@untargeted",
                    "category": "html",
                }],
                "published": True,
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            untargeted = next(
                item
                for item in state["course_structure_verification"]["subsections"]
                if item["vertical_locator"] == VERTICAL_2
            )
            self.assertEqual("unverified", untargeted["status"])
            self.assertEqual("unverified", untargeted["structure_status"])
            self.assertIn("expected_structure_missing", untargeted["unverified_reasons"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])

    def test_final_course_check_marks_missing_published_state_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{"page": 1}, {"page": 2}],
                "audios": [{"id": "audio-binding"}],
            }}, vertical_overrides={VERTICAL_2: {
                "blocks": [{"id": "block-v1:ORG+COURSE+RUN+type@html+block@untargeted", "category": "html"}],
                "omit_published": True,
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            course_check = state["course_structure_verification"]
            untargeted = next(item for item in course_check["subsections"] if item["vertical_locator"] == VERTICAL_2)
            self.assertEqual("unverified", untargeted["status"])
            self.assertIsNone(untargeted["published"])
            self.assertIn("published_state_missing", untargeted["unverified_reasons"])
            self.assertGreaterEqual(course_check["unverified_count"], 1)
            self.assertFalse(state["batch_result"]["all_sections_verified"])

    def test_final_course_read_failure_is_unverified_after_gallery_validation_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{"page": 1}, {"page": 2}],
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

    def test_initial_course_read_failure_does_not_skip_gallery_content_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{"page": 1}, {"page": 2}],
                "audios": [{"id": "audio-binding"}],
            }}, fail_course_reads={1})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("verified", state["course_structure_verification"]["status"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_final_course_check_compares_targeted_vertical_with_publisher_expectation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest)
            operation["galleries"][0]["vertical_integrity"]["expected"]["children"][0]["type"] = "html"
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{"page": 1}, {"page": 2}],
                "audios": [{"id": "audio-binding"}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            target = next(item for item in state["course_structure_verification"]["subsections"] if item["vertical_locator"] == VERTICAL_1)
            self.assertEqual("issue", target["structure_status"])
            self.assertIn("structure_mismatch", target["issues"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])

    def test_final_course_check_retains_unresolved_publisher_vertical_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest)
            operation["galleries"][0]["vertical_integrity"]["status"] = "unresolved"
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{"page": 1}, {"page": 2}],
                "audios": [{"id": "audio-binding"}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            target = next(item for item in state["course_structure_verification"]["subsections"] if item["vertical_locator"] == VERTICAL_1)
            self.assertEqual("issue", target["status"])
            self.assertIn("publisher_vertical_integrity_unresolved", target["issues"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])

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
                GALLERY_1: {"files": [], "time_config": [], "audios": []},
                GALLERY_2: {"files": [{"id": "b1"}, {"id": "b2"}], "time_config": [{}, {}], "audios": [{}]},
            }, fail_get_files={GALLERY_1: 1})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            self.assertEqual(2, studio.course_reads)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("unverified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("verified", state["galleries"][1]["validation"]["status"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])
            self.assertFalse(any(call["method"] in {"DELETE", "PUT"} for call in studio.calls))
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

    def test_cli_repairs_one_confirmed_mismatch_deletes_only_a_freshly_unreferenced_asset_and_rechecks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["galleries"][0]["publish"]["asset_ids"] = ["asset-checkpoint-orphan"]
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-old", "asset_key": "asset-old", "display_name": "old-page.png"}],
                "time_config": [{"page": 1}],
                "audios": [{"id": "old-binding"}],
            }}, extra_assets=[{"id": "asset-checkpoint-orphan", "display_name": "unreferenced-old.png"}])

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr)
            self.assertEqual("", stderr)
            methods_and_paths = [(call["method"], call["path"]) for call in studio.calls]
            remove_index = next(i for i, (method, path) in enumerate(methods_and_paths) if method == "POST" and path.endswith("/handler/remove_files"))
            delete_indexes = [i for i, (method, path) in enumerate(methods_and_paths) if method == "DELETE"]
            upload_indexes = [i for i, (method, path) in enumerate(methods_and_paths) if method == "POST" and path.endswith("/handler/file_upload")]
            publish_index = next(i for i, (method, path) in enumerate(methods_and_paths) if method == "POST" and path == f"/xblock/{VERTICAL_1}")
            self.assertEqual(2, len(delete_indexes))
            self.assertLess(remove_index, min(delete_indexes))
            self.assertLess(max(delete_indexes), min(upload_indexes))
            self.assertLess(max(upload_indexes), publish_index)
            self.assertEqual(5, studio.course_reads)
            self.assertEqual({
                f"/assets/{COURSE_KEY}/asset-old",
                f"/assets/{COURSE_KEY}/asset-checkpoint-orphan",
            }, {call["path"] for call in studio.calls if call["method"] == "DELETE"})
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual(1, gallery["repair_attempts"])
            self.assertEqual("completed", gallery["repair_history"][0]["status"])
            self.assertEqual("completed", gallery["repair_history"][0]["vertical_publish"]["status"])
            self.assertEqual(["asset-new-001", "asset-new-002"], gallery["publish"]["asset_ids"], json.dumps(state, ensure_ascii=False, indent=2))
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_cli_stops_repair_after_one_attempt_when_final_gallery_still_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-old", "asset_key": "asset-old", "display_name": "old-page.png"}],
                "time_config": [{"page": 1}],
                "audios": [{"id": "old-binding"}],
            }}, keep_mismatch_after_repair=True)

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual(1, gallery["repair_attempts"])
            self.assertEqual("mismatch", gallery["validation"]["status"])
            self.assertEqual("mismatch", gallery["repair_history"][0]["status"])
            self.assertTrue(any(call["path"].endswith("/handler/file_upload") for call in studio.calls))
            self.assertTrue(any(call["path"] == f"/xblock/{VERTICAL_1}" for call in studio.calls))

    def test_cli_keeps_verified_content_status_when_repair_vertical_publish_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-old", "asset_key": "asset-old"}],
                "time_config": [{}],
                "audios": [{}],
            }}, fail_vertical_publish=True)

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertEqual("failed", gallery["repair_history"][0]["status"])
            self.assertEqual("failed", gallery["repair_history"][0]["vertical_publish"]["status"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])

    def test_cli_repairs_play_config_and_zero_or_multiple_audio_binding_mismatches(self):
        cases = (
            ("play_config", {"time_config": [{"page": 1}], "audios": [{"id": "audio-1"}]}),
            ("zero_audios", {"time_config": [{"page": 1}, {"page": 2}], "audios": []}),
            ("multiple_audios", {"time_config": [{"page": 1}, {"page": 2}], "audios": [{"id": "a1"}, {"id": "a2"}]}),
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
                studio = FakeStudio({GALLERY_1: {
                    "files": [{"id": "asset-1"}, {"id": "asset-2"}],
                    "time_config": state["time_config"],
                    "audios": state["audios"],
                }})

                exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

                self.assertEqual(0, exit_code, stdout + stderr)
                operation = json.loads(operation_path.read_text(encoding="utf-8"))
                gallery = operation["galleries"][0]
                self.assertEqual(1, gallery["repair_attempts"])
                self.assertEqual("verified", gallery["validation"]["status"])
                self.assertTrue(operation["batch_result"]["all_sections_verified"])

    def test_cli_keeps_assets_referenced_by_another_gallery_and_deletes_only_orphans(self):
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
                    "time_config": [{"page": 1}, {"page": 2}],
                    "audios": [],
                },
                GALLERY_2: {
                    "files": [{"id": "asset-shared", "asset_key": "asset-shared"}],
                    "time_config": [{}],
                    "audios": [{}],
                },
            })

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(1, exit_code, stdout + stderr)
            deleted_paths = [call["path"] for call in studio.calls if call["method"] == "DELETE"]
            self.assertEqual([f"/assets/{COURSE_KEY}/asset-orphan"], deleted_paths)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            cleanup = state["galleries"][0]["repair_history"][0]["asset_cleanup"]
            self.assertIn("asset-shared", cleanup["retained_asset_ids"])
            self.assertIn("asset-orphan", cleanup["deleted_asset_ids"])
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("unverified", state["course_structure_verification"]["status"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])
            delete_index = next(i for i, call in enumerate(studio.calls) if call["method"] == "DELETE")
            peer_reference_index = max(i for i, call in enumerate(studio.calls[:delete_index]) if call["path"].endswith(f"/preview/xblock/{GALLERY_2}/handler/get_files"))
            self.assertLess(peer_reference_index, delete_index)

    def test_cli_skips_deletion_when_fresh_all_gallery_reference_scan_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-old", "asset_key": "asset-old"}],
                "time_config": [{}],
                "audios": [{}],
            }}, fail_get_files={GALLERY_1: 5})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, extra_args=("--skip-oss-multipart",))

            self.assertEqual(0, exit_code, stdout + stderr + operation_path.read_text(encoding="utf-8"))
            self.assertFalse(any(call["method"] == "DELETE" for call in studio.calls))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            cleanup = state["galleries"][0]["repair_history"][0]["asset_cleanup"]
            self.assertEqual("incomplete", cleanup["status"])
            self.assertEqual(["asset-old"], cleanup["skipped_asset_ids"])
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])

    def test_cli_marks_malformed_handler_responses_unverified_without_repair(self):
        cases = (
            ("get_files", {"files": [{"name": "no-id"}], "total_count": 1}),
            ("get_play_config", {"play_config": {"time_config": "not-a-list"}}),
            ("get_audios", {"audios": [{"id": "audio"}, None]}),
        )
        for handler, response in cases:
            with self.subTest(handler=handler), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = self._write_bundle(root, page_count=2)
                operation_path = root / "batch-operation.json"
                self._write_operation_file(operation_path, self._operation(manifest))
                studio = FakeStudio({GALLERY_1: {
                    "files": [{"id": "a"}, {"id": "b"}],
                    "time_config": [{}, {}],
                    "audios": [{}],
                }}, payload_overrides={(GALLERY_1, handler): response})

                exit_code, stdout, stderr = self._run_cli(operation_path, studio)

                self.assertEqual(1, exit_code, stdout + stderr)
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                self.assertEqual("unverified", state["galleries"][0]["validation"]["status"])
                self.assertEqual(0, state["galleries"][0]["repair_attempts"])
                self.assertFalse(any(call["method"] in {"DELETE", "PUT"} for call in studio.calls))

    def test_cli_requires_successful_initial_vertical_publish_for_batch_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest, initial_vertical_status="failed"))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertFalse(state["batch_result"]["all_sections_verified"])

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
                "time_config": [{}, {}],
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            result = state["batch_result"]["galleries"][0]
            self.assertEqual("upload_audio_prepare", result["publish"]["failed_step"])
            self.assertEqual(502, result["publish"]["failure_http_status"])
            self.assertEqual("publish_vertical", result["initial_vertical_publish"]["failed_step"])
            self.assertEqual(403, result["initial_vertical_publish"]["http_status"])
            self.assertNotIn("vertical-secret", stdout + stderr + json.dumps(state))

    def test_partial_publish_failure_still_returns_audit_request_after_gallery_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest, initial_vertical_status="failed")
            operation["galleries"][0]["publish"].update({
                "status": "failed",
                "failed_step": "upload_image[2]",
                "error_summary": "Studio request failed (HTTP 503) during upload_image[2]",
            })
            operation["galleries"][0]["vertical_integrity"].update({
                "status": "unresolved",
                "steps": [{"step": "resolve_replacement_target", "status": "failed"}],
            })
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, finalize_audit=False)

            self.assertEqual(1, exit_code, stdout + stderr)
            self.assertTrue(any(call["path"].endswith("get_audios") for call in studio.calls))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("failed", state["galleries"][0]["publish"]["status"])
            self.assertEqual("pending", state["course_audit_request"]["status"])
            self.assertEqual("pending", state["batch_result"]["course_audit"]["status"])

    def test_validation_persists_pending_audit_request_without_running_auditor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, finalize_audit=False)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            result = state["batch_result"]
            request = result["course_audit_request"]
            self.assertEqual("pending", request["status"])
            self.assertEqual(COURSE_KEY, request["course_id"])
            self.assertEqual("https://studio.example.com/course/" + COURSE_KEY, request["course_url"])
            working_directory = operation_path.with_name("batch-operation.course-audit")
            self.assertEqual(str(working_directory), request["working_directory"])
            self.assertEqual(
                str(working_directory / "auditor" / "course-v1_ORG+COURSE+RUN" / "report.json"),
                request["report_path"],
            )
            self.assertEqual(request, state["course_audit_request"])
            self.assertEqual("pending", result["course_audit"]["status"])
            self.assertEqual("unverified", result["operator_report"]["status"])
            self.assertIn("待运行", result["operator_report"]["text"])

    def test_validation_uses_environment_cookie_and_csrf_without_persisting_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
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

            with patch("sys.argv", args), patch.object(publisher.requests, "Session", return_value=studio.session), patch.dict(
                "os.environ", env, clear=False
            ), redirect_stdout(stdout):
                exit_code = module.main()

            self.assertEqual(1, exit_code, stdout.getvalue())
            self.assertTrue(any(call["path"].endswith("get_files") for call in studio.calls))
            self.assertEqual("csrf-secret", studio.calls[0]["kwargs"]["headers"]["X-CSRFToken"])
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("pending", state["course_audit_request"]["status"])
            self.assertNotIn("session-secret", json.dumps(state))
            self.assertNotIn("csrf-secret", json.dumps(state))

    def test_finalize_audit_report_merges_saved_report_without_studio_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})
            self._run_cli(operation_path, studio, finalize_audit=False)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            request = state["course_audit_request"]
            report_path = Path(request["report_path"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps({
                "course_id": COURSE_KEY,
                "status": "issue",
                "pass_count": 0,
                "issue_count": 1,
                "unverified_count": 0,
                "sequential_sections": [{
                    "name": "1.2",
                    "locator": "block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                    "studio_url": "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                    "checks": [{
                        "check_id": "NLMCA-013",
                        "status": "issue",
                        "actual": {"published": True, "has_changes": True},
                    }],
                }],
                "target_sections": [],
                "course_findings": [],
            }, ensure_ascii=False), encoding="utf-8")
            studio_call_count = len(studio.calls)
            stdout = StringIO()
            finalize_args = [
                "validate_imagegallery_batch.py",
                "--operation-file", str(operation_path),
                "--finalize-audit-report", str(report_path),
            ]

            with patch("sys.argv", finalize_args), redirect_stdout(stdout):
                exit_code = module.main()

            self.assertEqual(1, exit_code, stdout.getvalue())
            self.assertEqual(studio_call_count, len(studio.calls))
            result = json.loads(operation_path.read_text(encoding="utf-8"))["batch_result"]
            self.assertEqual("issue", result["course_audit"]["status"])
            self.assertEqual("https://studio.example.com/course/" + COURSE_KEY, result["course_audit"]["course_url"])
            self.assertEqual("completed", result["course_audit_request"]["status"])
            self.assertEqual("issue", result["operator_report"]["status"])
            self.assertIn("未发布改动", result["operator_report"]["text"])

    def test_operator_report_groups_publisher_and_auditor_findings_by_sequential_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation = self._operation(manifest, initial_vertical_status="failed")
            operation["galleries"][0]["publish"].update({
                "status": "failed",
                "failed_step": "upload_image[2]",
                "error_summary": "Studio request failed (HTTP 503) during upload_image[2]",
            })
            operation["galleries"][0]["vertical_integrity"].update({
                "status": "unresolved",
                "steps": [{"step": "resolve_replacement_target", "status": "failed"}],
            })
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})
            auditor_report = {
                "status": "issue",
                "pass_count": 1,
                "issue_count": 1,
                "unverified_count": 0,
                "sequential_sections": [{
                    "name": "1.2",
                    "locator": "block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                    "studio_url": "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                    "checks": [{
                        "check_id": "NLMCA-013",
                        "name": "Sequential publication state",
                        "status": "issue",
                        "actual": {"published": True, "has_changes": True},
                    }],
                }],
                "target_sections": [],
                "course_findings": [],
            }

            self._run_cli(operation_path, studio, audit_report=auditor_report)

            result = json.loads(operation_path.read_text(encoding="utf-8"))["batch_result"]
            self.assertEqual("issue", result["course_audit"]["status"])
            self.assertFalse(result["all_sections_verified"])
            report = result["operator_report"]
            self.assertEqual("issue", report["status"])
            self.assertEqual("categories", report["mode"])
            groups_by_name = {group["name"]: group for group in report["groups"]}
            self.assertIn("ImageGallery 相关问题", groups_by_name)
            self.assertIn("章节未发布", groups_by_name)
            imagegallery_finding = groups_by_name["ImageGallery 相关问题"]["findings"][0]
            self.assertEqual("1.2", imagegallery_finding["name"])
            self.assertEqual(
                "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                imagegallery_finding["studio_url"],
            )
            self.assertEqual(VERTICAL_1, imagegallery_finding["element_locator"])
            self.assertEqual("小节", imagegallery_finding["element_name"])
            self.assertTrue(any("图片" in item["message"] for item in groups_by_name["ImageGallery 相关问题"]["findings"]))
            self.assertTrue(any("替换" in item["message"] for item in groups_by_name["小节组件结构"]["findings"]))
            self.assertTrue(any("未发布改动" in item["message"] for item in groups_by_name["章节未发布"]["findings"]))

    def test_operator_report_can_keep_the_section_grouped_layout(self):
        report = module._operator_report({
            "course_structure_verification": {"status": "verified", "subsections": []},
            "course_audit": {
                "status": "issue",
                "sequential_sections": [{
                    "name": "1.2",
                    "locator": "block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                    "studio_url": "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                    "checks": [{
                        "check_id": "NLMCA-013",
                        "status": "issue",
                        "actual": {"published": False, "has_changes": False},
                    }],
                }],
                "target_sections": [],
                "course_findings": [],
            },
            "galleries": [],
        }, report_mode="sections")

        self.assertEqual("sections", report["mode"])
        self.assertEqual("1.2", report["groups"][0]["name"])
        self.assertIn("没有公开版本", report["text"])

    def test_cli_report_mode_can_select_the_section_grouped_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})
            audit_report = {
                "course_id": COURSE_KEY,
                "status": "issue",
                "issue_count": 1,
                "unverified_count": 0,
                "sequential_sections": [{
                    "name": "1.2",
                    "locator": "block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                    "studio_url": "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@sequential+block@section",
                    "checks": [{
                        "check_id": "NLMCA-013",
                        "status": "issue",
                        "actual": {"published": False, "has_changes": False},
                    }],
                }],
                "target_sections": [],
                "course_findings": [],
            }

            self._run_cli(
                operation_path,
                studio,
                extra_args=("--report-mode", "sections"),
                audit_report=audit_report,
            )

            report = json.loads(operation_path.read_text(encoding="utf-8"))["batch_result"]["operator_report"]
            self.assertEqual("sections", report["mode"])
            self.assertEqual("1.2", report["groups"][0]["name"])

    def test_operator_report_groups_subtitle_findings_and_links_the_target_vertical(self):
        sequential_locator = "block-v1:ORG+COURSE+RUN+type@sequential+block@section"
        sequential_url = "https://studio.example.com/container/" + sequential_locator
        vertical_url = "https://studio.example.com/container/" + VERTICAL_1
        report = module._operator_report({
            "course_structure_verification": {
                "status": "verified",
                "subsections": [{
                    "section_title": "1.2 课程小节",
                    "section_locator": sequential_locator,
                    "vertical_title": "赋能内容",
                    "vertical_locator": VERTICAL_1,
                    "studio_url": vertical_url,
                }],
            },
            "course_audit": {
                "status": "issue",
                "sequential_sections": [],
                "target_sections": [{
                    "name": "赋能内容",
                    "locator": VERTICAL_1,
                    "studio_url": vertical_url,
                    "checks": [
                        {
                            "check_id": "NLMCA-010",
                            "status": "issue",
                            "evidence": {"affected_pages": [{"page": 9, "marker_count": 1}]},
                        },
                        {
                            "check_id": "NLMCA-012",
                            "status": "issue",
                            "evidence": {"affected_pages": [{"page": 2, "unrecognized_bold_count": 2}]},
                        },
                    ],
                }],
                "course_findings": [],
            },
            "galleries": [],
        })

        groups_by_name = {group["name"]: group for group in report["groups"]}
        quote_finding = groups_by_name["开头出现 >"]["findings"][0]
        bold_finding = groups_by_name["加粗识别失败"]["findings"][0]
        self.assertEqual("1.2 课程小节", quote_finding["name"])
        self.assertEqual(sequential_url, quote_finding["studio_url"])
        self.assertEqual("赋能内容", quote_finding["element_name"])
        self.assertEqual(VERTICAL_1, quote_finding["element_locator"])
        self.assertEqual(vertical_url, quote_finding["element_studio_url"])
        self.assertIn("查看“赋能内容”", report["text"])
        self.assertIn("第 9 页出现 1 个开头为 `>` 的大于号", report["text"])
        self.assertIn("第 2 页识别加粗失败 2 处", report["text"])
        self.assertEqual("赋能内容", bold_finding["element_name"])

    def test_unrouteable_auditor_finding_uses_course_link_alongside_section_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})
            unsafe_url = "https://untrusted.example/container/not-a-locator"
            auditor_report = {
                "status": "issue",
                "pass_count": 0,
                "issue_count": 2,
                "unverified_count": 0,
                "sequential_sections": [
                    {
                        "name": "1.2",
                        "locator": "not a valid Studio locator",
                        "studio_url": unsafe_url,
                        "checks": [{
                            "check_id": "NLMCA-013",
                            "status": "issue",
                            "actual": {"published": False, "has_changes": False},
                        }],
                    },
                    {
                        "name": "1.3",
                        "locator": "block-v1:ORG+COURSE+RUN+type@sequential+block@section3",
                        "studio_url": "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@sequential+block@section3",
                        "checks": [{
                            "check_id": "NLMCA-013",
                            "status": "issue",
                            "actual": {"published": False, "has_changes": False},
                        }],
                    },
                ],
                "target_sections": [],
                "course_findings": [],
            }

            self._run_cli(operation_path, studio, audit_report=auditor_report)

            report = json.loads(operation_path.read_text(encoding="utf-8"))["batch_result"]["operator_report"]
            self.assertEqual(1, len(report["groups"]))
            self.assertEqual("categories", report["mode"])
            self.assertEqual("章节未发布", report["groups"][0]["name"])
            routed_finding = next(
                finding for finding in report["groups"][0]["findings"]
                if finding["name"] == "1.3"
            )
            self.assertEqual(
                "block-v1:ORG+COURSE+RUN+type@sequential+block@section3",
                routed_finding["locator"],
            )
            self.assertEqual(1, len(report["course_findings"]))
            course_finding = report["course_findings"][0]
            self.assertEqual("issue", course_finding["status"])
            self.assertEqual("https://studio.example.com/course/" + COURSE_KEY, course_finding["studio_url"])
            self.assertIn("1.2", course_finding["message"])
            self.assertIn("没有公开版本", course_finding["message"])
            self.assertNotIn("untrusted.example", report["text"])
            self.assertIn("[课程]", report["text"])

    def test_cli_reports_auditor_unverified_when_course_identity_cannot_be_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            stdout = StringIO()
            args = [
                "validate_imagegallery_batch.py",
                "--studio-url", "https://studio.example.com/course/not-a-container",
                "--operation-file", str(operation_path),
                "--execute",
            ]

            with patch("sys.argv", args), redirect_stdout(stdout):
                exit_code = module.main()

            self.assertEqual(2, exit_code)
            result = json.loads(stdout.getvalue())
            self.assertEqual("unverified", result["course_audit"]["status"])
            self.assertEqual("course_identity", result["course_audit"]["course_findings"][0]["evidence"]["failure_stage"])
            self.assertIn("课程审核未能运行", result["operator_report"]["text"])

    def test_unverified_audit_keeps_publication_result_and_course_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})
            audit_report = module._audit_unverified(COURSE_KEY, stage="auditor_install")
            audit_report["course_url"] = "https://studio.example.com/course/" + COURSE_KEY

            exit_code, stdout, stderr = self._run_cli(operation_path, studio, audit_report=audit_report)

            self.assertEqual(1, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            self.assertEqual("completed", state["galleries"][0]["publish"]["status"])
            self.assertEqual("unverified", state["batch_result"]["course_audit"]["status"])
            finding = state["batch_result"]["operator_report"]["course_findings"][0]
            self.assertEqual("https://studio.example.com/course/" + COURSE_KEY, finding["studio_url"])
            self.assertEqual("unverified", finding["status"])
            self.assertIn("无法确认", state["batch_result"]["operator_report"]["text"])
            self.assertIn("课程审核程序未能完成运行", state["batch_result"]["operator_report"]["text"])

    def test_all_clear_operator_report_is_short_and_has_no_problem_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, self._operation(manifest))
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a"}, {"id": "asset-b"}],
                "time_config": [{}, {}],
                "audios": [{}],
            }})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

            self.assertEqual(0, exit_code, stdout + stderr)
            report = json.loads(operation_path.read_text(encoding="utf-8"))["batch_result"]["operator_report"]
            self.assertEqual("pass", report["status"])
            self.assertEqual([], report["groups"])
            self.assertEqual("", report["summary"])
            self.assertEqual("", report["text"])

    def test_course_structure_completeness_rejects_a_missing_vertical_child_list(self):
        self.assertFalse(module.course_structure_payload_is_complete(make_incomplete_course_structure()))

    def test_reference_scan_fails_closed_for_an_incomplete_unrelated_gallery_subtree(self):
        payload = make_incomplete_course_structure()
        with patch.object(module, "read_json_call", return_value=(payload, {"status": "ok"})) as read:
            references, locators, error = module.scan_course_gallery_references(
                SimpleNamespace(
                    studio_base="https://studio.example.com",
                    course_key=COURSE_KEY,
                    studio_url="https://studio.example.com/course/" + COURSE_KEY,
                ),
                expected_gallery_locators={GALLERY_1},
            )

        self.assertIsNone(references)
        self.assertEqual([], locators)
        self.assertIn("incomplete", error.lower())
        read.assert_called_once()

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
