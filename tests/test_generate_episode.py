from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.generate_episode import (
    GENERATION_MAX_OUTPUT_TOKENS,
    ModelProbeError,
    PREFERRED_MODELS,
    atomic_create_text,
    body_model_candidates,
    body_plan_relevance_report,
    calculate_next_episode,
    call_with_retry,
    create_gemini_client,
    enforce_plan_state_constraints,
    extract_title_and_body,
    generate_episode_from_plan,
    generate_episode_with_model_fallback,
    generated_on_kst_date,
    generate_scene_plan,
    list_generation_models,
    log_model_catalog_summary,
    merge_state,
    model_catalog_document,
    ordered_model_candidates,
    parse_response,
    probe_and_select_model,
    safe_error_reason,
    save_episode_and_state,
)


def catalog(*names: str) -> list[dict[str, object]]:
    return [
        {"name": name, "supported_actions": ["generateContent"]}
        for name in names
    ]


def valid_update() -> dict[str, object]:
    return {
        "summary": "요약",
        "protagonist_changes": ["피로가 누적됨"],
        "new_facts": ["승인 단가는 건당 계산된다"],
        "open_threads": ["반려된 문서"],
        "continuity_notes": ["왼손 장갑이 찢어졌다"],
        "next_episode_pressure": "다음 교대가 다가온다",
        "actual_time_range": "교대 시작 뒤 20분",
        "direct_continuation": False,
        "scene_completed": False,
        "current_scene": "세척실 첫 교대",
        "next_immediate_actions": ["막힌 배수구를 확인한다"],
        "primary_arc_progress": {
            "arc": "청소 노동과 신체 손상",
            "stage": "첫 노출",
            "change": "작업 조건을 직접 경험한다",
            "next_possible_change": "통증이 누적된다",
            "withheld_information": ["이전 작업자의 퇴사 이유"],
            "current_pressure": "교대 시간이 줄어든다",
            "long_term_outcome": "몸의 손상이 선택을 제한한다",
            "on_hold": False,
        },
        "supporting_arc_progress": [],
        "revealed_information": ["세척 시간이 자동 배정된다"],
        "withheld_information": ["이전 작업자의 퇴사 이유"],
        "planned_long_term_reveals": ["이전 작업자의 퇴사 이유"],
        "locations": ["지하 세척실"],
        "character_combination": ["주인공"],
        "central_labor": "배수구 청소",
        "conflict_types": ["작업 속도와 안전"],
        "social_themes": ["생산성과 신체 위험"],
        "document_formats": ["일반 서술"],
        "ai_interaction_types": ["없음"],
        "emotional_start": "무감각",
        "emotional_end": "경계",
        "opening_pattern": "진행 중 행동",
        "ending_pattern": "새 정보",
        "core_images": ["검은 배수구"],
        "core_sentence_structure": "짧은 감각 문장",
        "repetition_risks": [],
        "variations_applied": ["청소를 관계가 아닌 위험 탐색으로 사용"],
        "motif_cooldown_updates": [],
        "new_threads": ["이전 작업자의 흔적"],
        "maintained_threads": ["반려된 문서"],
        "resolved_threads": [],
        "new_questions": ["이전 작업자는 왜 떠났는가"],
        "next_required_connection": "배수구 덮개를 드는 동작",
        "narrative_mode": "일반적인 1인칭 서술",
        "narrative_pace": "일상 축적",
        "narrative_function": "노동 조건 첫 노출",
        "twist_type": "없음",
        "major_event": False,
        "major_reveal": False,
        "symbol_updates": [],
    }


def valid_plan() -> dict[str, object]:
    return {
        "continuation_point": "새 교대 시작",
        "direct_continuation": False,
        "continued_actions": [],
        "central_scene": "세척실 첫 교대",
        "main_characters": ["주인공"],
        "location": "지하 세척실",
        "changes": ["작업 조건을 알게 된다"],
        "intentionally_unchanged": ["장기 법적 위험"],
        "reveal_information": ["자동 배정 시간"],
        "withhold_information": ["이전 작업자의 퇴사 이유"],
        "social_theme": "생산성과 신체 위험",
        "avoid_recent_repetition": [],
        "expected_ending_point": "배수구 덮개를 드는 순간",
        "arc_impact": "청소 노동의 조건을 처음 드러냄",
        "threads_to_touch": [],
        "add_new_thread": True,
        "style_feature": "짧은 감각 문장",
        "narrative_mode": "일반적인 1인칭 서술",
        "narrative_pace": "일상 축적",
        "active_arc": "청소 노동과 신체 손상",
        "supporting_arcs": [],
        "central_labor": "배수구 청소",
        "conflict_type": "작업 속도와 안전",
        "document_format": "일반 서술",
        "ai_interaction_type": "없음",
        "opening_pattern": "진행 중 행동",
        "ending_pattern": "새 정보",
        "core_image": "검은 배수구",
        "narrative_function": "노동 조건 첫 노출",
        "motifs_used": [],
    }


class ModelSelectionTests(unittest.TestCase):
    def test_default_priority_matches_supported_flash_policy(self) -> None:
        self.assertEqual(
            PREFERRED_MODELS,
            (
                "gemini-3.5-flash",
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
                "gemini-flash-latest",
                "gemini-flash-lite-latest",
            ),
        )
        self.assertNotIn("gemini-2.5-flash", PREFERRED_MODELS)

    def test_list_only_generate_content_models_and_sort(self) -> None:
        client = SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: [
                    SimpleNamespace(
                        name="models/z-model",
                        supported_actions=["generateContent"],
                        display_name="Z",
                    ),
                    SimpleNamespace(
                        name="models/embed",
                        supported_actions=["embedContent"],
                    ),
                    SimpleNamespace(
                        name="models/a-model",
                        supported_actions=["generateContent"],
                    ),
                ]
            )
        )
        self.assertEqual(
            [item["name"] for item in list_generation_models(client)],
            ["a-model", "z-model"],
        )

    def test_explicit_priority_ignores_catalog_order(self) -> None:
        models = catalog(
            "gemini-flash-latest",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
        )
        self.assertEqual(
            ordered_model_candidates(models)[0],
            [
                "gemini-3.5-flash",
                "gemini-3.1-flash-lite",
                "gemini-flash-latest",
            ],
        )
        self.assertEqual(
            ordered_model_candidates(
                models, "models/gemini-3.1-flash-lite"
            )[0][0],
            "gemini-3.1-flash-lite",
        )

    def test_stable_and_latest_beat_preview(self) -> None:
        models = catalog(
            "gemini-4.0-flash-preview",
            "gemini-flash-latest",
            "gemini-4.0-flash",
        )
        self.assertEqual(
            ordered_model_candidates(models)[0],
            [
                "gemini-flash-latest",
                "gemini-4.0-flash",
                "gemini-4.0-flash-preview",
            ],
        )

    def test_non_text_models_are_excluded_from_fallback(self) -> None:
        names = (
            "gemini-3.5-flash",
            "gemini-3.5-flash-image",
            "gemini-tts-preview",
            "gemini-robotics-er",
            "gemini-computer-use-preview",
            "gemini-deep-research",
            "lyria-realtime",
            "nano-banana-pro",
            "antigravity-model",
        )
        self.assertEqual(
            ordered_model_candidates(catalog(*names))[0],
            ["gemini-3.5-flash"],
        )

    def test_catalog_separates_list_generate_and_probe_results(self) -> None:
        listed = catalog("gemini-3.5-flash")
        listed.append({"name": "embed-only", "supported_actions": ["embedContent"]})
        document = model_catalog_document(
            listed,
            "gemini-3.5-flash",
            ["gemini-3.5-flash"],
            [{"name": "old-model", "status_code": 404, "reason": "not found"}],
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            document["listed_models"],
            ["gemini-3.5-flash", "embed-only"],
        )
        self.assertEqual(
            document["generate_content_models"],
            ["gemini-3.5-flash"],
        )
        self.assertEqual(
            document["probe_succeeded_models"],
            ["gemini-3.5-flash"],
        )
        self.assertEqual(document["probe_failed_models"][0]["status_code"], 404)
        self.assertEqual(document["generation_failed_models"], [])
        self.assertEqual(document["selected_model"], "gemini-3.5-flash")

    def test_transient_probe_failure_remains_body_fallback_candidate(self) -> None:
        self.assertEqual(
            body_model_candidates(
                ["primary", "busy-model", "removed-model"],
                "primary",
                [
                    {"name": "busy-model", "transient": True},
                    {"name": "removed-model", "transient": False},
                ],
            ),
            ["primary", "busy-model"],
        )


class ProbeError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"{status_code} {message}")
        self.status_code = status_code


class ProbeModels:
    def __init__(self, outcomes: dict[str, list[object]]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, str, object]] = []

    def generate_content(self, model: str, contents: str, config: object) -> object:
        self.calls.append((model, contents, config))
        outcome = self.outcomes[model].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(text=outcome)


class ProbeSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._captured_probe_output = io.StringIO()
        self._probe_redirect = redirect_stdout(self._captured_probe_output)
        self._probe_redirect.__enter__()

    def tearDown(self) -> None:
        self._probe_redirect.__exit__(None, None, None)

    def client(self, outcomes: dict[str, list[object]]) -> SimpleNamespace:
        return SimpleNamespace(models=ProbeModels(outcomes))

    def test_listed_model_404_falls_back_to_second_candidate(self) -> None:
        client = self.client(
            {
                "gemini-3.5-flash": [
                    ProbeError(404, "NOT_FOUND model no longer available")
                ],
                "gemini-3.5-flash-lite": ["OK"],
            }
        )
        selected, succeeded, failed = probe_and_select_model(
            client,
            catalog("gemini-3.5-flash", "gemini-3.5-flash-lite"),
            sleep=lambda _: None,
        )
        self.assertEqual(selected, "gemini-3.5-flash-lite")
        self.assertEqual(succeeded, ["gemini-3.5-flash-lite"])
        self.assertEqual(failed[0]["name"], "gemini-3.5-flash")
        self.assertEqual(failed[0]["status_code"], 404)
        self.assertEqual(
            [call[0] for call in client.models.calls],
            ["gemini-3.5-flash", "gemini-3.5-flash-lite"],
        )
        self.assertEqual(client.models.calls[0][1], "Reply OK.")
        self.assertEqual(client.models.calls[0][2].max_output_tokens, 1)

    def test_configured_model_failure_uses_automatic_fallback(self) -> None:
        client = self.client(
            {
                "custom-gemini-flash": [
                    ProbeError(403, "PERMISSION_DENIED access denied")
                ],
                "gemini-3.5-flash": ["OK"],
            }
        )
        selected, _, failed = probe_and_select_model(
            client,
            catalog("gemini-3.5-flash", "custom-gemini-flash"),
            configured="models/custom-gemini-flash",
            sleep=lambda _: None,
        )
        self.assertEqual(selected, "gemini-3.5-flash")
        self.assertEqual(failed[0]["name"], "custom-gemini-flash")

    def test_all_candidates_fail(self) -> None:
        client = self.client(
            {
                "gemini-3.5-flash": [ProbeError(404, "NOT_FOUND")],
                "gemini-3.5-flash-lite": [
                    ProbeError(403, "PERMISSION_DENIED")
                ],
            }
        )
        with self.assertRaisesRegex(ModelProbeError, "모든") as raised:
            probe_and_select_model(
                client,
                catalog("gemini-3.5-flash", "gemini-3.5-flash-lite"),
                sleep=lambda _: None,
            )
        self.assertEqual(len(raised.exception.failed_models), 2)

    def test_model_incompatible_400_falls_back(self) -> None:
        client = self.client(
            {
                "gemini-3.5-flash": [
                    ProbeError(
                        400,
                        "INVALID_ARGUMENT model does not support request",
                    )
                ],
                "gemini-3.5-flash-lite": ["OK"],
            }
        )
        selected, _, failed = probe_and_select_model(
            client,
            catalog("gemini-3.5-flash", "gemini-3.5-flash-lite"),
            sleep=lambda _: None,
        )
        self.assertEqual(selected, "gemini-3.5-flash-lite")
        self.assertEqual(failed[0]["status_code"], 400)

    def test_429_retries_same_model_then_succeeds(self) -> None:
        sleeps: list[float] = []
        client = self.client(
            {
                "gemini-3.5-flash": [
                    ProbeError(429, "RESOURCE_EXHAUSTED model not available now"),
                    ProbeError(429, "RESOURCE_EXHAUSTED model not available now"),
                    "OK",
                ]
            }
        )
        selected, succeeded, failed = probe_and_select_model(
            client,
            catalog("gemini-3.5-flash"),
            sleep=sleeps.append,
        )
        self.assertEqual(selected, "gemini-3.5-flash")
        self.assertEqual(succeeded, ["gemini-3.5-flash"])
        self.assertEqual(failed, [])
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(len(client.models.calls), 3)

    def test_503_retries_are_limited_then_falls_back(self) -> None:
        sleeps: list[float] = []
        client = self.client(
            {
                "gemini-3.5-flash": [
                    ProbeError(503, "UNAVAILABLE"),
                    ProbeError(503, "UNAVAILABLE"),
                    ProbeError(503, "UNAVAILABLE"),
                ],
                "gemini-3.5-flash-lite": ["OK"],
            }
        )
        selected, succeeded, failed = probe_and_select_model(
            client,
            catalog("gemini-3.5-flash", "gemini-3.5-flash-lite"),
            sleep=sleeps.append,
        )
        self.assertEqual(selected, "gemini-3.5-flash-lite")
        self.assertEqual(succeeded, ["gemini-3.5-flash-lite"])
        self.assertEqual(failed[0]["status_code"], 503)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(
            [call[0] for call in client.models.calls],
            ["gemini-3.5-flash"] * 3 + ["gemini-3.5-flash-lite"],
        )

    def test_exhausted_429_retries_then_falls_back(self) -> None:
        sleeps: list[float] = []
        client = self.client(
            {
                "gemini-3.5-flash": [
                    ProbeError(429, "RESOURCE_EXHAUSTED"),
                    ProbeError(429, "RESOURCE_EXHAUSTED"),
                    ProbeError(429, "RESOURCE_EXHAUSTED"),
                ],
                "gemini-3.5-flash-lite": ["OK"],
            }
        )
        selected, _, failed = probe_and_select_model(
            client,
            catalog("gemini-3.5-flash", "gemini-3.5-flash-lite"),
            sleep=sleeps.append,
        )
        self.assertEqual(selected, "gemini-3.5-flash-lite")
        self.assertEqual(failed[0]["status_code"], 429)
        self.assertEqual(sleeps, [1, 2])

    def test_all_transient_candidates_fail_with_clear_error(self) -> None:
        client = self.client(
            {
                "gemini-3.5-flash": [ProbeError(503, "UNAVAILABLE")] * 3,
                "gemini-3.5-flash-lite": [
                    ProbeError(429, "RESOURCE_EXHAUSTED")
                ]
                * 3,
            }
        )
        with self.assertRaisesRegex(
            ModelProbeError, "일시적 오류가 재시도 후에도"
        ) as raised:
            probe_and_select_model(
                client,
                catalog("gemini-3.5-flash", "gemini-3.5-flash-lite"),
                sleep=lambda _: None,
            )
        self.assertEqual(len(raised.exception.failed_models), 2)


class PipelineStageTests(unittest.TestCase):
    @staticmethod
    def response(
        update: dict[str, object],
        body_text: str = "세척실에서 배수구 청소를 시작했다. ",
        title: str = "세척실",
    ) -> str:
        public = f"# {title}\n\n" + (body_text * 80)
        return json.dumps(
            {"public_markdown": public, "state_update": update},
            ensure_ascii=False,
        )

    def test_plan_stage_returns_private_validated_plan(self) -> None:
        client = SimpleNamespace(
            models=ProbeModels(
                {"gemini-test": [json.dumps(valid_plan(), ensure_ascii=False)]}
            )
        )
        generated = generate_scene_plan(
            client,
            "gemini-test",
            {"history": [], "recent_scene_fingerprints": []},
            1,
        )
        self.assertEqual(generated["central_scene"], "세척실 첫 교대")
        self.assertEqual(client.models.calls[0][2].response_mime_type, "application/json")
        self.assertEqual(
            client.models.calls[0][2].max_output_tokens,
            GENERATION_MAX_OUTPUT_TOKENS,
        )

    def test_malformed_plan_json_is_regenerated_once(self) -> None:
        client = SimpleNamespace(
            models=ProbeModels(
                {
                    "gemini-test": [
                        '{"continuation_point": "잘린 응답',
                        json.dumps(valid_plan(), ensure_ascii=False),
                    ]
                }
            )
        )
        generated = generate_scene_plan(
            client,
            "gemini-test",
            {"history": [], "recent_scene_fingerprints": []},
            1,
        )
        self.assertEqual(generated["central_scene"], "세척실 첫 교대")
        self.assertEqual(len(client.models.calls), 2)
        self.assertIn(
            "직전 계획 응답은 불완전",
            client.models.calls[1][1],
        )

    def test_malformed_plan_json_retry_is_limited(self) -> None:
        client = SimpleNamespace(
            models=ProbeModels(
                {
                    "gemini-test": [
                        '{"continuation_point": "첫 번째 잘림',
                        '{"continuation_point": "두 번째 잘림',
                        '{"continuation_point": "세 번째 잘림',
                    ]
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "JSON 파싱 실패"):
            generate_scene_plan(
                client,
                "gemini-test",
                {"history": [], "recent_scene_fingerprints": []},
                1,
            )
        self.assertEqual(len(client.models.calls), 3)

    def test_body_stage_uses_plan_and_returns_separate_state(self) -> None:
        public = "# 세척실\n\n" + ("물비린내가 바닥에 남아 있었다. " * 80)
        response = json.dumps(
            {
                "public_markdown": public,
                "state_update": valid_update(),
            },
            ensure_ascii=False,
        )
        client = SimpleNamespace(
            models=ProbeModels({"gemini-test": [response]})
        )
        title, body, update = generate_episode_from_plan(
            client,
            "gemini-test",
            {"history": [], "recent_scene_fingerprints": []},
            1,
            valid_plan(),
        )
        self.assertEqual(title, "세척실")
        self.assertNotIn("state_update", body)
        self.assertEqual(update["actual_time_range"], "교대 시작 뒤 20분")

    def test_body_transient_failure_switches_to_probed_fallback_model(self) -> None:
        client = SimpleNamespace(
            models=ProbeModels({"gemini-fallback": ["OK"]})
        )
        transient = ProbeError(503, "high demand")
        successful = ("제목", "본문", valid_update())
        with patch(
            "scripts.generate_episode.generate_episode_from_plan",
            side_effect=[transient, successful],
        ) as generate:
            title, body, _, model, probed, failures = (
                generate_episode_with_model_fallback(
                    client,
                    ["gemini-primary", "gemini-fallback"],
                    {},
                    1,
                    valid_plan(),
                    already_probed=["gemini-primary"],
                    sleep=lambda _: None,
                )
            )

        self.assertEqual((title, body, model), ("제목", "본문", "gemini-fallback"))
        self.assertEqual(probed, ["gemini-primary", "gemini-fallback"])
        self.assertEqual(failures[0]["stage"], "body_generation")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            [call[0] for call in client.models.calls],
            ["gemini-fallback"],
        )

    def test_primary_arc_metadata_mismatch_is_normalized_without_retry(self) -> None:
        update = valid_update()
        update["primary_arc_progress"]["arc"] = "생계와 부채"
        client = SimpleNamespace(
            models=ProbeModels(
                {"gemini-test": [self.response(update)]}
            )
        )
        _, _, normalized = generate_episode_from_plan(
            client, "gemini-test", {}, 1, valid_plan()
        )
        self.assertEqual(
            normalized["primary_arc_progress"]["arc"],
            valid_plan()["active_arc"],
        )
        self.assertEqual(len(client.models.calls), 1)

    def test_missing_primary_arc_is_filled_from_plan(self) -> None:
        update = valid_update()
        del update["primary_arc_progress"]
        client = SimpleNamespace(
            models=ProbeModels(
                {"gemini-test": [self.response(update)]}
            )
        )
        _, _, normalized = generate_episode_from_plan(
            client, "gemini-test", {}, 1, valid_plan()
        )
        self.assertEqual(
            normalized["primary_arc_progress"]["arc"],
            valid_plan()["active_arc"],
        )

    def test_plan_primary_found_in_body_supporting_arcs_succeeds(self) -> None:
        update = valid_update()
        planned_progress = dict(update["primary_arc_progress"])
        update["primary_arc_progress"] = {
            **planned_progress,
            "arc": "생계와 부채",
        }
        update["supporting_arc_progress"] = [planned_progress]
        client = SimpleNamespace(
            models=ProbeModels(
                {"gemini-test": [self.response(update)]}
            )
        )
        _, _, normalized = generate_episode_from_plan(
            client, "gemini-test", {}, 1, valid_plan()
        )
        self.assertEqual(
            normalized["primary_arc_progress"]["arc"],
            "청소 노동과 신체 손상",
        )
        self.assertEqual(len(client.models.calls), 1)

    def test_location_label_difference_is_warning_only(self) -> None:
        update = valid_update()
        update["locations"] = ["지하 위생 작업 구역"]
        client = SimpleNamespace(
            models=ProbeModels(
                {"gemini-test": [self.response(update)]}
            )
        )
        _, _, normalized = generate_episode_from_plan(
            client, "gemini-test", {}, 1, valid_plan()
        )
        self.assertEqual(normalized["locations"], ["지하 세척실"])
        self.assertEqual(len(client.models.calls), 1)

    def test_missing_unfinished_action_alone_triggers_body_retry(self) -> None:
        state = {
            "current_scene": "문 앞",
            "unresolved_immediate_actions": ["문을 연다"],
        }
        client = SimpleNamespace(
            models=ProbeModels(
                {
                    "gemini-test": [
                        self.response(
                            valid_update(),
                            "세척실에서 배수구 청소를 시작했다. ",
                        ),
                        self.response(
                            valid_update(),
                            "문을 연다. 세척실에서 배수구 청소를 시작했다. ",
                        ),
                    ]
                }
            )
        )
        generate_episode_from_plan(
            client, "gemini-test", state, 1, valid_plan()
        )
        self.assertEqual(len(client.models.calls), 2)

    def test_retry_prompt_contains_the_actual_validation_failure(self) -> None:
        client = SimpleNamespace(
            models=ProbeModels(
                {
                    "gemini-test": [
                        self.response(
                            valid_update(),
                            "옥상에서 계약 승인 서류와 회의만 검토했다. ",
                            "옥상 회의",
                        ),
                        self.response(valid_update()),
                    ]
                }
            )
        )
        generate_episode_from_plan(
            client, "gemini-test", {}, 1, valid_plan()
        )
        self.assertIn("직전 응답 폐기 사유", client.models.calls[1][1])
        self.assertIn("계획의 중심 사건", client.models.calls[1][1])

    def test_long_immediate_action_accepts_natural_paraphrase(self) -> None:
        plan = valid_plan()
        plan["main_characters"] = ["강태수"]
        plan["central_scene"] = "반려 버튼 앞의 선택"
        state = {
            "unresolved_immediate_actions": [
                "반려 버튼 위의 손가락 묘사에서 직접 시작하여, 태수가 검증 "
                "시스템의 덫을 피해 갈 임시방편이나 내적 갈등을 묘사함"
            ]
        }
        report = body_plan_relevance_report(
            "선택",
            "태수는 반려 버튼 위에서 손가락을 떼고 임시방편을 생각했다.",
            plan,
            state,
        )
        self.assertTrue(report["relevant"], report["issues"])

    def test_replan_constraints_are_repaired_from_story_state(self) -> None:
        plan = valid_plan()
        state = {
            "current_scene": "반려 버튼 앞",
            "next_required_connection": "멈춘 손가락에서 직접 이어진다",
            "unresolved_immediate_actions": ["반려 여부를 결정한다"],
            "motif_cooldowns": {},
        }
        repaired, repairs = enforce_plan_state_constraints(plan, state)
        self.assertTrue(repaired["direct_continuation"])
        self.assertIn("반려 여부를 결정한다", repaired["continued_actions"])
        self.assertTrue(repairs)

    def test_output_validation_failure_switches_model(self) -> None:
        client = SimpleNamespace(
            models=ProbeModels({"gemini-fallback": ["OK"]})
        )
        successful = ("제목", "본문", valid_update())
        with patch(
            "scripts.generate_episode.generate_episode_from_plan",
            side_effect=[ValueError("truncated JSON"), successful],
        ) as generate:
            result = generate_episode_with_model_fallback(
                client,
                ["gemini-primary", "gemini-fallback"],
                {},
                1,
                valid_plan(),
                already_probed=["gemini-primary"],
                sleep=lambda _: None,
            )
        self.assertEqual(result[3], "gemini-fallback")
        self.assertFalse(result[5][0]["transient"])
        self.assertEqual(generate.call_count, 2)

    def test_completely_different_event_triggers_body_retry(self) -> None:
        client = SimpleNamespace(
            models=ProbeModels(
                {
                    "gemini-test": [
                        self.response(
                            valid_update(),
                            "옥상에서 계약 승인 서류와 회의만 검토했다. ",
                            "옥상 회의",
                        ),
                        self.response(
                            valid_update(),
                            "세척실에서 배수구 청소를 시작했다. ",
                        ),
                    ]
                }
            )
        )
        generate_episode_from_plan(
            client, "gemini-test", {}, 1, valid_plan()
        )
        self.assertEqual(len(client.models.calls), 2)

    def test_relevance_uses_body_not_metadata_labels(self) -> None:
        report = body_plan_relevance_report(
            "세척실",
            "세척실에서 배수구 청소를 시작했다.",
            valid_plan(),
            {},
        )
        self.assertTrue(report["relevant"])


class ResponseTests(unittest.TestCase):
    def test_parse_json_and_outer_fence(self) -> None:
        public = "# 야간 승인\n\n" + ("긴 문장입니다. " * 160)
        raw = "```json\n" + json.dumps(
            {"public_markdown": public, "state_update": valid_update()},
            ensure_ascii=False,
        ) + "\n```"
        parsed, update = parse_response(raw)
        self.assertEqual(parsed, public.strip())
        self.assertEqual(update["summary"], "요약")

    def test_invalid_json_and_short_body_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON"):
            parse_response("not json")
        raw = json.dumps(
            {"public_markdown": "# 제목\n\n짧음", "state_update": valid_update()},
            ensure_ascii=False,
        )
        with self.assertRaisesRegex(ValueError, "짧"):
            parse_response(raw)

    def test_extract_title_rejects_episode_number(self) -> None:
        body = "본문" * 700
        self.assertEqual(
            extract_title_and_body(f"# 교대 시간\n\n{body}")[0],
            "교대 시간",
        )
        with self.assertRaisesRegex(ValueError, "번호"):
            extract_title_and_body(f"# 1화. 교대 시간\n\n{body}")


class StateAndFileTests(unittest.TestCase):
    def test_daily_generation_guard_uses_kst_calendar_date(self) -> None:
        now = datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc)
        self.assertTrue(
            generated_on_kst_date(
                {"last_generated_at": "2026-01-01T15:30:00+00:00"},
                now,
            )
        )
        self.assertFalse(
            generated_on_kst_date(
                {"last_generated_at": "2026-01-01T14:30:00+00:00"},
                now,
            )
        )

    def test_calculate_next_episode_and_mismatch(self) -> None:
        paths = [Path("001.md"), Path("002.md")]
        self.assertEqual(calculate_next_episode({"next_episode": 3}, paths), 3)
        with self.assertRaisesRegex(ValueError, "일치하지"):
            calculate_next_episode({"next_episode": 2}, paths)

    def test_merge_state_is_non_mutating_and_deduplicates_facts(self) -> None:
        state = {
            "next_episode": 1,
            "history": [],
            "new_facts": ["기존 사실"],
        }
        update = valid_update()
        update["new_facts"] = ["기존 사실", "새 사실"]
        merged = merge_state(
            state,
            update,
            1,
            "제목",
            valid_plan(),
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(state["next_episode"], 1)
        self.assertEqual(merged["next_episode"], 2)
        self.assertEqual(merged["new_facts"], ["기존 사실", "새 사실"])
        self.assertEqual(merged["history"][0]["episode"], 1)
        self.assertEqual(merged["recent_scene_fingerprints"][0]["episode"], 1)
        self.assertEqual(merged["current_scene_started_episode"], 1)
        self.assertEqual(
            merged["arc_states"]["청소 노동과 신체 손상"][
                "last_progress_episode"
            ],
            1,
        )

    def test_merge_state_bounds_compressed_history_and_facts(self) -> None:
        state = {
            "next_episode": 21,
            "history": [
                {"episode": number, "title": "제목", "summary": "요약"}
                for number in range(1, 21)
            ],
            "new_facts": [f"사실 {number}" for number in range(60)],
        }
        update = valid_update()
        update["new_facts"] = ["새 사실"]
        merged = merge_state(state, update, 21, "제목", valid_plan())
        self.assertEqual(len(merged["history"]), 20)
        self.assertEqual(merged["history"][0]["episode"], 2)
        self.assertEqual(len(merged["new_facts"]), 60)
        self.assertEqual(merged["new_facts"][-1], "새 사실")

    def test_same_mock_update_merges_deterministically(self) -> None:
        state = {"next_episode": 1, "history": [], "new_facts": []}
        generated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = merge_state(
            state,
            valid_update(),
            1,
            "제목",
            valid_plan(),
            generated_at=generated_at,
        )
        second = merge_state(
            state,
            valid_update(),
            1,
            "제목",
            valid_plan(),
            generated_at=generated_at,
        )
        self.assertEqual(first, second)

    def test_merge_uses_all_plan_owned_structural_fields(self) -> None:
        update = valid_update()
        update["locations"] = ["다른 장소"]
        update["character_combination"] = ["다른 인물"]
        update["conflict_types"] = ["다른 갈등"]
        update["social_themes"] = ["다른 주제"]
        update["narrative_mode"] = "다른 서술"
        update["ending_pattern"] = "다른 결말"
        update["direct_continuation"] = True
        update["motif_cooldown_updates"] = ["major_accident"]
        merged = merge_state(
            {"next_episode": 1, "history": [], "new_facts": []},
            update,
            1,
            "제목",
            valid_plan(),
        )
        fingerprint = merged["recent_scene_fingerprints"][-1]
        self.assertEqual(merged["active_arc"], valid_plan()["active_arc"])
        self.assertEqual(merged["supporting_arcs"], valid_plan()["supporting_arcs"])
        self.assertEqual(fingerprint["locations"], [valid_plan()["location"]])
        self.assertEqual(
            fingerprint["character_combination"],
            valid_plan()["main_characters"],
        )
        self.assertEqual(
            fingerprint["conflict_type"], [valid_plan()["conflict_type"]]
        )
        self.assertEqual(
            fingerprint["social_theme"], [valid_plan()["social_theme"]]
        )
        self.assertEqual(merged["narrative_mode"], valid_plan()["narrative_mode"])

    def test_atomic_create_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "001.md"
            atomic_create_text(path, "first")
            with self.assertRaises(FileExistsError):
                atomic_create_text(path, "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "first")

    def test_state_write_failure_rolls_back_new_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            episode_path = root / "episodes" / "001.md"
            state_path = root / "story_state.json"
            state_path.write_text('{"next_episode": 1}\n', encoding="utf-8")
            with (
                patch(
                    "scripts.generate_episode.atomic_write_text",
                    side_effect=OSError("simulated state failure"),
                ),
                self.assertRaisesRegex(OSError, "simulated"),
            ):
                save_episode_and_state(
                    episode_path,
                    "complete episode",
                    state_path,
                    '{"next_episode": 2}\n',
                )
            self.assertFalse(episode_path.exists())
            self.assertEqual(
                state_path.read_text(encoding="utf-8"),
                '{"next_episode": 1}\n',
            )

    def test_retry_only_transient_failures(self) -> None:
        calls = 0
        sleeps: list[float] = []

        class TransientError(Exception):
            status_code = 429

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TransientError()
            return "ok"

        self.assertEqual(
            call_with_retry(operation, "test", sleep=sleeps.append),
            "ok",
        )
        self.assertEqual(sleeps, [1, 2])


class LoggingAndNetworkSafetyTests(unittest.TestCase):
    def test_default_catalog_log_is_compact(self) -> None:
        large_catalog = catalog(*(f"gemini-model-{number}" for number in range(56)))
        output = io.StringIO()
        with redirect_stdout(output):
            log_model_catalog_summary(
                large_catalog,
                ["gemini-model-1", "gemini-model-2"],
            )
        logged = output.getvalue()
        self.assertIn("전체 56개", logged)
        self.assertIn("후보 모델: gemini-model-1, gemini-model-2", logged)
        self.assertNotIn("gemini-model-55", logged)

    def test_probe_reason_does_not_log_full_response_dict(self) -> None:
        error = ProbeError(
            503,
            "{'error': {'code': 503, 'message': 'service unavailable', "
            "'details': ['very long response body']}}",
        )
        reason = safe_error_reason(error)
        self.assertIn("service unavailable", reason)
        self.assertNotIn("details", reason)

    def test_ci_client_creation_is_blocked_without_explicit_generation_opt_in(
        self,
    ) -> None:
        with (
            patch.dict(
                os.environ,
                {"CI": "true", "SERIAL_NOVEL_ALLOW_GEMINI_NETWORK": "0"},
                clear=False,
            ),
            patch("scripts.generate_episode.genai.Client") as client,
        ):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                create_gemini_client("fake-key")
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
