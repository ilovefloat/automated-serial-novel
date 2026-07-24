from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts"
STATE_DIR = ROOT / "state"
EPISODES_DIR = ROOT / "docs" / "episodes"
MODEL_CATALOG_PATH = STATE_DIR / "model_catalog.json"
STATE_MARKER = "---STORY_STATE_UPDATE---"

# API에서 실제 사용 가능 여부를 확인한 뒤 이 순서대로 우선 선택한다.
# 목록에 하나도 없으면 generateContent 지원 모델 중 하나를 자동 선택한다.
PREFERRED_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(read_text(path))


def normalize_model_name(name: str) -> str:
    return name.removeprefix("models/")


def list_generation_models(client: genai.Client) -> list[dict[str, Any]]:
    """현재 API 키로 조회되는 generateContent 지원 모델을 반환한다."""
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

    catalog.sort(key=lambda item: item["name"])
    return catalog


def save_model_catalog(catalog: list[dict[str, Any]]) -> None:
    MODEL_CATALOG_PATH.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "models": catalog,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def choose_model(client: genai.Client) -> tuple[str, list[dict[str, Any]]]:
    catalog = list_generation_models(client)
    save_model_catalog(catalog)

    available = {item["name"] for item in catalog}
    if not available:
        raise RuntimeError("현재 API 키로 generateContent를 지원하는 모델을 찾지 못했습니다.")

    configured = (os.environ.get("GEMINI_MODEL") or "").strip()
    if configured:
        configured = normalize_model_name(configured)
        if configured not in available:
            names = ", ".join(sorted(available))
            raise RuntimeError(
                f"설정된 모델 {configured!r}을 현재 사용할 수 없습니다. "
                f"generateContent 지원 모델: {names}"
            )
        return configured, catalog

    for preferred in PREFERRED_MODELS:
        if preferred in available:
            return preferred, catalog

    # 모델명만으로 품질이나 가격을 추정하지 않는다. 기능이 확인된 목록의
    # 첫 항목을 재현 가능하게 선택하고, 선택 결과를 로그와 원고에 기록한다.
    return sorted(available)[0], catalog


def recent_episodes(limit: int = 3) -> str:
    files = sorted(EPISODES_DIR.glob("*.md"))
    if not files:
        return "(아직 공개된 연재분 없음)"
    selected = files[-limit:]
    chunks = []
    for path in selected:
        chunks.append(f"\n### {path.name}\n{read_text(path)}")
    return "\n".join(chunks)


def strip_outer_fence(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"```(?:markdown|md|text|json)?\s*(.*?)\s*```", text, re.S)
    return match.group(1).strip() if match else text


def parse_response(raw: str) -> tuple[str, dict[str, Any]]:
    cleaned = strip_outer_fence(raw)
    if STATE_MARKER not in cleaned:
        raise ValueError(f"응답에 필수 구분자 {STATE_MARKER!r}가 없습니다.")

    public_text, state_text = cleaned.rsplit(STATE_MARKER, 1)
    public_text = public_text.strip()
    state_text = strip_outer_fence(state_text.strip())

    if len(public_text) < 1200:
        raise ValueError("생성된 공개 본문이 지나치게 짧습니다.")

    try:
        update = json.loads(state_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"상태 갱신 JSON 파싱 실패: {exc}") from exc

    required = {
        "summary",
        "protagonist_changes",
        "new_facts",
        "open_threads",
        "next_episode_pressure",
    }
    missing = required - update.keys()
    if missing:
        raise ValueError(f"상태 갱신에 필드가 없습니다: {sorted(missing)}")

    return public_text, update


def extract_title_and_body(public_text: str) -> tuple[str, str]:
    lines = [line.rstrip() for line in public_text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) < 2:
        raise ValueError("제목과 본문을 구분할 수 없습니다.")

    title = re.sub(r"^#+\s*", "", lines[0]).strip().strip('"“”')
    body = "\n".join(lines[1:]).strip()
    if not title or len(body) < 1000:
        raise ValueError("제목 또는 본문이 유효하지 않습니다.")
    return title, body


def merge_state(
    state: dict[str, Any],
    update: dict[str, Any],
    episode_number: int,
    title: str,
) -> dict[str, Any]:
    history = state.setdefault("history", [])
    history.append(
        {
            "episode": episode_number,
            "title": title,
            "summary": update["summary"],
        }
    )
    state["summary"] = update["summary"]
    state["protagonist_changes"] = update["protagonist_changes"]

    old_facts = state.get("new_facts", [])
    state["new_facts"] = list(dict.fromkeys(old_facts + update["new_facts"]))
    state["open_threads"] = update["open_threads"]
    state["next_episode_pressure"] = update["next_episode_pressure"]
    state["next_episode"] = episode_number + 1
    state["last_generated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def episode_markdown(
    episode_number: int,
    title: str,
    body: str,
    model: str,
) -> str:
    generated = datetime.now(timezone.utc).date().isoformat()
    safe_title = title.replace('"', "'")
    return (
        "---\n"
        f"episode: {episode_number}\n"
        f'title: "{safe_title}"\n'
        f'date: "{generated}"\n'
        f'model: "{model}"\n'
        "---\n\n"
        f"# {episode_number}화. {title}\n\n"
        f"{body}\n"
    )


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 2

    state_path = STATE_DIR / "story_state.json"
    state = load_json(state_path)
    episode_number = int(state["next_episode"])

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    output_path = EPISODES_DIR / f"{episode_number:03d}.md"
    if output_path.exists():
        raise FileExistsError(f"에피소드 파일이 이미 존재합니다: {output_path}")

    client = genai.Client(api_key=api_key)
    model, catalog = choose_model(client)
    print(f"generateContent 지원 모델 {len(catalog)}개 확인")
    print(f"선택 모델: {model}")

    system_prompt = read_text(PROMPTS / "system.md")
    episode_template = read_text(PROMPTS / "episode.md")
    user_prompt = episode_template.format(
        episode_number=episode_number,
        story_state=json.dumps(state, ensure_ascii=False, indent=2),
        continuity=read_text(STATE_DIR / "continuity.md"),
        recent_episodes=recent_episodes(),
    )

    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.9,
            top_p=0.95,
            max_output_tokens=8192,
        ),
    )

    raw = response.text or ""
    public_text, update = parse_response(raw)
    title, body = extract_title_and_body(public_text)

    output_path.write_text(
        episode_markdown(episode_number, title, body, model),
        encoding="utf-8",
    )
    new_state = merge_state(state, update, episode_number, title)
    state_path.write_text(
        json.dumps(new_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"생성 완료: {output_path}")
    print(f"제목: {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
