import importlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
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
            for item in gallery["files"]:
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
                    "asset_ids": [f"old-{index + 1}-{page}" for page in range(1, 3)],
                    "images": [
                        {"page": page, "image": f"page-{page:03d}.png", "asset_id": f"old-{index + 1}-{page}"}
                        for page in range(1, 3)
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

    def _run_cli(self, operation_path, studio, *, extra_args=()):
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
        with patch("sys.argv", args), patch.object(publisher.requests, "Session", return_value=studio.session), patch.dict(
            "os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = module.main()
        return exit_code, stdout.getvalue(), stderr.getvalue()

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

    def test_cli_verifies_counts_without_comparing_server_file_names_or_scanning_course_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._write_bundle(root, page_count=2)
            operation_path = root / "batch-operation.json"
            operation = self._operation(manifest)
            operation["skill_provenance"] = {"commit": "fixture-commit", "installation_path": "fixture-skill"}
            operation["provenance"] = {"repository_commit": "fallback-commit"}
            self._write_operation_file(operation_path, operation)
            studio = FakeStudio({GALLERY_1: {
                "files": [{"id": "asset-a", "display_name": "renamed-a.png"}, {"id": "asset-b", "display_name": "renamed-b.png"}],
                "time_config": [{"page": 1}, {"page": 2}],
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
            self._write_operation_file(operation_path, self._operation(manifest, galleries=(GALLERY_1, GALLERY_2)))
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
                self._write_operation_file(operation_path, self._operation(manifest))
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
            self._write_operation_file(operation_path, self._operation(manifest))
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
                "files": [{"id": "a"}, {"id": "b"}],
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
                "files": [{"id": "a"}, {"id": "b"}],
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


if __name__ == "__main__":
    unittest.main()
