import argparse
import importlib.util
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "publish_imagegallery_block.py"
ADAPTER_PATH = ROOT / "references" / "adapter.template.json"
SKILL_PATH = ROOT / "SKILL.md"


spec = importlib.util.spec_from_file_location("publish_imagegallery_block", SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


class PublishImageGalleryBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = json.loads(ADAPTER_PATH.read_text(encoding="utf-8"))

    def test_atomic_write_json_retries_transient_permission_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operation.json"
            real_replace = module.os.replace
            replace_calls = 0

            def flaky_replace(source, destination):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls <= 2:
                    raise PermissionError("temporary sharing violation")
                real_replace(source, destination)

            with patch.object(module.os, "replace", side_effect=flaky_replace):
                with patch.object(module.time, "sleep") as sleep:
                    module.atomic_write_json(path, {"status": "saved"})

            self.assertEqual(3, replace_calls)
            self.assertEqual(2, sleep.call_count)
            self.assertEqual({"status": "saved"}, json.loads(path.read_text(encoding="utf-8")))

    def test_atomic_write_json_preserves_last_permission_error_after_retry_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operation.json"
            error = PermissionError("destination remained locked")
            with patch.object(module.os, "replace", side_effect=error) as replace:
                with patch.object(module.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError) as caught:
                        module.atomic_write_json(path, {"status": "saved"})

            self.assertIs(error, caught.exception)
            self.assertEqual(module.ATOMIC_WRITE_REPLACE_RETRIES + 1, replace.call_count)
            self.assertEqual(module.ATOMIC_WRITE_REPLACE_RETRIES, sleep.call_count)

    def _write_manifest_bundle(self, root: Path) -> Path:
        image_path = root / "page-001.png"
        audio_path = root / "full_speech.mp3"
        manifest_path = root / "imagesgallery.json"
        image_path.write_bytes(b"fake-image")
        audio_path.write_bytes(b"fake-audio")
        manifest_path.write_text(
            json.dumps(
                {
                    "audio": {"full_audio": str(audio_path)},
                    "items": [
                        {
                            "page": 1,
                            "image": str(image_path),
                            "subtitle": "第一页",
                            "start": 0,
                            "end": 1000,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest_path

    def _write_multi_page_manifest_bundle(self, root: Path, page_count: int = 2) -> Path:
        items = []
        for page in range(1, page_count + 1):
            image_path = root / f"page-{page:03d}.png"
            image_path.write_bytes(f"image-{page}".encode())
            items.append(
                {
                    "page": page,
                    "image": image_path.name,
                    "subtitle": f"第{page}页",
                    "start": (page - 1) * 1000,
                    "end": page * 1000,
                }
            )
        audio_path = root / "full_speech.mp3"
        audio_path.write_bytes(b"fake-audio")
        manifest_path = root / "imagesgallery.json"
        manifest_path.write_text(
            json.dumps({"audio": {"full_audio": str(audio_path)}, "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest_path

    def _write_operation_file(self, path: Path, manifest_path: Path, *, gallery_key: str = "gallery-1") -> None:
        path.write_text(
            json.dumps(
                {
                    "operation_id": "operation-1",
                    "galleries": [
                        {
                            "gallery_key": gallery_key,
                            "manifest": str(manifest_path),
                            "vertical_locator": "block-v1:ORG+COURSE+RUN+type@vertical+block@vertical123",
                            "gallery_locator": "",
                            "operator_note": "preserve this field",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _set_interrupted_order_checkpoint(self, operation_path: Path, *, failed: bool = False) -> None:
        state = json.loads(operation_path.read_text(encoding="utf-8"))
        state["galleries"][0]["gallery_locator"] = "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@gallery123"
        publish = {
            "gallery_key": "gallery-1",
            "status": "failed" if failed else "in_progress",
            "completed_stage": "images_uploaded",
            "images_started": True,
            "images": [
                {"page": 1, "image": "page-001.png", "asset_id": "asset-001"},
                {"page": 2, "image": "page-002.png", "asset_id": "asset-002"},
            ],
            "asset_ids": ["asset-001", "asset-002"],
            "steps": [],
        }
        if failed:
            publish["failed_step"] = "change_files_order"
        else:
            publish["active_step"] = "change_files_order"
        state["galleries"][0]["publish"] = publish
        operation_path.write_text(json.dumps(state), encoding="utf-8")

    def _run_publisher_cli(self, manifest_path: Path, operation_path: Path, fake_session, *, gallery_key: str = "gallery-1") -> tuple[int, str, str]:
        args = [
            "publish_imagegallery_block.py",
            "--studio-url",
            "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@vertical123",
            "--manifest",
            str(manifest_path),
            "--execute",
            "--cookie",
            "sessionid=do-not-persist; csrftoken=csrf-do-not-persist",
            "--skip-oss-multipart",
            "--operation-file",
            str(operation_path),
            "--gallery-key",
            gallery_key,
            "--auth-retry-delay-seconds",
            "0",
        ]
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.argv", args), patch.object(module.requests, "Session", return_value=fake_session), patch.dict(
            "os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = module.main()
        return exit_code, stdout.getvalue(), stderr.getvalue()

    class _FakeResponse:
        def __init__(self, payload: object, status_code: int = 200):
            self.status_code = status_code
            self.headers = {"content-type": "application/json"}
            self.text = json.dumps(payload)
            self._payload = payload

        def json(self):
            return self._payload

    class _FakeCookies:
        def set(self, *_args, **_kwargs):
            return None

    class _FakeStudioSession:
        def __init__(
            self,
            *,
            missing_upload_id: bool = False,
            incomplete_files: bool = False,
            fail_audio_prepare_once: bool = False,
            file_asset_ids: list[str] | None = None,
            fail_get_files: bool = False,
        ):
            self.cookies = PublishImageGalleryBlockTests._FakeCookies()
            self.missing_upload_id = missing_upload_id
            self.incomplete_files = incomplete_files
            self.fail_audio_prepare_once = fail_audio_prepare_once
            self.file_asset_ids = file_asset_ids
            self.fail_get_files = fail_get_files
            self.audio_prepare_count = 0
            self.calls: list[tuple[str, str]] = []

        def request(self, *, method: str, url: str, **_kwargs):
            path = urlparse(url).path
            self.calls.append((method, path))
            if path == "/xblock/":
                return PublishImageGalleryBlockTests._FakeResponse(
                    {"locator": "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@gallery123"}
                )
            if path.endswith("/handler/file_upload"):
                if self.missing_upload_id:
                    return PublishImageGalleryBlockTests._FakeResponse(
                        {
                            "message": "stored",
                            "Credentials": {"AccessKeyId": "sts-secret-access-key", "SessionToken": "sts-secret-token"},
                            "Cookie": "cookie-secret",
                            "Authorization": "Bearer authorization-secret",
                        }
                    )
                page_index = sum(call_path.endswith("/handler/file_upload") for _verb, call_path in self.calls)
                return PublishImageGalleryBlockTests._FakeResponse({"id": f"asset-{page_index:03d}"})
            if path.endswith("/handler/get_files"):
                if self.fail_get_files:
                    return PublishImageGalleryBlockTests._FakeResponse({"message": "unavailable"}, status_code=503)
                asset_ids = self.file_asset_ids or ["asset-001", "asset-002"]
                filenames = {"asset-001": "page-001.png", "asset-002": "page-002.png"}
                files = [{"id": asset_id, "display_name": filenames.get(asset_id, "unexpected.png")} for asset_id in asset_ids]
                if self.incomplete_files:
                    files = files[:1]
                return PublishImageGalleryBlockTests._FakeResponse({"files": files, "total_count": len(files)})
            if path.endswith("/handler/change_files_order"):
                return PublishImageGalleryBlockTests._FakeResponse({"ok": True})
            if path.endswith("/handler/get_upload_info"):
                self.audio_prepare_count += 1
                if self.fail_audio_prepare_once and self.audio_prepare_count == 1:
                    return PublishImageGalleryBlockTests._FakeResponse(
                        {"Credentials": {"AccessKeyId": "sts-secret-access-key"}, "message": "missing key"}
                    )
                return PublishImageGalleryBlockTests._FakeResponse(
                    {
                        "file_key": "12345678-1234-1234-1234-123456789abc",
                        "Credentials": {"AccessKeyId": "sts-secret-access-key", "SessionToken": "sts-secret-token"},
                    }
                )
            return PublishImageGalleryBlockTests._FakeResponse({"ok": True, "Authorization": "Bearer raw-secret"})

    def _in_place_args(self, manifest_path: Path, course_path: Path, *, execute: bool = False, cleanup_only: bool = False, section_list_json: str = "") -> argparse.Namespace:
        vertical_id = "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical"
        return argparse.Namespace(
            studio_url=f"https://studio.example.com/container/{vertical_id}",
            manifest=str(manifest_path),
            adapter=str(ADAPTER_PATH),
            execute=execute,
            dry_run=not execute,
            timeout=30,
            vertical_block_id=vertical_id,
            block_url="",
            source_vertical_block_id=vertical_id,
            section_id="",
            section_title="",
            block_type="",
            section_list_json=section_list_json,
            cleanup_only=cleanup_only,
            course_structure_json=str(course_path),
            courses_base="",
            account="",
            password="",
            cookie="csrftoken=fake",
            csrf_token="",
            skip_oss_multipart=True,
            auth_retry_max_retries=4,
            auth_retry_delay_seconds=0,
            targets_file="",
            report_file="",
        )

    def _mark_manifest_source(self, manifest_path: Path, *, block_type: str | None, section_id: str = "1.1", section_title: str = "1.1 示例课程", subtitle: str = "## 正文\n课程内容") -> None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if block_type is not None:
            manifest["block_type"] = block_type
        manifest["section_id"] = section_id
        manifest["section_title"] = section_title
        manifest["items"][0]["subtitle"] = subtitle
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def _add_terminal_summary(self, course_structure: dict, *, locator: str = "block-v1:FIRAx+1040045+20260807+type@html+block@terminal-summary", name: str = "要点小结") -> dict:
        block = {
            "name": name,
            "category": "html",
            "block_location": locator,
            "html": "  <h2>### 本节要点</h2><p><img src='slide.png'/>总结正文</p>",
        }
        course_structure["chapters"][0]["sections"][0]["verticals"][0]["blocks"].append(block)
        return block

    def _make_course_structure(self, *, include_gallery: bool, include_target_html: bool, include_section_html: bool) -> dict:
        target_vertical_blocks = [
            {
                "name": "导语",
                "category": "problem",
                "block_location": "block-v1:FIRAx+1040045+20260807+type@problem+block@intro",
            }
        ]
        if include_gallery:
            target_vertical_blocks.append(
                {
                    "name": "有声幻灯片",
                    "category": "imagesgallery",
                    "block_location": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
                }
            )
        if include_target_html:
            target_vertical_blocks.append(
                {
                    "name": "文字讲解",
                    "category": "html",
                    "block_location": "block-v1:FIRAx+1040045+20260807+type@html+block@targethtml",
                    "html": "<p>短文</p>",
                }
            )

        section_verticals = [
            {
                "name": "赋能内容",
                "block_location": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                "block_order": "001.001.001",
                "blocks": target_vertical_blocks,
            },
            {
                "name": "在线训战",
                "block_location": "block-v1:FIRAx+1040045+20260807+type@vertical+block@sectionhtmlvertical",
                "block_order": "001.001.002",
                "blocks": (
                    [
                        {
                            "name": "知识点",
                            "category": "html",
                            "block_location": "block-v1:FIRAx+1040045+20260807+type@html+block@shorthtml",
                            "html": "<p>短</p>",
                        },
                        {
                            "name": "文字讲解",
                            "category": "html",
                            "block_location": "block-v1:FIRAx+1040045+20260807+type@html+block@wordiesthtml",
                            "html": "<p>这是更长的一段正文内容，应该被默认认为是主讲解 html block。</p>",
                        },
                    ]
                    if include_section_html
                    else []
                ),
            },
        ]

        return {
            "course_id": "course-v1:FIRAx+1040045+20260807",
            "chapters": [
                {
                    "name": "第一章",
                    "block_location": "block-v1:FIRAx+1040045+20260807+type@chapter+block@chapter1",
                    "sections": [
                        {
                            "name": "1.1 示例课程",
                            "block_location": "block-v1:FIRAx+1040045+20260807+type@sequential+block@section1",
                            "verticals": section_verticals,
                        }
                    ],
                }
            ],
        }

    def _make_concise_course_structure(self, *, gallery_locator: str = "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@livegallery") -> dict:
        return {
            "id": "course-v1:FIRAx+211181+20251122",
            "category": "course",
            "display_name": "示例课程",
            "child_info": {
                "children": [
                    {
                        "id": "block-v1:FIRAx+211181+20251122+type@chapter+block@chapter1",
                        "category": "chapter",
                        "display_name": "第一章",
                        "child_info": {
                            "children": [
                                {
                                    "id": "block-v1:FIRAx+211181+20251122+type@sequential+block@section1",
                                    "category": "sequential",
                                    "display_name": "1.1 示例课程",
                                    "child_info": {
                                        "children": [
                                            {
                                                "id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                                                "category": "vertical",
                                                "display_name": "赋能内容",
                                                "child_info": {
                                                    "children": [
                                                        {
                                                            "id": "block-v1:FIRAx+211181+20251122+type@problem+block@intro",
                                                            "category": "problem",
                                                            "display_name": "导语",
                                                        },
                                                        {
                                                            "id": gallery_locator,
                                                            "category": "imagesgallery",
                                                            "display_name": "有声幻灯片",
                                                        },
                                                    ]
                                                },
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        }

    def _concise_target_vertical_children(self, payload: dict) -> list[dict]:
        return payload["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]

    def _make_concise_course_structure_with_two_galleries(
        self,
        *,
        legacy_gallery_locator: str,
        created_gallery_locator: str,
    ) -> dict:
        payload = self._make_concise_course_structure(gallery_locator=legacy_gallery_locator)
        vertical_children = self._concise_target_vertical_children(payload)
        vertical_children.append(
            {
                "id": created_gallery_locator,
                "category": "imagesgallery",
                "display_name": "有声幻灯片 - 新创建版本",
            }
        )
        return payload

    def _make_mixed_course_structure(self) -> dict:
        course_structure = self._make_course_structure(
            include_gallery=False,
            include_target_html=True,
            include_section_html=False,
        )
        course_structure["chapters"][0]["sections"][0]["verticals"].append(
            {
                "name": "别处已有 gallery",
                "block_location": "block-v1:FIRAx+1040045+20260807+type@vertical+block@othergalleryvertical",
                "block_order": "001.001.003",
                "blocks": [
                    {
                        "name": "有声幻灯片",
                        "category": "imagesgallery",
                        "block_location": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@othergallery",
                    }
                ],
            }
        )
        return course_structure

    def test_normalize_block_url_strips_query_and_fragment(self) -> None:
        self.assertEqual(
            module.normalize_block_url(
                "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@abc?action=new#unit-1"
            ),
            "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@abc",
        )

    def test_build_container_block_url(self) -> None:
        self.assertEqual(
            "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@imagesgallery+block@xyz",
            module.build_container_block_url(
                "https://studio.example.com",
                "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@xyz",
            ),
        )
        self.assertEqual("", module.build_container_block_url("", "block-v1:ORG+COURSE+RUN+type@imagesgallery+block@xyz"))
        self.assertEqual("", module.build_container_block_url("https://studio.example.com", ""))

    def test_upload_image_template_includes_csrf_sensitive_headers(self) -> None:
        headers = self.adapter["upload_image"]["headers"]
        self.assertEqual(headers.get("Origin"), "{studio_base}")
        self.assertEqual(headers.get("Referer"), "{block_url}")

    def test_adapter_includes_reorder_delete_and_publish_actions(self) -> None:
        reorder = self.adapter["reorder_children"]
        delete = self.adapter["delete_block"]
        publish = self.adapter["publish_vertical"]

        self.assertEqual("PUT", reorder["method"])
        self.assertEqual("{studio_base}/xblock/{vertical_block_id}", reorder["url"])
        self.assertEqual("{block_url}", reorder["headers"]["Referer"])
        self.assertEqual("{vertical_children_obj}", reorder["json"]["children"])

        self.assertEqual("DELETE", delete["method"])
        self.assertEqual("{studio_base}/xblock/{delete_block_locator}", delete["url"])
        self.assertEqual("{block_url}", delete["headers"]["Referer"])

        self.assertEqual("POST", publish["method"])
        self.assertEqual("{studio_base}/xblock/{vertical_block_id}", publish["url"])
        self.assertEqual("PATCH", publish["headers"]["X-HTTP-Method-Override"])
        self.assertEqual("make_public", publish["json"]["publish"])

    def test_mutating_templates_use_canonical_block_url(self) -> None:
        for step_name in (
            "change_files_order",
            "upload_audio_prepare",
            "upload_audio_register",
            "save_audio_binding",
            "submit_studio_edits",
            "save_block",
            "reorder_children",
            "delete_block",
            "publish_vertical",
        ):
            with self.subTest(step=step_name):
                headers = self.adapter[step_name]["headers"]
                self.assertEqual(headers.get("Referer"), "{block_url}")
                self.assertEqual(headers.get("Origin"), "{studio_base}")

    def test_plan_publish_replacement_uses_existing_gallery_for_imagesgallery_courses(self) -> None:
        course_structure = self._make_course_structure(
            include_gallery=True,
            include_target_html=False,
            include_section_html=False,
        )

        plan = module.plan_publish_replacement(
            course_structure,
            source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            target_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
        )

        self.assertEqual("imagesgallery_based", plan["course_kind"])
        self.assertEqual("replaced", plan["result_type"])
        self.assertEqual("target_vertical", plan["match_scope"])
        self.assertEqual(
            "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
            plan["matched_source_block_locator"],
        )
        self.assertEqual(
            "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            plan["working_vertical_block_id"],
        )

    def test_explicit_html_type_targets_文字讲解_even_when_vertical_contains_gallery(self) -> None:
        course_structure = self._make_course_structure(
            include_gallery=True,
            include_target_html=True,
            include_section_html=True,
        )

        plan = module.plan_publish_replacement(
            course_structure,
            source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            target_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            block_type="html",
        )

        self.assertEqual("html_based", plan["course_kind"])
        self.assertEqual(
            "block-v1:FIRAx+1040045+20260807+type@html+block@targethtml",
            plan["matched_source_block_locator"],
        )
        self.assertEqual(3, plan["initial_snapshot"]["direct_child_count"])
        self.assertEqual("imagesgallery", plan["initial_snapshot"]["children"][1]["type"])

    def test_explicit_html_type_does_not_guess_from_another_vertical(self) -> None:
        course_structure = self._make_course_structure(
            include_gallery=False,
            include_target_html=False,
            include_section_html=True,
        )

        plan = module.plan_publish_replacement(
            course_structure,
            source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            target_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            block_type="html",
        )

        self.assertEqual("missing_html_replacement_target", plan["planning_issue"]["code"])
        self.assertEqual("", plan["matched_source_block_locator"])

    def test_explicit_gallery_type_requires_a_unique_existing_gallery(self) -> None:
        course_structure = self._make_course_structure(
            include_gallery=True,
            include_target_html=True,
            include_section_html=False,
        )
        target_vertical = course_structure["chapters"][0]["sections"][0]["verticals"][0]
        target_vertical["blocks"].append(
            {
                "name": "旧版有声幻灯片",
                "category": "imagesgallery",
                "block_location": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@secondgallery",
            }
        )

        plan = module.plan_publish_replacement(
            course_structure,
            source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            target_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            block_type="imagesgallery",
        )

        self.assertEqual("ambiguous_gallery_replacement_target", plan["planning_issue"]["code"])
        self.assertEqual("", plan["matched_source_block_locator"])

    def test_resolve_source_block_type_detects_section_list_conflict_with_manifest_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "imagesgallery.json"
            section_list_path = root / "section-list.json"
            manifest_path.write_text("{}", encoding="utf-8")
            section_list_path.write_text(
                json.dumps({"sections": [{"section_id": "1.1", "section_title": "示例", "block_type": "html"}]}),
                encoding="utf-8",
            )

            resolution = module.resolve_source_block_type(
                {"section_id": "1.1", "section_title": "示例", "block_type": "imagesgallery"},
                {"section_id": "1.1", "section_title": "示例", "block_type": "imagesgallery"},
                manifest_path=manifest_path,
            )

        self.assertEqual("conflict", resolution["status"])

    def test_operation_vertical_integrity_uses_validator_expected_snapshot_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operation_path = Path(tmp) / "batch-operation.json"
            operation_path.write_text(
                json.dumps({"galleries": [{"gallery_key": "gallery-1"}]}),
                encoding="utf-8",
            )
            before = {
                "direct_child_count": 2,
                "children": [
                    {"locator": "block-v1:org+course+run+type@html+block@body", "type": "html", "order": 1},
                    {"locator": "block-v1:org+course+run+type@html+block@summary", "type": "html", "order": 2},
                ],
            }
            expected = {
                "direct_child_count": 2,
                "children": [
                    {"locator": "block-v1:org+course+run+type@imagesgallery+block@new", "type": "imagesgallery", "order": 1},
                    {"locator": "block-v1:org+course+run+type@html+block@summary", "type": "html", "order": 2},
                ],
            }

            module.persist_vertical_integrity_evidence(
                argparse.Namespace(
                    operation_file=str(operation_path), gallery_key="gallery-1", execute=True,
                ),
                {
                    "status": "replaced",
                    "block_type": "html",
                    "vertical_published": True,
                    "vertical_snapshot_before": before,
                    "expected_vertical_snapshot_after_delete": expected,
                    "provenance": {"commit": "verified-commit", "inputs": []},
                    "steps": [{"step": "publish_vertical", "status": "completed"}],
                },
            )
            updated_operation = json.loads(operation_path.read_text(encoding="utf-8"))
            entry = updated_operation["galleries"][0]

        self.assertEqual("verified-commit", updated_operation["skill_provenance"]["commit"])
        self.assertEqual("pending_validation", entry["vertical_integrity"]["status"])
        self.assertEqual("html", entry["vertical_integrity"]["block_type"])
        self.assertEqual(before, entry["vertical_integrity"]["before"])
        self.assertEqual(expected, entry["vertical_integrity"]["expected"])
        self.assertIsNone(entry["vertical_integrity"]["published"])
        self.assertEqual("verified-commit", entry["vertical_integrity"]["provenance"]["commit"])

    def test_skill_provenance_distinguishes_clean_and_dirty_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_root = Path(tmp) / "publisher-skill"
            script_path = skill_root / "scripts" / "publish_imagegallery_block.py"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("# active publisher", encoding="utf-8")
            (skill_root / "SKILL.md").write_text("name: publisher-skill", encoding="utf-8")
            subprocess.run(["git", "-C", str(skill_root), "init"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(skill_root), "config", "user.name", "Publisher Test"], check=True)
            subprocess.run(["git", "-C", str(skill_root), "config", "user.email", "publisher-test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(skill_root), "remote", "add", "origin", "https://example.invalid/publisher-skill.git"], check=True)
            subprocess.run(["git", "-C", str(skill_root), "add", "SKILL.md", "scripts/publish_imagegallery_block.py"], check=True)
            subprocess.run(["git", "-C", str(skill_root), "commit", "-m", "fixture"], check=True, capture_output=True, text=True)
            head = subprocess.run(
                ["git", "-C", str(skill_root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            section_list = Path(tmp) / "section-list.json"
            section_list.write_text('{"sections":[]}', encoding="utf-8")
            section_list_hash = module.file_sha256(section_list)

            with patch.object(module, "__file__", str(script_path)):
                clean_provenance = module.skill_provenance(input_paths=[str(section_list)])
                script_path.write_text("# modified publisher", encoding="utf-8")
                dirty_provenance = module.skill_provenance(input_paths=[str(section_list)])

        self.assertEqual("clean", clean_provenance["working_tree_state"])
        self.assertEqual(head, clean_provenance["commit"])
        self.assertEqual(head, clean_provenance["base_commit"])
        self.assertNotEqual("unknown", clean_provenance["repository"])
        self.assertNotEqual("unknown", clean_provenance["ref"])
        self.assertNotEqual("unknown", clean_provenance["content_sha256"])
        self.assertEqual("dirty", dirty_provenance["working_tree_state"])
        self.assertEqual("unknown", dirty_provenance["commit"])
        self.assertEqual(head, dirty_provenance["base_commit"])
        self.assertNotEqual(clean_provenance["content_sha256"], dirty_provenance["content_sha256"])
        self.assertEqual(section_list_hash, dirty_provenance["inputs"][0]["sha256"])

    def test_skill_provenance_includes_dirty_validator_in_digest_and_commit_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_root = Path(tmp) / "publisher-skill"
            script_path = skill_root / "scripts" / "publish_imagegallery_block.py"
            validator_path = skill_root / "scripts" / "validate_imagegallery_batch.py"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("# active publisher", encoding="utf-8")
            validator_path.write_text("# active validator", encoding="utf-8")
            (skill_root / "SKILL.md").write_text("name: publisher-skill", encoding="utf-8")
            subprocess.run(["git", "-C", str(skill_root), "init"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(skill_root), "config", "user.name", "Publisher Test"], check=True)
            subprocess.run(["git", "-C", str(skill_root), "config", "user.email", "publisher-test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(skill_root), "add", "SKILL.md", "scripts/publish_imagegallery_block.py", "scripts/validate_imagegallery_batch.py"], check=True)
            subprocess.run(["git", "-C", str(skill_root), "commit", "-m", "fixture"], check=True, capture_output=True, text=True)
            head = subprocess.run(
                ["git", "-C", str(skill_root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with patch.object(module, "__file__", str(script_path)):
                clean_provenance = module.skill_provenance()
                validator_path.write_text("# modified validator", encoding="utf-8")
                dirty_provenance = module.skill_provenance()

        self.assertEqual("clean", clean_provenance["working_tree_state"])
        self.assertEqual(head, clean_provenance["commit"])
        self.assertEqual(head, clean_provenance["base_commit"])
        self.assertEqual("dirty", dirty_provenance["working_tree_state"])
        self.assertEqual("unknown", dirty_provenance["commit"])
        self.assertEqual(head, dirty_provenance["base_commit"])
        self.assertNotEqual(clean_provenance["content_sha256"], dirty_provenance["content_sha256"])

    def test_skill_provenance_marks_copied_skill_metadata_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_root = Path(tmp) / "installed-skill"
            script_path = skill_root / "scripts" / "publish_imagegallery_block.py"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("# copied skill", encoding="utf-8")
            (skill_root / "SKILL.md").write_text("name: copied-skill", encoding="utf-8")
            section_list = Path(tmp) / "section-list.json"
            section_list.write_text('{"sections":[{"block_type":"html"}]}', encoding="utf-8")
            missing_input = Path(tmp) / "missing-section-list.json"
            section_list_hash = module.file_sha256(section_list)

            with patch.object(module, "__file__", str(script_path)):
                provenance = module.skill_provenance(input_paths=[str(section_list), str(missing_input)])

        self.assertEqual("unknown", provenance["repository"])
        self.assertEqual("unknown", provenance["ref"])
        self.assertEqual("unknown", provenance["commit"])
        self.assertEqual("unknown", provenance["base_commit"])
        self.assertEqual("unknown", provenance["working_tree_state"])
        self.assertNotEqual("unknown", provenance["content_sha256"])
        self.assertEqual(section_list_hash, provenance["inputs"][0]["sha256"])
        self.assertEqual("unknown", provenance["inputs"][1]["sha256"])

    def _run_single_publish_create_reconciliation(
        self,
        *,
        mutation_applied: bool,
        first_response: str = "timeout",
        mutate_successful_create: bool = True,
        first_success_mutates: bool = False,
        reconciliation_locator: str = "",
    ):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_manifest_bundle(Path(tmp))
            vertical_id = "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical"
            created_locator = "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@createdaftertimeout"
            before = {
                "direct_child_count": 1,
                "children": [
                    {"locator": "block-v1:FIRAx+1040045+20260807+type@html+block@body", "type": "html", "order": 1},
                ],
            }
            after = {
                "direct_child_count": 2,
                "children": [
                    {"locator": "block-v1:FIRAx+1040045+20260807+type@html+block@body", "type": "html", "order": 1},
                    {"locator": created_locator, "type": "imagesgallery", "order": 2},
                ],
            }
            observed = {"applied": mutation_applied}
            args = argparse.Namespace(
                studio_url=f"https://studio.example.com/container/{vertical_id}",
                manifest=str(manifest_path),
                adapter=str(ADAPTER_PATH),
                execute=True,
                dry_run=False,
                timeout=30,
                vertical_block_id=vertical_id,
                block_url="",
                courses_base="",
                account="",
                password="",
                cookie="csrftoken=fake",
                csrf_token="",
                skip_oss_multipart=True,
                auth_retry_max_retries=4,
                auth_retry_delay_seconds=0,
                reconcile_create_block=lambda: {
                    "status": "applied" if observed["applied"] else "not_applied",
                    "block_locator": (reconciliation_locator or created_locator) if observed["applied"] else "",
                    "observed_snapshot": after if observed["applied"] else before,
                },
            )
            calls: list[str] = []

            def fake_call_template(_session, name, _template, ctx, _execute, _timeout):
                calls.append(name)
                if name == "create_block":
                    create_call_count = len([call for call in calls if call == "create_block"])
                    if create_call_count == 1 and first_response == "timeout":
                        raise module.TemplateCallError(name, 504, {"detail": "timeout"})
                    if mutate_successful_create and (create_call_count > 1 or first_success_mutates):
                        observed["applied"] = True
                    ctx["block_locator"] = created_locator
                    return {"step": name, "status": "ok", "block_locator": created_locator}
                if name.startswith("upload_image"):
                    return {"step": name, "status": "ok", "asset_id": "asset-001"}
                if name == "upload_audio_prepare":
                    ctx["file_key"] = "12345678-1234-1234-1234-123456789abc"
                return {"step": name, "status": "ok"}

            with patch.dict("os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False):
                with patch.object(module.requests, "Session", return_value=self._FakeStudioSession()):
                    with patch.object(module, "call_template", side_effect=fake_call_template):
                        try:
                            result = module.run_single_publish(args)
                        except Exception as exc:
                            result = {
                                "status": "failed",
                                "failed_step": getattr(exc, "step_name", "create_block"),
                                "error": exc,
                            }
        return result, calls, created_locator

    def test_create_timeout_after_mutation_is_reconciled_without_recreating_gallery(self) -> None:
        result, calls, created_locator = self._run_single_publish_create_reconciliation(mutation_applied=True)

        create_step = next(step for step in result["steps"] if step["step"] == "create_block")
        self.assertEqual(1, calls.count("create_block"))
        self.assertEqual("reconciled", create_step["status"])
        self.assertEqual("reconciled_after_reread", create_step["attempts"][0]["status"])
        self.assertEqual(1, result["create_attempts"])
        self.assertEqual(created_locator, result["block_locator"])

    def test_create_timeout_without_mutation_retries_once_then_records_retried(self) -> None:
        result, calls, created_locator = self._run_single_publish_create_reconciliation(mutation_applied=False)

        create_step = next(step for step in result["steps"] if step["step"] == "create_block")
        self.assertEqual(2, calls.count("create_block"))
        self.assertEqual("retried", create_step["status"])
        self.assertEqual(
            ["not_applied_after_reconciliation", "response_ok"],
            [attempt["status"] for attempt in create_step["attempts"]],
        )
        self.assertEqual(2, result["create_attempts"])
        self.assertEqual(created_locator, result["block_locator"])

    def test_successful_create_response_without_vertical_child_retries_once(self) -> None:
        result, calls, _created_locator = self._run_single_publish_create_reconciliation(
            mutation_applied=False,
            first_response="success",
        )

        create_step = next(step for step in result["steps"] if step["step"] == "create_block")
        self.assertEqual(2, calls.count("create_block"))
        self.assertEqual("retried", create_step["status"])
        self.assertEqual(
            ["postcondition_missing_after_response", "response_ok"],
            [attempt["status"] for attempt in create_step["attempts"]],
        )
        self.assertEqual("applied", create_step["attempts"][1]["postcondition_status"])

    def test_successful_create_response_with_matching_vertical_child_completes(self) -> None:
        result, calls, created_locator = self._run_single_publish_create_reconciliation(
            mutation_applied=False,
            first_response="success",
            first_success_mutates=True,
        )

        create_step = next(step for step in result["steps"] if step["step"] == "create_block")
        self.assertEqual(1, calls.count("create_block"))
        self.assertEqual("completed", create_step["status"])
        self.assertEqual("applied", create_step["attempts"][0]["postcondition_status"])
        self.assertEqual(created_locator, create_step["block_locator"])

    def test_successful_create_response_with_locator_mismatch_stops_before_upload(self) -> None:
        result, calls, _created_locator = self._run_single_publish_create_reconciliation(
            mutation_applied=True,
            first_response="success",
            first_success_mutates=True,
            reconciliation_locator="block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@different",
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual("create_block", result["failed_step"])
        self.assertEqual(1, calls.count("create_block"))
        self.assertFalse(any(call.startswith("upload_image") for call in calls))

    def test_successful_create_response_still_missing_after_retry_stops_before_upload(self) -> None:
        result, calls, _created_locator = self._run_single_publish_create_reconciliation(
            mutation_applied=False,
            first_response="success",
            mutate_successful_create=False,
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual("create_block", result["failed_step"])
        self.assertEqual(2, calls.count("create_block"))
        self.assertFalse(any(call.startswith("upload_image") for call in calls))

    def test_process_persists_original_snapshot_and_wires_create_reconciler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = self._write_manifest_bundle(root)
            self._mark_manifest_source(manifest_path, block_type="html")
            course = self._make_course_structure(
                include_gallery=False,
                include_target_html=True,
                include_section_html=False,
            )
            current_structure = json.loads(json.dumps(course, ensure_ascii=False))
            vertical = current_structure["chapters"][0]["sections"][0]["verticals"][0]
            vertical_id = vertical["block_location"]
            created_locator = "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@create-reconciled"
            operation_path = root / "batch-operation.json"
            operation_path.write_text(
                json.dumps({"galleries": [{"gallery_key": "gallery-1", "manifest": str(manifest_path), "vertical_locator": vertical_id}]}),
                encoding="utf-8",
            )
            args = self._in_place_args(manifest_path, root / "course.json", execute=True)
            args.operation_file = str(operation_path)
            args.gallery_key = "gallery-1"
            args.execute = True
            args.cookie = "csrftoken=fake"
            observed_reconciliation: list[dict[str, object]] = []

            def fake_live_plan(**kwargs):
                plan = module.plan_publish_replacement(
                    current_structure,
                    source_vertical_block_id=kwargs["source_vertical_block_id"],
                    target_vertical_block_id=kwargs["target_vertical_block_id"],
                    block_type=kwargs.get("block_type", ""),
                    excluded_block_locators=kwargs.get("excluded_block_locators", []),
                )
                return plan, {"step": "load_live_target_structure", "status": "ok"}

            def fake_single_publish(publish_args):
                saved = json.loads(operation_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    module.plan_publish_replacement(
                        course,
                        source_vertical_block_id=vertical_id,
                        target_vertical_block_id=vertical_id,
                        block_type="html",
                    )["initial_snapshot"],
                    saved["galleries"][0]["vertical_integrity"]["before"],
                )
                vertical["blocks"].append({
                    "name": "有声幻灯片",
                    "category": "imagesgallery",
                    "block_location": created_locator,
                })
                evidence = publish_args.reconcile_create_block()
                observed_reconciliation.append(evidence)
                self.assertEqual("applied", evidence["status"])
                return {
                    "studio_base": "https://studio.example.com",
                    "block_locator": created_locator,
                    "steps": [{"step": "create_block", "status": "reconciled"}],
                    "create_attempts": 1,
                }

            with patch.dict("os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False):
                with patch.object(module, "maybe_fetch_live_replacement_plan", side_effect=fake_live_plan):
                    with patch.object(module, "run_single_publish", side_effect=fake_single_publish):
                        with patch.object(
                            module,
                            "apply_in_place_publish_actions",
                            return_value={"status": "replaced", "vertical_published": True, "steps": []},
                        ):
                            result = module.process_publish_target(
                                {
                                    "name": "1.1",
                                    "manifest": str(manifest_path),
                                    "section_id": "1.1",
                                    "target_vertical_block_location": vertical_id,
                                    "source_vertical_block_location": vertical_id,
                                    "studio_vertical_url": f"https://studio.example.com/container/{vertical_id}",
                                },
                                course,
                                args,
                                self.adapter,
                            )

        self.assertEqual("replaced", result["status"])
        self.assertEqual(1, len(observed_reconciliation))
        self.assertEqual("applied", observed_reconciliation[0]["status"])

    def test_process_target_with_missing_type_stops_before_live_read_or_gallery_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_manifest_bundle(Path(tmp))
            target_vertical = "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical"
            target = {
                "name": "1.1",
                "manifest": str(manifest_path),
                "source_vertical_block_location": target_vertical,
                "target_vertical_block_location": target_vertical,
                "studio_vertical_url": f"https://studio.example.com/container/{target_vertical}",
            }
            args = argparse.Namespace(
                execute=True, timeout=30, cleanup_only=False, cookie="csrftoken=fake",
                csrf_token="", account="", password="", courses_base="", adapter=str(ADAPTER_PATH),
            )
            with patch.object(module, "maybe_fetch_live_replacement_plan", side_effect=AssertionError("must fail before live reads")):
                with patch.object(module, "run_single_publish", side_effect=AssertionError("must fail before creating Gallery")):
                    result = module.process_publish_target(
                        target,
                        self._make_course_structure(include_gallery=False, include_target_html=True, include_section_html=False),
                        args,
                        self.adapter,
                    )

        self.assertEqual("failed", result["status"])
        self.assertEqual("resolve_source_block_type", result["failed_step"])

    def test_batch_report_marks_preflight_failures_as_skipped_publish_vertical(self) -> None:
        cases = {
            "missing_type": "resolve_source_block_type",
            "conflicting_type": "resolve_source_block_type",
            "missing_gallery": "resolve_replacement_target",
            "ambiguous_gallery": "resolve_replacement_target",
            "live_read_unavailable": "load_live_target_structure",
            "missing_target_vertical": "target_vertical",
            "missing_studio_url": "target_vertical",
            "missing_manifest": "resolve_source_block_type",
        }
        target_vertical = "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical"
        for scenario, expected_failure in cases.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest_path = self._write_manifest_bundle(root)
                if scenario == "missing_type":
                    source_type = None
                    target_type = ""
                    include_gallery = False
                    include_target_html = True
                    execute = False
                elif scenario == "conflicting_type":
                    source_type = "html"
                    target_type = "imagesgallery"
                    include_gallery = False
                    include_target_html = True
                    execute = False
                elif scenario in {"missing_gallery", "ambiguous_gallery"}:
                    source_type = "imagesgallery"
                    target_type = "imagesgallery"
                    include_gallery = scenario == "ambiguous_gallery"
                    include_target_html = False
                    execute = False
                else:
                    source_type = "html"
                    target_type = "html"
                    include_gallery = False
                    include_target_html = True
                    execute = True
                if scenario != "missing_manifest":
                    self._mark_manifest_source(manifest_path, block_type=source_type)
                course = self._make_course_structure(
                    include_gallery=include_gallery,
                    include_target_html=include_target_html,
                    include_section_html=False,
                )
                if scenario == "ambiguous_gallery":
                    course["chapters"][0]["sections"][0]["verticals"][0]["blocks"].append({
                        "name": "第二个 Gallery",
                        "category": "imagesgallery",
                        "block_location": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@secondgallery",
                    })
                course_path = root / "course.json"
                course_path.write_text(json.dumps(course, ensure_ascii=False), encoding="utf-8")
                targets_path = root / "targets.json"
                target_manifest = root / "missing-manifest.json" if scenario == "missing_manifest" else manifest_path
                target_vertical_id = "" if scenario == "missing_target_vertical" else target_vertical
                target_vertical_url = (
                    "" if scenario == "missing_studio_url"
                    else f"https://studio.example.com/container/{target_vertical}"
                )
                targets_path.write_text(json.dumps({
                    "course_structure_json": str(course_path),
                    "targets": [{
                        "name": "1.1",
                        "manifest": str(target_manifest),
                        "block_type": target_type,
                        "target_vertical_block_location": target_vertical_id,
                        "source_vertical_block_location": target_vertical,
                        "studio_vertical_url": target_vertical_url,
                    }],
                }, ensure_ascii=False), encoding="utf-8")
                report_path = root / "report.json"
                args = argparse.Namespace(
                    targets_file=str(targets_path),
                    report_file=str(report_path),
                    course_structure_json="",
                    section_list_json="",
                    adapter=str(ADAPTER_PATH),
                    execute=execute,
                    cleanup_only=False,
                    timeout=30,
                    cookie="csrftoken=fake",
                    csrf_token="",
                    account="",
                    password="",
                    courses_base="",
                )
                if scenario == "live_read_unavailable":
                    live_failure = {
                        "step": "load_live_target_structure",
                        "status": "warning",
                        "note": "live target unavailable",
                    }
                    with patch.object(module, "maybe_fetch_live_replacement_plan", return_value=(None, live_failure)):
                        module.run_batch_publish(args)
                else:
                    module.run_batch_publish(args)

                report = json.loads(report_path.read_text(encoding="utf-8"))
                result = report["results"][0]
                self.assertEqual(expected_failure, result["failed_step"])
                publish_step = next(
                    step for step in result["steps"] if step.get("step") == "publish_vertical"
                )
                self.assertEqual("skipped", publish_step["status"])

    def test_process_target_with_standalone_summary_page_stops_before_gallery_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_manifest_bundle(Path(tmp))
            self._mark_manifest_source(manifest_path, block_type="html", subtitle="## 本节要点\n不得进入 Gallery")
            target_vertical = "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical"
            target = {
                "name": "1.1",
                "manifest": str(manifest_path),
                "block_type": "html",
                "target_vertical_block_location": target_vertical,
                "source_vertical_block_location": target_vertical,
                "studio_vertical_url": f"https://studio.example.com/container/{target_vertical}",
            }
            args = argparse.Namespace(execute=True, timeout=30, cleanup_only=False)
            with patch.object(module, "run_single_publish", side_effect=AssertionError("must not create a leaking Gallery")):
                result = module.process_publish_target(
                    target,
                    self._make_course_structure(include_gallery=False, include_target_html=True, include_section_html=False),
                    args,
                    self.adapter,
                )

        self.assertEqual("failed", result["status"])
        self.assertEqual("stage_b_output_contract", result["failed_step"])
        self.assertEqual([1], result["leaking_summary_pages"])
        self.assertEqual("skipped", result["steps"][-1]["status"])

    def _run_fake_vertical_replacement(
        self,
        *,
        uncertain_move: str = "",
        fail_delete: bool = False,
        noop_delete_once: bool = False,
        wrong_delete: bool = False,
    ):
        course = self._make_course_structure(
            include_gallery=False,
            include_target_html=True,
            include_section_html=False,
        )
        target_vertical = course["chapters"][0]["sections"][0]["verticals"][0]
        target_locator = target_vertical["block_location"]
        created_locator = "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@newgallery"
        baseline_plan = module.plan_publish_replacement(
            course,
            source_vertical_block_id=target_locator,
            target_vertical_block_id=target_locator,
            block_type="html",
        )
        live_course = json.loads(json.dumps(course, ensure_ascii=False))
        live_vertical = live_course["chapters"][0]["sections"][0]["verticals"][0]
        live_vertical["blocks"].append({
            "name": "有声幻灯片",
            "category": "imagesgallery",
            "block_location": created_locator,
        })
        live_plan = module.plan_publish_replacement(
            live_course,
            source_vertical_block_id=target_locator,
            target_vertical_block_id=target_locator,
            block_type="html",
            excluded_block_locators=[created_locator],
        )
        live_plan["resolved_block_type"] = "html"
        live_plan["plan_source"] = "live_target"
        counts = {"reorder_children": 0, "delete_block": 0, "publish_vertical": 0}
        reconciliation_state = {"applied_move": False}

        def fake_write(_session, name, _template, ctx, _execute, _timeout):
            counts[name] = counts.get(name, 0) + 1
            if name == "reorder_children":
                if uncertain_move == "not_applied_once" and counts[name] == 1:
                    raise module.TemplateCallError(name, 504, {"detail": "timeout"})
                by_locator = {block["block_location"]: block for block in live_vertical["blocks"]}
                live_vertical["blocks"] = [by_locator[locator] for locator in ctx["vertical_children_obj"]]
                reconciliation_state["applied_move"] = True
                if uncertain_move == "applied_then_timeout" and counts[name] == 1:
                    raise module.TemplateCallError(name, 504, {"detail": "timeout"})
            elif name == "delete_block":
                if fail_delete:
                    raise module.TemplateCallError(name, 503, {"detail": "delete unavailable"})
                if noop_delete_once and counts[name] == 1:
                    return {"step": name, "status": "ok"}
                locator = (
                    "block-v1:FIRAx+1040045+20260807+type@problem+block@intro"
                    if wrong_delete
                    else ctx["delete_block_locator"]
                )
                live_vertical["blocks"] = [block for block in live_vertical["blocks"] if block["block_location"] != locator]
            elif name == "publish_vertical":
                live_vertical["published"] = True
            return {"step": name, "status": "ok"}

        def fake_state(*_args, **_kwargs):
            return module.vertical_child_snapshot(live_vertical), live_vertical.get("published")

        args = argparse.Namespace(execute=True, timeout=30, cookie="csrftoken=fake", csrf_token="", account="", password="", courses_base="")
        with patch.object(module, "maybe_fetch_live_replacement_plan", return_value=(live_plan, {"step": "load_live_target_structure", "status": "ok"})):
            with patch.object(module, "build_studio_session_from_args", return_value=(object(), "csrf", True)):
                with patch.object(module, "fetch_vertical_state", side_effect=fake_state):
                    with patch.object(module, "call_template", side_effect=fake_write):
                        outcome = None
                        failure = None
                        try:
                            outcome = module.apply_in_place_publish_actions(
                                {"studio_base": "https://studio.example.com", "block_locator": created_locator, "steps": []},
                                adapter=self.adapter,
                                replacement_plan=baseline_plan,
                                studio_url=f"https://studio.example.com/container/{target_locator}",
                                execute=True,
                                timeout=30,
                                args=args,
                            )
                        except module.PublishFailure as exc:
                            failure = exc
        return outcome, failure, counts

    def test_uncertain_applied_move_is_reported_without_replaying_for_final_validation(self) -> None:
        result, failure, counts = self._run_fake_vertical_replacement(uncertain_move="applied_then_timeout")

        self.assertIsNone(result)
        self.assertEqual("reorder_children", failure.step_name)
        self.assertEqual(1, counts["reorder_children"])
        self.assertIn("publish_vertical", [step["step"] for step in failure.steps if step["status"] == "skipped"])

    def test_unresolved_delete_retries_once_then_skips_vertical_publish(self) -> None:
        result, failure, counts = self._run_fake_vertical_replacement(fail_delete=True)

        self.assertIsNone(result)
        self.assertEqual(1, counts["delete_block"])
        self.assertEqual(0, counts["publish_vertical"])
        self.assertEqual("delete_block", failure.step_name)
        self.assertIn("publish_vertical", [step["step"] for step in failure.steps if step["status"] == "skipped"])

    def test_successful_delete_defers_postcondition_check_and_continues_to_publish(self) -> None:
        result, failure, counts = self._run_fake_vertical_replacement(noop_delete_once=True)

        self.assertIsNone(failure)
        self.assertEqual("replaced", result["status"])
        self.assertEqual(1, counts["delete_block"])
        self.assertEqual(1, counts["publish_vertical"])
        delete_step = next(step for step in result["steps"] if step["step"] == "delete_block")
        self.assertEqual("completed", delete_step["status"])
        self.assertEqual("deferred_to_batch_validation", delete_step["verification"])

    def test_uncertain_unapplied_move_is_reported_without_retrying_mid_batch(self) -> None:
        result, failure, counts = self._run_fake_vertical_replacement(uncertain_move="not_applied_once")

        self.assertIsNone(result)
        self.assertEqual("reorder_children", failure.step_name)
        self.assertEqual(1, counts["reorder_children"])

    def test_count_preserving_wrong_deletion_is_left_for_batch_snapshot_validation(self) -> None:
        result, failure, counts = self._run_fake_vertical_replacement(wrong_delete=True)

        self.assertIsNotNone(result)
        self.assertIsNone(failure)
        self.assertEqual("replaced", result["status"])
        self.assertEqual(1, counts["delete_block"])
        self.assertEqual(1, counts["publish_vertical"])
        delete_step = next(step for step in result["steps"] if step["step"] == "delete_block")
        self.assertEqual("deferred_to_batch_validation", delete_step["verification"])

    def test_load_course_structure_snapshot_normalizes_concise_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_path = tmp_path / "course.concise.json"
            course_path.write_text(
                json.dumps(self._make_concise_course_structure(), ensure_ascii=False),
                encoding="utf-8",
            )

            course_structure = module.load_course_structure_snapshot(course_path)

        self.assertIn("chapters", course_structure)
        plan = module.plan_publish_replacement(
            course_structure,
            source_vertical_block_id="block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
            target_vertical_block_id="block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
        )
        self.assertEqual("replaced", plan["result_type"])
        self.assertEqual(
            "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@livegallery",
            plan["matched_source_block_locator"],
        )

    def test_plan_publish_replacement_can_fall_back_to_section_html(self) -> None:
        course_structure = self._make_course_structure(
            include_gallery=False,
            include_target_html=False,
            include_section_html=True,
        )

        plan = module.plan_publish_replacement(
            course_structure,
            source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            target_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
        )

        self.assertEqual("html_based", plan["course_kind"])
        self.assertEqual("replaced", plan["result_type"])
        self.assertEqual("section", plan["match_scope"])
        self.assertEqual(
            "block-v1:FIRAx+1040045+20260807+type@vertical+block@sectionhtmlvertical",
            plan["working_vertical_block_id"],
        )
        self.assertEqual(
            "block-v1:FIRAx+1040045+20260807+type@html+block@wordiesthtml",
            plan["matched_source_block_locator"],
        )

    def test_plan_publish_replacement_uses_html_path_for_mixed_course_when_target_vertical_has_no_gallery(self) -> None:
        plan = module.plan_publish_replacement(
            self._make_mixed_course_structure(),
            source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            target_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
        )

        self.assertEqual("html_based", plan["course_kind"])
        self.assertEqual("replaced", plan["result_type"])
        self.assertEqual(
            "block-v1:FIRAx+1040045+20260807+type@html+block@targethtml",
            plan["matched_source_block_locator"],
        )

    def test_build_vertical_children_places_new_block_before_replaced_source(self) -> None:
        children = [
            "block-v1:FIRAx+1040045+20260807+type@html+block@intro",
            "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
            "block-v1:FIRAx+1040045+20260807+type@problem+block@quiz",
            "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@newgallery",
        ]

        reordered = module.build_vertical_children(
            children,
            new_block_locator="block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@newgallery",
            replace_block_locator="block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
        )

        self.assertEqual(
            [
                "block-v1:FIRAx+1040045+20260807+type@html+block@intro",
                "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@newgallery",
                "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
                "block-v1:FIRAx+1040045+20260807+type@problem+block@quiz",
            ],
            reordered,
        )

    def test_resolve_rerun_action_skips_previous_success_to_avoid_duplicate_creation(self) -> None:
        action = module.resolve_rerun_action(
            {
                "status": "replaced",
                "note": "上一轮已经成功",
            },
            current_structure_block_ids=[],
        )

        self.assertEqual("skip", action["action"])
        self.assertIn("already succeeded", action["reason"])

    def test_resolve_rerun_action_requests_orphan_cleanup_when_created_block_is_missing(self) -> None:
        action = module.resolve_rerun_action(
            {
                "status": "failed",
                "created_block_locator": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@newgallery",
                "failed_step": "publish_vertical",
            },
            current_structure_block_ids=[
                "block-v1:FIRAx+1040045+20260807+type@html+block@intro",
                "block-v1:FIRAx+1040045+20260807+type@problem+block@quiz",
            ],
        )

        self.assertEqual("cleanup_orphaned_created_block", action["action"])
        self.assertIn("missing from refreshed course structure", action["reason"])

    def test_run_batch_publish_groups_results_and_continues_after_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            targets_file = tmp_path / "imagegallery-push-studio-targets.json"
            report_file = tmp_path / "publish-report.json"
            section_list = tmp_path / "section-list.json"
            (tmp_path / "course.json").write_text(json.dumps({"chapters": []}, ensure_ascii=False), encoding="utf-8")
            section_list.write_text('{"sections":[]}', encoding="utf-8")
            targets_file.write_text(
                json.dumps(
                    {
                        "course_structure_json": str(tmp_path / "course.json"),
                        "section_list_json": str(section_list),
                        "targets": [
                            {"name": "1.1", "manifest": "a.json", "target_vertical_block_location": "vertical-a", "studio_vertical_url": "https://studio.example.com/container/vertical-a"},
                            {"name": "1.2", "manifest": "b.json", "target_vertical_block_location": "vertical-b", "studio_vertical_url": "https://studio.example.com/container/vertical-b"},
                            {"name": "1.3", "manifest": "c.json", "target_vertical_block_location": "vertical-c", "studio_vertical_url": "https://studio.example.com/container/vertical-c"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            calls: list[str] = []

            def fake_runner(target, _course_structure, _args, _adapter, previous_result=None):
                calls.append(target["name"])
                if target["name"] == "1.1":
                    return {"status": "replaced", "target_vertical_url": target["studio_vertical_url"], "note": "ok"}
                if target["name"] == "1.2":
                    return {"status": "fallback_inserted", "target_vertical_url": target["studio_vertical_url"], "note": "fallback"}
                raise module.PublishFailure("publish_vertical", "target vertical publish failed")

            args = argparse.Namespace(
                targets_file=str(targets_file),
                report_file=str(report_file),
            )

            result = module.run_batch_publish(args, publish_target_fn=fake_runner)

            self.assertEqual(["1.1", "1.2", "1.3"], calls)
            self.assertEqual(1, result["summary"]["replaced_count"])
            self.assertEqual(1, result["summary"]["fallback_inserted_count"])
            self.assertEqual(1, result["summary"]["failed_count"])
            self.assertEqual(1, result["summary"]["manual_followup_failed_count"])
            self.assertEqual("1.3", result["failed"][0]["name"])
            self.assertEqual("1.3", result["manual_followup_failed"][0]["name"])
            self.assertTrue(report_file.exists())
            section_input = next(item for item in result["provenance"]["inputs"] if item["name"] == "section-list.json")
            self.assertEqual(module.file_sha256(section_list), section_input["sha256"])

    def test_run_batch_publish_reloads_course_structure_before_each_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_path = tmp_path / "course.json"
            targets_file = tmp_path / "imagegallery-push-studio-targets.json"
            report_file = tmp_path / "publish-report.json"

            course_path.write_text(json.dumps({"chapters": [{"name": "v1"}]}, ensure_ascii=False), encoding="utf-8")
            targets_file.write_text(
                json.dumps(
                    {
                        "course_structure_json": str(course_path),
                        "targets": [
                            {"name": "1.1", "manifest": "a.json", "target_vertical_block_location": "vertical-a", "studio_vertical_url": "https://studio.example.com/container/vertical-a"},
                            {"name": "1.2", "manifest": "b.json", "target_vertical_block_location": "vertical-b", "studio_vertical_url": "https://studio.example.com/container/vertical-b"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            seen_course_names: list[str] = []

            def fake_runner(target, course_structure, _args, _adapter, previous_result=None):
                seen_course_names.append(course_structure["chapters"][0]["name"])
                if target["name"] == "1.1":
                    course_path.write_text(json.dumps({"chapters": [{"name": "v2"}]}, ensure_ascii=False), encoding="utf-8")
                return {"status": "replaced", "target_vertical_url": target["studio_vertical_url"], "note": "ok"}

            args = argparse.Namespace(
                targets_file=str(targets_file),
                report_file=str(report_file),
            )

            module.run_batch_publish(args, publish_target_fn=fake_runner)

        self.assertEqual(["v1", "v2"], seen_course_names)

    def test_run_batch_publish_reports_invalid_targets_json_with_step_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            targets_file = tmp_path / "imagegallery-push-studio-targets.json"
            targets_file.write_text('{"targets": [}', encoding="utf-8")

            args = argparse.Namespace(
                targets_file=str(targets_file),
                report_file="",
                adapter=str(ADAPTER_PATH),
            )

            with self.assertRaises(module.PublishFailure) as ctx:
                module.run_batch_publish(args)

        self.assertEqual("load_batch_targets", ctx.exception.step_name)
        self.assertIn("invalid json file", str(ctx.exception))
        self.assertIn(str(targets_file), str(ctx.exception))

    def test_run_batch_publish_reports_invalid_previous_report_json_with_step_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_path = tmp_path / "course.json"
            targets_file = tmp_path / "imagegallery-push-studio-targets.json"
            report_file = tmp_path / "publish-report.json"
            course_path.write_text(json.dumps({"chapters": []}, ensure_ascii=False), encoding="utf-8")
            targets_file.write_text(
                json.dumps(
                    {
                        "course_structure_json": str(course_path),
                        "targets": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report_file.write_text('{"results": [}', encoding="utf-8")

            args = argparse.Namespace(
                targets_file=str(targets_file),
                report_file=str(report_file),
                adapter=str(ADAPTER_PATH),
            )

            with self.assertRaises(module.PublishFailure) as ctx:
                module.run_batch_publish(args)

        self.assertEqual("load_previous_batch_results", ctx.exception.step_name)
        self.assertIn(str(report_file), str(ctx.exception))

    def test_run_in_place_publish_imagesgallery_sample_returns_operator_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = self._write_manifest_bundle(tmp_path)
            self._mark_manifest_source(manifest_path, block_type="imagesgallery")
            section_list_path = tmp_path / "section-list.json"
            section_list_path.write_text(
                json.dumps({"sections": [{"section_id": "1.1", "section_title": "1.1 示例课程", "block_type": "imagesgallery"}]}),
                encoding="utf-8",
            )
            course_path = tmp_path / "course.json"
            course_path.write_text(
                json.dumps(
                    self._make_course_structure(
                        include_gallery=True,
                        include_target_html=False,
                        include_section_html=False,
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                studio_url="https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                manifest=str(manifest_path),
                adapter=str(ADAPTER_PATH),
                execute=False,
                dry_run=True,
                timeout=30,
                vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                block_url="",
                source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                course_structure_json=str(course_path),
                courses_base="",
                account="",
                password="",
                cookie="csrftoken=fake",
                csrf_token="",
                skip_oss_multipart=False,
                auth_retry_max_retries=4,
                auth_retry_delay_seconds=0,
                targets_file="",
                report_file="",
                section_list_json=str(section_list_path),
            )

            with patch.dict("os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False):
                result = module.run_in_place_publish(args)

            section_input = next(item for item in result["provenance"]["inputs"] if item["name"] == "section-list.json")
            self.assertEqual(module.file_sha256(section_list_path), section_input["sha256"])

        self.assertEqual("replaced", result["status"])
        self.assertEqual(
            "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            result["target_vertical_url"],
        )
        self.assertEqual(
            "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
            result["matched_source_block_url"],
        )
        self.assertTrue(result["created_imagegallery_block_url"].startswith("https://studio.example.com/container/"))

    def test_run_in_place_publish_html_section_sample_returns_working_vertical_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = self._write_manifest_bundle(tmp_path)
            self._mark_manifest_source(manifest_path, block_type="html")
            course_path = tmp_path / "course.json"
            course_path.write_text(
                json.dumps(
                    self._make_course_structure(
                        include_gallery=False,
                        include_target_html=True,
                        include_section_html=False,
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                studio_url="https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                manifest=str(manifest_path),
                adapter=str(ADAPTER_PATH),
                execute=False,
                dry_run=True,
                timeout=30,
                vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                block_url="",
                source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                course_structure_json=str(course_path),
                courses_base="",
                account="",
                password="",
                cookie="csrftoken=fake",
                csrf_token="",
                skip_oss_multipart=False,
                auth_retry_max_retries=4,
                auth_retry_delay_seconds=0,
                targets_file="",
                report_file="",
            )

            with patch.dict("os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False):
                result = module.run_in_place_publish(args)

        self.assertEqual("replaced", result["status"])
        self.assertEqual(
            "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            result["target_vertical_url"],
        )
        self.assertEqual(
            "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            result["requested_target_vertical_url"],
        )
        self.assertEqual(
            "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@html+block@targethtml",
            result["matched_source_block_url"],
        )

    def test_html_origin_publish_preserves_summary_components_after_saving_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = self._write_manifest_bundle(tmp_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["block_type"] = "html"
            manifest["section_id"] = "1.1"
            manifest["section_title"] = "1.1 示例课程"
            manifest["items"][0]["subtitle"] = "# 本节要点\n总结内容"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            course_structure = self._make_course_structure(
                include_gallery=False,
                include_target_html=True,
                include_section_html=False,
            )
            terminal_summary = {
                "name": "要点小结",
                "category": "html",
                "block_location": "block-v1:FIRAx+1040045+20260807+type@html+block@terminal-summary",
                "html": "  <h2>### 本节要点</h2><p><img src='slide.png'/>总结正文</p>",
            }
            target_vertical = course_structure["chapters"][0]["sections"][0]["verticals"][0]
            target_vertical["blocks"].append(terminal_summary)
            target_vertical["blocks"].insert(
                0,
                {
                    "name": "本节要点",
                    "category": "html",
                    "block_location": "block-v1:FIRAx+1040045+20260807+type@html+block@early-summary",
                    "html": "<h2>本节要点</h2><p>另一份回顾</p>",
                },
            )

            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")
            args = argparse.Namespace(
                studio_url="https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                manifest=str(manifest_path),
                adapter=str(ADAPTER_PATH),
                execute=False,
                dry_run=True,
                timeout=30,
                vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                course_structure_json=str(course_path),
                courses_base="",
                account="",
                password="",
                cookie="csrftoken=fake",
                csrf_token="",
                skip_oss_multipart=False,
                auth_retry_max_retries=4,
                auth_retry_delay_seconds=0,
                targets_file="",
                report_file="",
            )

            result = module.run_in_place_publish(args)

        self.assertEqual("failed", result["status"])
        self.assertEqual("stage_b_output_contract", result["failed_step"])
        self.assertEqual([1], result["leaking_summary_pages"])
        self.assertIn("publish_vertical", result["skipped_steps"])

    def test_cleanup_only_dry_run_is_unsupported_without_studio_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_structure = self._make_course_structure(
                include_gallery=True,
                include_target_html=True,
                include_section_html=False,
            )
            target_vertical = course_structure["chapters"][0]["sections"][0]["verticals"][0]
            gallery = next(block for block in target_vertical["blocks"] if block["category"] == "imagesgallery")
            gallery["metadata"] = {
                "play_config": {
                    "time_config": [
                        {"page": 1, "subtitle": "课程正文"},
                        {"page": 2, "subtitle": "## 本节要点\n最后一页"},
                    ]
                }
            }
            summary = {
                "name": "本节要点",
                "category": "html",
                "block_location": "block-v1:FIRAx+1040045+20260807+type@html+block@cleanup-summary",
                "html": "<h2>本节要点</h2><p>旧摘要</p>",
            }
            target_vertical["blocks"].append(summary)
            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")

            args = argparse.Namespace(
                studio_url="https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                manifest="",
                adapter=str(ADAPTER_PATH),
                execute=False,
                dry_run=True,
                timeout=30,
                vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                course_structure_json=str(course_path),
                section_id="1.1",
                section_title="1.1 示例课程",
                section_list_json="",
                cleanup_only=True,
                courses_base="",
                account="",
                password="",
                cookie="csrftoken=fake",
                csrf_token="",
                skip_oss_multipart=False,
                auth_retry_max_retries=4,
                auth_retry_delay_seconds=0,
                targets_file="",
                report_file="",
            )

            result = module.run_in_place_publish(args)

        self.assertEqual("unsupported", result["status"])
        self.assertEqual("cleanup_only_unsupported", result["failed_step"])
        self.assertEqual("preserved", result["summary_cleanup"]["status"])
        self.assertIn("cleanup-only is unsupported", result["note"])
        self.assertEqual([], result["steps"])

    def test_cleanup_only_execute_is_unsupported_before_studio_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_structure = self._make_course_structure(
                include_gallery=True,
                include_target_html=True,
                include_section_html=False,
            )
            target_vertical = course_structure["chapters"][0]["sections"][0]["verticals"][0]
            gallery = next(block for block in target_vertical["blocks"] if block["category"] == "imagesgallery")
            gallery["metadata"] = {"play_config": {"time_config": [{"page": 1, "subtitle": "第一部分"}, {"page": 2, "subtitle": "# 本节要点"}]}}
            target_vertical["blocks"].append(
                {
                    "name": "本节要点",
                    "category": "html",
                    "block_location": "block-v1:FIRAx+1040045+20260807+type@html+block@execute-cleanup-summary",
                    "html": "<h2>本节要点</h2><p>旧摘要</p>",
                }
            )
            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")
            args = self._in_place_args(Path(""), course_path, execute=True, cleanup_only=True)
            calls: list[tuple[str, str]] = []

            def fake_adapter(_session, name, _template, ctx, _execute, _timeout):
                calls.append((name, str(ctx.get("delete_block_locator", ""))))
                return {"step": name, "status": "ok"}

            with patch.object(module, "fetch_live_course_structure", side_effect=AssertionError("must not read Studio")) as live_fetch:
                with patch.object(module, "build_studio_session_from_args", side_effect=AssertionError("must not authenticate")) as session_builder:
                    with patch.object(module, "call_template", side_effect=fake_adapter):
                        result = module.run_in_place_publish(args)
            live_fetch.assert_not_called()
            session_builder.assert_not_called()

        self.assertEqual("unsupported", result["status"])
        self.assertEqual("cleanup_only_unsupported", result["failed_step"])
        self.assertEqual("preserved", result["summary_cleanup"]["status"])
        self.assertEqual([], calls)
        self.assertEqual([], result["steps"])

        cli_stdout = StringIO()
        with patch("sys.argv", ["publish_imagegallery_block.py", "--cleanup-only", "--execute"]):
            with patch.object(module, "build_studio_session_from_args", side_effect=AssertionError("must not authenticate")):
                with patch.object(module, "fetch_live_course_structure", side_effect=AssertionError("must not read Studio")):
                    with patch.object(module, "call_template", side_effect=AssertionError("must not write Studio")):
                        with redirect_stdout(cli_stdout):
                            exit_code = module.main()
        self.assertEqual(2, exit_code, cli_stdout.getvalue())
        self.assertEqual("unsupported", json.loads(cli_stdout.getvalue())["status"])

    def test_cleanup_only_is_unsupported_even_without_a_summary_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            course_structure = self._make_course_structure(
                include_gallery=True,
                include_target_html=True,
                include_section_html=False,
            )
            gallery = next(
                block
                for block in course_structure["chapters"][0]["sections"][0]["verticals"][0]["blocks"]
                if block["category"] == "imagesgallery"
            )
            gallery["metadata"] = {"play_config": {"time_config": [{"page": 1, "subtitle": "没有摘要页"}]}}
            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")

            result = module.run_in_place_publish(
                self._in_place_args(Path(""), course_path, cleanup_only=True)
            )

        self.assertEqual("unsupported", result["status"])
        self.assertEqual("cleanup_only_unsupported", result["failed_step"])
        self.assertEqual("preserved", result["summary_cleanup"]["status"])
        self.assertEqual([], result["steps"])

    def test_summary_components_are_untouched_while_body_replacement_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = self._write_manifest_bundle(tmp_path)
            self._mark_manifest_source(manifest_path, block_type="html")
            course_structure = self._make_course_structure(
                include_gallery=False,
                include_target_html=True,
                include_section_html=False,
            )
            summary = self._add_terminal_summary(course_structure)
            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")
            plan = module.plan_publish_replacement(
                course_structure,
                source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                target_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                block_type="html",
            )
            args = self._in_place_args(manifest_path, course_path, execute=False)
            calls: list[tuple[str, str]] = []
            created_locator = "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@newgallery"

            def fake_adapter(_session, name, _template, ctx, _execute, _timeout):
                locator = str(ctx.get("delete_block_locator", ""))
                calls.append((name, locator))
                if name == "create_block":
                    ctx["block_locator"] = created_locator
                if name == "delete_block" and locator == summary["block_location"]:
                    raise module.TemplateCallError(name, 500, {"detail": "delete failed"})
                if name.startswith("upload_image"):
                    return {"step": name, "status": "ok", "asset_id": "asset-001"}
                if name == "upload_audio_prepare":
                    ctx["file_key"] = "12345678-1234-1234-1234-123456789abc"
                return {"step": name, "status": "ok", "url": "fake://" + name}

            with patch.dict("os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False):
                with patch.object(module, "maybe_fetch_live_replacement_plan", return_value=(plan, {"step": "load_live_target_structure", "status": "ok"})):
                    with patch.object(module, "build_studio_session_from_args", return_value=(object(), "csrf", True)):
                        with patch.object(module, "call_template", side_effect=fake_adapter):
                            result = module.process_publish_target(
                                {
                                    "name": "1.1",
                                    "manifest": str(manifest_path),
                                    "section_id": "1.1",
                                    "block_type": "html",
                                    "target_vertical_block_location": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                                    "source_vertical_block_location": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                                    "studio_vertical_url": "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                                },
                                course_structure,
                                args,
                                self.adapter,
                            )

        self.assertEqual("replaced", result["status"])
        self.assertTrue(result["created_imagegallery_block_url"].endswith("__dry_run_created_imagesgallery__"))
        self.assertTrue(result["target_vertical_url"].endswith("block@targetvertical"))
        self.assertEqual("", result["matched_summary_block_url"])
        self.assertEqual("preserved", result["summary_cleanup"]["status"])
        self.assertIn(("delete_block", plan["matched_source_block_locator"]), calls)
        self.assertNotIn(("delete_block", summary["block_location"]), calls)
        self.assertIn("publish_vertical", [name for name, _locator in calls])
        self.assertNotIn("rollback", [name for name, _locator in calls])
        self.assertIn("save_block", result["completed_steps"])
        self.assertNotIn("delete_block", result["failed_steps"])

    def test_summary_preservation_does_not_depend_on_section_list_source_fallback(self) -> None:
        for use_section_id in (True, False):
            with self.subTest(use_section_id=use_section_id), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                manifest_path = self._write_manifest_bundle(tmp_path)
                self._mark_manifest_source(
                    manifest_path,
                    block_type=None,
                    section_id="1.1" if use_section_id else "",
                    section_title="共同标题",
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.pop("block_type", None)
                if not use_section_id:
                    manifest.pop("section_id", None)
                    manifest.pop("section_title", None)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

                section_list_path = tmp_path / "section-list.json"
                sections = [
                    {"section_id": "1.1", "section_title": "共同标题", "block_type": "html"},
                    {"section_id": "1.2", "section_title": "共同标题", "block_type": "imagesgallery"},
                ] if use_section_id else [
                    {"section_id": "unique-id", "section_title": "唯一标题", "block_type": "html"},
                ]
                section_list_path.write_text(json.dumps({"sections": sections}, ensure_ascii=False), encoding="utf-8")
                course_structure = self._make_course_structure(
                    include_gallery=False,
                    include_target_html=True,
                    include_section_html=False,
                )
                self._add_terminal_summary(course_structure)
                course_path = tmp_path / "course.json"
                course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")
                args = self._in_place_args(manifest_path, course_path, section_list_json=str(section_list_path))
                if not use_section_id:
                    args.section_title = "唯一标题"

                result = module.run_in_place_publish(args)

                self.assertEqual("preserved", result["summary_cleanup"]["status"])

    def test_summary_preservation_does_not_depend_on_section_list_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_dir = tmp_path / "another-course" / "gallery"
            manifest_dir.mkdir(parents=True)
            manifest_path = self._write_manifest_bundle(manifest_dir)
            self._mark_manifest_source(
                manifest_path,
                block_type="html",
                section_id="1.1",
                section_title="1.1 示例课程",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            unrelated_section_list = tmp_path / "section-list.json"
            unrelated_section_list.write_text(
                json.dumps(
                    {"sections": [{"section_id": "1.1", "section_title": "1.1 示例课程", "block_type": "html"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            course_structure = self._make_course_structure(
                include_gallery=False,
                include_target_html=True,
                include_section_html=False,
            )
            self._add_terminal_summary(course_structure)
            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")

            result = module.run_in_place_publish(self._in_place_args(manifest_path, course_path))
            explicit_path_result = module.run_in_place_publish(
                self._in_place_args(
                    manifest_path,
                    course_path,
                    section_list_json=str(unrelated_section_list),
                )
            )

        self.assertEqual("replaced", result["status"])
        self.assertEqual("preserved", result["summary_cleanup"]["status"])
        delete_urls = [step.get("url", "") for step in result["steps"] if step["step"] == "delete_block"]
        self.assertFalse(any(url.endswith("block@terminal-summary") for url in delete_urls))
        self.assertEqual("preserved", explicit_path_result["summary_cleanup"]["status"])

    def test_missing_ambiguous_and_conflicting_source_metadata_fails_before_replacement(self) -> None:
        scenarios = ("missing", "ambiguous", "conflicting")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                manifest_path = self._write_manifest_bundle(tmp_path)
                self._mark_manifest_source(manifest_path, block_type="html" if scenario == "conflicting" else None)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if scenario != "conflicting":
                    manifest.pop("block_type", None)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                section_list_path = tmp_path / "section-list.json"
                if scenario == "ambiguous":
                    section_list_path.write_text(
                        json.dumps(
                            {"sections": [
                                {"section_id": "1.1", "section_title": "1.1 示例课程", "block_type": "html"},
                                {"section_id": "1.1", "section_title": "1.1 示例课程", "block_type": "imagesgallery"},
                            ]},
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                course_structure = self._make_course_structure(
                    include_gallery=False,
                    include_target_html=True,
                    include_section_html=False,
                )
                summary = self._add_terminal_summary(course_structure)
                course_path = tmp_path / "course.json"
                course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")
                args = self._in_place_args(
                    manifest_path,
                    course_path,
                    section_list_json=str(section_list_path) if scenario == "ambiguous" else "",
                )
                target = {
                    "name": "1.1",
                    "manifest": str(manifest_path),
                    "section_id": "1.1",
                    "section_title": "1.1 示例课程",
                    "block_type": "imagesgallery" if scenario == "conflicting" else "",
                    "target_vertical_block_location": args.vertical_block_id,
                    "source_vertical_block_location": args.vertical_block_id,
                    "studio_vertical_url": args.studio_url,
                }
                result = module.process_publish_target(target, course_structure, args, self.adapter)

                self.assertEqual("failed", result["status"])
                self.assertTrue(result["requires_manual_followup"])
                self.assertIn(result["failed_step"], {"resolve_source_block_type"})
                self.assertNotIn("delete_block", [step.get("step") for step in result["steps"]])

    def test_summary_preservation_is_independent_of_gallery_heading_and_match_count(self) -> None:
        for scenario in ("gallery_without_summary", "html_no_match", "html_nonterminal_summary", "imagegallery_origin", "ambiguous_summary"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                manifest_path = self._write_manifest_bundle(tmp_path)
                source_type = "imagesgallery" if scenario == "imagegallery_origin" else "html"
                subtitle = "## 正文\n普通课程内容"
                self._mark_manifest_source(manifest_path, block_type=source_type, subtitle=subtitle)
                include_gallery = scenario == "imagegallery_origin"
                course_structure = self._make_course_structure(
                    include_gallery=include_gallery,
                    include_target_html=True,
                    include_section_html=False,
                )
                if scenario in {"gallery_without_summary", "html_nonterminal_summary", "imagegallery_origin"}:
                    self._add_terminal_summary(course_structure)
                    if scenario == "html_nonterminal_summary":
                        course_structure["chapters"][0]["sections"][0]["verticals"][0]["blocks"].append(
                            {
                                "name": "随堂练习",
                                "category": "problem",
                                "block_location": "block-v1:FIRAx+1040045+20260807+type@problem+block@after-summary",
                            }
                        )
                elif scenario == "html_no_match":
                    pass
                else:
                    self._add_terminal_summary(course_structure, locator="block-v1:FIRAx+1040045+20260807+type@html+block@summary-one")
                    self._add_terminal_summary(course_structure, locator="block-v1:FIRAx+1040045+20260807+type@html+block@summary-two", name="总结")
                course_path = tmp_path / "course.json"
                course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")

                result = module.run_in_place_publish(self._in_place_args(manifest_path, course_path))

                self.assertEqual("replaced", result["status"])
                self.assertEqual("preserved", result["summary_cleanup"]["status"])
                delete_urls = [step.get("url", "") for step in result["steps"] if step["step"] == "delete_block"]
                self.assertFalse(any("summary" in url for url in delete_urls))
                if scenario == "imagegallery_origin":
                    self.assertTrue(any(url.endswith("block@legacygallery") for url in delete_urls))

    def test_publish_only_retry_preserves_summary_without_repeating_body_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = self._write_manifest_bundle(tmp_path)
            self._mark_manifest_source(manifest_path, block_type="html")
            initial_structure = self._make_course_structure(
                include_gallery=False,
                include_target_html=True,
                include_section_html=False,
            )
            self._add_terminal_summary(initial_structure)
            current_structure = initial_structure
            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(initial_structure, ensure_ascii=False), encoding="utf-8")
            args = self._in_place_args(manifest_path, course_path, execute=True)
            target = {
                "name": "1.1",
                "manifest": str(manifest_path),
                "section_id": "1.1",
                "target_vertical_block_location": args.vertical_block_id,
                "source_vertical_block_location": args.vertical_block_id,
                "studio_vertical_url": args.studio_url,
            }
            created_locator = "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@newgallery"
            calls: list[str] = []
            allow_publish_success = False
            target_vertical = current_structure["chapters"][0]["sections"][0]["verticals"][0]
            target_vertical["published"] = False

            def fake_live_plan(**kwargs):
                return (
                    module.plan_publish_replacement(
                        current_structure,
                        source_vertical_block_id=kwargs["source_vertical_block_id"],
                        target_vertical_block_id=kwargs["target_vertical_block_id"],
                        block_type=kwargs.get("block_type", ""),
                        excluded_block_locators=kwargs.get("excluded_block_locators", []),
                    ),
                    {"step": "load_live_target_structure", "status": "ok"},
                )

            def fake_adapter(_session, name, _template, ctx, _execute, _timeout):
                nonlocal allow_publish_success
                calls.append(name)
                if name == "create_block":
                    ctx["block_locator"] = created_locator
                    target_vertical["blocks"].append(
                        {
                            "name": "有声幻灯片",
                            "category": "imagesgallery",
                            "block_location": created_locator,
                        }
                    )
                if name == "reorder_children":
                    blocks_by_locator = {
                        block.get("block_location"): block for block in target_vertical["blocks"]
                    }
                    target_vertical["blocks"] = [
                        blocks_by_locator[locator]
                        for locator in ctx["vertical_children_obj"]
                        if locator in blocks_by_locator
                    ]
                if name == "delete_block":
                    target_vertical["blocks"] = [
                        block for block in target_vertical["blocks"]
                        if block.get("block_location") != ctx["delete_block_locator"]
                    ]
                if name == "publish_vertical" and not allow_publish_success:
                    raise module.TemplateCallError(name, 500, {"detail": "publish failed"})
                if name == "publish_vertical":
                    target_vertical["published"] = True
                if name.startswith("upload_image"):
                    return {"step": name, "status": "ok", "asset_id": "asset-001"}
                if name == "upload_audio_prepare":
                    ctx["file_key"] = "12345678-1234-1234-1234-123456789abc"
                return {"step": name, "status": "ok"}

            with patch.dict("os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False):
                with patch.object(module, "maybe_fetch_live_replacement_plan", side_effect=fake_live_plan):
                    with patch.object(module, "build_studio_session_from_args", return_value=(object(), "csrf", True)):
                        with patch.object(module, "fetch_vertical_state", side_effect=lambda _session, **kwargs: module.vertical_snapshot_for_course(current_structure, kwargs["vertical_locator"])):
                            with patch.object(module, "call_template", side_effect=fake_adapter):
                                first_result = module.process_publish_target(target, initial_structure, args, self.adapter)
                                self.assertEqual("failed", first_result["status"])
                                self.assertEqual("publish_vertical", first_result["failed_step"])
                                self.assertEqual("preserved", first_result["summary_cleanup"]["status"])
                                self.assertEqual(1, calls.count("publish_vertical"))

                                allow_publish_success = True
                                start_retry_calls = len(calls)
                                second_result = module.process_publish_target(
                                    target,
                                    current_structure,
                                    args,
                                    self.adapter,
                                    previous_result=first_result,
                                )

        self.assertEqual("replaced", second_result["status"])
        self.assertTrue(second_result["resumed_from_previous_run"])
        self.assertEqual(["publish_vertical"], calls[start_retry_calls:])
        self.assertEqual("preserved", second_result["summary_cleanup"]["status"])
        self.assertEqual(created_locator, second_result["created_block_locator"])

    def test_batch_preserves_html_summary_components_for_each_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = self._write_manifest_bundle(tmp_path)
            self._mark_manifest_source(manifest_path, block_type="html")
            course_structure = self._make_course_structure(
                include_gallery=False,
                include_target_html=True,
                include_section_html=False,
            )
            summary = self._add_terminal_summary(course_structure)
            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")
            targets_path = tmp_path / "targets.json"
            report_path = tmp_path / "report.json"
            target_vertical_id = "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical"
            later_vertical_id = "block-v1:FIRAx+1040045+20260807+type@vertical+block@latervertical"
            targets_path.write_text(
                json.dumps(
                    {
                        "course_structure_json": str(course_path),
                        "targets": [
                            {
                                "name": "1.1 summary is preserved",
                                "manifest": str(manifest_path),
                                "target_vertical_block_location": target_vertical_id,
                                "source_vertical_block_location": target_vertical_id,
                                "studio_vertical_url": f"https://studio.example.com/container/{target_vertical_id}",
                            },
                            {
                                "name": "1.2 later success",
                                "manifest": str(manifest_path),
                                "target_vertical_block_location": later_vertical_id,
                                "source_vertical_block_location": later_vertical_id,
                                "studio_vertical_url": f"https://studio.example.com/container/{later_vertical_id}",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            args = self._in_place_args(manifest_path, course_path, execute=False)
            args.targets_file = str(targets_path)
            args.report_file = str(report_path)
            calls: list[str] = []

            def fake_runner(target, structure, publish_args, adapter, previous_result=None):
                if target["name"] == "1.2 later success":
                    calls.append(target["name"])
                    return {"status": "replaced", "target_vertical_url": target["studio_vertical_url"], "steps": []}
                calls.append(target["name"])
                plan = module.plan_publish_replacement(
                    structure,
                    source_vertical_block_id=target["source_vertical_block_location"],
                    target_vertical_block_id=target["target_vertical_block_location"],
                )
                created_locator = "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@batch-gallery"

                def fake_live_plan(**kwargs):
                    return (plan, {"step": "load_live_target_structure", "status": "ok"})

                def fake_adapter(_session, name, _template, ctx, _execute, _timeout):
                    if name == "create_block":
                        ctx["block_locator"] = created_locator
                    if name == "delete_block" and ctx.get("delete_block_locator") == summary["block_location"]:
                        raise module.TemplateCallError(name, 500, {"detail": "summary cleanup failed"})
                    if name.startswith("upload_image"):
                        return {"step": name, "status": "ok", "asset_id": "asset-001"}
                    if name == "upload_audio_prepare":
                        ctx["file_key"] = "12345678-1234-1234-1234-123456789abc"
                    return {"step": name, "status": "ok"}

                with patch.dict("os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False):
                    with patch.object(module, "maybe_fetch_live_replacement_plan", side_effect=fake_live_plan):
                        with patch.object(module, "build_studio_session_from_args", return_value=(object(), "csrf", True)):
                            with patch.object(module, "call_template", side_effect=fake_adapter):
                                return module.process_publish_target(target, structure, publish_args, adapter, previous_result=previous_result)

            result = module.run_batch_publish(args, publish_target_fn=fake_runner)

        self.assertEqual(["1.1 summary is preserved", "1.2 later success"], calls)
        self.assertEqual(0, result["summary"]["failed_count"])
        self.assertEqual(2, result["summary"]["replaced_count"])
        self.assertEqual("preserved", result["replaced"][0]["summary_cleanup"]["status"])
        self.assertEqual("1.2 later success", result["replaced"][1]["name"])

    def test_batch_cleanup_only_target_is_unsupported_before_studio_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = self._write_manifest_bundle(tmp_path)
            course_structure = self._make_course_structure(
                include_gallery=True,
                include_target_html=True,
                include_section_html=False,
            )
            course_path = tmp_path / "course.json"
            course_path.write_text(json.dumps(course_structure, ensure_ascii=False), encoding="utf-8")
            targets_path = tmp_path / "targets.json"
            report_path = tmp_path / "report.json"
            target_vertical_id = "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical"
            targets_path.write_text(
                json.dumps(
                    {
                        "course_structure_json": str(course_path),
                        "targets": [
                            {
                                "name": "legacy cleanup target",
                                "manifest": str(manifest_path),
                                "cleanup_only": True,
                                "target_vertical_block_location": target_vertical_id,
                                "source_vertical_block_location": target_vertical_id,
                                "studio_vertical_url": f"https://studio.example.com/container/{target_vertical_id}",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            args = self._in_place_args(manifest_path, course_path, execute=True)
            args.targets_file = str(targets_path)
            args.report_file = str(report_path)

            with patch.object(module, "fetch_live_course_structure", side_effect=AssertionError("must not read Studio")) as live_fetch:
                with patch.object(module, "build_studio_session_from_args", side_effect=AssertionError("must not authenticate")) as session_builder:
                    with patch.object(module, "call_template", side_effect=AssertionError("must not write Studio")) as adapter_call:
                        result = module.run_batch_publish(args)

            live_fetch.assert_not_called()
            session_builder.assert_not_called()
            adapter_call.assert_not_called()
            self.assertEqual("unsupported", result["results"][0]["status"])
            self.assertEqual("preserved", result["results"][0]["summary_cleanup"]["status"])
            self.assertEqual(1, result["summary"]["unsupported_count"])
            self.assertEqual("unsupported", result["unsupported"][0]["status"])
            self.assertNotIn("cleaned", result)
            self.assertNotIn("cleanup_planned", result)
            self.assertNotIn("cleaned_count", result["summary"])
            self.assertNotIn("cleanup_planned_count", result["summary"])

            cli_stdout = StringIO()
            cli_args = [
                "publish_imagegallery_block.py",
                "--targets-file",
                str(targets_path),
                "--report-file",
                str(report_path),
                "--execute",
            ]
            with patch("sys.argv", cli_args):
                with patch.object(module, "build_studio_session_from_args", side_effect=AssertionError("must not authenticate")):
                    with patch.object(module, "fetch_live_course_structure", side_effect=AssertionError("must not read Studio")):
                        with patch.object(module, "call_template", side_effect=AssertionError("must not write Studio")):
                            with redirect_stdout(cli_stdout):
                                exit_code = module.main()
            self.assertEqual(2, exit_code, cli_stdout.getvalue())
            self.assertEqual("unsupported", json.loads(cli_stdout.getvalue())["results"][0]["status"])

    def test_apply_in_place_publish_actions_supports_dry_run_without_created_locator(self) -> None:
        result = module.apply_in_place_publish_actions(
            {
                "studio_base": "https://studio.example.com",
                "block_locator": "",
                "steps": [],
            },
            adapter=self.adapter,
            replacement_plan={
                "result_type": "replaced",
                "course_kind": "imagesgallery_based",
                "match_scope": "target_vertical",
                "target_vertical_block_id": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                "working_vertical_block_id": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                "current_children": [
                    "block-v1:FIRAx+1040045+20260807+type@problem+block@intro",
                    "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
                ],
                "matched_source_block_locator": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
            },
            studio_url="https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            execute=False,
            timeout=30,
            args=argparse.Namespace(
                cookie="csrftoken=fake",
                csrf_token="",
                account="",
                password="",
                courses_base="",
            ),
        )

        self.assertEqual("replaced", result["status"])
        self.assertEqual("__dry_run_created_imagesgallery__", result["created_block_locator"])
        self.assertEqual(
            ["reorder_children", "delete_block", "publish_vertical"],
            [step["step"] for step in result["steps"]],
        )

    def test_apply_in_place_publish_actions_dry_run_bootstraps_csrf_when_credentials_exist(self) -> None:
        with patch.object(module, "login_and_get_studio_session", return_value=(module.requests.Session(), "csrf-from-login")) as login_mock:
            result = module.apply_in_place_publish_actions(
                {
                    "studio_base": "https://studio.example.com",
                    "block_locator": "",
                    "steps": [],
                },
                adapter=self.adapter,
                replacement_plan={
                    "result_type": "replaced",
                    "course_kind": "imagesgallery_based",
                    "match_scope": "target_vertical",
                    "target_vertical_block_id": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                    "working_vertical_block_id": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                    "current_children": [
                        "block-v1:FIRAx+1040045+20260807+type@problem+block@intro",
                        "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
                    ],
                    "matched_source_block_locator": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@legacygallery",
                },
                studio_url="https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                execute=False,
                timeout=30,
                args=argparse.Namespace(
                    cookie="",
                    csrf_token="",
                    account="demo-account",
                    password="demo-password",
                    courses_base="https://courses.example.com",
                ),
            )

        login_mock.assert_called_once()
        self.assertEqual(
            ["<redacted>", "<redacted>", "<redacted>"],
            [step["request"]["headers"]["X-CSRFToken"] for step in result["steps"]],
        )

    def test_apply_in_place_publish_actions_uses_live_target_children_when_available(self) -> None:
        called_steps: list[dict[str, object]] = []

        def fake_call_template(_session, name, _tmpl, ctx, _execute, _timeout):
            called_steps.append(
                {
                    "step": name,
                    "children": list(ctx["vertical_children_obj"]),
                    "delete": ctx["delete_block_locator"],
                    "vertical": ctx["vertical_block_id"],
                }
            )
            return {"step": name, "status": "ok"}

        live_plan = {
            "result_type": "replaced",
            "course_kind": "imagesgallery_based",
            "match_scope": "target_vertical",
            "plan_source": "live_target",
            "source_vertical_block_id": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            "target_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
            "working_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
            "current_children": [
                "block-v1:FIRAx+211181+20251122+type@problem+block@intro",
                "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@livegallery",
            ],
            "matched_source_block_locator": "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@livegallery",
        }

        with patch.object(module, "maybe_fetch_live_replacement_plan", return_value=(live_plan, {"step": "load_live_target_structure", "status": "ok"})):
            with patch.object(module, "build_studio_session_from_args", return_value=(object(), "fake", True)):
                with patch.object(module, "call_template", side_effect=fake_call_template):
                    result = module.apply_in_place_publish_actions(
                        {
                            "studio_base": "https://studio.example.com",
                            "block_locator": "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@newgallery",
                            "steps": [],
                        },
                        adapter=self.adapter,
                        replacement_plan={
                            "result_type": "replaced",
                            "course_kind": "imagesgallery_based",
                            "match_scope": "target_vertical",
                            "plan_source": "course_structure_json",
                            "source_vertical_block_id": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                            "target_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                            "working_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                            "current_children": [
                                "block-v1:FIRAx+211181+20251122+type@problem+block@intro",
                                "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@stalegallery",
                            ],
                            "matched_source_block_locator": "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@stalegallery",
                        },
                        studio_url="https://studio.example.com/container/block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                        execute=False,
                        timeout=30,
                        args=argparse.Namespace(
                            execute=True,
                            cookie="csrftoken=fake",
                            csrf_token="",
                            account="",
                            password="",
                            courses_base="",
                        ),
                    )

        self.assertEqual("live_target", result["plan_source"])
        self.assertEqual(
            [
                "block-v1:FIRAx+211181+20251122+type@problem+block@intro",
                "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@newgallery",
                "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@livegallery",
            ],
            called_steps[0]["children"],
        )
        self.assertEqual(
            "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@livegallery",
            called_steps[1]["delete"],
        )

    def test_apply_in_place_publish_actions_excludes_created_gallery_from_live_replan(self) -> None:
        called_steps: list[dict[str, object]] = []
        created_block_locator = "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@newgallery"
        legacy_block_locator = "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@legacygallery"

        def fake_call_template(_session, name, _tmpl, ctx, _execute, _timeout):
            called_steps.append(
                {
                    "step": name,
                    "children": list(ctx["vertical_children_obj"]),
                    "delete": ctx["delete_block_locator"],
                }
            )
            return {"step": name, "status": "ok"}

        with patch.object(module, "build_studio_session_from_args", return_value=(object(), "fake", True)):
            with patch.object(
                module,
                "fetch_live_course_structure",
                return_value=module.normalize_course_structure_payload(
                    self._make_concise_course_structure_with_two_galleries(
                        legacy_gallery_locator=legacy_block_locator,
                        created_gallery_locator=created_block_locator,
                    )
                ),
            ):
                with patch.object(module, "call_template", side_effect=fake_call_template):
                    result = module.apply_in_place_publish_actions(
                        {
                            "studio_base": "https://studio.example.com",
                            "block_locator": created_block_locator,
                            "steps": [],
                        },
                        adapter=self.adapter,
                        replacement_plan={
                            "result_type": "replaced",
                            "course_kind": "imagesgallery_based",
                            "match_scope": "target_vertical",
                            "plan_source": "course_structure_json",
                            "source_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                            "target_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                            "working_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                            "current_children": [
                                "block-v1:FIRAx+211181+20251122+type@problem+block@intro",
                                legacy_block_locator,
                            ],
                            "matched_source_block_locator": legacy_block_locator,
                        },
                        studio_url="https://studio.example.com/container/block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                        execute=False,
                        timeout=30,
                        args=argparse.Namespace(
                            execute=True,
                            cookie="csrftoken=fake",
                            csrf_token="",
                            account="",
                            password="",
                            courses_base="",
                        ),
                    )

        self.assertEqual("live_target", result["plan_source"])
        self.assertEqual(legacy_block_locator, called_steps[1]["delete"])
        self.assertEqual(
            [
                "block-v1:FIRAx+211181+20251122+type@problem+block@intro",
                created_block_locator,
                legacy_block_locator,
            ],
            called_steps[0]["children"],
        )

    def test_apply_in_place_publish_actions_requires_live_target_validation_in_execute_mode(self) -> None:
        with patch.object(
            module,
            "maybe_fetch_live_replacement_plan",
            return_value=(None, {"step": "load_live_target_structure", "status": "warning", "note": "live fetch failed"}),
        ):
            with self.assertRaises(module.PublishFailure) as ctx:
                module.apply_in_place_publish_actions(
                    {
                        "studio_base": "https://studio.example.com",
                        "block_locator": "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@newgallery",
                        "steps": [],
                    },
                    adapter=self.adapter,
                    replacement_plan={
                        "result_type": "replaced",
                        "course_kind": "imagesgallery_based",
                        "match_scope": "target_vertical",
                        "plan_source": "course_structure_json",
                        "source_vertical_block_id": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                        "target_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                        "working_vertical_block_id": "block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                        "current_children": [
                            "block-v1:FIRAx+211181+20251122+type@problem+block@intro",
                            "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@legacygallery",
                        ],
                        "matched_source_block_locator": "block-v1:FIRAx+211181+20251122+type@imagesgallery+block@legacygallery",
                    },
                    studio_url="https://studio.example.com/container/block-v1:FIRAx+211181+20251122+type@vertical+block@targetvertical",
                    execute=True,
                    timeout=30,
                    args=argparse.Namespace(
                        execute=True,
                        cookie="csrftoken=fake",
                        csrf_token="",
                        account="",
                        password="",
                        courses_base="",
                    ),
                )

        self.assertEqual("load_live_target_structure", ctx.exception.step_name)
        self.assertIn("live fetch failed", str(ctx.exception))

    def test_process_publish_target_cleans_orphaned_created_block_before_fresh_rerun(self) -> None:
        bundle_dir = tempfile.TemporaryDirectory()
        self.addCleanup(bundle_dir.cleanup)
        manifest_path = self._write_manifest_bundle(Path(bundle_dir.name))
        self._mark_manifest_source(manifest_path, block_type="imagesgallery")
        course_structure = self._make_course_structure(
            include_gallery=True,
            include_target_html=False,
            include_section_html=False,
        )
        previous_result = {
            "status": "failed",
            "created_block_locator": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@orphan",
            "failed_step": "publish_vertical",
        }

        live_plan = module.mark_replacement_plan_source(
            module.plan_publish_replacement(
                course_structure,
                source_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                target_vertical_block_id="block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            ),
            "live_target",
        )

        with patch.object(module, "maybe_fetch_live_replacement_plan", return_value=(live_plan, {"step": "load_live_target_structure", "status": "ok"})):
            with patch.object(
                module,
                "cleanup_orphaned_created_block",
                return_value={"step": "cleanup_orphaned_created_block", "status": "deleted"},
            ) as cleanup_mock:
                with patch.object(
                    module,
                    "run_single_publish",
                    return_value={
                        "studio_base": "https://studio.example.com",
                        "block_locator": "block-v1:FIRAx+1040045+20260807+type@imagesgallery+block@fresh",
                        "steps": [],
                    },
                ):
                    with patch.object(
                        module,
                        "apply_in_place_publish_actions",
                        return_value={"status": "replaced", "steps": []},
                    ) as apply_mock:
                        result = module.process_publish_target(
                            {
                                "name": "1.1",
                                "manifest": str(manifest_path),
                                "source_vertical_block_location": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                                "target_vertical_block_location": "block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                                "studio_vertical_url": "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
                            },
                            course_structure,
                            argparse.Namespace(
                                execute=True,
                                timeout=30,
                                adapter=str(ADAPTER_PATH),
                                dry_run=False,
                                courses_base="",
                                account="",
                                password="",
                                cookie="csrftoken=fake",
                                csrf_token="",
                                skip_oss_multipart=False,
                                auth_retry_max_retries=4,
                                auth_retry_delay_seconds=0,
                            ),
                            self.adapter,
                            previous_result=previous_result,
                        )

        cleanup_mock.assert_called_once()
        apply_mock.assert_called_once()
        single_publish_result = apply_mock.call_args.args[0]
        self.assertEqual("cleanup_orphaned_created_block", single_publish_result["steps"][0]["step"])
        self.assertEqual("replaced", result["status"])

    def test_call_template_keeps_invalid_json_body_for_diagnostics(self) -> None:
        class FakeSession:
            def request(self, **_kwargs):
                class FakeResponse:
                    status_code = 200
                    headers = {"content-type": "application/json"}
                    text = "not-json"

                    def json(self):
                        raise json.JSONDecodeError("Expecting value", "not-json", 0)

                return FakeResponse()

        record = module.call_template(
            FakeSession(),
            "delete_block",
            {"method": "DELETE", "url": "https://studio.example.com/xblock/abc"},
            {},
            True,
            30,
        )

        self.assertEqual("Studio returned invalid JSON", record["response_parse_warning"])
        self.assertNotIn("response", record)

    def test_cli_reconciles_missing_upload_ids_by_unique_complete_filename_order_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_multi_page_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            fake = self._FakeStudioSession(missing_upload_id=True)

            exit_code, stdout, stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(0, exit_code, stdout + stderr)
            self.assertEqual("", stderr)
            self.assertEqual(1, sum(path.endswith("/handler/get_files") for _method, path in fake.calls), stdout)
            self.assertEqual(1, sum(path.endswith("/handler/change_files_order") for _method, path in fake.calls))
            self.assertEqual(1, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            gallery = state["galleries"][0]
            self.assertEqual("preserve this field", gallery["operator_note"])
            self.assertEqual("block-v1:ORG+COURSE+RUN+type@imagesgallery+block@gallery123", gallery["gallery_locator"])
            self.assertEqual(["asset-001", "asset-002"], gallery["publish"]["asset_ids"])
            self.assertEqual("completed", gallery["publish"]["status"])
            output = stdout + operation_path.read_text(encoding="utf-8")
            for secret in ("sts-secret-access-key", "sts-secret-token", "cookie-secret", "authorization-secret", "do-not-persist"):
                self.assertNotIn(secret, output)
            self.assertNotIn('"response"', output)

    def test_cli_stops_before_ordering_when_missing_id_reconciliation_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_multi_page_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            fake = self._FakeStudioSession(missing_upload_id=True, incomplete_files=True)

            exit_code, stdout, stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(1, exit_code)
            self.assertEqual("", stderr)
            self.assertEqual(1, sum(path.endswith("/handler/get_files") for _method, path in fake.calls), stdout)
            self.assertEqual(0, sum(path.endswith("/handler/change_files_order") for _method, path in fake.calls))
            self.assertEqual(1, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))
            result = json.loads(stdout)
            self.assertEqual("failed", result["status"])
            self.assertEqual("upload_image[1]", result["failed_step"])
            self.assertNotIn("Credentials", stdout)
            self.assertNotIn("message", stdout)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            publish = state["galleries"][0]["publish"]
            self.assertEqual("block_created", publish["completed_stage"])
            self.assertEqual("failed", publish["status"])
            self.assertEqual([], publish["asset_ids"])
            self.assertNotIn("response", json.dumps(state))

            retry_exit, retry_stdout, retry_stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(1, retry_exit)
            self.assertEqual("", retry_stderr)
            self.assertIn("already attempted", retry_stdout)
            self.assertEqual(1, sum(path.endswith("/handler/get_files") for _method, path in fake.calls))
            self.assertEqual(1, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))

    def test_cli_resumes_from_audio_stage_without_reuploading_confirmed_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            fake = self._FakeStudioSession(fail_audio_prepare_once=True)

            first_exit, first_stdout, first_stderr = self._run_publisher_cli(manifest_path, operation_path, fake)
            first_state = json.loads(operation_path.read_text(encoding="utf-8"))
            first_publish = first_state["galleries"][0]["publish"]
            self.assertEqual(1, first_exit, first_stdout + first_stderr)
            self.assertEqual("", first_stderr)
            self.assertEqual("images_ordered", first_publish["completed_stage"])
            self.assertEqual("upload_audio_prepare", first_publish["failed_step"])
            self.assertNotIn("sts-secret-access-key", first_stdout + json.dumps(first_state))

            second_exit, second_stdout, second_stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(0, second_exit, second_stderr)
            self.assertEqual("", second_stderr)
            self.assertEqual(1, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))
            self.assertEqual(1, sum(path.endswith("/handler/change_files_order") for _method, path in fake.calls))
            self.assertEqual(2, sum(path.endswith("/handler/get_upload_info") for _method, path in fake.calls))
            final_state = json.loads(operation_path.read_text(encoding="utf-8"))
            publish = final_state["galleries"][0]["publish"]
            self.assertEqual("completed", publish["status"])
            self.assertEqual("saved", publish["completed_stage"])
            self.assertEqual(["asset-001"], publish["asset_ids"])
            self.assertNotIn("response", json.dumps(final_state))
            self.assertNotIn("sts-secret-access-key", second_stdout + json.dumps(final_state))

    def test_cli_reconciles_interrupted_file_order_before_replaying_order_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_multi_page_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            self._set_interrupted_order_checkpoint(operation_path)
            fake = self._FakeStudioSession()

            exit_code, stdout, stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(0, exit_code, stdout + stderr)
            self.assertEqual("", stderr)
            self.assertEqual(1, sum(path.endswith("/handler/get_files") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/change_files_order") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))
            publish = json.loads(operation_path.read_text(encoding="utf-8"))["galleries"][0]["publish"]
            self.assertEqual("saved", publish["completed_stage"])
            self.assertIn("change_files_order_reconciliation", [step["step"] for step in publish["steps"]])

    def test_cli_retries_interrupted_file_order_when_same_assets_are_still_out_of_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_multi_page_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            self._set_interrupted_order_checkpoint(operation_path, failed=True)
            fake = self._FakeStudioSession(file_asset_ids=["asset-002", "asset-001"])

            exit_code, stdout, stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(0, exit_code, stdout + stderr)
            self.assertEqual("", stderr)
            self.assertEqual(1, sum(path.endswith("/handler/get_files") for _method, path in fake.calls))
            self.assertEqual(1, sum(path.endswith("/handler/change_files_order") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))
            publish = json.loads(operation_path.read_text(encoding="utf-8"))["galleries"][0]["publish"]
            self.assertEqual("saved", publish["completed_stage"])
            self.assertEqual("completed", publish["status"])

    def test_cli_keeps_interrupted_order_pending_when_get_files_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_multi_page_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            self._set_interrupted_order_checkpoint(operation_path, failed=True)
            fake = self._FakeStudioSession(fail_get_files=True)

            first_exit, first_stdout, first_stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(1, first_exit)
            self.assertEqual("", first_stderr)
            self.assertEqual(1, sum(path.endswith("/handler/get_files") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/change_files_order") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))
            publish = json.loads(operation_path.read_text(encoding="utf-8"))["galleries"][0]["publish"]
            self.assertEqual("failed", publish["status"])
            self.assertEqual("images_uploaded", publish["completed_stage"])
            self.assertEqual("change_files_order", publish["failed_step"])
            self.assertNotIn("unavailable", first_stdout)

            second_exit, second_stdout, second_stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(1, second_exit)
            self.assertEqual("", second_stderr)
            self.assertEqual(2, sum(path.endswith("/handler/get_files") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/change_files_order") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))

    def test_cli_does_not_retry_interrupted_order_when_remote_asset_ids_differ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_multi_page_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            self._set_interrupted_order_checkpoint(operation_path)
            fake = self._FakeStudioSession(file_asset_ids=["asset-001", "foreign-asset"])

            exit_code, stdout, stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(1, exit_code)
            self.assertEqual("", stderr)
            self.assertEqual(1, sum(path.endswith("/handler/get_files") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/change_files_order") for _method, path in fake.calls))
            self.assertEqual(0, sum(path.endswith("/handler/file_upload") for _method, path in fake.calls))
            publish = json.loads(operation_path.read_text(encoding="utf-8"))["galleries"][0]["publish"]
            self.assertEqual("failed", publish["status"])
            self.assertEqual("images_uploaded", publish["completed_stage"])
            self.assertEqual("change_files_order", publish["failed_step"])
            self.assertNotIn("foreign-asset", stdout)

    def test_operation_file_requires_one_matching_gallery_key_before_studio_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            fake = self._FakeStudioSession()

            args = [
                "publish_imagegallery_block.py",
                "--studio-url",
                "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@vertical123",
                "--manifest",
                str(manifest_path),
                "--execute",
                "--operation-file",
                str(operation_path),
            ]
            stdout = StringIO()
            stderr = StringIO()
            with patch("sys.argv", args), patch.object(module.requests, "Session", return_value=fake):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as exit_context:
                        module.main()

            self.assertEqual(2, exit_context.exception.code)
            self.assertEqual([], fake.calls)
            self.assertIn("--operation-file and --gallery-key must be supplied together", stderr.getvalue())

    def test_operation_file_rejects_duplicate_gallery_keys_before_studio_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manifest_path = self._write_manifest_bundle(root)
            operation_path = root / "batch-operation.json"
            self._write_operation_file(operation_path, manifest_path)
            state = json.loads(operation_path.read_text(encoding="utf-8"))
            state["galleries"].append(dict(state["galleries"][0]))
            operation_path.write_text(json.dumps(state), encoding="utf-8")
            fake = self._FakeStudioSession()

            exit_code, stdout, stderr = self._run_publisher_cli(manifest_path, operation_path, fake)

            self.assertEqual(1, exit_code)
            self.assertEqual("", stderr)
            self.assertIn("exactly one matching gallery_key", stdout)
            self.assertEqual([], fake.calls)

    def test_auth_related_failure_detection(self) -> None:
        self.assertTrue(module.is_auth_related_failure(401, {"detail": "Not Login yet"}))
        self.assertTrue(module.is_auth_related_failure(403, {"message": "csrf token invalid"}))
        self.assertFalse(module.is_auth_related_failure(500, {"message": "internal error"}))

    def test_call_with_auth_retry_retries_auth_failures(self) -> None:
        attempts = {"count": 0}
        refreshes = {"count": 0}

        def flaky_call():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise module.TemplateCallError("upload_audio_register", 401, {"detail": "Not Login yet", "token": "retry-secret"})
            return {"ok": True}

        def refresh_auth():
            refreshes["count"] += 1

        result, retry_events = module.call_with_auth_retry(
            flaky_call,
            refresh_auth,
            max_retries=4,
            delay_seconds=0,
        )

        self.assertEqual({"ok": True}, result)
        self.assertEqual(3, attempts["count"])
        self.assertEqual(2, refreshes["count"])
        self.assertEqual(2, len(retry_events))
        self.assertEqual("upload_audio_register", retry_events[0]["step"])
        self.assertNotIn("retry-secret", json.dumps(retry_events))

    def test_call_with_auth_retry_does_not_retry_non_auth_failures(self) -> None:
        with self.assertRaises(module.TemplateCallError):
            module.call_with_auth_retry(
                lambda: (_ for _ in ()).throw(module.TemplateCallError("save_block", 500, {"detail": "boom"})),
                lambda: None,
                max_retries=4,
                delay_seconds=0,
            )

    def test_normalize_auth_retry_max_retries_clamps_to_skill_limit(self) -> None:
        self.assertEqual(4, module.normalize_auth_retry_max_retries(10))
        self.assertEqual(4, module.normalize_auth_retry_max_retries("99"))
        self.assertEqual(0, module.normalize_auth_retry_max_retries(-5))

    def test_run_returns_target_vertical_url_for_operator_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = self._write_manifest_bundle(tmp_path)
            adapter_path = tmp_path / "adapter.json"

            adapter_path.write_text(
                json.dumps(
                    {
                        "create_block": {"method": "POST", "url": "https://studio.example.com/create"},
                        "upload_image": {"method": "POST", "url": "https://studio.example.com/upload"},
                        "save_block": {"method": "POST", "url": "https://studio.example.com/save"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                studio_url="https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@vertical123",
                manifest=str(manifest_path),
                adapter=str(adapter_path),
                execute=False,
                dry_run=True,
                timeout=30,
                vertical_block_id="",
                block_url="",
                courses_base="",
                account="",
                password="",
                cookie="csrftoken=fake",
                csrf_token="",
                skip_oss_multipart=False,
                auth_retry_max_retries=4,
                auth_retry_delay_seconds=0,
            )

            with patch.dict(
                "os.environ",
                {
                    "FIRA_SAAS_OP_ACCOUNT": "",
                    "FIRA_SAAS_OP_PASSWORD": "",
                },
                clear=False,
            ):
                result = module.run(args)

        self.assertEqual(
            "https://studio.example.com/container/block-v1:ORG+COURSE+RUN+type@vertical+block@vertical123",
            result["target_vertical_url"],
        )
        self.assertEqual("", result["created_imagegallery_block_url"])
        self.assertEqual("", result["created_block_url"])

    def test_skill_doc_mentions_batch_publish_guardrails(self) -> None:
        skill_doc = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("必须 **顺序执行**", skill_doc)
        self.assertIn("最多重试 **4** 次", skill_doc)
        self.assertIn("目标 vertical URL", skill_doc)
        self.assertIn("新建的 `imagesgallery` block URL", skill_doc)
        self.assertIn("cleanup-only", skill_doc)
        self.assertIn("返回 `unsupported`", skill_doc)
        self.assertIn("--section-list-json", skill_doc)
        self.assertIn("`本节要点组件` 会保留", skill_doc)
        self.assertIn("保留独立的 `本节要点组件`", skill_doc)
        self.assertIn("失败步骤", skill_doc)


if __name__ == "__main__":
    unittest.main()
