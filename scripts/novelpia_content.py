from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import markdown
import nh3
import yaml

ROOT = Path(__file__).resolve().parents[1]
EPISODES_DIR = ROOT / "docs" / "episodes"
FRONT_MATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
HEADING = re.compile(r"^\s*#\s+(.+?)\s*$")
EPISODE_PREFIX = re.compile(
    r"^\s*(?:\[\s*)?(?:제\s*)?\d+\s*(?:화|회)(?:\s*\])?"
    r"\s*[.:\-–—]?\s*",
    re.IGNORECASE,
)
ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "blockquote",
    "hr",
    "ul",
    "ol",
    "li",
}
DROP_CONTENT_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "svg",
    "math",
    "template",
}


@dataclass(frozen=True)
class EpisodeContent:
    episode: int
    title: str
    date: str
    markdown_body: str
    html_body: str
    plain_text: str
    path: Path
    repository_path: str


class _PlainTextParser(HTMLParser):
    block_tags = {"p", "blockquote", "li", "hr", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag in self.block_tags and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_text(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def html_to_plain_text(value: str) -> str:
    parser = _PlainTextParser()
    parser.feed(value)
    return normalize_text("".join(parser.parts))


def blank_line_separator_count(value: str) -> int:
    return len(
        re.findall(
            r"<br\s*/?>\s*<br\s*/?>",
            value,
            flags=re.IGNORECASE,
        )
    )


def has_editor_indentation(value: str) -> bool:
    return bool(
        re.search(
            r"(?:^|<br\s*/?>)\s*(?:&nbsp;|\u00a0){2,}",
            value,
            flags=re.IGNORECASE,
        )
    )


def strip_episode_prefix(title: str) -> str:
    cleaned = EPISODE_PREFIX.sub("", title, count=1).strip()
    return cleaned or title.strip()


def resolve_episode_path(
    requested_path: str | Path,
    *,
    repository_root: Path = ROOT,
    episodes_dir: Path | None = None,
) -> Path:
    raw = str(requested_path).strip()
    if not raw:
        raise ValueError("episode_path가 비어 있습니다.")
    candidate_input = Path(raw)
    if candidate_input.is_absolute():
        raise ValueError("절대 episode_path는 허용되지 않습니다.")
    if any(part == ".." for part in candidate_input.parts):
        raise ValueError("episode_path의 상위 디렉터리 이동은 허용되지 않습니다.")
    if candidate_input.suffix.lower() != ".md":
        raise ValueError("Markdown 에피소드 파일만 허용됩니다.")

    root = repository_root.resolve()
    allowed = (episodes_dir or root / "docs" / "episodes").resolve()
    candidate = (root / candidate_input).resolve(strict=True)
    try:
        candidate.relative_to(allowed)
    except ValueError as exc:
        raise ValueError("docs/episodes 외부 파일은 허용되지 않습니다.") from exc
    if not candidate.is_file():
        raise ValueError("episode_path가 파일이 아닙니다.")
    return candidate


def markdown_to_safe_html(markdown_body: str) -> str:
    # Generated prose occasionally contains incidental leading spaces, tabs,
    # trailing spaces, or several blank lines. In a contenteditable/Summernote
    # pipeline those can become code blocks, indentation, or oversized gaps.
    normalized_lines = [
        line.strip()
        for line in markdown_body.replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
    ]
    normalized_markdown = re.sub(
        r"\n{3,}",
        "\n\n",
        "\n".join(normalized_lines),
    ).strip()
    rendered = markdown.markdown(
        normalized_markdown,
        extensions=["nl2br", "sane_lists"],
        output_format="html",
    )
    cleaned = nh3.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes={},
        url_schemes=set(),
        clean_content_tags=DROP_CONTENT_TAGS,
        link_rel=None,
    )
    # Novelpia applies its own paragraph margins and indentation after
    # Summernote submission. Empty <p><br></p> spacer blocks therefore produce
    # inconsistent gaps. Flatten prose paragraphs to explicit <br><br>
    # separators so editor and viewer CSS cannot add a second layout layer.
    compatible = re.sub(
        r"</p>\s*(?=<(?:blockquote|hr|ul|ol)(?:\s|>|/))",
        "</p><br><br>",
        cleaned,
        flags=re.IGNORECASE,
    )
    compatible = re.sub(
        r"(</(?:blockquote|ul|ol)>|<hr\s*/?>)\s*"
        r"(?=<(?:p|blockquote|hr|ul|ol)(?:\s|>|/))",
        r"\1<br><br>",
        compatible,
        flags=re.IGNORECASE,
    )
    compatible = re.sub(
        r"</p>\s*<p(?:\s[^>]*)?>",
        "<br><br>",
        compatible,
        flags=re.IGNORECASE,
    )
    compatible = re.sub(
        r"</?p(?:\s[^>]*)?>",
        "",
        compatible,
        flags=re.IGNORECASE,
    )
    compatible = re.sub(r"\s*\n\s*", "", compatible)
    compatible = re.sub(r">\s+<", "><", compatible)
    compatible = compatible.replace("&nbsp;", " ").replace("\u00a0", " ")
    return compatible.strip()


def _front_matter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    match = FRONT_MATTER.match(text)
    if not match:
        raise ValueError(f"YAML front matter가 없습니다: {path}")
    try:
        values = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML front matter가 잘못되었습니다: {path}") from exc
    if not isinstance(values, dict):
        raise ValueError(f"YAML front matter는 객체여야 합니다: {path}")
    return values, text[match.end() :]


def parse_episode_markdown(
    requested_path: str | Path,
    *,
    repository_root: Path = ROOT,
    episodes_dir: Path | None = None,
) -> EpisodeContent:
    path = resolve_episode_path(
        requested_path,
        repository_root=repository_root,
        episodes_dir=episodes_dir,
    )
    text = path.read_text(encoding="utf-8")
    values, public = _front_matter(text, path)
    try:
        episode = int(values["episode"])
        date = str(values["date"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("episode 또는 date front matter가 잘못되었습니다.") from exc
    if episode <= 0 or episode != int(path.stem):
        raise ValueError("파일명과 episode 번호가 일치하지 않습니다.")

    lines = public.lstrip().splitlines()
    heading_title = ""
    if lines:
        match = HEADING.match(lines[0])
        if match:
            heading_title = match.group(1)
            lines = lines[1:]
    raw_title = str(values.get("title", "")).strip() or heading_title.strip()
    title = strip_episode_prefix(raw_title)
    if not title:
        raise ValueError("에피소드 제목이 비어 있습니다.")

    markdown_body = "\n".join(lines).strip()
    if not markdown_body:
        raise ValueError("공개 본문이 비어 있습니다.")
    safe_html = markdown_to_safe_html(markdown_body)
    plain_text = html_to_plain_text(safe_html)
    if not plain_text:
        raise ValueError("안전한 HTML 변환 후 본문이 비었습니다.")
    repository_path = path.relative_to(repository_root.resolve()).as_posix()
    return EpisodeContent(
        episode=episode,
        title=title,
        date=date,
        markdown_body=markdown_body,
        html_body=safe_html,
        plain_text=plain_text,
        path=path,
        repository_path=repository_path,
    )
