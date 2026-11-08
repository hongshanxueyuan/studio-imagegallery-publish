import importlib
import copy
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
    blocks_by_vertical = {VERTICAL_1: [], VERTICAL_2: []}
    for locator in galleries:
        vertical = VERTICAL_1 if locator == GALLERY_1 else VERTICAL_2
        blocks_by_vertical[vertical].extend([
            {"id": locator, "category": "imagesgallery", "display_name": "有声幻灯片"},
            {
                "id": vertical.replace("type@vertical", "type@html").replace("block@vertical", "block@summary"),
                "category": "html",
                "display_name": "本节要点",
            },
        ])
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
            self.assertEqual(["page-001.png", "page-002.png"], [item["display_name"] for item in studio.galleries[GALLERY_1]["files"]])

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
                    "files": [
                        {"id": "old-a", "asset_key": "old-a", "display_name": "wrong-1.png"},
                        {"id": "old-b", "asset_key": "old-b", "display_name": "wrong-2.png"},
                    ],
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

    def test_cli_reports_filename_mismatch_without_scanning_course_asset_inventory(self):
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
                "files": [{"id": "asset-a", "display_name": "renamed-a.png"}, {"id": "asset-b", "display_name": "renamed-b.png"}],
                "time_config": valid_time_config(2),
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
                GALLERY_1: {"files": [], "time_config": [], "audios": []},
                GALLERY_2: {"files": [{"id": "b1"}, {"id": "b2"}], "time_config": valid_time_config(2), "audios": [{}]},
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
                "time_config": valid_time_config(1),
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
            self.assertEqual(6, studio.course_reads)
            self.assertEqual({
                f"/assets/{COURSE_KEY}/asset-old",
                f"/assets/{COURSE_KEY}/asset-checkpoint-orphan",
            }, {call["path"] for call in studio.calls if call["method"] == "DELETE"})
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
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

    def test_cli_keeps_verified_content_status_when_repair_vertical_publish_fails(self):
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

            self.assertEqual(0, exit_code, stdout + stderr)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("verified", gallery["validation"]["status"])
            self.assertEqual("failed", gallery["repair_history"][0]["status"])
            self.assertEqual("failed", gallery["repair_history"][0]["vertical_publish"]["status"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])

    def test_cli_repairs_play_config_and_zero_or_multiple_audio_binding_mismatches(self):
        cases = (
            ("play_config", {"time_config": valid_time_config(1), "audios": [{"id": "audio-1"}]}),
            ("disabled_subtitles", {"time_config": valid_time_config(2), "audios": [{"id": "audio-1"}], "show_subtitles": False}),
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
            deleted_paths = [call["path"] for call in studio.calls if call["method"] == "DELETE"]
            self.assertEqual([f"/assets/{COURSE_KEY}/asset-orphan"], deleted_paths)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            cleanup = state["galleries"][0]["repair_history"][0]["asset_cleanup"]
            self.assertIn("asset-shared", cleanup["retained_asset_ids"])
            self.assertIn("asset-orphan", cleanup["deleted_asset_ids"])
            self.assertEqual("verified", state["galleries"][0]["validation"]["status"])
            self.assertEqual("verified", state["course_structure_verification"]["status"])
            self.assertTrue(state["batch_result"]["all_sections_verified"])
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
                "time_config": valid_time_config(1),
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
                    "files": [{"id": "a"}, {"id": "b"}],
                    "time_config": valid_time_config(2),
                    "audios": [{}],
                }}, payload_overrides={(GALLERY_1, handler): response})

                exit_code, stdout, stderr = self._run_cli(operation_path, studio)

                self.assertEqual(1, exit_code, stdout + stderr)
                state = json.loads(operation_path.read_text(encoding="utf-8"))
                self.assertEqual("unverified", state["galleries"][0]["validation"]["status"])
                self.assertEqual(0, state["galleries"][0]["repair_attempts"])
                self.assertFalse(any(call["method"] in {"DELETE", "PUT"} for call in studio.calls))

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

            with patch("sys.argv", args), patch.object(publisher.requests, "Session", return_value=studio.session), patch.dict(
                "os.environ", env, clear=False
            ), redirect_stdout(stdout):
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
            studio = FakeStudio({
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
            }, fail_get_files={GALLERY_2: 1})

            exit_code, stdout, stderr = self._run_cli(operation_path, studio)

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
            self.assertEqual("mismatch", state["galleries"][0]["validation"]["status"])
            self.assertEqual("unverified", state["galleries"][1]["validation"]["status"])
            self.assertNotIn("operation-secret", json.dumps(state))

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
