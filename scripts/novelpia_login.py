from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_EDITOR_URL = "https://novelpia.com/mynovel/all/write/442975"
DEFAULT_AUTH_PATH = Path("secrets/novelpia-auth.json")


def save_storage_state(context: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        context.storage_state(path=str(path), indexed_db=True)
    except TypeError:
        context.storage_state(path=str(path))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="노벨피아 최초 로컬 로그인")
    parser.add_argument("--editor-url", default=DEFAULT_EDITOR_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_AUTH_PATH)
    parser.add_argument("--timeout-minutes", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if urlparse(args.editor_url).hostname != "novelpia.com":
        print("노벨피아 공식 작성 URL만 사용할 수 있습니다.", file=sys.stderr)
        return 2

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=False, channel="chrome")
        except Exception:
            browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        print(
            "브라우저에서 Google 로그인을 직접 완료해 주세요. "
            "비밀번호, CAPTCHA, 추가 인증은 이 스크립트가 처리하지 않습니다."
        )
        page.goto(args.editor_url, wait_until="domcontentloaded")
        try:
            page.locator("#content_subject").wait_for(
                state="visible",
                timeout=args.timeout_minutes * 60_000,
            )
        except PlaywrightTimeoutError:
            print(
                "제한 시간 안에 노벨피아 작성 화면을 확인하지 못했습니다.",
                file=sys.stderr,
            )
            browser.close()
            return 1
        if urlparse(page.url).hostname != "novelpia.com":
            print("로그인 후 현재 페이지가 novelpia.com이 아닙니다.", file=sys.stderr)
            browser.close()
            return 1
        save_storage_state(context, args.output)
        print(f"로그인 세션 저장 완료: {args.output}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
