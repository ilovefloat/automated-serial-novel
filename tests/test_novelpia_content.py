from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scripts.novelpia_content import (
    blank_line_separator_count,
    has_editor_indentation,
    markdown_to_safe_html,
    parse_episode_markdown,
    resolve_episode_path,
    strip_episode_prefix,
)


def episode_text(
    number: int = 12,
    title: str = "회색 빛의 간격",
    body: str = "첫 문단입니다.\n둘째 줄입니다.\n\n마지막 문단입니다.",
) -> str:
    return (
        "---\n"
        f"episode: {number}\n"
        f'title: "{title}"\n'
        'date: "2026-07-25"\n'
        "---\n\n"
        f"# {number}화. {title}\n\n"
        f"{body}\n"
    )


class EpisodeParsingTests(unittest.TestCase):
    def make_repository(self, text: str | None = None) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        episodes = root / "docs" / "episodes"
        episodes.mkdir(parents=True)
        (episodes / "012.md").write_text(text or episode_text(), encoding="utf-8")
        return temporary, root

    def test_yaml_episode_title_date_and_public_body(self) -> None:
        temporary, root = self.make_repository()
        with temporary:
            parsed = parse_episode_markdown(
                "docs/episodes/012.md", repository_root=root
            )
        self.assertEqual(parsed.episode, 12)
        self.assertEqual(parsed.title, "회색 빛의 간격")
        self.assertEqual(parsed.date, "2026-07-25")
        self.assertNotIn("episode:", parsed.html_body)
        self.assertNotIn("12화", parsed.html_body)

    def test_title_episode_number_is_removed_without_duplication(self) -> None:
        for title in ("12화. 회색 빛의 간격", "제12화 회색 빛의 간격", "[12회] 회색 빛의 간격"):
            with self.subTest(title=title):
                self.assertEqual(strip_episode_prefix(title), "회색 빛의 간격")

    def test_safe_markdown_features(self) -> None:
        rendered = markdown_to_safe_html(
            "문단\n줄바꿈\n\n**굵게** *기울임*\n\n> 인용\n\n---\n\n- 하나\n- 둘"
        )
        for tag in ("<br", "<strong>", "<em>", "<blockquote>", "<hr", "<ul>", "<li>"):
            self.assertIn(tag, rendered)
        self.assertNotIn("<p", rendered)

    def test_paragraphs_use_css_independent_break_pairs(self) -> None:
        rendered = markdown_to_safe_html("첫 문단입니다.\n\n둘째 문단입니다.")
        self.assertEqual(
            rendered,
            "첫 문단입니다.<br><br>둘째 문단입니다.",
        )
        self.assertEqual(blank_line_separator_count(rendered), 1)
        self.assertNotIn("<p><br></p>", rendered)

    def test_incidental_indentation_and_extra_blank_lines_are_normalized(
        self,
    ) -> None:
        rendered = markdown_to_safe_html(
            " \t첫 문단입니다.  \n\n\n\n    둘째 문단입니다.\n\t셋째 줄입니다."
        )
        self.assertEqual(
            rendered,
            "첫 문단입니다.<br><br>둘째 문단입니다.<br>셋째 줄입니다.",
        )
        self.assertFalse(has_editor_indentation(rendered))
        self.assertEqual(blank_line_separator_count(rendered), 1)

    def test_dangerous_html_is_removed_or_neutralized(self) -> None:
        rendered = markdown_to_safe_html(
            '<script>alert(1)</script><iframe src="https://tracker"></iframe>'
            '<form><input onfocus="steal()"><button>전송</button></form>'
            '<p style="background:url(https://tracker)" onclick="x()">안전</p>'
            '<a href="javascript:alert(1)">링크</a><img src="https://tracker/pixel">'
        )
        lowered = rendered.lower()
        for danger in (
            "<script",
            "<iframe",
            "<form",
            "<input",
            "<button",
            "onclick",
            "onfocus",
            "javascript:",
            "style=",
            "<img",
            "tracker",
        ):
            self.assertNotIn(danger, lowered)
        self.assertIn("안전", rendered)

    def test_path_traversal_absolute_external_and_non_markdown_are_blocked(self) -> None:
        temporary, root = self.make_repository()
        with temporary:
            for path in (
                "../secret.md",
                str((root / "docs" / "episodes" / "012.md").resolve()),
                "README.md",
                "docs/episodes/012.txt",
            ):
                with self.subTest(path=path), self.assertRaises((ValueError, FileNotFoundError)):
                    resolve_episode_path(path, repository_root=root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_symlink_escape_is_blocked(self) -> None:
        temporary, root = self.make_repository()
        with temporary:
            outside = root / "outside.md"
            outside.write_text(episode_text(), encoding="utf-8")
            link = root / "docs" / "episodes" / "999.md"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(ValueError):
                resolve_episode_path(
                    "docs/episodes/999.md", repository_root=root
                )


if __name__ == "__main__":
    unittest.main()
