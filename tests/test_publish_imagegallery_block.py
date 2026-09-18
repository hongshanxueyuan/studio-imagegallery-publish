import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
            (tmp_path / "course.json").write_text(json.dumps({"chapters": []}, ensure_ascii=False), encoding="utf-8")
            targets_file.write_text(
                json.dumps(
                    {
                        "course_structure_json": str(tmp_path / "course.json"),
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
            )

            with patch.dict("os.environ", {"FIRA_SAAS_OP_ACCOUNT": "", "FIRA_SAAS_OP_PASSWORD": ""}, clear=False):
                result = module.run_in_place_publish(args)

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
            course_path = tmp_path / "course.json"
            course_path.write_text(
                json.dumps(
                    self._make_course_structure(
                        include_gallery=False,
                        include_target_html=False,
                        include_section_html=True,
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
            "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@sectionhtmlvertical",
            result["target_vertical_url"],
        )
        self.assertEqual(
            "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@vertical+block@targetvertical",
            result["requested_target_vertical_url"],
        )
        self.assertEqual(
            "https://studio.example.com/container/block-v1:FIRAx+1040045+20260807+type@html+block@wordiesthtml",
            result["matched_source_block_url"],
        )

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
            ["csrf-from-login", "csrf-from-login", "csrf-from-login"],
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
                                "manifest": "a.json",
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

        self.assertEqual("Expecting value", record["response_parse_warning"][:15])
        self.assertEqual("not-json", record["response"]["text"])

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
                raise module.TemplateCallError("upload_audio_register", 401, {"detail": "Not Login yet"})
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


if __name__ == "__main__":
    unittest.main()
