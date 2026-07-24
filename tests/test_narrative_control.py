from __future__ import annotations

import unittest

from scripts.narrative_control import (
    ARC_NAMES,
    active_cooldowns,
    advance_motif_cooldowns,
    migrate_story_state,
    narrative_pace_guidance,
    plan_continuity_errors,
    plan_cooldown_violations,
    scene_plan_repetition_report,
    text_similarity_report,
    validate_scene_plan,
)


def plan() -> dict[str, object]:
    return {
        "continuation_point": "손잡이를 잡은 직후",
        "direct_continuation": True,
        "continued_actions": ["문을 연다"],
        "central_scene": "멈춘 문을 여는 장면",
        "main_characters": ["주인공", "민서"],
        "location": "지하 세척실",
        "changes": ["민서가 작업 순서를 바꾼다"],
        "intentionally_unchanged": ["법적 위험"],
        "reveal_information": ["민서의 작업 방식"],
        "withhold_information": ["민서의 과거"],
        "social_theme": "신체 노동의 숙련",
        "avoid_recent_repetition": ["혼자 청소하며 시작"],
        "expected_ending_point": "문 안의 소리를 듣는다",
        "arc_impact": "동료 관계가 시작된다",
        "threads_to_touch": ["멈춘 세척기"],
        "add_new_thread": False,
        "style_feature": "대화 사이 감각 묘사",
        "narrative_mode": "실시간 노동 장면",
        "narrative_pace": "관계 심화",
        "active_arc": "주변 노동자들과의 관계",
        "supporting_arcs": ["청소 노동과 신체 손상"],
        "central_labor": "세척기 수동 개방",
        "conflict_type": "규정과 현장 판단",
        "document_format": "일반 서술",
        "ai_interaction_type": "없음",
        "opening_pattern": "이전 동작의 직접 연속",
        "ending_pattern": "감각적 질문",
        "core_image": "젖은 손잡이",
        "narrative_function": "반복 노동을 동료 관계 형성으로 전환",
        "motifs_used": [],
    }


class MigrationTests(unittest.TestCase):
    def test_legacy_state_receives_new_schema_without_losing_values(self) -> None:
        legacy = {
            "series_title": "제목",
            "next_episode": 4,
            "open_threads": ["기존 복선"],
        }
        migrated = migrate_story_state(legacy)
        self.assertEqual(migrated["next_episode"], 4)
        self.assertEqual(migrated["open_threads"], ["기존 복선"])
        self.assertEqual(set(migrated["arc_states"]), set(ARC_NAMES))
        self.assertIn("recent_scene_fingerprints", migrated)
        self.assertIn("motif_cooldowns", migrated)


class PlanningTests(unittest.TestCase):
    def test_plan_schema_validation(self) -> None:
        self.assertEqual(
            validate_scene_plan(plan())["active_arc"],
            "주변 노동자들과의 관계",
        )

    def test_unfinished_scene_requires_exact_continuation_actions(self) -> None:
        state = migrate_story_state(
            {
                "current_scene": "세척실 문 앞",
                "current_scene_started_episode": 3,
                "unresolved_immediate_actions": ["문을 연다", "민서에게 묻는다"],
            }
        )
        incomplete = plan()
        errors = plan_continuity_errors(incomplete, state)
        self.assertTrue(any("민서에게 묻는다" in error for error in errors))
        incomplete["continued_actions"].append("민서에게 묻는다")
        self.assertEqual(plan_continuity_errors(incomplete, state), [])

    def test_recently_used_motif_is_blocked_except_direct_continuation(self) -> None:
        state = migrate_story_state({})
        state["motif_cooldowns"]["major_accident"]["remaining"] = 4
        candidate = plan()
        candidate["direct_continuation"] = False
        candidate["motifs_used"] = ["major_accident"]
        self.assertEqual(
            plan_cooldown_violations(candidate, state),
            ["major_accident"],
        )
        candidate["direct_continuation"] = True
        self.assertEqual(plan_cooldown_violations(candidate, state), [])

    def test_plan_repetition_detects_same_narrative_function(self) -> None:
        candidate = plan()
        fingerprint = {
            "episode": 8,
            "locations": ["지하 세척실"],
            "character_combination": ["주인공", "민서"],
            "central_labor": "세척기 수동 개방",
            "conflict_type": ["규정과 현장 판단"],
            "social_theme": ["신체 노동의 숙련"],
            "document_format": ["일반 서술"],
            "ai_interaction_type": ["없음"],
            "opening_pattern": "이전 동작의 직접 연속",
            "ending_pattern": "감각적 질문",
            "core_images": ["젖은 손잡이"],
            "narrative_function": "반복 노동을 동료 관계 형성으로 전환",
        }
        report = scene_plan_repetition_report(candidate, [fingerprint])
        self.assertTrue(report["too_similar"])

    def test_pace_guidance_slows_repeated_event_development(self) -> None:
        state = {
            "history": [
                {"narrative_pace": "위험 증가"},
                {"narrative_pace": "사건 전개"},
            ]
        }
        self.assertIn("여파", narrative_pace_guidance(state))


class CooldownTests(unittest.TestCase):
    def test_cooldown_decrements_and_resets_when_used(self) -> None:
        state = migrate_story_state({})
        state["motif_cooldowns"]["company_direct_warning"]["remaining"] = 3
        updated = advance_motif_cooldowns(
            state["motif_cooldowns"],
            ["approval_document_error"],
            12,
        )
        self.assertEqual(updated["company_direct_warning"]["remaining"], 2)
        self.assertEqual(updated["approval_document_error"]["remaining"], 3)
        self.assertEqual(updated["approval_document_error"]["last_used_episode"], 12)
        self.assertEqual(active_cooldowns({"motif_cooldowns": updated})[
            "approval_document_error"
        ], 3)


class SimilarityTests(unittest.TestCase):
    def test_identical_episode_is_flagged(self) -> None:
        body = "\n\n".join(
            [
                "나는 젖은 손잡이를 잡고 민서가 말한 순서를 다시 떠올렸다.",
                "세척기 안에서 물이 돌지 않았고 규정표의 숫자만 줄어들었다.",
                "민서는 문을 열기 전에 전원을 끄라고 다시 말했다.",
            ]
            * 12
        )
        report = text_similarity_report(
            "젖은 손잡이",
            body,
            [{"episode": 7, "title": "젖은 손잡이", "body": body}],
        )
        self.assertTrue(report["too_similar"])
        self.assertEqual(report["similar_episode"], 7)
        self.assertGreater(report["max_score"], 0.9)

    def test_distinct_episode_is_not_flagged(self) -> None:
        new_body = "옥상에서 민서는 월세 고지서를 접었다. " * 80
        old_body = "지하 세척기의 배수구를 솔로 문질렀다. " * 80
        report = text_similarity_report(
            "접힌 고지서",
            new_body,
            [{"episode": 3, "title": "배수구", "body": old_body}],
        )
        self.assertFalse(report["too_similar"])


if __name__ == "__main__":
    unittest.main()
