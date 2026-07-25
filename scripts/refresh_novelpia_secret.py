from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

SECRET_NAME = "NOVELPIA_AUTH_STATE_B64"
MAX_SECRET_BYTES = 48 * 1024


def load_and_validate_storage_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("storage state 파일을 읽을 수 없습니다.") from exc
    if not raw:
        raise ValueError("storage state 파일이 비어 있습니다.")
    try:
        state = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("storage state가 유효한 JSON이 아닙니다.") from exc
    if not isinstance(state, dict):
        raise ValueError("storage state 최상위 값은 객체여야 합니다.")
    if "cookies" not in state and "origins" not in state:
        raise ValueError("storage state에 cookies 또는 origins가 없습니다.")

    cookies = state.get("cookies", [])
    origins = state.get("origins", [])
    cookie_match = any(
        isinstance(cookie, dict)
        and (
            str(cookie.get("domain", "")).lstrip(".").lower() == "novelpia.com"
            or str(cookie.get("domain", "")).lstrip(".").lower().endswith(
                ".novelpia.com"
            )
        )
        for cookie in cookies
    )
    origin_match = any(
        isinstance(origin, dict)
        and urlparse(str(origin.get("origin", ""))).hostname
        in {"novelpia.com", "www.novelpia.com"}
        for origin in origins
    )
    if not (cookie_match or origin_match):
        raise ValueError("novelpia.com 관련 cookie 또는 origin이 없습니다.")
    return state


def storage_state_base64(path: Path) -> str:
    load_and_validate_storage_state(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    if len(encoded.encode("ascii")) > MAX_SECRET_BYTES:
        raise ValueError("Base64 storage state가 GitHub Secret 크기 제한을 넘습니다.")
    return encoded


def update_repository_secret(
    encoded: str,
    repository: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if not repository or "/" not in repository:
        raise ValueError("GITHUB_REPOSITORY 값이 올바르지 않습니다.")
    if not os.environ.get("GH_TOKEN"):
        raise ValueError("GH_TOKEN 환경변수가 없습니다.")
    command = ["gh", "secret", "set", SECRET_NAME, "--repo", repository]
    result = runner(
        command,
        input=encoded,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("GitHub repository secret 갱신에 실패했습니다.")


def update_session_status(state_path: Path, status: str) -> None:
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["session_refresh_status"] = status
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="노벨피아 storage state Secret 갱신")
    parser.add_argument("storage_state", type=Path)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        encoded = storage_state_base64(args.storage_state)
        if not args.validate_only:
            update_repository_secret(encoded, args.repo)
        if args.state:
            update_session_status(
                args.state, "validated" if args.validate_only else "updated"
            )
        print(
            "노벨피아 storage state 검증 완료"
            if args.validate_only
            else "노벨피아 세션 Secret 갱신 완료"
        )
        return 0
    except Exception as exc:
        if args.state:
            update_session_status(args.state, "update_failed")
        print(f"세션 Secret 처리 실패: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
