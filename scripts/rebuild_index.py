from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
EPISODES = DOCS / "episodes"
STATE = ROOT / "state" / "story_state.json"
MANIFEST = DOCS / "episodes.json"
FRONT_MATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)


def atomic_write_text(path: Path, text: str) -> None:
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


def parse_front_matter(text: str, path: Path) -> dict[str, str]:
    match = FRONT_MATTER.match(text)
    if not match:
        raise ValueError(f"front matter가 파일 첫 부분에 없습니다: {path}")
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        key, separator, raw_value = line.partition(":")
        if not separator or not re.fullmatch(r"[a-z_]+", key):
            raise ValueError(f"front matter 형식이 잘못되었습니다: {path}")
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def parse_episode(path: Path) -> dict[str, Any]:
    if not re.fullmatch(r"\d{3,}\.md", path.name):
        raise ValueError(f"에피소드 파일명이 안전한 숫자 형식이 아닙니다: {path.name}")
    values = parse_front_matter(path.read_text(encoding="utf-8"), path)
    try:
        number = int(values["episode"])
        title = values["title"].strip()
        date = values["date"].strip()
    except (KeyError, ValueError) as exc:
        raise ValueError(f"필수 front matter를 파싱할 수 없습니다: {path}") from exc
    if number != int(path.stem):
        raise ValueError(f"파일명과 에피소드 번호가 일치하지 않습니다: {path}")
    if not title or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError(f"제목 또는 날짜가 유효하지 않습니다: {path}")
    return {
        "episode": number,
        "title": title,
        "date": date,
        "file": f"episodes/{path.name}",
    }


def collect_episodes(directory: Path) -> list[dict[str, Any]]:
    episodes = [parse_episode(path) for path in directory.glob("*.md")]
    episodes.sort(key=lambda item: item["episode"])
    numbers = [item["episode"] for item in episodes]
    if numbers and numbers != list(range(1, numbers[-1] + 1)):
        raise ValueError(f"에피소드 번호가 연속적이지 않습니다: {numbers}")
    return episodes


def render_episode_list(episodes: list[dict[str, Any]]) -> str:
    if not episodes:
        return '<li class="empty">아직 생성된 에피소드가 없습니다.</li>'
    items = []
    for episode in reversed(episodes):
        file_param = quote(episode["file"], safe="/")
        href = f"episode.html?file={file_param}"
        items.append(
            "<li>"
            f'<a href="{html.escape(href, quote=True)}">'
            f'<strong>{episode["episode"]}화</strong> '
            f'{html.escape(episode["title"])}</a>'
            f'<time datetime="{html.escape(episode["date"], quote=True)}">'
            f'{html.escape(episode["date"])}</time>'
            "</li>"
        )
    return "\n".join(items)


def render_index(
    template: str, series_title: str, episodes: list[dict[str, Any]]
) -> str:
    return (
        template.replace("{{SERIES_TITLE}}", html.escape(series_title))
        .replace("{{EPISODE_LIST}}", render_episode_list(episodes))
    )


def main() -> None:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"상태 파일을 읽을 수 없습니다: {STATE}") from exc
    title = state.get("series_title", "자동 연재 소설")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("series_title은 비어 있지 않은 문자열이어야 합니다.")

    episodes = collect_episodes(EPISODES)
    expected_next = episodes[-1]["episode"] + 1 if episodes else 1
    if state.get("next_episode") != expected_next:
        raise ValueError(
            "상태와 공개 에피소드가 일치하지 않습니다: "
            f"state={state.get('next_episode')}, expected={expected_next}"
        )

    template = (DOCS / "index.template.html").read_text(encoding="utf-8")
    rendered = render_index(template, title, episodes)
    manifest = {
        "series_title": title,
        "episodes": episodes,
    }
    atomic_write_text(DOCS / "index.html", rendered)
    atomic_write_text(
        MANIFEST,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    print(f"연재 목록 갱신: {len(episodes)}편")


if __name__ == "__main__":
    main()
