from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from scripts.novelpia_content import EpisodeContent
from scripts.refresh_novelpia_secret import (
    MAX_SECRET_BYTES,
    load_and_validate_storage_state,
    storage_state_base64,
    update_repository_secret,
)
from scripts.upload_novelpia import (
    NovelpiaError,
    classify_unavailable_page,
    ensure_not_duplicate,
    populate_and_verify_editor,
    should_publish,
    submit_once_and_determine,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "novelpia_editor.html"


def content() -> EpisodeContent:
    plain = (
        "첫 문장은 편집기 검증을 위한 충분한 길이의 핵심 문자열입니다. "
        "중간에는 여러 문단과 내용이 자연스럽게 이어집니다. "
        "마지막 문장은 입력 결과의 뒷부분을 검증하기 위한 핵심 문자열입니다."
    )
    return EpisodeContent(
        episode=12,
        title="회색 빛의 간격",
        date="2026-07-25",
        markdown_body=plain,
        html_body=f"<p>{plain}</p>",
        plain_text=plain,
        path=ROOT / "docs" / "episodes" / "012.md",
        repository_path="docs/episodes/012.md",
    )


class PlaywrightFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.page = self.browser.new_page()
        self.page.set_content(FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.page.close()

    def test_normal_summernote_editor_is_detected_and_api_used(self) -> None:
        verification = populate_and_verify_editor(self.page, content())
        self.assertTrue(verification.used_summernote)
        self.assertEqual(
            self.page.locator("#content_subject").input_value(), content().title
        )
        self.assertIn("마지막 문장", verification.actual_text)

    def test_dom_fallback_sets_visible_editor_and_events(self) -> None:
        self.page.evaluate("delete window.jQuery; delete window.$")
        verification = populate_and_verify_editor(self.page, content())
        self.assertFalse(verification.used_summernote)
        self.assertIn("첫 문장", verification.actual_text)

    def test_selector_change_is_detected(self) -> None:
        self.page.locator(".note-editable").evaluate(
            "(element) => element.className = 'changed-editor'"
        )
        with self.assertRaisesRegex(NovelpiaError, "본문 editor"):
            populate_and_verify_editor(self.page, content())

    def test_login_expired_and_captcha_pages_are_distinguished(self) -> None:
        self.page.set_content("<body>로그인이 필요합니다. Google로 계속</body>")
        error = classify_unavailable_page(self.page, 200, "https://novelpia.com/mynovel/all/write/442975")
        self.assertEqual(error.code, "NOVELPIA_AUTH_EXPIRED")
        self.page.set_content("<body>CAPTCHA 로봇이 아닙니다</body>")
        error = classify_unavailable_page(self.page, 200, "https://novelpia.com/mynovel/all/write/442975")
        self.assertEqual(error.code, "NOVELPIA_AUTH_EXPIRED")

    def test_preview_and_disabled_publish_never_submit(self) -> None:
        populate_and_verify_editor(self.page, content())
        self.assertFalse(should_publish(True, True))
        self.assertFalse(should_publish(False, False))
        self.assertEqual(self.page.evaluate("window.submitCount"), 0)

    def test_title_validation_failure_never_submits(self) -> None:
        self.page.locator("#content_subject").evaluate(
            """element => element.addEventListener("input", () => {
              element.value = element.value.slice(0, 3);
            })"""
        )
        with self.assertRaises(NovelpiaError) as raised:
            populate_and_verify_editor(self.page, content())
        self.assertEqual(raised.exception.code, "NOVELPIA_VALIDATION_FAILED")
        self.assertEqual(self.page.evaluate("window.submitCount"), 0)

    def test_body_validation_failure_never_submits(self) -> None:
        self.page.evaluate(
            """window.jQuery = window.$ = element => ({
              summernote(command, html) { element.innerHTML = "<p>손실</p>"; }
            });
            window.jQuery.fn = { summernote() {} };"""
        )
        with self.assertRaises(NovelpiaError) as raised:
            populate_and_verify_editor(self.page, content())
        self.assertEqual(raised.exception.code, "NOVELPIA_VALIDATION_FAILED")
        self.assertEqual(self.page.evaluate("window.submitCount"), 0)

    def test_unknown_result_does_not_click_submit_twice(self) -> None:
        verification = populate_and_verify_editor(self.page, content())
        with self.assertRaises(NovelpiaError) as raised:
            submit_once_and_determine(
                self.page,
                verification,
                "about:blank",
                timeout_seconds=0.2,
            )
        self.assertEqual(
            raised.exception.code, "NOVELPIA_SUBMIT_RESULT_UNKNOWN"
        )
        self.assertEqual(self.page.evaluate("window.submitCount"), 1)


class StateTests(unittest.TestCase):
    def test_duplicate_and_unknown_are_blocked_but_force_is_allowed(self) -> None:
        published = {
            "last_success_episode": 13,
            "episode_path": "docs/episodes/013.md",
            "publish_status": "published",
            "published_episodes": [12, 13],
            "unknown_result_episodes": [],
        }
        with self.assertRaises(NovelpiaError):
            ensure_not_duplicate(content(), published, False)
        ensure_not_duplicate(content(), published, True)

        unknown = {
            "last_success_episode": None,
            "episode_path": "",
            "publish_status": "unknown",
            "published_episodes": [],
            "unknown_result_episodes": [12],
        }
        with self.assertRaises(NovelpiaError):
            ensure_not_duplicate(content(), unknown, False)
        ensure_not_duplicate(content(), unknown, True)


class StorageStateTests(unittest.TestCase):
    def write_state(self, value: object) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "auth.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return temporary, path

    def test_valid_json_requires_novelpia_cookie_or_origin(self) -> None:
        temporary, path = self.write_state(
            {"cookies": [{"domain": ".novelpia.com", "name": "session", "value": "x"}], "origins": []}
        )
        with temporary:
            self.assertIn("cookies", load_and_validate_storage_state(path))
        temporary, path = self.write_state({"cookies": [], "origins": []})
        with temporary, self.assertRaises(ValueError):
            load_and_validate_storage_state(path)

    def test_invalid_json_and_secret_size_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_and_validate_storage_state(path)
            large = {
                "cookies": [
                    {
                        "domain": ".novelpia.com",
                        "name": "session",
                        "value": "x" * MAX_SECRET_BYTES,
                    }
                ],
                "origins": [],
            }
            path.write_text(json.dumps(large), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "크기"):
                storage_state_base64(path)

    def test_secret_value_is_stdin_not_command_argument(self) -> None:
        calls: list[tuple[list[str], str]] = []

        def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append((command, str(kwargs["input"])))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.dict(os.environ, {"GH_TOKEN": "token"}, clear=False):
            update_repository_secret(
                "sensitive-base64", "owner/repo", runner=runner
            )
        command, stdin = calls[0]
        self.assertNotIn("sensitive-base64", command)
        self.assertEqual(stdin, "sensitive-base64")


class RepositorySafetyTests(unittest.TestCase):
    def test_auth_patterns_are_ignored_and_artifacts_are_narrow(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            "secrets/",
            "playwright/.auth/",
            "*.auth.json",
            "novelpia-auth*.json",
            "playwright-report/",
            "test-results/",
        ):
            self.assertIn(pattern, ignore)
        workflow = (
            ROOT / ".github" / "workflows" / "publish-novelpia.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("path: preview/novelpia-editor.png", workflow)
        self.assertNotIn("path: secrets/", workflow)
        tracked = subprocess.run(
            ["git", "ls-files", "secrets", "*.auth.json", "novelpia-auth*.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(tracked, "")


if __name__ == "__main__":
    unittest.main()
