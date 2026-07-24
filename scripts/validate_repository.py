from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class HtmlValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.errors: list[str] = []
        self.has_lang = False
        self.has_viewport = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if tag == "html" and values.get("lang") == "ko":
            self.has_lang = True
        if tag == "meta" and values.get("name") == "viewport":
            self.has_viewport = True
        element_id = values.get("id")
        if element_id:
            if element_id in self.ids:
                self.errors.append(f"duplicate id: {element_id}")
            self.ids.add(element_id)
        for attribute in ("src", "href"):
            value = values.get(attribute) or ""
            if value.startswith(("http://", "https://", "//")):
                self.errors.append(f"external dependency: {value}")


def validate_html(path: Path) -> list[str]:
    validator = HtmlValidator()
    validator.feed(path.read_text(encoding="utf-8"))
    if not validator.has_lang:
        validator.errors.append("missing html lang=ko")
    if not validator.has_viewport:
        validator.errors.append("missing viewport")
    return [f"{path}: {error}" for error in validator.errors]


def validate_workflow(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    errors: list[str] = []
    triggers = data.get("on", {})
    if "workflow_dispatch" not in triggers or "schedule" not in triggers:
        errors.append("manual and scheduled triggers are required")
    if data.get("permissions") != {"contents": "write"}:
        errors.append("workflow permissions must be exactly contents: write")
    if not data.get("concurrency"):
        errors.append("workflow concurrency is required")
    if "${{ secrets.GEMINI_API_KEY }}" not in text:
        errors.append("GEMINI_API_KEY must use the repository secret")
    if "git push origin HEAD:main" not in text:
        errors.append("workflow must explicitly push origin/main")
    return [f"{path}: {error}" for error in errors]


def main() -> int:
    errors: list[str] = []
    for relative in (
        "docs/index.html",
        "docs/index.template.html",
        "docs/episode.html",
    ):
        errors.extend(validate_html(ROOT / relative))
    errors.extend(validate_workflow(ROOT / ".github/workflows/generate.yml"))

    state = json.loads(
        (ROOT / "state/story_state.json").read_text(encoding="utf-8")
    )
    if not isinstance(state.get("next_episode"), int):
        errors.append("state/story_state.json: next_episode must be an integer")

    source_roots = (
        ROOT / ".github",
        ROOT / "docs",
        ROOT / "prompts",
        ROOT / "scripts",
        ROOT / "state",
        ROOT / "tests",
    )
    source_files = (
        path
        for source_root in source_roots
        for path in source_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    tracked_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in source_files
    )
    if re.search(r"AIza[0-9A-Za-z_-]{20,}", tracked_text):
        errors.append("possible Gemini API key found in repository")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Repository validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
