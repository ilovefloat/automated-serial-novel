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


def validate_novelpia_workflow(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    errors: list[str] = []
    triggers = data.get("on", {})
    if "workflow_dispatch" not in triggers or "workflow_call" not in triggers:
        errors.append("manual and reusable triggers are required")
    if data.get("permissions") != {"contents": "write"}:
        errors.append("workflow permissions must be exactly contents: write")
    if not data.get("concurrency"):
        errors.append("workflow concurrency is required")
    required_fragments = (
        "${{ secrets.NOVELPIA_AUTH_STATE_B64 }}",
        "${{ secrets.GH_SECRET_UPDATE_TOKEN }}",
        "${{ vars.NOVELPIA_EDITOR_URL }}",
        "${{ vars.NOVELPIA_PUBLISH_ENABLED }}",
        "python -m playwright install --with-deps chromium",
        "path: preview/novelpia-editor.png",
        "Remove all temporary authentication material",
    )
    for fragment in required_fragments:
        if fragment not in text:
            errors.append(f"missing required workflow fragment: {fragment}")
    if re.search(r"path:\s*(?:\.|/|secrets/)\s*$", text, re.MULTILINE):
        errors.append("artifact path is too broad or sensitive")
    if re.search(r"gh secret set[^\n]*NOVELPIA_AUTH_STATE_B64[^\n]+\$\{", text):
        errors.append("secret value may be exposed in a command argument")
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
    errors.extend(
        validate_novelpia_workflow(
            ROOT / ".github/workflows/publish-novelpia.yml"
        )
    )

    state = json.loads(
        (ROOT / "state/story_state.json").read_text(encoding="utf-8")
    )
    if not isinstance(state.get("next_episode"), int):
        errors.append("state/story_state.json: next_episode must be an integer")
    narrative_fields = {
        "narrative_mode",
        "narrative_pace",
        "current_scene",
        "current_scene_started_episode",
        "active_arc",
        "supporting_arcs",
        "arc_states",
        "recent_scene_fingerprints",
        "recent_opening_patterns",
        "recent_ending_patterns",
        "recent_locations",
        "recent_character_combinations",
        "recent_conflict_types",
        "recent_social_themes",
        "recent_document_formats",
        "recent_ai_interaction_types",
        "motif_cooldowns",
        "last_major_event_episode",
        "last_major_reveal_episode",
        "unresolved_immediate_actions",
        "withheld_information",
        "planned_long_term_reveals",
        "recurring_symbols",
        "symbol_meaning_history",
        "narrative_repetition_warnings",
    }
    missing_narrative_fields = sorted(narrative_fields - state.keys())
    if missing_narrative_fields:
        errors.append(
            "state/story_state.json: missing narrative fields "
            f"{missing_narrative_fields}"
        )
    if not (ROOT / "prompts/plan.md").is_file():
        errors.append("prompts/plan.md: private planning prompt is required")
    publish_state = json.loads(
        (ROOT / "state/novelpia_publish_state.json").read_text(encoding="utf-8")
    )
    required_publish_fields = {
        "last_success_episode",
        "last_success_at",
        "episode_path",
        "title",
        "published_url",
        "publish_status",
        "last_error_code",
        "session_refresh_status",
        "unknown_result_episodes",
    }
    missing_publish_fields = sorted(required_publish_fields - publish_state.keys())
    if missing_publish_fields:
        errors.append(
            "state/novelpia_publish_state.json: missing fields "
            f"{missing_publish_fields}"
        )
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "secrets/",
        "playwright/.auth/",
        "*.auth.json",
        "novelpia-auth*.json",
        "playwright-report/",
        "test-results/",
    ):
        if pattern not in ignore:
            errors.append(f".gitignore: missing sensitive pattern {pattern}")

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
