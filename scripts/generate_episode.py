from __future__ import annotations

import argparse
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

try:
    from scripts.narrative_control import (
        ARC_NAMES,
        NARRATIVE_PACES,
        active_cooldowns,
        apply_narrative_state_update,
        fingerprint_from_update,
        migrate_story_state,
        narrative_pace_guidance,
        plan_continuity_errors,
        plan_cooldown_violations,
        scene_plan_repetition_report,
        scene_plan_schema,
        text_similarity_report,
        validate_scene_plan,
    )
except ModuleNotFoundError:
    from narrative_control import (  # type: ignore[no-redef]
        ARC_NAMES,
        NARRATIVE_PACES,
        active_cooldowns,
        apply_narrative_state_update,
        fingerprint_from_update,
        migrate_story_state,
        narrative_pace_guidance,
        plan_continuity_errors,
        plan_cooldown_violations,
        scene_plan_repetition_report,
        scene_plan_schema,
        text_similarity_report,
        validate_scene_plan,
    )

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts"
STATE_DIR = ROOT / "state"
EPISODES_DIR = ROOT / "docs" / "episodes"
MODEL_CATALOG_PATH = STATE_DIR / "model_catalog.json"
MIN_PUBLIC_CHARS = 700
MAX_ATTEMPTS = 3
MAX_RESPONSE_ATTEMPTS = 2
MAX_HISTORY_ITEMS = 20
MAX_FACT_ITEMS = 60
PROBE_MAX_OUTPUT_TOKENS = 1

# 이름 목록의 반환 순서와 무관하게, 실제 사용 가능한 모델 중 이 순서로 선택한다.
PREFERRED_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
)
STABLE_PREFERRED_MODELS = PREFERRED_MODELS[:3]
LATEST_PREFERRED_MODELS = PREFERRED_MODELS[3:]
PREVIEW_MODEL_MARKERS = ("preview", "experimental", "-exp")
EXCLUDED_FALLBACK_MARKERS = (
    "embedding",
    "imagen",
    "veo",
    "tts",
    "audio",
    "live",
    "robotics",
    "computer-use",
    "deep-research",
    "lyria",
    "nano-banana",
    "antigravity",
)
MODEL_ACCESS_MESSAGE_MARKERS = (
    "no longer available",
    "not available",
    "not found",
    "permission denied",
    "access denied",
    "access is restricted",
    "does not have access",
    "not supported for generatecontent",
    "not supported for generate_content",
)
MODEL_INCOMPATIBLE_MESSAGE_MARKERS = (
    "model is not supported",
    "model does not support",
    "unsupported model",
    "incompatible model",
    "invalid model",
    "generatecontent is not supported",
    "generate_content is not supported",
    "not supported for generate_content",
)

T = TypeVar("T")


class ModelProbeError(RuntimeError):
    def __init__(
        self,
        message: str,
        failed_models: list[dict[str, Any]],
        succeeded_models: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.failed_models = failed_models
        self.succeeded_models = succeeded_models or []


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


def list_models(client: genai.Client) -> list[dict[str, Any]]:
    """Return the complete model catalog exposed to the current API key."""
    catalog: list[dict[str, Any]] = []
    for model in client.models.list():
        raw_name = getattr(model, "name", None)
        if not raw_name:
            continue
        supported_actions = list(getattr(model, "supported_actions", None) or [])
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


def generation_models(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in catalog
        if "generateContent" in item.get("supported_actions", [])
    ]


def list_generation_models(client: genai.Client) -> list[dict[str, Any]]:
    """Compatibility helper used by tests and callers needing one filtered list."""
    return generation_models(list_models(client))


def is_automatic_text_candidate(name: str) -> bool:
    lowered = name.lower()
    if any(marker in lowered for marker in EXCLUDED_FALLBACK_MARKERS):
        return False
    if re.search(r"gemini-.*-image(?:-|$)", lowered):
        return False
    return True


def fallback_model_key(name: str) -> tuple[int, int, int, str]:
    lowered = name.lower()
    version = re.search(r"gemini-(\d+)(?:\.(\d+))?", lowered)
    major = int(version.group(1)) if version else 0
    minor = int(version.group(2) or 0) if version else 0
    family_rank = 0 if "flash" in lowered else 1 if "pro" in lowered else 2
    return family_rank, -major, -minor, name


def ordered_model_candidates(
    catalog: list[dict[str, Any]], configured: str | None = None
) -> tuple[list[str], list[dict[str, Any]]]:
    generate_names = {item["name"] for item in generation_models(catalog)}
    listed_names = {item["name"] for item in catalog}
    candidates: list[str] = []
    initial_failures: list[dict[str, Any]] = []

    requested = normalize_model_name(configured) if configured and configured.strip() else ""
    if requested:
        if requested in generate_names:
            candidates.append(requested)
        else:
            status = "UNSUPPORTED_ACTION" if requested in listed_names else "NOT_LISTED"
            failure = {
                "name": requested,
                "status_code": status,
                "reason": (
                    "models.list()에 있지만 generateContent를 지원하지 않음"
                    if status == "UNSUPPORTED_ACTION"
                    else "models.list()에 없음"
                ),
            }
            initial_failures.append(failure)
            print(
                f"경고: GEMINI_MODEL {requested!r} 검증 실패 "
                f"({failure['status_code']}): {failure['reason']}; 자동 fallback을 시도합니다.",
                file=sys.stderr,
            )

    automatic = {
        name for name in generate_names if is_automatic_text_candidate(name)
    }
    stable_priority = [
        name for name in STABLE_PREFERRED_MODELS if name in automatic
    ]
    latest_priority = [
        name for name in LATEST_PREFERRED_MODELS if name in automatic
    ]
    priority_names = set(PREFERRED_MODELS)
    other_names = automatic - priority_names
    other_stable = sorted(
        (
            name
            for name in other_names
            if "latest" not in name.lower()
            and not any(marker in name.lower() for marker in PREVIEW_MODEL_MARKERS)
        ),
        key=fallback_model_key,
    )
    other_latest = sorted(
        (
            name
            for name in other_names
            if "latest" in name.lower()
            and not any(marker in name.lower() for marker in PREVIEW_MODEL_MARKERS)
        ),
        key=fallback_model_key,
    )
    preview = sorted(
        (
            name
            for name in other_names
            if any(marker in name.lower() for marker in PREVIEW_MODEL_MARKERS)
        ),
        key=fallback_model_key,
    )

    for name in (
        stable_priority
        + latest_priority
        + other_stable
        + other_latest
        + preview
    ):
        if name not in candidates:
            candidates.append(name)
    return candidates, initial_failures


def error_status_code(exc: BaseException) -> int | None:
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    match = re.search(r"\b(400|403|404|408|429|500|502|503|504)\b", str(exc))
    return int(match.group(1)) if match else None


def safe_error_reason(exc: BaseException, limit: int = 300) -> str:
    reason = " ".join(str(exc).split())
    secret = os.environ.get("GEMINI_API_KEY") or ""
    if secret:
        reason = reason.replace(secret, "[REDACTED]")
    reason = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", reason)
    return reason[:limit] or exc.__class__.__name__


def is_model_unavailable_error(exc: BaseException) -> bool:
    status = error_status_code(exc)
    lowered = str(exc).lower()
    return status in {403, 404} or any(
        marker in lowered for marker in MODEL_ACCESS_MESSAGE_MARKERS
    )


def is_model_incompatible_error(exc: BaseException) -> bool:
    if error_status_code(exc) != 400:
        return False
    lowered = str(exc).lower()
    return any(marker in lowered for marker in MODEL_INCOMPATIBLE_MESSAGE_MARKERS)


def probe_model(
    client: genai.Client,
    model: str,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    call_with_retry(
        lambda: client.models.generate_content(
            model=model,
            contents="Reply OK.",
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
            ),
        ),
        f"모델 probe {model}",
        sleep=sleep,
    )


def probe_and_select_model(
    client: genai.Client,
    catalog: list[dict[str, Any]],
    configured: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    candidates, failures = ordered_model_candidates(catalog, configured)
    transient_failures: list[dict[str, Any]] = []
    if not generation_models(catalog):
        raise ModelProbeError(
            "models.list()에서 generateContent 지원 모델을 찾지 못했습니다.",
            failures,
        )
    if not candidates:
        raise ModelProbeError(
            "소설 텍스트 생성에 적합한 probe 후보를 찾지 못했습니다.",
            failures,
        )

    for model in candidates:
        print(f"모델 probe 시작: {model}")
        try:
            probe_model(client, model, sleep=sleep)
        except Exception as exc:
            status = error_status_code(exc)
            failure = {
                "name": model,
                "status_code": status or "UNKNOWN",
                "reason": safe_error_reason(exc),
            }
            failures.append(failure)
            print(
                f"모델 probe 실패: {model} "
                f"status={failure['status_code']} reason={failure['reason']}",
                file=sys.stderr,
            )
            if is_transient_error(exc):
                transient_failures.append(failure)
                print(
                    f"경고: {model}의 일시적 오류가 제한 재시도 후에도 계속되어 "
                    "이번 실행에서 다음 후보를 probe합니다.",
                    file=sys.stderr,
                )
                continue
            if is_model_unavailable_error(exc) or is_model_incompatible_error(exc):
                if configured and model == normalize_model_name(configured):
                    print(
                        f"경고: GEMINI_MODEL {model!r}을 실제 호출할 수 없어 "
                        "자동 fallback을 시도합니다.",
                        file=sys.stderr,
                    )
                continue
            raise ModelProbeError(
                f"모델 probe 중 복구할 수 없는 오류가 발생했습니다: "
                f"{model} status={status or 'UNKNOWN'}",
                failures,
            ) from exc
        print(f"모델 probe 성공: {model}")
        return model, [model], failures

    if transient_failures:
        details = ", ".join(
            f"{failure['name']} status={failure['status_code']}"
            for failure in transient_failures
        )
        raise ModelProbeError(
            "모든 소설 텍스트 생성 후보 모델의 probe가 실패했습니다. "
            f"일시적 오류가 재시도 후에도 계속된 후보: {details}",
            failures,
        )
    raise ModelProbeError(
        "모든 소설 텍스트 생성 후보 모델의 probe가 실패했습니다.",
        failures,
    )


def model_catalog_document(
    catalog: list[dict[str, Any]],
    selected_model: str | None,
    probe_succeeded_models: list[str],
    probe_failed_models: list[dict[str, Any]],
    checked_at: datetime,
) -> dict[str, Any]:
    generate_catalog = generation_models(catalog)
    return {
        "checked_at": checked_at.isoformat(),
        "listed_models": [item["name"] for item in catalog],
        "generate_content_models": [
            item["name"] for item in generate_catalog
        ],
        "probe_succeeded_models": probe_succeeded_models,
        "probe_failed_models": probe_failed_models,
        "selected_model": selected_model,
        "selection_priority": list(PREFERRED_MODELS),
        "fallback_policy": (
            "configured; preferred stable; preferred latest aliases; "
            "other stable/latest; preview"
        ),
        "model_details": catalog,
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


def recent_episode_records(
    directory: Path = EPISODES_DIR,
    limit: int = 5,
    fingerprints: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    fingerprint_by_episode = {
        item.get("episode"): item for item in (fingerprints or [])
    }
    files = sorted(
        (path for path in directory.glob("*.md") if path.stem.isdigit()),
        key=lambda path: int(path.stem),
    )[-limit:]
    records: list[dict[str, Any]] = []
    for path in files:
        markdown = read_text(path)
        episode_number = int(path.stem)
        title_match = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', markdown, re.M)
        body = re.sub(r"\A---\r?\n.*?\r?\n---\s*", "", markdown, flags=re.DOTALL)
        body = re.sub(r"\A# [^\r\n]+\r?\n+", "", body)
        records.append(
            {
                "episode": episode_number,
                "title": title_match.group(1) if title_match else path.stem,
                "body": body.strip(),
                "fingerprint": fingerprint_by_episode.get(episode_number),
            }
        )
    return records


def recent_episode_summaries(
    state: dict[str, Any], limit: int = 10
) -> list[dict[str, Any]]:
    history = state.get("history", [])
    return history[-limit:] if isinstance(history, list) else []


def compact_prompt_state(state: dict[str, Any]) -> dict[str, Any]:
    omitted = {
        "history",
        "recent_scene_fingerprints",
        "recent_opening_patterns",
        "recent_ending_patterns",
        "recent_locations",
        "recent_character_combinations",
        "recent_conflict_types",
        "recent_social_themes",
        "recent_document_formats",
        "recent_ai_interaction_types",
    }
    return {key: value for key, value in state.items() if key not in omitted}


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
    scalar_fields = (
        "summary",
        "next_episode_pressure",
        "actual_time_range",
        "central_labor",
        "emotional_start",
        "emotional_end",
        "opening_pattern",
        "ending_pattern",
        "core_sentence_structure",
        "next_required_connection",
        "narrative_mode",
        "narrative_pace",
        "narrative_function",
        "twist_type",
    )
    list_fields = (
        "protagonist_changes",
        "new_facts",
        "open_threads",
        "continuity_notes",
        "next_immediate_actions",
        "supporting_arc_progress",
        "revealed_information",
        "withheld_information",
        "planned_long_term_reveals",
        "locations",
        "character_combination",
        "conflict_types",
        "social_themes",
        "document_formats",
        "ai_interaction_types",
        "core_images",
        "repetition_risks",
        "variations_applied",
        "motif_cooldown_updates",
        "new_threads",
        "maintained_threads",
        "resolved_threads",
        "new_questions",
        "symbol_updates",
    )
    for field in scalar_fields:
        if not isinstance(update.get(field), str) or not update[field].strip():
            raise ValueError(f"state_update.{field}는 비어 있지 않은 문자열이어야 합니다.")
    for field in list_fields:
        value = update.get(field)
        if not isinstance(value, list) or not all(
            (
                isinstance(item, str) and item.strip()
                if field not in {"supporting_arc_progress", "symbol_updates"}
                else isinstance(item, dict)
            )
            for item in value
        ):
            raise ValueError(f"state_update.{field}의 배열 형식이 잘못되었습니다.")
    for field in ("direct_continuation", "scene_completed", "major_event", "major_reveal"):
        if not isinstance(update.get(field), bool):
            raise ValueError(f"state_update.{field}는 boolean이어야 합니다.")
    if not isinstance(update.get("current_scene"), str):
        raise ValueError("state_update.current_scene은 문자열이어야 합니다.")
    if not update["scene_completed"] and not update["current_scene"].strip():
        raise ValueError("진행 중 장면에는 current_scene이 필요합니다.")
    if update["narrative_pace"] not in NARRATIVE_PACES:
        raise ValueError("state_update.narrative_pace가 허용된 값이 아닙니다.")

    arc_progress_fields = (
        "arc",
        "stage",
        "change",
        "next_possible_change",
        "current_pressure",
        "long_term_outcome",
    )
    progress_items = [update.get("primary_arc_progress")] + update[
        "supporting_arc_progress"
    ]
    if not isinstance(progress_items[0], dict):
        raise ValueError("state_update.primary_arc_progress는 객체여야 합니다.")
    if len(update["supporting_arc_progress"]) > 2:
        raise ValueError("보조 아크 진행은 최대 2개여야 합니다.")
    for progress in progress_items:
        if not isinstance(progress, dict):
            raise ValueError("아크 진행 항목은 객체여야 합니다.")
        if progress.get("arc") not in ARC_NAMES:
            raise ValueError("등록되지 않은 장기 아크 진행이 있습니다.")
        for field in arc_progress_fields:
            if not isinstance(progress.get(field), str) or not progress[field].strip():
                raise ValueError(f"아크 진행의 {field}가 유효하지 않습니다.")
        if not isinstance(progress.get("on_hold"), bool):
            raise ValueError("아크 진행의 on_hold는 boolean이어야 합니다.")
        withheld = progress.get("withheld_information")
        if not isinstance(withheld, list) or not all(
            isinstance(item, str) and item.strip() for item in withheld
        ):
            raise ValueError("아크 진행의 withheld_information 형식이 잘못되었습니다.")
    if update["primary_arc_progress"]["arc"] in {
        progress["arc"] for progress in update["supporting_arc_progress"]
    }:
        raise ValueError("주요 아크와 보조 아크가 중복됩니다.")

    for symbol in update["symbol_updates"]:
        if not all(
            isinstance(symbol.get(field), str) and symbol[field].strip()
            for field in ("symbol", "meaning", "development")
        ):
            raise ValueError("상징 갱신 형식이 잘못되었습니다.")
    return update


def validate_update_against_plan(
    update: dict[str, Any], plan: dict[str, Any]
) -> None:
    if update["direct_continuation"] != plan["direct_continuation"]:
        raise ValueError("본문 상태와 계획의 직접 연속 여부가 다릅니다.")
    if update["primary_arc_progress"]["arc"] != plan["active_arc"]:
        raise ValueError("본문 상태와 계획의 주요 아크가 다릅니다.")
    if {
        progress["arc"] for progress in update["supporting_arc_progress"]
    } != set(plan["supporting_arcs"]):
        raise ValueError("본문 상태와 계획의 보조 아크가 다릅니다.")
    if update["narrative_mode"] != plan["narrative_mode"]:
        raise ValueError("본문 상태와 계획의 서술 형식이 다릅니다.")
    if update["narrative_pace"] != plan["narrative_pace"]:
        raise ValueError("본문 상태와 계획의 서사 속도가 다릅니다.")
    if update["narrative_function"] != plan["narrative_function"]:
        raise ValueError("본문 상태와 계획의 서사 기능이 다릅니다.")
    if not set(update["motif_cooldown_updates"]).issubset(
        set(plan["motifs_used"])
    ):
        raise ValueError("계획에 없던 냉각 소재가 본문 상태에 추가되었습니다.")


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
    scene_plan: dict[str, Any],
    similarity_report: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    validate_update_against_plan(update, scene_plan)
    merged = migrate_story_state(state)
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
            "narrative_pace": update["narrative_pace"],
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
    resolved = set(update["resolved_threads"])
    existing_threads = [
        thread for thread in merged.get("open_threads", []) if thread not in resolved
    ]
    merged["open_threads"] = list(
        dict.fromkeys(
            existing_threads
            + update["maintained_threads"]
            + update["new_threads"]
            + update["open_threads"]
        )
    )[-50:]
    merged["continuity_notes"] = update["continuity_notes"]
    merged["next_episode_pressure"] = update["next_episode_pressure"]
    merged["next_episode"] = episode_number + 1
    merged["last_generated_at"] = (generated_at or utc_now()).isoformat()
    return apply_narrative_state_update(
        merged,
        update,
        episode_number,
        similarity_report=similarity_report,
    )


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
    status = error_status_code(exc)
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
    string_array = {"type": "array", "items": {"type": "string"}}
    arc_progress = {
        "type": "object",
        "required": [
            "arc",
            "stage",
            "change",
            "next_possible_change",
            "withheld_information",
            "current_pressure",
            "long_term_outcome",
            "on_hold",
        ],
        "properties": {
            "arc": {"type": "string", "enum": list(ARC_NAMES)},
            "stage": {"type": "string"},
            "change": {"type": "string"},
            "next_possible_change": {"type": "string"},
            "withheld_information": {
                "type": "array",
                "items": {"type": "string"},
            },
            "current_pressure": {"type": "string"},
            "long_term_outcome": {"type": "string"},
            "on_hold": {"type": "boolean"},
        },
    }
    state_properties: dict[str, Any] = {
        field: {"type": "string"}
        for field in (
            "summary",
            "next_episode_pressure",
            "actual_time_range",
            "current_scene",
            "central_labor",
            "emotional_start",
            "emotional_end",
            "opening_pattern",
            "ending_pattern",
            "core_sentence_structure",
            "next_required_connection",
            "narrative_mode",
            "narrative_function",
            "twist_type",
        )
    }
    state_properties["narrative_pace"] = {
        "type": "string",
        "enum": list(NARRATIVE_PACES),
    }
    for field in (
        "protagonist_changes",
        "new_facts",
        "open_threads",
        "continuity_notes",
        "next_immediate_actions",
        "revealed_information",
        "withheld_information",
        "planned_long_term_reveals",
        "locations",
        "character_combination",
        "conflict_types",
        "social_themes",
        "document_formats",
        "ai_interaction_types",
        "core_images",
        "repetition_risks",
        "variations_applied",
        "motif_cooldown_updates",
        "new_threads",
        "maintained_threads",
        "resolved_threads",
        "new_questions",
    ):
        state_properties[field] = string_array
    for field in (
        "direct_continuation",
        "scene_completed",
        "major_event",
        "major_reveal",
    ):
        state_properties[field] = {"type": "boolean"}
    state_properties["primary_arc_progress"] = arc_progress
    state_properties["supporting_arc_progress"] = {
        "type": "array",
        "items": arc_progress,
    }
    state_properties["symbol_updates"] = {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["symbol", "meaning", "development"],
            "properties": {
                "symbol": {"type": "string"},
                "meaning": {"type": "string"},
                "development": {"type": "string"},
            },
        },
    }
    return {
        "type": "object",
        "required": ["public_markdown", "state_update"],
        "properties": {
            "public_markdown": {"type": "string"},
            "state_update": {
                "type": "object",
                "required": list(state_properties),
                "properties": state_properties,
            },
        },
    }


def build_prompt(
    state: dict[str, Any],
    episode_number: int,
    scene_plan: dict[str, Any],
) -> str:
    template = read_text(PROMPTS / "episode.md")
    return template.format(
        episode_number=episode_number,
        story_state=json.dumps(
            compact_prompt_state(state),
            ensure_ascii=False,
            indent=2,
        ),
        continuity=read_text(STATE_DIR / "continuity.md"),
        recent_episodes=recent_episodes(),
        scene_plan=json.dumps(scene_plan, ensure_ascii=False, indent=2),
    )


def build_plan_prompt(
    state: dict[str, Any],
    episode_number: int,
    feedback: str = "(첫 계획: 추가 피드백 없음)",
) -> str:
    template = read_text(PROMPTS / "plan.md")
    return template.format(
        episode_number=episode_number,
        story_state=json.dumps(
            compact_prompt_state(state),
            ensure_ascii=False,
            indent=2,
        ),
        recent_fingerprints=json.dumps(
            state.get("recent_scene_fingerprints", []),
            ensure_ascii=False,
            indent=2,
        ),
        active_cooldowns=json.dumps(
            active_cooldowns(state),
            ensure_ascii=False,
            indent=2,
        ),
        recent_summaries=json.dumps(
            recent_episode_summaries(state),
            ensure_ascii=False,
            indent=2,
        ),
        pace_guidance=narrative_pace_guidance(state),
        feedback=feedback,
    )


def parse_json_object(raw: str, label: str) -> dict[str, Any]:
    if not raw or not raw.strip():
        raise ValueError(f"{label} 응답이 비어 있습니다.")
    try:
        payload = json.loads(strip_outer_fence(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON 파싱 실패: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 응답은 JSON 객체여야 합니다.")
    return payload


def generate_scene_plan(
    client: genai.Client,
    model: str,
    state: dict[str, Any],
    episode_number: int,
    feedback: str = "(첫 계획: 추가 피드백 없음)",
) -> dict[str, Any]:
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=model,
            contents=build_plan_prompt(state, episode_number, feedback),
            config=types.GenerateContentConfig(
                temperature=0.65,
                top_p=0.9,
                max_output_tokens=2048,
                response_mime_type="application/json",
                response_json_schema=scene_plan_schema(),
            ),
        ),
        "비공개 장면 계획 생성",
    )
    try:
        raw = response.text or ""
    except (AttributeError, ValueError) as exc:
        raise ValueError("장면 계획 응답에서 텍스트를 읽을 수 없습니다.") from exc
    return validate_scene_plan(parse_json_object(raw, "장면 계획"))


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


def plan_quality_report(
    plan: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    repetition = scene_plan_repetition_report(
        plan,
        state.get("recent_scene_fingerprints", []),
    )
    return {
        "repetition": repetition,
        "continuity_errors": plan_continuity_errors(plan, state),
        "cooldown_violations": plan_cooldown_violations(plan, state),
    }


def plan_needs_revision(report: dict[str, Any]) -> bool:
    return bool(
        report["repetition"].get("too_similar")
        or report["continuity_errors"]
        or report["cooldown_violations"]
    )


def revision_feedback(
    report: dict[str, Any],
    text_report: dict[str, Any] | None = None,
) -> str:
    instructions = {
        "plan_repetition": report["repetition"],
        "continuity_errors": report["continuity_errors"],
        "cooldown_violations": report["cooldown_violations"],
        "text_similarity": text_report or {},
        "required_revision": (
            "직접 연속 조건을 지키면서 시작 방식, 장소의 서사 기능, 인물 조합, "
            "갈등, 문서 형식, 종료 방식 중 하나 이상을 실질적으로 바꿀 것. "
            "사회비판 설명은 구체적 행동과 결과로 바꿀 것."
        ),
    }
    return json.dumps(instructions, ensure_ascii=False, indent=2)


def generate_episode_from_plan(
    client: genai.Client,
    model: str,
    state: dict[str, Any],
    episode_number: int,
    scene_plan: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    system_prompt = read_text(PROMPTS / "system.md")
    user_prompt = build_prompt(state, episode_number, scene_plan)
    for response_attempt in range(1, MAX_RESPONSE_ATTEMPTS + 1):
        raw = generate_raw_response(client, model, system_prompt, user_prompt)
        try:
            title, body, update = parse_generated_episode(raw)
            validate_update_against_plan(update, scene_plan)
            return title, body, update
        except ValueError:
            if response_attempt == MAX_RESPONSE_ATTEMPTS:
                raise
            print(
                "응답 형식 또는 계획 일치 검증 실패, 본문 생성을 한 번 더 시도합니다.",
                file=sys.stderr,
            )
    raise AssertionError("unreachable")


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
    parser.add_argument(
        "--result-json",
        type=Path,
        help="생성된 정확한 에피소드 경로와 번호를 기록할 JSON 파일",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 2

    source_state_path = STATE_DIR / "story_state.json"
    state = migrate_story_state(load_json(source_state_path))
    episode_number = calculate_next_episode(state, EPISODES_DIR.glob("*.md"))
    target_episodes, target_state, target_catalog = output_paths(args.preview_dir)
    output_path = target_episodes / f"{episode_number:03d}.md"
    if output_path.exists():
        raise FileExistsError(f"에피소드 파일이 이미 존재합니다: {output_path}")

    client = genai.Client(api_key=api_key)
    catalog = call_with_retry(
        lambda: list_models(client),
        "모델 목록 조회",
    )
    generated_at = utc_now()
    listed_names = [item["name"] for item in catalog]
    generate_names = [item["name"] for item in generation_models(catalog)]
    print(f"models.list() 전체 모델 {len(listed_names)}개: {', '.join(listed_names)}")
    print(
        f"generateContent 지원 모델 {len(generate_names)}개: "
        f"{', '.join(generate_names)}"
    )
    try:
        model, probe_succeeded, probe_failed = probe_and_select_model(
            client,
            catalog,
            os.environ.get("GEMINI_MODEL"),
        )
    except ModelProbeError as exc:
        failed_catalog = model_catalog_document(
            catalog,
            None,
            exc.succeeded_models,
            exc.failed_models,
            generated_at,
        )
        atomic_write_text(
            target_catalog,
            json.dumps(failed_catalog, ensure_ascii=False, indent=2) + "\n",
        )
        raise
    catalog_document = model_catalog_document(
        catalog,
        model,
        probe_succeeded,
        probe_failed,
        generated_at,
    )
    atomic_write_text(
        target_catalog,
        json.dumps(catalog_document, ensure_ascii=False, indent=2) + "\n",
    )
    print(f"선택 모델: {model}")

    recent_records = recent_episode_records(
        fingerprints=state.get("recent_scene_fingerprints", []),
    )
    scene_plan = generate_scene_plan(
        client,
        model,
        state,
        episode_number,
    )
    plan_report = plan_quality_report(scene_plan, state)
    quality_retry_used = False
    if plan_needs_revision(plan_report):
        quality_retry_used = True
        print(
            "장면 계획에서 연속성·냉각 기간·반복 위험을 감지해 한 번 재계획합니다."
        )
        scene_plan = generate_scene_plan(
            client,
            model,
            state,
            episode_number,
            revision_feedback(plan_report),
        )
        plan_report = plan_quality_report(scene_plan, state)
        if plan_needs_revision(plan_report):
            raise ValueError(
                "재계획 후에도 연속성·냉각 기간·반복 위험 조건을 충족하지 못했습니다."
            )

    title, body, update = generate_episode_from_plan(
        client,
        model,
        state,
        episode_number,
        scene_plan,
    )
    fingerprint = fingerprint_from_update(update, episode_number)
    similarity = text_similarity_report(
        title,
        body,
        recent_records,
        fingerprint=fingerprint,
    )
    print(
        f"최근 원고 최대 유사도: {similarity['max_score']:.4f} "
        f"(비교 화: {similarity['similar_episode']})"
    )
    if similarity["too_similar"]:
        if quality_retry_used:
            raise ValueError(
                "허용된 품질 재시도를 이미 사용했고 생성 원고의 반복 위험이 높습니다."
            )
        quality_retry_used = True
        print("생성 원고의 반복 위험이 높아 한 번 재계획하고 재생성합니다.")
        scene_plan = generate_scene_plan(
            client,
            model,
            state,
            episode_number,
            revision_feedback(plan_report, similarity),
        )
        plan_report = plan_quality_report(scene_plan, state)
        if plan_needs_revision(plan_report):
            raise ValueError("유사도 재계획이 서사 제약을 충족하지 못했습니다.")
        title, body, update = generate_episode_from_plan(
            client,
            model,
            state,
            episode_number,
            scene_plan,
        )
        fingerprint = fingerprint_from_update(update, episode_number)
        similarity = text_similarity_report(
            title,
            body,
            recent_records,
            fingerprint=fingerprint,
        )
        print(f"재생성 후 최근 원고 최대 유사도: {similarity['max_score']:.4f}")
        if similarity["too_similar"]:
            raise ValueError("재생성 후에도 최근 원고와 지나치게 유사합니다.")

    new_state = merge_state(
        state,
        update,
        episode_number,
        title,
        scene_plan,
        similarity_report=similarity,
        generated_at=generated_at,
    )

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
    if args.result_json is not None:
        try:
            reported_path = output_path.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            reported_path = str(output_path.resolve())
        atomic_write_text(
            args.result_json,
            json.dumps(
                {
                    "episode": episode_number,
                    "episode_path": reported_path,
                    "mode": mode,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    print(f"생성 완료 ({mode}): {output_path}")
    print(f"제목: {title}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        message = safe_error_reason(exc, limit=2000)
        print(f"{exc.__class__.__name__}: {message}", file=sys.stderr)
        raise SystemExit(1)
