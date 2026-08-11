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
from datetime import datetime, timedelta, timezone
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
BODY_GENERATION_ATTEMPTS = 6
MAX_BODY_MODEL_CANDIDATES = 4
MAX_RESPONSE_ATTEMPTS = 3
MAX_PLAN_RESPONSE_ATTEMPTS = 3
GENERATION_MAX_OUTPUT_TOKENS = 16384
MAX_HISTORY_ITEMS = 20
MAX_FACT_ITEMS = 60
PROBE_MAX_OUTPUT_TOKENS = 1
KST = timezone(timedelta(hours=9), name="KST")

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


def log(message: str) -> None:
    """Emit ordered progress output even when Actions captures a pipe."""
    print(message, flush=True)


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


def generated_on_kst_date(
    state: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    raw = state.get("last_generated_at")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        generated_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return generated_at.astimezone(KST).date() == current.astimezone(KST).date()


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
            log(
                f"경고: GEMINI_MODEL {requested!r} 검증 실패 "
                f"({failure['status_code']}): {failure['reason']}; 자동 fallback을 시도합니다."
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
    raw_message = getattr(exc, "message", None)
    reason = " ".join(
        str(raw_message if isinstance(raw_message, str) else exc).split()
    )
    # google-genai exceptions can stringify the entire response dictionary.
    # Keep only a short, human-readable message instead of response bodies.
    message_match = re.search(
        r"['\"]message['\"]\s*:\s*['\"]([^'\"]+)", reason
    )
    if message_match:
        reason = message_match.group(1)
    elif reason.startswith(("{", "[")):
        reason = exc.__class__.__name__
    reason = re.sub(r"\s*\{.*$", "", reason)
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
        log(f"모델 probe 시작: {model}")
        try:
            probe_model(client, model, sleep=sleep)
        except Exception as exc:
            status = error_status_code(exc)
            failure = {
                "name": model,
                "status_code": status or "UNKNOWN",
                "reason": safe_error_reason(exc),
                "transient": is_transient_error(exc),
            }
            failures.append(failure)
            log(
                f"모델 probe 실패: {model} "
                f"status={failure['status_code']} reason={failure['reason']}"
            )
            if is_transient_error(exc):
                transient_failures.append(failure)
                log(
                    f"경고: {model}의 일시적 오류가 제한 재시도 후에도 계속되어 "
                    "이번 실행에서 다음 후보를 probe합니다."
                )
                continue
            if is_model_unavailable_error(exc) or is_model_incompatible_error(exc):
                if configured and model == normalize_model_name(configured):
                    log(
                        f"경고: GEMINI_MODEL {model!r}을 실제 호출할 수 없어 "
                        "자동 fallback을 시도합니다."
                    )
                continue
            raise ModelProbeError(
                f"모델 probe 중 복구할 수 없는 오류가 발생했습니다: "
                f"{model} status={status or 'UNKNOWN'}",
                failures,
            ) from exc
        log(f"모델 probe 성공: {model}")
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
    generation_failed_models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    generate_catalog = generation_models(catalog)
    return {
        "checked_at": checked_at.isoformat(),
        "models": copy.deepcopy(catalog),
        "listed_models": [item["name"] for item in catalog],
        "generate_content_models": [
            item["name"] for item in generate_catalog
        ],
        "probe_succeeded_models": probe_succeeded_models,
        "probe_failed_models": probe_failed_models,
        "generation_failed_models": generation_failed_models or [],
        "selected_model": selected_model,
        "selection_priority": list(PREFERRED_MODELS),
        "fallback_policy": (
            "configured; preferred stable; preferred latest aliases; "
            "other stable/latest; preview"
        ),
        "model_details": catalog,
    }


def log_model_catalog_summary(
    catalog: list[dict[str, Any]],
    candidates: list[str],
    debug: bool = False,
) -> None:
    listed_names = [item["name"] for item in catalog]
    generate_names = [item["name"] for item in generation_models(catalog)]
    log(
        f"models.list() 결과: 전체 {len(listed_names)}개, "
        f"generateContent {len(generate_names)}개"
    )
    log(f"후보 모델: {', '.join(candidates)}")
    if debug:
        log(f"debug 전체 모델: {', '.join(listed_names)}")
        log(f"debug generateContent 모델: {', '.join(generate_names)}")


def body_model_candidates(
    candidates: Iterable[str],
    selected_model: str,
    probe_failures: Iterable[dict[str, Any]],
) -> list[str]:
    """Keep transient probe failures eligible for later generation fallback."""
    permanently_failed = {
        failure["name"]
        for failure in probe_failures
        if not failure.get("transient", False)
    }
    return [selected_model] + [
        candidate
        for candidate in candidates
        if candidate != selected_model and candidate not in permanently_failed
    ]


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


def _progress_fallback(arc: str, plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "arc": arc,
        "stage": "계획에 따른 진행",
        "change": str(plan["arc_impact"]),
        "next_possible_change": str(plan["expected_ending_point"]),
        "withheld_information": list(plan["withhold_information"]),
        "current_pressure": str(plan["central_scene"]),
        "long_term_outcome": str(plan["arc_impact"]),
        "on_hold": False,
    }


def normalize_update_from_plan(
    update: Any, plan: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Overlay plan-owned structural metadata onto a body state update."""
    if not isinstance(update, dict):
        raise ValueError("state_update는 JSON 객체여야 합니다.")
    normalized = copy.deepcopy(update)
    warnings: list[str] = []

    raw_primary = normalized.get("primary_arc_progress")
    raw_supporting = normalized.get("supporting_arc_progress")
    candidates = [
        progress
        for progress in (
            [raw_primary] if isinstance(raw_primary, dict) else []
        )
        + (raw_supporting if isinstance(raw_supporting, list) else [])
        if isinstance(progress, dict)
    ]

    def choose_progress(arc: str, used: set[int]) -> dict[str, Any]:
        for progress in candidates:
            if id(progress) not in used and progress.get("arc") == arc:
                used.add(id(progress))
                result = copy.deepcopy(progress)
                result["arc"] = arc
                return result
        for progress in candidates:
            if id(progress) not in used:
                used.add(id(progress))
                result = _progress_fallback(arc, plan)
                for field, value in progress.items():
                    if field != "arc" and value not in (None, "", []):
                        result[field] = copy.deepcopy(value)
                result["arc"] = arc
                return result
        return _progress_fallback(arc, plan)

    body_primary = (
        raw_primary.get("arc") if isinstance(raw_primary, dict) else None
    )
    body_supporting = [
        progress.get("arc")
        for progress in raw_supporting or []
        if isinstance(progress, dict)
    ] if isinstance(raw_supporting, list) else []
    if body_primary != plan["active_arc"]:
        warnings.append(
            "primary arc metadata differs or is missing "
            f"(body={body_primary!r}, plan={plan['active_arc']!r})"
        )
    if set(body_supporting) != set(plan["supporting_arcs"]):
        warnings.append(
            "supporting arc metadata differs, is reordered, or is missing"
        )

    used_progress: set[int] = set()
    normalized["primary_arc_progress"] = choose_progress(
        str(plan["active_arc"]), used_progress
    )
    normalized["supporting_arc_progress"] = [
        choose_progress(str(arc), used_progress)
        for arc in plan["supporting_arcs"]
    ]

    plan_fields: dict[str, Any] = {
        "locations": [plan["location"]],
        "character_combination": list(plan["main_characters"]),
        "central_labor": plan["central_labor"],
        "conflict_types": [plan["conflict_type"]],
        "social_themes": [plan["social_theme"]],
        "document_formats": [plan["document_format"]],
        "ai_interaction_types": [plan["ai_interaction_type"]],
        "narrative_mode": plan["narrative_mode"],
        "narrative_pace": plan["narrative_pace"],
        "narrative_function": plan["narrative_function"],
        "opening_pattern": plan["opening_pattern"],
        "ending_pattern": plan["ending_pattern"],
        "direct_continuation": plan["direct_continuation"],
        "motif_cooldown_updates": list(plan["motifs_used"]),
    }
    for field, planned_value in plan_fields.items():
        body_value = normalized.get(field)
        if body_value != planned_value:
            warnings.append(
                f"{field} metadata differs or is missing; plan value applied"
            )
        normalized[field] = copy.deepcopy(planned_value)
    return normalized, warnings


def validate_update_against_plan(
    update: dict[str, Any], plan: dict[str, Any]
) -> list[str]:
    """Return metadata warnings; plan/body string differences are non-fatal."""
    _, warnings = normalize_update_from_plan(update, plan)
    return warnings


def parse_response(
    raw: str, scene_plan: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
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
    update: Any = payload.get("state_update")
    if scene_plan is not None:
        update, warnings = normalize_update_from_plan(update, scene_plan)
        for warning in warnings:
            log(f"메타데이터 경고: {warning}")
    return public_text, validate_state_update(update)


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
    update, metadata_warnings = normalize_update_from_plan(update, scene_plan)
    update = validate_state_update(update)
    for warning in metadata_warnings:
        log(f"상태 병합 메타데이터 경고: {warning}")
    log("상태 병합: 계획의 구조 메타데이터를 authoritative source로 적용")
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


def server_retry_delay(exc: BaseException, maximum: int = 120) -> int | None:
    """Read Gemini's retry hint without trusting an unbounded server delay."""
    message = safe_error_reason(exc, limit=2000).lower()
    match = re.search(
        r"(?:retry\s+in\s*|retrydelay['\"\s:]+)([0-9]+(?:\.[0-9]+)?)s",
        message,
    )
    if not match:
        return None
    # Add a one-second margin so a 59.9s quota window has actually reset.
    return min(maximum, max(1, int(float(match.group(1))) + 1))


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
            delay = max(
                2 ** (attempt - 1),
                server_retry_delay(exc) or 0,
            )
            log(
                f"{label} 일시 오류, {delay}초 후 재시도 "
                f"({attempt}/{attempts - 1})"
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
    plan_owned_optional = {
        "primary_arc_progress",
        "supporting_arc_progress",
        "locations",
        "character_combination",
        "central_labor",
        "conflict_types",
        "social_themes",
        "document_formats",
        "ai_interaction_types",
        "narrative_mode",
        "narrative_pace",
        "narrative_function",
        "opening_pattern",
        "ending_pattern",
        "direct_continuation",
        "motif_cooldown_updates",
    }
    return {
        "type": "object",
        "required": ["public_markdown", "state_update"],
        "properties": {
            "public_markdown": {"type": "string"},
            "state_update": {
                "type": "object",
                "required": [
                    field
                    for field in state_properties
                    if field not in plan_owned_optional
                ],
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
    attempt_feedback = feedback
    for plan_attempt in range(1, MAX_PLAN_RESPONSE_ATTEMPTS + 1):
        log(
            f"장면 계획 응답 생성 "
            f"({plan_attempt}/{MAX_PLAN_RESPONSE_ATTEMPTS})"
        )
        response = call_with_retry(
            lambda: client.models.generate_content(
                model=model,
                contents=build_plan_prompt(
                    state, episode_number, attempt_feedback
                ),
                config=types.GenerateContentConfig(
                    temperature=0.65,
                    top_p=0.9,
                    # The full Korean plan schema can exceed 2,048 tokens.
                    # A cutoff produces a syntactically unterminated JSON string.
                    max_output_tokens=GENERATION_MAX_OUTPUT_TOKENS,
                    response_mime_type="application/json",
                    response_json_schema=scene_plan_schema(),
                ),
            ),
            "비공개 장면 계획 생성",
        )
        try:
            raw = response.text or ""
        except (AttributeError, ValueError) as exc:
            parse_error = ValueError(
                "장면 계획 응답에서 텍스트를 읽을 수 없습니다."
            )
            parse_error.__cause__ = exc
        else:
            try:
                return validate_scene_plan(
                    parse_json_object(raw, "장면 계획")
                )
            except ValueError as exc:
                parse_error = exc

        if plan_attempt == MAX_PLAN_RESPONSE_ATTEMPTS:
            raise parse_error
        log(
            f"장면 계획 응답 검증 실패: {parse_error}; "
            "파일이나 상태를 변경하지 않고 계획 응답을 한 번 더 생성합니다."
        )
        attempt_feedback = (
            f"{feedback}\n\n"
            "직전 계획 응답은 불완전하거나 JSON 스키마를 충족하지 못했다. "
            "모든 필드를 포함한 완결된 JSON 객체 하나만 다시 반환할 것."
        )
    raise AssertionError("unreachable")


def parse_generated_episode(
    raw: str, scene_plan: dict[str, Any] | None = None
) -> tuple[str, str, dict[str, Any]]:
    public_text, update = parse_response(raw, scene_plan)
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
                max_output_tokens=GENERATION_MAX_OUTPUT_TOKENS,
                response_mime_type="application/json",
                response_json_schema=response_schema(),
            ),
        ),
        "본문 생성",
        attempts=BODY_GENERATION_ATTEMPTS,
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


def _meaningful_tokens(value: str) -> set[str]:
    generic = {
        "주인공",
        "인물",
        "장면",
        "사건",
        "행동",
        "시작",
        "계속",
        "순간",
        "대한",
    }
    particles = (
        "으로부터",
        "에게서",
        "에서는",
        "으로",
        "에서",
        "에게",
        "까지",
        "부터",
        "처럼",
        "하고",
        "하며",
        "한다",
        "하는",
        "했다",
        "된다",
        "하는",
        "을",
        "를",
        "은",
        "는",
        "이",
        "가",
        "와",
        "과",
        "에",
        "도",
    )
    tokens: set[str] = set()
    for raw in re.findall(r"[가-힣A-Za-z0-9]+", value.lower()):
        token = raw
        for particle in particles:
            if token.endswith(particle) and len(token) > len(particle) + 1:
                token = token[: -len(particle)]
                break
        if len(token) >= 2 and token not in generic:
            tokens.add(token)
    return tokens


def _concept_present(concept: str, text_tokens: set[str]) -> bool:
    concept_tokens = _meaningful_tokens(concept)
    if not concept_tokens:
        return True
    # Korean plans often use a long instruction sentence while the prose uses
    # natural inflections, a given name, or a shorter paraphrase. Requiring a
    # third of every instruction token made valid continuations impossible to
    # pass (for example, "강태수" in the plan and "태수" in the prose).
    matched = {
        concept_token
        for concept_token in concept_tokens
        if any(
            concept_token == text_token
            or (
                min(len(concept_token), len(text_token)) >= 2
                and (
                    concept_token in text_token
                    or text_token in concept_token
                )
            )
            for text_token in text_tokens
        )
    }
    required = max(1, min(3, (len(concept_tokens) + 2) // 3))
    return len(matched) >= required


def enforce_plan_state_constraints(
    plan: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Deterministically preserve state invariants after an imperfect replan."""
    repaired = copy.deepcopy(plan)
    repairs: list[str] = []
    current_scene = str(state.get("current_scene") or "").strip()
    required_actions = [
        str(action).strip()
        for action in state.get("unresolved_immediate_actions", [])
        if str(action).strip()
    ]
    if current_scene or required_actions:
        if not repaired.get("direct_continuation"):
            repaired["direct_continuation"] = True
            repairs.append("direct_continuation=true")
        continued = [
            str(action).strip()
            for action in repaired.get("continued_actions", [])
            if str(action).strip()
        ]
        missing = [action for action in required_actions if action not in continued]
        if missing:
            repaired["continued_actions"] = list(dict.fromkeys(continued + missing))
            repairs.append("continued_actions restored from story state")
        required_connection = str(
            state.get("next_required_connection") or current_scene
        ).strip()
        if required_connection and not str(
            repaired.get("continuation_point") or ""
        ).strip():
            repaired["continuation_point"] = required_connection
            repairs.append("continuation_point restored from story state")

    if not repaired.get("direct_continuation"):
        cooling = set(active_cooldowns(state))
        motifs = list(repaired.get("motifs_used", []))
        allowed = [motif for motif in motifs if motif not in cooling]
        if allowed != motifs:
            repaired["motifs_used"] = allowed
            repairs.append("cooldown motifs removed")
    return validate_scene_plan(repaired), repairs


def body_plan_relevance_report(
    title: str,
    body: str,
    plan: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Detect substantive divergence without comparing metadata labels."""
    text_tokens = _meaningful_tokens(f"{title}\n{body}")
    issues: list[str] = []

    distinctive_characters = [
        character
        for character in plan.get("main_characters", [])
        if _meaningful_tokens(str(character))
    ]
    if distinctive_characters and not any(
        _concept_present(str(character), text_tokens)
        for character in distinctive_characters
    ):
        issues.append("계획의 필수 인물이 한 명도 본문에 등장하지 않음")

    required_actions = [
        str(action)
        for action in state.get("unresolved_immediate_actions", [])
        if str(action).strip()
    ]
    missing_actions = [
        action
        for action in required_actions
        if not _concept_present(action, text_tokens)
    ]
    if missing_actions:
        issues.append(f"unfinished scene 필수 즉각 행동 누락: {missing_actions}")

    event_concepts = [
        str(plan.get("central_scene", "")),
        str(plan.get("central_labor", "")),
        *[str(item) for item in plan.get("changes", [])],
    ]
    if event_concepts and not any(
        _concept_present(concept, text_tokens)
        for concept in event_concepts
        if concept.strip()
    ):
        issues.append("계획의 중심 사건이 본문에 없음")

    forbidden_cooldowns = set(active_cooldowns(state)) - set(
        plan.get("motifs_used", [])
    )
    repeated = [
        motif
        for motif in forbidden_cooldowns
        if _concept_present(motif.replace("_", " "), text_tokens)
    ]
    if repeated:
        issues.append(f"금지된 cooldown 소재를 중심 사건으로 사용: {repeated}")

    return {"relevant": not issues, "issues": issues}


def generate_episode_from_plan(
    client: genai.Client,
    model: str,
    state: dict[str, Any],
    episode_number: int,
    scene_plan: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    system_prompt = read_text(PROMPTS / "system.md")
    base_user_prompt = build_prompt(state, episode_number, scene_plan)
    attempt_feedback = ""
    for response_attempt in range(1, MAX_RESPONSE_ATTEMPTS + 1):
        log(f"본문 생성 ({response_attempt}/{MAX_RESPONSE_ATTEMPTS})")
        user_prompt = base_user_prompt
        if attempt_feedback:
            user_prompt += (
                "\n\n## 직전 응답 폐기 사유와 이번 재작성의 필수 조건\n"
                + attempt_feedback
                + "\n직전 원고를 수정해서 되풀이하지 말고, 처음부터 완결된 JSON "
                "객체 하나를 다시 작성하라. state_update 값은 간결하게 쓴다."
            )
        raw = generate_raw_response(client, model, system_prompt, user_prompt)
        try:
            title, body, update = parse_generated_episode(raw, scene_plan)
            relevance = body_plan_relevance_report(
                title, body, scene_plan, state
            )
            if not relevance["relevant"]:
                raise ValueError(
                    "본문이 계획과 실질적으로 무관합니다: "
                    + "; ".join(relevance["issues"])
                )
            log("본문 검증 결과: 계획과 실질적으로 관련됨")
            return title, body, update
        except ValueError as exc:
            if response_attempt == MAX_RESPONSE_ATTEMPTS:
                raise
            attempt_feedback = safe_error_reason(exc, limit=1200)
            log(
                f"본문 형식 또는 실질 관련성 검증 실패: {exc}; "
                "실패 이유를 반영해 본문을 다시 생성합니다."
            )
    raise AssertionError("unreachable")


def generate_episode_with_model_fallback(
    client: genai.Client,
    candidates: list[str],
    state: dict[str, Any],
    episode_number: int,
    scene_plan: dict[str, Any],
    already_probed: Iterable[str] = (),
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[
    str,
    str,
    dict[str, Any],
    str,
    list[str],
    list[dict[str, Any]],
]:
    """Generate a body, moving to another model after exhausted transient errors."""
    ordered = list(dict.fromkeys(candidates))[:MAX_BODY_MODEL_CANDIDATES]
    if not ordered:
        raise ValueError("본문 생성에 사용할 모델 후보가 없습니다.")

    probed = list(dict.fromkeys(already_probed))
    probed_set = set(probed)
    failures: list[dict[str, Any]] = []
    last_recoverable: Exception | None = None

    for index, candidate in enumerate(ordered, start=1):
        if candidate not in probed_set:
            log(
                f"대체 모델 probe 시작: {candidate} "
                f"({index}/{len(ordered)})"
            )
            try:
                probe_model(client, candidate, sleep=sleep)
            except Exception as exc:
                failure = {
                    "name": candidate,
                    "stage": "fallback_probe",
                    "status_code": error_status_code(exc) or "UNKNOWN",
                    "reason": safe_error_reason(exc),
                    "transient": is_transient_error(exc),
                }
                failures.append(failure)
                log(
                    f"대체 모델 probe 실패: {candidate} "
                    f"status={failure['status_code']} reason={failure['reason']}"
                )
                if (
                    is_transient_error(exc)
                    or is_model_unavailable_error(exc)
                    or is_model_incompatible_error(exc)
                ):
                    continue
                raise
            probed.append(candidate)
            probed_set.add(candidate)
            log(f"대체 모델 probe 성공: {candidate}")

        try:
            title, body, update = generate_episode_from_plan(
                client,
                candidate,
                state,
                episode_number,
                scene_plan,
            )
            return title, body, update, candidate, probed, failures
        except Exception as exc:
            recoverable_output = isinstance(exc, ValueError)
            if not is_transient_error(exc) and not recoverable_output:
                raise
            last_recoverable = exc
            failure = {
                "name": candidate,
                "stage": "body_generation",
                "status_code": error_status_code(exc) or "UNKNOWN",
                "reason": safe_error_reason(exc),
                "transient": is_transient_error(exc),
            }
            failures.append(failure)
            if index < len(ordered):
                reason = "일시 오류" if is_transient_error(exc) else "출력 검증 실패"
                log(f"본문 {reason}가 반복되어 다음 모델로 전환합니다: "
                    f"{candidate} -> {ordered[index]}")

    if last_recoverable is not None:
        raise last_recoverable
    raise ModelProbeError(
        "본문 생성용 대체 모델을 probe하지 못했습니다.",
        failures,
        probed,
    )


def output_paths(preview_dir: Path | None) -> tuple[Path, Path, Path]:
    if preview_dir is None:
        return EPISODES_DIR, STATE_DIR / "story_state.json", MODEL_CATALOG_PATH
    return (
        preview_dir / "episodes",
        preview_dir / "story_state.json",
        preview_dir / "model_catalog.json",
    )


def save_episode_and_state(
    episode_path: Path,
    episode_text: str,
    state_path: Path,
    state_text: str,
) -> None:
    """Commit episode/state together, rolling back a newly created episode."""
    created_episode = False
    try:
        atomic_create_text(episode_path, episode_text)
        created_episode = True
        atomic_write_text(state_path, state_text)
    except Exception:
        if created_episode:
            episode_path.unlink(missing_ok=True)
        raise


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
    parser.add_argument(
        "--debug-model-catalog",
        action="store_true",
        help="models.list 전체 이름을 진행 로그에도 출력",
    )
    parser.add_argument(
        "--skip-if-generated-today",
        action="store_true",
        help="KST 기준 오늘 이미 생성했다면 성공으로 종료",
    )
    return parser.parse_args(argv)


def create_gemini_client(api_key: str) -> genai.Client:
    if (
        os.environ.get("CI", "").lower() == "true"
        and os.environ.get("SERIAL_NOVEL_ALLOW_GEMINI_NETWORK") != "1"
    ):
        raise RuntimeError(
            "CI Gemini network access is disabled outside the generation step."
        )
    return genai.Client(api_key=api_key)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 2

    source_state_path = STATE_DIR / "story_state.json"
    state = migrate_story_state(load_json(source_state_path))
    generated_at = utc_now()
    if args.skip_if_generated_today and generated_on_kst_date(
        state, generated_at
    ):
        log("KST 기준 오늘 에피소드가 이미 생성되어 이번 예약 실행을 건너뜁니다.")
        if args.result_json is not None:
            skipped_mode = (
                "preview" if args.preview_dir is not None else "publish"
            )
            atomic_write_text(
                args.result_json,
                json.dumps(
                    {
                        "episode": None,
                        "episode_path": "",
                        "mode": skipped_mode,
                        "skipped": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        return 0
    episode_number = calculate_next_episode(state, EPISODES_DIR.glob("*.md"))
    target_episodes, target_state, target_catalog = output_paths(args.preview_dir)
    output_path = target_episodes / f"{episode_number:03d}.md"
    if output_path.exists():
        raise FileExistsError(f"에피소드 파일이 이미 존재합니다: {output_path}")

    client = create_gemini_client(api_key)
    log("models.list() 조회 시작")
    catalog = call_with_retry(
        lambda: list_models(client),
        "모델 목록 조회",
    )
    candidates, _ = ordered_model_candidates(
        catalog, os.environ.get("GEMINI_MODEL")
    )
    log_model_catalog_summary(
        catalog, candidates, debug=args.debug_model_catalog
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
    log(f"선택 모델: {model}")

    recent_records = recent_episode_records(
        fingerprints=state.get("recent_scene_fingerprints", []),
    )
    log("계획 생성 시작")
    scene_plan = generate_scene_plan(
        client,
        model,
        state,
        episode_number,
    )
    plan_report = plan_quality_report(scene_plan, state)
    if plan_needs_revision(plan_report):
        log(
            "장면 계획에서 연속성·냉각 기간·반복 위험을 감지해 한 번 재계획합니다."
        )
        scene_plan = generate_scene_plan(
            client,
            model,
            state,
            episode_number,
            revision_feedback(plan_report),
        )
        scene_plan, repairs = enforce_plan_state_constraints(scene_plan, state)
        for repair in repairs:
            log(f"재계획 안전 보정: {repair}")
        plan_report = plan_quality_report(scene_plan, state)
        if plan_needs_revision(plan_report):
            log(
                "재계획 후 남은 품질 경고를 기록하고 생성을 계속합니다: "
                + json.dumps(plan_report, ensure_ascii=False)
            )
        else:
            log("재계획 완료")
    else:
        log("재계획 불필요")

    body_candidates = body_model_candidates(candidates, model, probe_failed)
    title, body, update, model, probe_succeeded, generation_failed = (
        generate_episode_with_model_fallback(
            client,
            body_candidates,
            state,
            episode_number,
            scene_plan,
            already_probed=probe_succeeded,
        )
    )
    catalog_document = model_catalog_document(
        catalog,
        model,
        probe_succeeded,
        probe_failed,
        generated_at,
        generation_failed,
    )
    atomic_write_text(
        target_catalog,
        json.dumps(catalog_document, ensure_ascii=False, indent=2) + "\n",
    )
    fingerprint = fingerprint_from_update(update, episode_number)
    similarity = text_similarity_report(
        title,
        body,
        recent_records,
        fingerprint=fingerprint,
    )
    log(
        f"최근 원고 최대 유사도: {similarity['max_score']:.4f} "
        f"(비교 화: {similarity['similar_episode']})"
    )
    if similarity["too_similar"]:
        log("생성 원고의 반복 위험이 높아 한 번 재계획하고 재생성합니다.")
        scene_plan = generate_scene_plan(
            client,
            model,
            state,
            episode_number,
            revision_feedback(plan_report, similarity),
        )
        plan_report = plan_quality_report(scene_plan, state)
        if plan_needs_revision(plan_report):
            scene_plan, repairs = enforce_plan_state_constraints(scene_plan, state)
            for repair in repairs:
                log(f"유사도 재계획 안전 보정: {repair}")
            plan_report = plan_quality_report(scene_plan, state)
            if plan_needs_revision(plan_report):
                log(
                    "유사도 재계획의 남은 품질 경고를 기록하고 계속합니다: "
                    + json.dumps(plan_report, ensure_ascii=False)
                )
        retry_candidates = [model] + [
            candidate
            for candidate in body_candidates
            if candidate != model
        ]
        (
            title,
            body,
            update,
            model,
            probe_succeeded,
            retry_failures,
        ) = generate_episode_with_model_fallback(
            client,
            retry_candidates,
            state,
            episode_number,
            scene_plan,
            already_probed=probe_succeeded,
        )
        generation_failed.extend(retry_failures)
        catalog_document = model_catalog_document(
            catalog,
            model,
            probe_succeeded,
            probe_failed,
            generated_at,
            generation_failed,
        )
        atomic_write_text(
            target_catalog,
            json.dumps(catalog_document, ensure_ascii=False, indent=2) + "\n",
        )
        fingerprint = fingerprint_from_update(update, episode_number)
        similarity = text_similarity_report(
            title,
            body,
            recent_records,
            fingerprint=fingerprint,
        )
        log(f"재생성 후 최근 원고 최대 유사도: {similarity['max_score']:.4f}")
        if similarity["too_similar"]:
            log(
                "재생성 후에도 유사도 경고가 남았지만 유효한 원고를 폐기하지 않고 "
                "상태의 반복 경고에 기록합니다."
            )

    new_state = merge_state(
        state,
        update,
        episode_number,
        title,
        scene_plan,
        similarity_report=similarity,
        generated_at=generated_at,
    )

    log("파일 저장 시작")
    # State is the commit point: it advances only after the public file and
    # diagnostic catalog have been written successfully.
    save_episode_and_state(
        output_path,
        episode_markdown(
            episode_number,
            title,
            body,
            model,
            generated_at=generated_at,
        ),
        target_state,
        json.dumps(new_state, ensure_ascii=False, indent=2) + "\n",
    )

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
    log(f"파일 저장 완료 ({mode}): {output_path}")
    log(f"제목: {title}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        message = safe_error_reason(exc, limit=2000)
        log(f"{exc.__class__.__name__}: {message}")
        raise SystemExit(1)
