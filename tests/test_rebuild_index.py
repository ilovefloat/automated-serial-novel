from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.rebuild_index import collect_episodes, parse_episode, render_index


def episode_text(number: int, title: str = "제목") -> str:
    return (
        "---\n"
        f"episode: {number}\n"
        f'title: "{title}"\n'
        'date: "2026-07-24"\n'
        'model: "gemini-test"\n'
        "---\n\n"
        f"# {number}화. {title}\n\n본문\n"
    )


class IndexTests(unittest.TestCase):
    def test_parse_episode_and_collect_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "002.md").write_text(episode_text(2), encoding="utf-8")
            (root / "001.md").write_text(episode_text(1), encoding="utf-8")
            episodes = collect_episodes(root)
            self.assertEqual([item["episode"] for item in episodes], [1, 2])
            self.assertEqual(episodes[0]["file"], "episodes/001.md")

    def test_filename_and_front_matter_number_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "001.md"
            path.write_text(episode_text(2), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "일치하지"):
                parse_episode(path)

    def test_missing_episode_number_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "001.md").write_text(episode_text(1), encoding="utf-8")
            (root / "003.md").write_text(episode_text(3), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "연속적"):
                collect_episodes(root)

    def test_index_escapes_title_and_builds_safe_link(self) -> None:
        rendered = render_index(
            "<title>{{SERIES_TITLE}}</title><ol>{{EPISODE_LIST}}</ol>",
            "A & B",
            [
                {
                    "episode": 1,
                    "title": "<script>",
                    "date": "2026-07-24",
                    "file": "episodes/001.md",
                }
            ],
        )
        self.assertIn("A &amp; B", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("episode.html?file=episodes/001.md", rendered)
        self.assertNotIn("<script>", rendered)


if __name__ == "__main__":
    unittest.main()
