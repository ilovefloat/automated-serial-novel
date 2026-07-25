from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_EDITOR_URL = "https://novelpia.com/mynovel/all/write/442975"
DEFAULT_AUTH_PATH = Path("secrets/novelpia-auth.json")
DEFAULT_PROFILE_PATH = Path("secrets/novelpia-login-profile")


def is_novelpia_host(hostname: str | None) -> bool:
    return hostname == "novelpia.com" or bool(
        hostname and hostname.endswith(".novelpia.com")
    )


def novelpia_only_storage_state(state: dict[str, Any]) -> dict[str, Any]:
    cookies = [
        cookie
        for cookie in state.get("cookies", [])
        if isinstance(cookie, dict)
        and is_novelpia_host(
            str(cookie.get("domain", "")).lstrip(".").lower()
        )
    ]
    origins = [
        origin
        for origin in state.get("origins", [])
        if isinstance(origin, dict)
        and is_novelpia_host(
            urlparse(str(origin.get("origin", ""))).hostname
        )
    ]
    return {"cookies": cookies, "origins": origins}


def save_storage_state(context: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = context.storage_state(indexed_db=True)
    except TypeError:
        state = context.storage_state()
    filtered = novelpia_only_storage_state(state)
    if not filtered["cookies"] and not filtered["origins"]:
        raise ValueError("novelpia.com 인증 데이터가 storage state에 없습니다.")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(filtered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def find_installed_chrome(explicit: Path | None = None) -> Path:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"지정한 Chrome 실행 파일이 없습니다: {candidate}")

    command = shutil.which("chrome") or shutil.which("google-chrome")
    if command:
        return Path(command).resolve()

    candidates: list[Path] = []
    if sys.platform == "win32":
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(variable)
            if base:
                candidates.append(
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                )
    elif sys.platform == "darwin":
        candidates.append(
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        )
    else:
        for name in ("google-chrome-stable", "chromium", "chromium-browser"):
            command = shutil.which(name)
            if command:
                candidates.append(Path(command))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "설치된 Google Chrome을 찾지 못했습니다. --chrome-path로 지정해 주세요."
    )


def open_uncontrolled_chrome(
    chrome_path: Path, profile_path: Path, editor_url: str
) -> subprocess.Popen[bytes]:
    profile_path.mkdir(parents=True, exist_ok=True)
    command = [
        str(chrome_path),
        f"--user-data-dir={profile_path.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        editor_url,
    ]
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="노벨피아 최초 로컬 로그인")
    parser.add_argument("--editor-url", default=DEFAULT_EDITOR_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_AUTH_PATH)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--chrome-path", type=Path)
    parser.add_argument("--timeout-minutes", type=int, default=2)
    parser.add_argument(
        "--keep-login-profile",
        action="store_true",
        help="성공 후 Google 로그인이 포함된 전용 Chrome 프로필을 보존",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if urlparse(args.editor_url).hostname != "novelpia.com":
        print("노벨피아 공식 작성 URL만 사용할 수 있습니다.", file=sys.stderr)
        return 2

    try:
        chrome_path = find_installed_chrome(args.chrome_path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    profile_path = args.profile_dir.expanduser().resolve()
    print(
        "지금 여는 창은 Playwright나 원격 디버깅에 연결되지 않은 일반 Chrome입니다."
    )
    print(
        "이 Chrome에서 Google 로그인을 직접 완료하고 노벨피아 작성 화면이 "
        "보이는지 확인한 뒤, 이 전용 Chrome의 모든 창을 완전히 닫아 주세요."
    )
    process = open_uncontrolled_chrome(
        chrome_path, profile_path, args.editor_url
    )
    try:
        input("Chrome을 완전히 닫은 다음 Enter를 누르세요: ")
    except (EOFError, KeyboardInterrupt):
        print("로그인 세션 추출을 취소했습니다.", file=sys.stderr)
        return 130
    if process.poll() is None:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print(
                "전용 Chrome이 아직 실행 중입니다. 모든 창을 닫고 다시 실행해 주세요.",
                file=sys.stderr,
            )
            return 1

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            executable_path=str(chrome_path),
            headless=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        print(
            "로그인이 끝난 Chrome 프로필에서 노벨피아 세션만 확인합니다. "
            "Google 로그인 화면은 조작하지 않습니다."
        )
        page.goto(args.editor_url, wait_until="domcontentloaded")
        try:
            page.locator("#content_subject").wait_for(
                state="visible",
                timeout=args.timeout_minutes * 60_000,
            )
        except PlaywrightTimeoutError:
            print(
                "노벨피아 작성 화면을 확인하지 못했습니다. 일반 Chrome에서 "
                "로그인과 작성 화면 진입을 완료했는지 확인해 주세요.",
                file=sys.stderr,
            )
            context.close()
            return 1
        if urlparse(page.url).hostname != "novelpia.com":
            print(
                "세션 확인 중 로그인 페이지로 이동했습니다. 자동으로 우회하지 않습니다.",
                file=sys.stderr,
            )
            context.close()
            return 1
        try:
            save_storage_state(context, args.output)
        except ValueError as exc:
            print(f"노벨피아 세션 저장 실패: {exc}", file=sys.stderr)
            context.close()
            return 1
        print(f"로그인 세션 저장 완료: {args.output}")
        context.close()

    default_profile = DEFAULT_PROFILE_PATH.resolve()
    if not args.keep_login_profile and profile_path == default_profile:
        try:
            shutil.rmtree(profile_path)
            print("Google 로그인이 포함된 임시 Chrome 프로필을 삭제했습니다.")
        except OSError:
            print(
                f"주의: 임시 Chrome 프로필을 직접 삭제해 주세요: {profile_path}",
                file=sys.stderr,
            )
    elif args.keep_login_profile:
        print(
            "주의: 전용 Chrome 프로필에는 Google 로그인 정보가 포함될 수 있습니다: "
            f"{profile_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
