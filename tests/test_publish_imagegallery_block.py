import importlib.util
import json
import unittest
from pathlib import Path


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

    def test_mutating_templates_use_canonical_block_url(self) -> None:
        for step_name in (
            "change_files_order",
            "upload_audio_prepare",
            "upload_audio_register",
            "save_audio_binding",
            "submit_studio_edits",
            "save_block",
        ):
            with self.subTest(step=step_name):
                headers = self.adapter[step_name]["headers"]
                self.assertEqual(headers.get("Referer"), "{block_url}")
                self.assertEqual(headers.get("Origin"), "{studio_base}")

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

    def test_skill_doc_mentions_batch_publish_guardrails(self) -> None:
        skill_doc = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("must run **sequentially**", skill_doc)
        self.assertIn("retry at most **4** times", skill_doc)
        self.assertIn("final clickable Studio block URL", skill_doc)
        self.assertIn("list every created Studio block URL separately", skill_doc)


if __name__ == "__main__":
    unittest.main()
