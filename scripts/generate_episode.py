from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts"
STATE_DIR = ROOT / "state"
EPISODES_DIR = ROOT / "docs" / "episodes"
MODEL_CATALOG_PATH = STATE_DIR / "model_catalog.json"
MIN_PUBLIC_CHARS = 1200
MAX_ATTEMPTS = 3
MAX_RESPONSE_ATTEMPTS = 2
MAX_HISTORY_ITEMS = 20
MAX_FACT_ITEMS = 60

# 이름 목록의 반환 순서와 무관하게, 실제 사용 가능한 모델 중 이 순서로 선택한다.
PREFERRED_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
)
UNSTABLE_MODEL_MARKERS = ("preview", "experimental", "-exp", "latest")
EXCLUDED_FALLBACK_MARKERS = (
    "embedding",
    "imagen",
    "veo",
    "tts",
    "audio",
    "live",
)

T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON 상태 파일을 읽을 수 없습니다: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 상태 파일의 최상위 값은 객체여야 합니다: {path}")
    return value


def atomic_write_text(path: Path, text: str) -> None:
    """Write a complete UTF-8 file, replacing the destination atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_create_text(path: Path, text: str) -> None:
    """Create a complete UTF-8 file without ever overwriting an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(f"에피소드 파일이 이미 존재합니다: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def normalize_model_name(name: str) -> str:
    return name.strip().removeprefix("models/")


def list_generation_models(client: genai.Client) -> list[dict[str, Any]]:
    """Return models exposed to this key that explicitly support generateContent."""
    catalog: list[dict[str, Any]] = []
    for model in client.models.list():
        raw_name = getattr(model, "name", None)
        if not raw_name:
            continue
        supported_actions = list(getattr(model, "supported_actions", None) or [])
        if "generateContent" not in supported_actions:
            continue
        catalog.append(
            {
                "name": normalize_model_name(raw_name),
                "display_name": getattr(model, "display_name", None),
                "supported_actions": supported_actions,
                "input_token_limit": getattr(model, "input_token_limit", None),
                "output_token_limit": getattr(model, "output_token_limit", None),
            }
        )
    return sorted(catalog, key=lambda item: item["name"])


def select_model(
    catalog: list[dict[str, Any]], configured: str | None = None
) -> str:
    available = {item["name"] for item in catalog}
    if not available:
        raise RuntimeError(
            "현재 API 키로 generateContent를 지원하는 모델을 찾지 못했습니다."
        )

    if configured and configured.strip():
        requested = normalize_model_name(configured)
        if requested not in available:
            names = ", ".join(sorted(available))
            raise RuntimeError(
                f"설정된 GEMINI_MODEL {requested!r}을 현재 사용할 수 없습니다. "
                f"사용 가능한 generateContent 모델: {names}"
            )
        return requested

    for preferred in PREFERRED_MODELS:
        if preferred in available:
            return preferred

    usable = [
        name
        for name in available
        if not any(marker in name.lower() for marker in EXCLUDED_FALLBACK_MARKERS)
    ]
    if not usable:
        raise RuntimeError("안전한 텍스트 생성 fallback 모델을 찾지 못했습니다.")

    stable = [
        name
        for name in usable
        if not any(marker in name.lower() for marker in UNSTABLE_MODEL_MARKERS)
    ]
    candidates = stable or usable

    def fallback_key(name: str) -> tuple[int, int, str]:
        lowered = name.lower()
        family_rank = 0 if "flash" in lowered else 1 if "pro" in lowered else 2
        lite_rank = 1 if "lite" in lowered else 0
        return family_rank, lite_rank, name

    return sorted(candidates, key=fallback_key)[0]


def model_catalog_document(
    catalog: list[dict[str, Any]], selected_model: str, checked_at: datetime
) -> dict[str, Any]:
    return {
        "checked_at": checked_at.isoformat(),
        "selected_model": selected_model,
        "selection_priority": list(PREFERRED_MODELS),
        "fallback_policy": "stable text model; flash before pro; lexical tie-break",
        "available_generate_content_models": [
            item["name"] for item in catalog
        ],
        "models": catalog,
    }


def episode_numbers(paths: Iterable[Path]) -> list[int]:
    numbers: list[int] = []
    for path in paths:
        if not re.fullmatch(r"\d{3,}\.md", path.name):
            continue
        numbers.append(int(path.stem))
    if len(numbers) != len(set(numbers)):
        raise ValueError("중복된 에피소드 번호가 있습니다.")
    return sorted(numbers)


def calculate_next_episode(
    state: dict[str, Any], paths: Iterable[Path]
) -> int:
    try:
        state_next = int(state["next_episode"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("story_state.json의 next_episode가 유효하지 않습니다.") from exc
    if state_next < 1:
        raise ValueError("next_episode는 1 이상이어야 합니다.")

    numbers = episode_numbers(paths)
    expected = (numbers[-1] + 1) if numbers else 1
    if state_next != expected:
        raise ValueError(
            "에피소드 파일과 상태 번호가 일치하지 않습니다: "
            f"state={state_next}, files={expected}"
        )
    return state_next


def recent_episodes(directory: Path = EPISODES_DIR, limit: int = 3) -> str:
    files = sorted(
        directory.glob("*.md"),
        key=lambda path: int(path.stem) if path.stem.isdigit() else -1,
    )
    selected = [path for path in files if path.stem.isdigit()][-limit:]
    if not selected:
        return "(아직 공개된 연재분 없음)"
    return "\n".join(
        f"\n### {path.name}\n{read_text(path)}" for path in selected
    )


def strip_outer_fence(text: str) -> str:
    cleaned = text.strip()
    match = re.fullmatch(
        r"```(?:markdown|md|text|json)?[ \t]*\r?\n(.*?)\r?\n```",
        cleaned,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else cleaned


def validate_state_update(update: Any) -> dict[str, Any]:
    if not isinstance(update, dict):
        raise ValueError("state_update는 JSON 객체여야 합니다.")
    scalar_fields = ("summary", "next_episode_pressure")
    list_fields = (
        "protagonist_changes",
        "new_facts",
        "open_threads",
        "continuity_notes",
    )
    for field in scalar_fields:
        if not isinstance(update.get(field), str) or not update[field].strip():
            raise ValueError(f"state_update.{field}는 비어 있지 않은 문자열이어야 합니다.")
    for field in list_fields:
        value = update.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(f"state_update.{field}는 문자열 배열이어야 합니다.")
    return update


def parse_response(raw: str) -> tuple[str, dict[str, Any]]:
    if not raw or not raw.strip():
        raise ValueError("Gemini가 빈 응답을 반환했습니다.")
    cleaned = strip_outer_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini 응답 JSON 파싱 실패: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Gemini 응답의 최상위 값은 JSON 객체여야 합니다.")

    public_text = payload.get("public_markdown")
    if not isinstance(public_text, str) or len(public_text.strip()) < MIN_PUBLIC_CHARS:
        raise ValueError("생성된 공개 본문이 비어 있거나 지나치게 짧습니다.")
    public_text = strip_outer_fence(public_text)
    if "```" in public_text:
        raise ValueError("공개 본문에 허용되지 않은 코드펜스가 포함되어 있습니다.")
    return public_text, validate_state_update(payload.get("state_update"))


def extract_title_and_body(public_text: str) -> tuple[str, str]:
    match = re.fullmatch(
        r"[ \t]*# (?!#)([^\r\n]+)\r?\n+(.*)",
        public_text.strip(),
        re.DOTALL,
    )
    if not match:
        raise ValueError("공개 본문은 한 개의 H1 제목으로 시작해야 합니다.")
    title = match.group(1).strip().strip('"“”')
    body = match.group(2).strip()
    if not title or len(title) > 100 or title.startswith(("#", "---")):
        raise ValueError("제목이 비어 있거나 형식이 잘못되었습니다.")
    if re.match(r"^\d+\s*화(?:[.\s]|$)", title):
        raise ValueError("제목에는 에피소드 번호를 포함하지 마십시오.")
    if len(body) < MIN_PUBLIC_CHARS:
        raise ValueError("생성된 본문이 지나치게 짧습니다.")
    return title, body


def merge_state(
    state: dict[str, Any],
    update: dict[str, Any],
    episode_number: int,
    title: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    merged = copy.deepcopy(state)
    history = merged.setdefault("history", [])
    if not isinstance(history, list):
        raise ValueError("state.history는 배열이어야 합니다.")
    if any(item.get("episode") == episode_number for item in history):
        raise ValueError(f"state.history에 {episode_number}화가 이미 있습니다.")
    history.append(
        {
            "episode": episode_number,
            "title": title,
            "summary": update["summary"],
        }
    )
    merged["history"] = history[-MAX_HISTORY_ITEMS:]
    merged["summary"] = update["summary"]
    merged["protagonist_changes"] = update["protagonist_changes"]
    old_facts = merged.get("new_facts", [])
    if not isinstance(old_facts, list):
        raise ValueError("state.new_facts는 배열이어야 합니다.")
    merged["new_facts"] = list(
        dict.fromkeys(old_facts + update["new_facts"])
    )[-MAX_FACT_ITEMS:]
    merged["open_threads"] = update["open_threads"]
    merged["continuity_notes"] = update["continuity_notes"]
    merged["next_episode_pressure"] = update["next_episode_pressure"]
    merged["next_episode"] = episode_number + 1
    merged["last_generated_at"] = (generated_at or utc_now()).isoformat()
    return merged


def episode_markdown(
    episode_number: int,
    title: str,
    body: str,
    model: str,
    generated_at: datetime | None = None,
) -> str:
    generated = (generated_at or utc_now()).date().isoformat()
    safe_title = title.replace("\\", "").replace('"', "'")
    safe_model = model.replace("\\", "").replace('"', "")
    return (
        "---\n"
        f"episode: {episode_number}\n"
        f'title: "{safe_title}"\n'
        f'date: "{generated}"\n'
        f'model: "{safe_model}"\n'
        "---\n\n"
        f"# {episode_number}화. {title}\n\n"
        f"{body}\n"
    )


def is_transient_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError)) or exc.__class__.__name__ in {
        "ServerError",
        "ServiceUnavailable",
        "TooManyRequests",
    }


def call_with_retry(
    operation: Callable[[], T],
    label: str,
    attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not is_transient_error(exc) or attempt == attempts:
                raise
            delay = 2 ** (attempt - 1)
            print(
                f"{label} 일시 오류, {delay}초 후 재시도 "
                f"({attempt}/{attempts - 1})",
                file=sys.stderr,
            )
            sleep(delay)
    raise AssertionError("unreachable")


def response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["public_markdown", "state_update"],
        "properties": {
            "public_markdown": {"type": "string"},
            "state_update": {
                "type": "object",
                "required": [
                    "summary",
                    "protagonist_changes",
                    "new_facts",
                    "open_threads",
                    "continuity_notes",
                    "next_episode_pressure",
                ],
                "properties": {
                    "summary": {"type": "string"},
                    "protagonist_changes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "new_facts": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "open_threads": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "continuity_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "next_episode_pressure": {"type": "string"},
                },
            },
        },
    }


def build_prompt(state: dict[str, Any], episode_number: int) -> str:
    template = read_text(PROMPTS / "episode.md")
    return template.format(
        episode_number=episode_number,
        story_state=json.dumps(state, ensure_ascii=False, indent=2),
        continuity=read_text(STATE_DIR / "continuity.md"),
        recent_episodes=recent_episodes(),
    )


def parse_generated_episode(raw: str) -> tuple[str, str, dict[str, Any]]:
    public_text, update = parse_response(raw)
    title, body = extract_title_and_body(public_text)
    return title, body, update


def generate_raw_response(
    client: genai.Client, model: str, system_prompt: str, user_prompt: str
) -> str:
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.85,
                top_p=0.95,
                max_output_tokens=8192,
                response_mime_type="application/json",
                response_json_schema=response_schema(),
            ),
        ),
        "본문 생성",
    )
    try:
        return response.text or ""
    except (AttributeError, ValueError) as exc:
        raise ValueError("Gemini 응답에서 텍스트를 읽을 수 없습니다.") from exc


def output_paths(preview_dir: Path | None) -> tuple[Path, Path, Path]:
    if preview_dir is None:
        return EPISODES_DIR, STATE_DIR / "story_state.json", MODEL_CATALOG_PATH
    return (
        preview_dir / "episodes",
        preview_dir / "story_state.json",
        preview_dir / "model_catalog.json",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="다음 연재 에피소드 한 편 생성")
    parser.add_argument(
        "--preview-dir",
        type=Path,
        help="저장소를 갱신하지 않고 이 디렉터리에 결과 스냅샷 저장",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 2

    source_state_path = STATE_DIR / "story_state.json"
    state = load_json(source_state_path)
    episode_number = calculate_next_episode(state, EPISODES_DIR.glob("*.md"))
    target_episodes, target_state, target_catalog = output_paths(args.preview_dir)
    output_path = target_episodes / f"{episode_number:03d}.md"
    if output_path.exists():
        raise FileExistsError(f"에피소드 파일이 이미 존재합니다: {output_path}")

    client = genai.Client(api_key=api_key)
    catalog = call_with_retry(
        lambda: list_generation_models(client),
        "모델 목록 조회",
    )
    model = select_model(catalog, os.environ.get("GEMINI_MODEL"))
    available_names = ", ".join(item["name"] for item in catalog)
    print(f"generateContent 지원 모델 {len(catalog)}개: {available_names}")
    print(f"선택 모델: {model}")

    generated_at = utc_now()
    system_prompt = read_text(PROMPTS / "system.md")
    user_prompt = build_prompt(state, episode_number)
    for response_attempt in range(1, MAX_RESPONSE_ATTEMPTS + 1):
        raw = generate_raw_response(client, model, system_prompt, user_prompt)
        try:
            title, body, update = parse_generated_episode(raw)
            break
        except ValueError:
            if response_attempt == MAX_RESPONSE_ATTEMPTS:
                raise
            print(
                "응답 형식 검증 실패, 본문 생성을 한 번 더 시도합니다.",
                file=sys.stderr,
            )
    new_state = merge_state(
        state, update, episode_number, title, generated_at=generated_at
    )
    catalog_document = model_catalog_document(catalog, model, generated_at)

    created_episode = False
    try:
        atomic_create_text(
            output_path,
            episode_markdown(
                episode_number,
                title,
                body,
                model,
                generated_at=generated_at,
            ),
        )
        created_episode = True
        atomic_write_text(
            target_catalog,
            json.dumps(catalog_document, ensure_ascii=False, indent=2) + "\n",
        )
        # State is the commit point: it advances only after the public file and
        # diagnostic catalog have been written successfully.
        atomic_write_text(
            target_state,
            json.dumps(new_state, ensure_ascii=False, indent=2) + "\n",
        )
    except Exception:
        if created_episode:
            output_path.unlink(missing_ok=True)
        raise

    mode = "preview" if args.preview_dir is not None else "publish"
    print(f"생성 완료 ({mode}): {output_path}")
    print(f"제목: {title}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        secret = os.environ.get("GEMINI_API_KEY") or ""
        message = str(exc)
        if secret:
            message = message.replace(secret, "[REDACTED]")
        print(f"{exc.__class__.__name__}: {message}", file=sys.stderr)
        raise SystemExit(1)
