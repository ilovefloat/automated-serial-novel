from __future__ import annotations

import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
EPISODES = DOCS / "episodes"
STATE = ROOT / "state" / "story_state.json"


def parse_episode(path: Path) -> tuple[int, str, str]:
    text = path.read_text(encoding="utf-8")
    episode_match = re.search(r"^episode:\s*(\d+)", text, re.M)
    title_match = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', text, re.M)
    date_match = re.search(r'^date:\s*["\']?(.*?)["\']?\s*$', text, re.M)
    if not (episode_match and title_match and date_match):
        raise ValueError(f"front matter 파싱 실패: {path}")
    return int(episode_match.group(1)), title_match.group(1), date_match.group(1)


def main() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    title = state.get("series_title", "자동 연재 소설")
    items = []
    for path in sorted(EPISODES.glob("*.md"), reverse=True):
        number, episode_title, date = parse_episode(path)
        href = f"episode.html?file=episodes/{path.name}"
        items.append(
            "<li>"
            f'<a href="{html.escape(href)}">'
            f"<strong>{number}화</strong> {html.escape(episode_title)}</a>"
            f"<time>{html.escape(date)}</time>"
            "</li>"
        )

    empty = "<li class='empty'>아직 생성된 에피소드가 없습니다.</li>"
    episode_list = "\n".join(items) if items else empty
    template = (DOCS / "index.template.html").read_text(encoding="utf-8")
    rendered = (
        template.replace("{{SERIES_TITLE}}", html.escape(title))
        .replace("{{EPISODE_LIST}}", episode_list)
    )
    (DOCS / "index.html").write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
