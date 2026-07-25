from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

try:
    from scripts.novelpia_content import (
        EpisodeContent,
        blank_line_separator_count,
        has_editor_indentation,
        normalize_text,
        parse_episode_markdown,
    )
    from scripts.novelpia_login import save_storage_state
    from scripts.refresh_novelpia_secret import load_and_validate_storage_state
except ModuleNotFoundError:
    from novelpia_content import (  # type: ignore
        EpisodeContent,
        blank_line_separator_count,
        has_editor_indentation,
        normalize_text,
        parse_episode_markdown,
    )
    from novelpia_login import save_storage_state  # type: ignore
    from refresh_novelpia_secret import (  # type: ignore
        load_and_validate_storage_state,
    )

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EDITOR_URL = "https://novelpia.com/mynovel/all/write/442975"
DEFAULT_STATE_PATH = ROOT / "state" / "novelpia_publish_state.json"
PLACEHOLDER_MARKERS = (
    "내용을 입력해",
    "본문을 입력해",
    "작품 내용을 입력",
    "회차 내용을 입력",
)
APPROVAL_TEXT = re.compile(r"^(확인|등록|작성\s*완료)$")
SUCCESS_TEXT = re.compile(
    r"(성공적으로|정상적으로).{0,20}(등록|작성|게시)"
    r"|(등록|작성|게시).{0,20}(완료되었습니다|성공했습니다|처리되었습니다)"
)
ERROR_TEXT = re.compile(r"(등록|작성|게시).{0,10}(실패|오류)|오류가 발생")


class NovelpiaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class EditorVerification:
    subject: Locator
    editor: Locator
    submit: Locator
    actual_text: str
    actual_html: str
    used_summernote: bool


@dataclass
class PublishResult:
    status: str
    published_url: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def bool_value(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def should_publish(publish_enabled: bool, preview_only: bool) -> bool:
    return publish_enabled and not preview_only


def default_state() -> dict[str, Any]:
    return {
        "last_success_episode": None,
        "last_success_at": None,
        "episode_path": "",
        "title": "",
        "published_url": "",
        "publish_status": "never",
        "last_error_code": "",
        "session_refresh_status": "never",
        "published_episodes": [],
        "unknown_result_episodes": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    state = default_state()
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("게시 상태 파일은 JSON 객체여야 합니다.")
        state.update(loaded)
    if not isinstance(state.get("unknown_result_episodes"), list):
        state["unknown_result_episodes"] = []
    if not isinstance(state.get("published_episodes"), list):
        state["published_episodes"] = []
    last_success = state.get("last_success_episode")
    if isinstance(last_success, int) and last_success not in state["published_episodes"]:
        state["published_episodes"].append(last_success)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def ensure_not_duplicate(
    content: EpisodeContent, state: dict[str, Any], force_republish: bool
) -> None:
    successful = (
        content.episode in state.get("published_episodes", [])
        or state.get("last_success_episode") == content.episode
        or state.get("episode_path") == content.repository_path
        and state.get("publish_status") == "published"
    )
    unknown = content.episode in state.get("unknown_result_episodes", [])
    if (successful or unknown) and not force_republish:
        raise NovelpiaError(
            "NOVELPIA_ALREADY_PUBLISHED",
            "성공 또는 결과 불명 상태인 회차의 자동 재게시는 차단됩니다.",
        )


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def validate_editor_url(url: str, expected_url: str) -> None:
    actual = urlparse(url)
    expected = urlparse(expected_url)
    if actual.hostname != "novelpia.com":
        raise NovelpiaError(
            "NOVELPIA_EDITOR_UNAVAILABLE",
            "현재 hostname이 novelpia.com이 아닙니다.",
        )
    if (
        not actual.path.startswith("/mynovel/all/write/")
        or actual.path != expected.path
    ):
        raise NovelpiaError(
            "NOVELPIA_EDITOR_UNAVAILABLE",
            "현재 URL이 허용된 작성 페이지가 아닙니다.",
        )


def _page_text(page: Page) -> str:
    try:
        return normalize_text(page.locator("body").inner_text(timeout=2_000))
    except Exception:
        return ""


def classify_unavailable_page(
    page: Page, response_status: int | None, expected_url: str
) -> NovelpiaError:
    parsed = urlparse(page.url)
    text = _page_text(page).lower()
    if parsed.hostname in {"accounts.google.com", "accounts.youtube.com"}:
        return NovelpiaError(
            "NOVELPIA_AUTH_EXPIRED", "Google 로그인 페이지로 이동했습니다."
        )
    if any(marker in text for marker in ("captcha", "로봇이 아닙니다", "보안문자")):
        return NovelpiaError(
            "NOVELPIA_AUTH_EXPIRED", "CAPTCHA 또는 추가 인증이 필요합니다."
        )
    if any(marker in text for marker in ("로그인", "google로 계속", "google 로그인")):
        return NovelpiaError(
            "NOVELPIA_AUTH_EXPIRED", "노벨피아 로그인 세션이 만료되었습니다."
        )
    if response_status == 401:
        return NovelpiaError(
            "NOVELPIA_AUTH_EXPIRED", "노벨피아 세션이 만료되었습니다."
        )
    if response_status == 403 or any(
        marker in text for marker in ("접근 권한", "권한이 없습니다", "접근이 거부")
    ):
        return NovelpiaError(
            "NOVELPIA_EDITOR_UNAVAILABLE", "작성 페이지 접근 권한이 없습니다."
        )
    if response_status is not None and response_status >= 500:
        return NovelpiaError(
            "NOVELPIA_EDITOR_UNAVAILABLE", "노벨피아 서버 오류가 발생했습니다."
        )
    if parsed.hostname == "novelpia.com" and parsed.path == urlparse(expected_url).path:
        return NovelpiaError(
            "NOVELPIA_SELECTOR_CHANGED", "확인된 편집기 selector를 찾지 못했습니다."
        )
    return NovelpiaError(
        "NOVELPIA_EDITOR_UNAVAILABLE", "노벨피아 작성 페이지를 사용할 수 없습니다."
    )


def wait_for_editor(page: Page, expected_url: str, response_status: int | None) -> None:
    try:
        page.locator("#content_subject").wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError as exc:
        raise classify_unavailable_page(page, response_status, expected_url) from exc
    validate_editor_url(page.url, expected_url)


def _visible_one(page: Page, selector: str, label: str) -> Locator:
    candidates = page.locator(selector)
    visible = [
        candidates.nth(index)
        for index in range(candidates.count())
        if candidates.nth(index).is_visible()
    ]
    if len(visible) != 1:
        raise NovelpiaError(
            "NOVELPIA_SELECTOR_CHANGED",
            f"보이는 {label} 요소가 정확히 하나가 아닙니다.",
        )
    return visible[0]


def _core_samples(text: str) -> tuple[str, str]:
    normalized = normalize_text(text)
    size = min(48, max(12, len(normalized) // 8))
    return normalized[:size], normalized[-size:]


def populate_and_verify_editor(
    page: Page, content: EpisodeContent
) -> EditorVerification:
    subject = _visible_one(page, "#content_subject", "제목 input")
    editor = _visible_one(
        page, '.note-editable[contenteditable="true"]', "본문 editor"
    )
    submit = _visible_one(page, "#submit_btn", "작성완료 버튼")

    subject.fill(content.title)
    subject.dispatch_event("input")
    subject.dispatch_event("change")
    subject.dispatch_event("blur")
    actual_title = subject.input_value()
    if not content.title or actual_title != content.title:
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "제목 입력값이 기대값과 다릅니다."
        )

    used_summernote = bool(
        page.evaluate(
            """([element, html]) => {
              const jq = window.jQuery || window.$;
              const noteFrame = element.closest(".note-editor");
              const source = noteFrame?.previousElementSibling;
              if (jq && jq.fn && typeof jq.fn.summernote === "function") {
                const initializedSource =
                  source && typeof jq(source).data === "function"
                    && jq(source).data("summernote")
                    ? source
                    : element;
                try {
                  jq(initializedSource).summernote("code", html);
                  if (element.innerHTML.trim()) {
                    for (const name of ["input", "change", "blur"]) {
                      element.dispatchEvent(new Event(name, { bubbles: true }));
                    }
                    return true;
                  }
                } catch (error) {
                  // Continue to the visible-editor fallback below.
                }
              }
              element.innerHTML = html;
              for (const name of ["input", "change", "blur"]) {
                element.dispatchEvent(new Event(name, { bubbles: true }));
              }
              return false;
            }""",
            [editor.element_handle(), content.html_body],
        )
    )
    actual_text = normalize_text(editor.inner_text())
    actual_html = editor.inner_html()
    expected_text = normalize_text(content.plain_text)
    placeholder = normalize_text(
        " ".join(
            filter(
                None,
                [
                    editor.get_attribute("data-placeholder"),
                    editor.get_attribute("aria-placeholder"),
                ],
            )
        )
    )
    if not actual_text or len(actual_text) < max(20, int(len(expected_text) * 0.75)):
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "본문이 비었거나 심각하게 손실됐습니다."
        )
    first, last = _core_samples(expected_text)
    if first not in actual_text or last not in actual_text:
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "본문 앞부분 또는 뒷부분 검증에 실패했습니다."
        )
    lowered = actual_text.lower()
    if (placeholder and placeholder in actual_text) or any(
        marker in lowered for marker in PLACEHOLDER_MARKERS
    ):
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "안내 placeholder가 본문에 포함됐습니다."
        )
    if not actual_html.strip():
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "본문 HTML이 비어 있습니다."
        )
    expected_blank_lines = blank_line_separator_count(content.html_body)
    actual_blank_lines = blank_line_separator_count(actual_html)
    if actual_blank_lines < expected_blank_lines:
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED",
            "편집기 입력 과정에서 문단 사이 공백이 손실됐습니다.",
        )
    if has_editor_indentation(actual_html):
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED",
            "편집기 본문에 의도하지 않은 공백 들여쓰기가 생겼습니다.",
        )
    return EditorVerification(
        subject=subject,
        editor=editor,
        submit=submit,
        actual_text=actual_text,
        actual_html=actual_html,
        used_summernote=used_summernote,
    )


def capture_safe_preview(page: Page, verification: EditorVerification, path: Path) -> None:
    subject_box = verification.subject.bounding_box()
    editor_box = verification.editor.bounding_box()
    if not subject_box or not editor_box:
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "preview 캡처 영역을 계산할 수 없습니다."
        )
    left = min(subject_box["x"], editor_box["x"])
    top = min(subject_box["y"], editor_box["y"])
    right = max(
        subject_box["x"] + subject_box["width"],
        editor_box["x"] + editor_box["width"],
    )
    bottom = max(
        subject_box["y"] + subject_box["height"],
        editor_box["y"] + editor_box["height"],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(path),
        clip={
            "x": max(0, left),
            "y": max(0, top),
            "width": right - left,
            "height": bottom - top,
        },
    )


def _handle_visible_modal(page: Page) -> None:
    selectors = (
        '.modal:visible',
        '.swal2-container:visible',
        '.sweet-alert:visible',
    )
    for selector in selectors:
        modal = page.locator(selector).last
        if not modal.is_visible():
            continue
        public_option = modal.get_by_text(re.compile(r"^공개$"), exact=True)
        if public_option.count() and public_option.first.is_visible():
            try:
                public_option.first.click()
            except Exception:
                pass
        buttons = modal.locator("button:visible, input[type=button]:visible")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            label = normalize_text(
                button.inner_text() or button.get_attribute("value") or ""
            )
            if APPROVAL_TEXT.fullmatch(label):
                button.click()
                return


def submit_once_and_determine(
    page: Page,
    verification: EditorVerification,
    editor_url: str,
    *,
    timeout_seconds: float = 12,
) -> PublishResult:
    if not verification.submit.is_enabled():
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "작성완료 버튼이 비활성화되어 있습니다."
        )
    start_url = page.url
    responses: list[tuple[int, str]] = []
    dialogs: list[str] = []

    def on_response(response: Any) -> None:
        parsed = urlparse(response.url)
        if parsed.hostname == "novelpia.com":
            responses.append((response.status, _safe_url(response.url)))

    def on_dialog(dialog: Any) -> None:
        message = normalize_text(dialog.message)[:200]
        dialogs.append(message)
        if APPROVAL_TEXT.search(message) or re.search(
            r"(등록|작성\s*완료).{0,12}(하시겠|진행)", message
        ):
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("response", on_response)
    page.on("dialog", on_dialog)
    try:
        verification.submit.click()
    except Exception as exc:
        raise NovelpiaError(
            "NOVELPIA_SUBMIT_FAILED", "작성완료 버튼 클릭에 실패했습니다."
        ) from exc
    # Never click #submit_btn again, even when the result remains unclear.
    try:
        page.wait_for_timeout(700)
        _handle_visible_modal(page)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            text = _page_text(page)
            if ERROR_TEXT.search(text):
                raise NovelpiaError(
                    "NOVELPIA_SUBMIT_FAILED", "노벨피아가 게시 오류를 표시했습니다."
                )
            if SUCCESS_TEXT.search(text):
                published_url = (
                    ""
                    if urlparse(page.url).path.startswith("/mynovel/all/write/")
                    else _safe_url(page.url)
                )
                return PublishResult("published", published_url)
            if page.url != start_url:
                parsed = urlparse(page.url)
                if parsed.hostname == "novelpia.com" and not parsed.path.startswith(
                    "/mynovel/all/write/"
                ):
                    return PublishResult("published", _safe_url(page.url))
            if any(
                status in range(200, 300)
                and re.search(r"(write|episode|novel|publish|upload)", url, re.I)
                for status, url in responses
            ) and page.url != editor_url:
                return PublishResult("published", _safe_url(page.url))
            page.wait_for_timeout(250)
    finally:
        page.remove_listener("response", on_response)
        page.remove_listener("dialog", on_dialog)
    raise NovelpiaError(
        "NOVELPIA_SUBMIT_RESULT_UNKNOWN",
        "작성완료 클릭 후 게시 성공을 확정할 수 없습니다.",
    )


def run_browser_operation(
    *,
    context: BrowserContext,
    page: Page,
    editor_url: str,
    content: EpisodeContent | None,
    publish_enabled: bool,
    preview_only: bool,
    screenshot_path: Path,
    refreshed_auth_path: Path,
    state: dict[str, Any],
    force_republish: bool,
    refresh_session_only: bool,
) -> PublishResult:
    response = page.goto(editor_url, wait_until="domcontentloaded", timeout=45_000)
    status = response.status if response is not None else None
    wait_for_editor(page, editor_url, status)

    if refresh_session_only:
        save_storage_state(context, refreshed_auth_path)
        state["session_refresh_status"] = "pending"
        return PublishResult("session_refreshed")
    if content is None:
        raise ValueError("refresh_session_only가 아니면 episode_path가 필요합니다.")
    ensure_not_duplicate(content, state, force_republish)
    verification = populate_and_verify_editor(page, content)
    publish_now = should_publish(publish_enabled, preview_only)
    if not publish_now:
        capture_safe_preview(page, verification, screenshot_path)
        save_storage_state(context, refreshed_auth_path)
        return PublishResult("previewed")

    # Final mandatory checks are intentionally adjacent to the single click.
    validate_editor_url(page.url, editor_url)
    if (
        not verification.subject.is_visible()
        or verification.subject.input_value() != content.title
    ):
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "제출 직전 제목 검증에 실패했습니다."
        )
    actual_text = normalize_text(verification.editor.inner_text())
    first, last = _core_samples(content.plain_text)
    if (
        not verification.editor.is_visible()
        or not actual_text
        or first not in actual_text
        or last not in actual_text
        or any(marker in actual_text.lower() for marker in PLACEHOLDER_MARKERS)
    ):
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED", "제출 직전 본문 검증에 실패했습니다."
        )
    if not verification.submit.is_visible() or not verification.submit.is_enabled():
        raise NovelpiaError(
            "NOVELPIA_VALIDATION_FAILED",
            "제출 직전 작성완료 버튼 검증에 실패했습니다.",
        )
    result = submit_once_and_determine(page, verification, editor_url)
    save_storage_state(context, refreshed_auth_path)
    if content.episode not in state["published_episodes"]:
        state["published_episodes"].append(content.episode)
        state["published_episodes"].sort()
    state.update(
        {
            "last_success_episode": content.episode,
            "last_success_at": utc_now(),
            "episode_path": content.repository_path,
            "title": content.title,
            "published_url": result.published_url,
            "publish_status": "published",
            "last_error_code": "",
            "session_refresh_status": "pending",
        }
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="노벨피아 회차 preview/게시")
    parser.add_argument("--episode-path")
    parser.add_argument("--auth-state", type=Path, required=True)
    parser.add_argument("--refreshed-auth", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--screenshot", type=Path, default=Path("preview/novelpia-editor.png"))
    parser.add_argument(
        "--editor-url",
        default=os.environ.get("NOVELPIA_EDITOR_URL") or DEFAULT_EDITOR_URL,
    )
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--force-republish", action="store_true")
    parser.add_argument("--refresh-session-only", action="store_true")
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = load_state(args.state)
    original_state = copy.deepcopy(state)
    preserve_publish_state = args.preview_only and not args.refresh_session_only
    content: EpisodeContent | None = None
    try:
        if not args.auth_state.is_file() or args.auth_state.stat().st_size == 0:
            raise NovelpiaError(
                "NOVELPIA_AUTH_MISSING", "복원된 storage state가 없습니다."
            )
        try:
            load_and_validate_storage_state(args.auth_state)
        except ValueError as exc:
            raise NovelpiaError(
                "NOVELPIA_AUTH_MISSING", "복원된 storage state가 유효하지 않습니다."
            ) from exc
        if not args.refresh_session_only:
            if not args.episode_path:
                raise ValueError("episode_path가 필요합니다.")
            content = parse_episode_markdown(args.episode_path)
        publish_enabled = bool_value(os.environ.get("NOVELPIA_PUBLISH_ENABLED"))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            context = browser.new_context(storage_state=str(args.auth_state))
            page = context.new_page()
            result = run_browser_operation(
                context=context,
                page=page,
                editor_url=args.editor_url,
                content=content,
                publish_enabled=publish_enabled,
                preview_only=args.preview_only,
                screenshot_path=args.screenshot,
                refreshed_auth_path=args.refreshed_auth,
                state=state,
                force_republish=args.force_republish,
                refresh_session_only=args.refresh_session_only,
            )
            browser.close()
        save_state(
            args.state,
            original_state if preserve_publish_state else state,
        )
        print(f"노벨피아 작업 완료: {result.status}")
        return 0
    except NovelpiaError as exc:
        if preserve_publish_state:
            print(f"{exc.code}: {exc}", file=sys.stderr)
            return 1
        state["last_error_code"] = exc.code
        state["publish_status"] = (
            "unknown"
            if exc.code == "NOVELPIA_SUBMIT_RESULT_UNKNOWN"
            else "failed"
        )
        if (
            exc.code == "NOVELPIA_SUBMIT_RESULT_UNKNOWN"
            and content is not None
            and content.episode not in state["unknown_result_episodes"]
        ):
            state["unknown_result_episodes"].append(content.episode)
        save_state(args.state, state)
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if preserve_publish_state:
            print(f"NOVELPIA_VALIDATION_FAILED: {exc}", file=sys.stderr)
            return 1
        state["last_error_code"] = "NOVELPIA_VALIDATION_FAILED"
        state["publish_status"] = "failed"
        save_state(args.state, state)
        print(f"NOVELPIA_VALIDATION_FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
