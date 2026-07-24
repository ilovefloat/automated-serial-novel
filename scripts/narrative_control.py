from __future__ import annotations

import copy
import math
import re
from collections import Counter
from typing import Any

ARC_NAMES = (
    "생계와 부채",
    "청소 노동과 신체 손상",
    "승인 노동과 법적 위험",
    "AI를 이용한 집필",
    "자기 목소리에 대한 불신",
    "연재 독자의 증가",
    "직장과 플랫폼의 감시",
    "주변 노동자들과의 관계",
    "승인 문서와 현실 사건의 연결",
    "작품의 상품화",
    "사회 전체의 자동화 변화",
    "내부 소설과 외부 소설의 경계",
)

NARRATIVE_PACES = (
    "정체",
    "일상 축적",
    "관계 심화",
    "위험 증가",
    "사건 전개",
    "여파",
    "회복",
    "방향 전환",
)

MOTIF_COOLDOWN_DEFAULTS = {
    "major_accident": 8,
    "company_direct_warning": 5,
    "reader_comments_focus": 4,
    "approval_document_error": 3,
    "ai_profanity_sanitization": 6,
    "memory_uncertainty": 5,
    "legal_liability_crisis": 7,
    "repeated_ending_phrase": 15,
}

PLAN_REQUIRED_STRING_FIELDS = (
    "continuation_point",
    "central_scene",
    "location",
    "social_theme",
    "expected_ending_point",
    "arc_impact",
    "style_feature",
    "narrative_mode",
    "narrative_pace",
    "active_arc",
    "central_labor",
    "conflict_type",
    "document_format",
    "ai_interaction_type",
    "opening_pattern",
    "ending_pattern",
    "core_image",
    "narrative_function",
)

PLAN_REQUIRED_LIST_FIELDS = (
    "continued_actions",
    "main_characters",
    "changes",
    "intentionally_unchanged",
    "reveal_information",
    "withhold_information",
    "avoid_recent_repetition",
    "supporting_arcs",
    "threads_to_touch",
    "motifs_used",
)

FINGERPRINT_FIELDS = (
    "locations",
    "character_combination",
    "central_labor",
    "conflict_type",
    "social_theme",
    "document_format",
    "ai_interaction_type",
    "emotional_start",
    "emotional_end",
    "opening_pattern",
    "ending_pattern",
    "core_images",
    "core_sentence_structure",
    "revealed_information",
    "new_questions",
    "resolved_threads",
    "twist_type",
    "narrative_function",
)

STOPWORDS = {
    "그리고",
    "그러나",
    "하지만",
    "그것",
    "이것",
    "있는",
    "없는",
    "했다",
    "한다",
    "있었다",
    "없었다",
    "나는",
    "내가",
    "그는",
    "그녀는",
    "에서",
    "으로",
    "에게",
    "까지",
    "부터",
}


def default_arc_states() -> dict[str, dict[str, Any]]:
    return {
        arc: {
            "current_stage": "미진행",
            "last_progress_episode": None,
            "next_possible_change": "",
            "withheld_information": [],
            "current_pressure": "",
            "long_term_outcome": "",
            "on_hold": True,
        }
        for arc in ARC_NAMES
    }


def default_motif_cooldowns() -> dict[str, dict[str, Any]]:
    return {
        motif: {
            "remaining": 0,
            "minimum_gap": minimum_gap,
            "last_used_episode": None,
        }
        for motif, minimum_gap in MOTIF_COOLDOWN_DEFAULTS.items()
    }


def migrate_story_state(state: dict[str, Any]) -> dict[str, Any]:
    migrated = copy.deepcopy(state)
    migrated.setdefault("narrative_mode", "일반적인 1인칭 서술")
    migrated.setdefault("narrative_pace", "일상 축적")
    migrated.setdefault("current_scene", "")
    migrated.setdefault("current_scene_started_episode", None)
    migrated.setdefault("active_arc", "생계와 부채")
    migrated.setdefault("supporting_arcs", [])

    arcs = migrated.setdefault("arc_states", {})
    for arc, default in default_arc_states().items():
        current = arcs.setdefault(arc, {})
        for key, value in default.items():
            current.setdefault(key, copy.deepcopy(value))

    for key in (
        "recent_scene_fingerprints",
        "recent_opening_patterns",
        "recent_ending_patterns",
        "recent_locations",
        "recent_character_combinations",
        "recent_conflict_types",
        "recent_social_themes",
        "recent_document_formats",
        "recent_ai_interaction_types",
        "unresolved_immediate_actions",
        "withheld_information",
        "planned_long_term_reveals",
        "recurring_symbols",
        "symbol_meaning_history",
        "narrative_repetition_warnings",
    ):
        migrated.setdefault(key, [])

    cooldowns = migrated.setdefault("motif_cooldowns", {})
    for motif, default in default_motif_cooldowns().items():
        current = cooldowns.setdefault(motif, {})
        for key, value in default.items():
            current.setdefault(key, value)

    migrated.setdefault("last_major_event_episode", None)
    migrated.setdefault("last_major_reveal_episode", None)
    migrated.setdefault("next_required_connection", "")
    return migrated


def scene_plan_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        field: {"type": "string"} for field in PLAN_REQUIRED_STRING_FIELDS
    }
    properties.update(
        {
            field: {"type": "array", "items": {"type": "string"}}
            for field in PLAN_REQUIRED_LIST_FIELDS
        }
    )
    properties["direct_continuation"] = {"type": "boolean"}
    properties["add_new_thread"] = {"type": "boolean"}
    return {
        "type": "object",
        "required": list(properties),
        "properties": properties,
    }


def validate_scene_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("장면 계획은 JSON 객체여야 합니다.")
    for field in PLAN_REQUIRED_STRING_FIELDS:
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError(f"장면 계획의 {field}는 비어 있지 않은 문자열이어야 합니다.")
    for field in PLAN_REQUIRED_LIST_FIELDS:
        value = plan.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(f"장면 계획의 {field}는 문자열 배열이어야 합니다.")
    for field in ("direct_continuation", "add_new_thread"):
        if not isinstance(plan.get(field), bool):
            raise ValueError(f"장면 계획의 {field}는 boolean이어야 합니다.")
    if plan["active_arc"] not in ARC_NAMES:
        raise ValueError("장면 계획의 active_arc가 등록된 장기 아크가 아닙니다.")
    if plan["narrative_pace"] not in NARRATIVE_PACES:
        raise ValueError("장면 계획의 narrative_pace가 허용된 값이 아닙니다.")
    if len(plan["supporting_arcs"]) > 2:
        raise ValueError("보조 아크는 최대 2개여야 합니다.")
    if any(arc not in ARC_NAMES for arc in plan["supporting_arcs"]):
        raise ValueError("등록되지 않은 보조 아크가 있습니다.")
    if plan["active_arc"] in plan["supporting_arcs"]:
        raise ValueError("주요 아크를 보조 아크에 중복할 수 없습니다.")
    return plan


def active_cooldowns(state: dict[str, Any]) -> dict[str, int]:
    return {
        motif: int(data.get("remaining", 0))
        for motif, data in state.get("motif_cooldowns", {}).items()
        if int(data.get("remaining", 0)) > 0
    }


def plan_cooldown_violations(
    plan: dict[str, Any], state: dict[str, Any]
) -> list[str]:
    if plan.get("direct_continuation"):
        return []
    cooling = active_cooldowns(state)
    return [motif for motif in plan.get("motifs_used", []) if motif in cooling]


def plan_continuity_errors(
    plan: dict[str, Any], state: dict[str, Any]
) -> list[str]:
    current_scene = str(state.get("current_scene") or "").strip()
    required_actions = set(state.get("unresolved_immediate_actions", []))
    if not current_scene and not required_actions:
        return []
    errors: list[str] = []
    if not plan.get("direct_continuation"):
        errors.append("종료되지 않은 장면은 직접 이어져야 합니다.")
    continued = set(plan.get("continued_actions", []))
    missing = required_actions - continued
    if missing:
        errors.append(f"이어야 할 즉각적 행동이 누락됨: {sorted(missing)}")
    if not plan.get("continuation_point", "").strip():
        errors.append("정확한 연결 지점이 없습니다.")
    return errors


def _as_set(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    if value is None:
        return set()
    text = str(value).strip().lower()
    return {text} if text else set()


def _set_similarity(left: Any, right: Any) -> float:
    left_set = _as_set(left)
    right_set = _as_set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def scene_plan_repetition_report(
    plan: dict[str, Any], recent_fingerprints: list[dict[str, Any]]
) -> dict[str, Any]:
    if not recent_fingerprints:
        return {"max_score": 0.0, "similar_episode": None, "too_similar": False}
    mapping = {
        "location": "locations",
        "main_characters": "character_combination",
        "central_labor": "central_labor",
        "conflict_type": "conflict_type",
        "social_theme": "social_theme",
        "document_format": "document_format",
        "ai_interaction_type": "ai_interaction_type",
        "opening_pattern": "opening_pattern",
        "ending_pattern": "ending_pattern",
        "core_image": "core_images",
        "narrative_function": "narrative_function",
    }
    scores: list[tuple[float, int | None]] = []
    for fingerprint in recent_fingerprints[-20:]:
        weighted_total = 0.0
        weight_sum = 0.0
        for plan_key, fingerprint_key in mapping.items():
            weight = 2.0 if plan_key in {
                "conflict_type",
                "document_format",
                "narrative_function",
            } else 1.0
            if plan.get("direct_continuation") and plan_key in {
                "location",
                "main_characters",
            }:
                weight = 0.35
            weighted_total += (
                _set_similarity(plan.get(plan_key), fingerprint.get(fingerprint_key))
                * weight
            )
            weight_sum += weight
        scores.append(
            (
                weighted_total / weight_sum if weight_sum else 0.0,
                fingerprint.get("episode"),
            )
        )
    max_score, episode = max(scores, default=(0.0, None))
    return {
        "max_score": round(max_score, 4),
        "similar_episode": episode,
        "too_similar": max_score >= 0.72,
    }


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[가-힣A-Za-z0-9]+", text.lower())
        if len(token) > 1 and token not in STOPWORDS
    ]


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    common = set(left) & set(right)
    numerator = sum(left[token] * right[token] for token in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _tfidf_vectors(documents: list[str]) -> list[dict[str, float]]:
    tokenized = [_tokens(document) for document in documents]
    document_count = len(tokenized)
    document_frequency = Counter(
        token for tokens in tokenized for token in set(tokens)
    )
    vectors: list[dict[str, float]] = []
    for tokens in tokenized:
        counts = Counter(tokens)
        total = max(1, sum(counts.values()))
        vectors.append(
            {
                token: (count / total)
                * (math.log((1 + document_count) / (1 + document_frequency[token])) + 1)
                for token, count in counts.items()
            }
        )
    return vectors


def _word_ngrams(text: str, size: int = 5) -> set[tuple[str, ...]]:
    tokens = _tokens(text)
    return {
        tuple(tokens[index : index + size])
        for index in range(max(0, len(tokens) - size + 1))
    }


def _jaccard(left: set[Any], right: set[Any]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _opening_signature(text: str) -> set[tuple[str, ...]]:
    sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", text.strip())
    return {
        tuple(_tokens(sentence)[:5])
        for sentence in sentences[:3]
        if _tokens(sentence)
    }


def _ending_text(text: str) -> str:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    return paragraphs[-1] if paragraphs else ""


def _top_terms(text: str, count: int = 20) -> set[str]:
    return {token for token, _ in Counter(_tokens(text)).most_common(count)}


def _paragraph_structure(text: str) -> tuple[int, float]:
    paragraphs = [paragraph for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    average = sum(len(paragraph) for paragraph in paragraphs) / max(1, len(paragraphs))
    return len(paragraphs), average


def text_similarity_report(
    title: str,
    body: str,
    recent_episodes: list[dict[str, Any]],
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not recent_episodes:
        return {
            "max_score": 0.0,
            "similar_episode": None,
            "too_similar": False,
            "signals": [],
        }
    documents = [body] + [str(item.get("body", "")) for item in recent_episodes]
    vectors = _tfidf_vectors(documents)
    new_ngrams = _word_ngrams(body)
    new_opening = _opening_signature(body)
    new_ending = _word_ngrams(_ending_text(body), size=3)
    new_terms = _top_terms(body)
    new_structure = _paragraph_structure(body)
    results: list[tuple[float, dict[str, float], dict[str, Any]]] = []

    for index, episode in enumerate(recent_episodes, start=1):
        old_body = str(episode.get("body", ""))
        old_structure = _paragraph_structure(old_body)
        paragraph_ratio = min(new_structure[0], old_structure[0]) / max(
            1, new_structure[0], old_structure[0]
        )
        average_ratio = min(new_structure[1], old_structure[1]) / max(
            1.0, new_structure[1], old_structure[1]
        )
        scene_score = 0.0
        old_fingerprint = episode.get("fingerprint")
        if fingerprint and isinstance(old_fingerprint, dict):
            scene_fields = (
                "locations",
                "character_combination",
                "conflict_type",
                "social_theme",
            )
            scene_score = sum(
                _set_similarity(fingerprint.get(field), old_fingerprint.get(field))
                for field in scene_fields
            ) / len(scene_fields)
        metrics = {
            "tfidf": _cosine(vectors[0], vectors[index]),
            "ngram": _jaccard(new_ngrams, _word_ngrams(old_body)),
            "opening": _jaccard(new_opening, _opening_signature(old_body)),
            "ending": _jaccard(
                new_ending,
                _word_ngrams(_ending_text(old_body), size=3),
            ),
            "top_terms": _jaccard(new_terms, _top_terms(old_body)),
            "structure": (paragraph_ratio + average_ratio) / 2,
            "scene": scene_score,
            "title": 1.0
            if title.strip().lower() == str(episode.get("title", "")).strip().lower()
            else 0.0,
        }
        score = (
            metrics["tfidf"] * 0.30
            + metrics["ngram"] * 0.20
            + metrics["opening"] * 0.10
            + metrics["ending"] * 0.15
            + metrics["top_terms"] * 0.10
            + metrics["structure"] * 0.05
            + metrics["scene"] * 0.05
            + metrics["title"] * 0.05
        )
        results.append((score, metrics, episode))

    score, metrics, episode = max(results, key=lambda item: item[0])
    signals = [
        name for name, value in metrics.items() if value >= 0.72
    ]
    return {
        "max_score": round(score, 4),
        "similar_episode": episode.get("episode"),
        "too_similar": score >= 0.70
        or metrics["title"] == 1.0
        or (metrics["opening"] >= 0.85 and metrics["ending"] >= 0.75),
        "signals": signals,
        "metrics": {key: round(value, 4) for key, value in metrics.items()},
    }


def advance_motif_cooldowns(
    cooldowns: dict[str, dict[str, Any]],
    used_motifs: list[str],
    episode_number: int,
) -> dict[str, dict[str, Any]]:
    updated = copy.deepcopy(cooldowns)
    for motif, default in default_motif_cooldowns().items():
        current = updated.setdefault(motif, copy.deepcopy(default))
        current["remaining"] = max(0, int(current.get("remaining", 0)) - 1)
        current.setdefault("minimum_gap", default["minimum_gap"])
        current.setdefault("last_used_episode", None)
    for motif in used_motifs:
        current = updated.setdefault(
            motif,
            {
                "remaining": 0,
                "minimum_gap": MOTIF_COOLDOWN_DEFAULTS.get(motif, 3),
                "last_used_episode": None,
            },
        )
        current["remaining"] = int(current.get("minimum_gap", 3))
        current["last_used_episode"] = episode_number
    return updated


def append_recent(
    existing: list[Any], values: list[Any], limit: int
) -> list[Any]:
    return (copy.deepcopy(existing) + copy.deepcopy(values))[-limit:]


def fingerprint_from_update(
    update: dict[str, Any], episode_number: int
) -> dict[str, Any]:
    fingerprint = {
        "episode": episode_number,
        "locations": update["locations"],
        "character_combination": update["character_combination"],
        "central_labor": update["central_labor"],
        "conflict_type": update["conflict_types"],
        "social_theme": update["social_themes"],
        "document_format": update["document_formats"],
        "ai_interaction_type": update["ai_interaction_types"],
        "emotional_start": update["emotional_start"],
        "emotional_end": update["emotional_end"],
        "opening_pattern": update["opening_pattern"],
        "ending_pattern": update["ending_pattern"],
        "core_images": update["core_images"],
        "core_sentence_structure": update["core_sentence_structure"],
        "revealed_information": update["revealed_information"],
        "new_questions": update["new_questions"],
        "resolved_threads": update["resolved_threads"],
        "twist_type": update["twist_type"],
        "narrative_function": update["narrative_function"],
        "narrative_pace": update["narrative_pace"],
    }
    return fingerprint


def update_arc_state(
    arc_states: dict[str, dict[str, Any]],
    progress: dict[str, Any],
    episode_number: int,
) -> None:
    arc = progress["arc"]
    if arc not in arc_states:
        return
    current = arc_states[arc]
    current["current_stage"] = progress["stage"]
    current["last_progress_episode"] = episode_number
    current["next_possible_change"] = progress["next_possible_change"]
    current["withheld_information"] = progress["withheld_information"]
    current["current_pressure"] = progress["current_pressure"]
    current["long_term_outcome"] = progress["long_term_outcome"]
    current["on_hold"] = progress["on_hold"]


def apply_narrative_state_update(
    state: dict[str, Any],
    update: dict[str, Any],
    episode_number: int,
    similarity_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state["narrative_mode"] = update["narrative_mode"]
    state["narrative_pace"] = update["narrative_pace"]
    state["active_arc"] = update["primary_arc_progress"]["arc"]
    state["supporting_arcs"] = [
        progress["arc"] for progress in update["supporting_arc_progress"]
    ][:2]
    update_arc_state(
        state["arc_states"],
        update["primary_arc_progress"],
        episode_number,
    )
    for progress in update["supporting_arc_progress"][:2]:
        update_arc_state(state["arc_states"], progress, episode_number)

    previous_scene_start = state.get("current_scene_started_episode")
    if update["scene_completed"]:
        state["current_scene"] = ""
        state["current_scene_started_episode"] = None
    else:
        state["current_scene"] = update["current_scene"]
        state["current_scene_started_episode"] = (
            previous_scene_start
            if update["direct_continuation"] and previous_scene_start is not None
            else episode_number
        )

    state["unresolved_immediate_actions"] = update["next_immediate_actions"]
    state["next_required_connection"] = update["next_required_connection"]
    revealed = set(update["revealed_information"])
    withheld = [
        item for item in state.get("withheld_information", []) if item not in revealed
    ]
    state["withheld_information"] = list(
        dict.fromkeys(withheld + update["withheld_information"])
    )[-50:]
    state["planned_long_term_reveals"] = [
        item
        for item in state.get("planned_long_term_reveals", [])
        if item not in revealed
    ]
    state["planned_long_term_reveals"] = list(
        dict.fromkeys(
            state["planned_long_term_reveals"]
            + update["planned_long_term_reveals"]
        )
    )[-50:]

    fingerprint = fingerprint_from_update(update, episode_number)
    state["recent_scene_fingerprints"] = append_recent(
        state.get("recent_scene_fingerprints", []),
        [fingerprint],
        20,
    )
    recent_fields = {
        "recent_opening_patterns": [update["opening_pattern"]],
        "recent_ending_patterns": [update["ending_pattern"]],
        "recent_locations": update["locations"],
        "recent_character_combinations": [update["character_combination"]],
        "recent_conflict_types": update["conflict_types"],
        "recent_social_themes": update["social_themes"],
        "recent_document_formats": update["document_formats"],
        "recent_ai_interaction_types": update["ai_interaction_types"],
    }
    for key, values in recent_fields.items():
        limit = 5 if key == "recent_document_formats" else 20
        state[key] = append_recent(state.get(key, []), values, limit)

    state["motif_cooldowns"] = advance_motif_cooldowns(
        state["motif_cooldowns"],
        update["motif_cooldown_updates"],
        episode_number,
    )
    if update["major_event"]:
        state["last_major_event_episode"] = episode_number
    if update["major_reveal"]:
        state["last_major_reveal_episode"] = episode_number

    for symbol_update in update["symbol_updates"]:
        symbol = symbol_update["symbol"]
        if symbol not in state["recurring_symbols"]:
            state["recurring_symbols"].append(symbol)
            state["recurring_symbols"] = state["recurring_symbols"][-20:]
        state["symbol_meaning_history"] = append_recent(
            state["symbol_meaning_history"],
            [
                {
                    "episode": episode_number,
                    "symbol": symbol,
                    "meaning": symbol_update["meaning"],
                    "development": symbol_update["development"],
                }
            ],
            50,
        )

    warnings = list(update["repetition_risks"])
    if similarity_report and similarity_report.get("signals"):
        warnings.extend(
            f"local_similarity:{signal}"
            for signal in similarity_report["signals"]
        )
    state["narrative_repetition_warnings"] = append_recent(
        state.get("narrative_repetition_warnings", []),
        list(dict.fromkeys(warnings)),
        20,
    )
    return state


def narrative_pace_guidance(state: dict[str, Any]) -> str:
    history = state.get("history", [])
    recent = [
        item.get("narrative_pace")
        for item in history[-4:]
        if item.get("narrative_pace")
    ]
    if len(recent) >= 2 and all(
        pace in {"위험 증가", "사건 전개", "방향 전환"} for pace in recent[-2:]
    ):
        return "최근 전개 강도가 높다. 여파, 회복, 일상 축적 중 하나를 우선 검토한다."
    if len(recent) >= 3 and all(
        pace in {"정체", "일상 축적", "회복"} for pace in recent[-3:]
    ):
        return (
            "최근 변화가 적다. 관계 심화, 경제 상태, 법적 위험, 독자 반응 중 "
            "하나를 실질적으로 움직인다."
        )
    return "현재 장면의 인과를 우선하고 큰 사건을 의무적으로 추가하지 않는다."
