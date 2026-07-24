from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.generate_episode import (
    ModelProbeError,
    PREFERRED_MODELS,
    atomic_create_text,
    calculate_next_episode,
    call_with_retry,
    extract_title_and_body,
    list_generation_models,
    merge_state,
    model_catalog_document,
    ordered_model_candidates,
    parse_response,
    probe_and_select_model,
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
        self.assertEqual(document["selected_model"], "gemini-3.5-flash")


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

    def test_503_retries_are_limited_and_do_not_fallback(self) -> None:
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
        with self.assertRaisesRegex(ModelProbeError, "일시적"):
            probe_and_select_model(
                client,
                catalog("gemini-3.5-flash", "gemini-3.5-flash-lite"),
                sleep=sleeps.append,
            )
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(
            [call[0] for call in client.models.calls],
            ["gemini-3.5-flash"] * 3,
        )


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
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(state["next_episode"], 1)
        self.assertEqual(merged["next_episode"], 2)
        self.assertEqual(merged["new_facts"], ["기존 사실", "새 사실"])
        self.assertEqual(merged["history"][0]["episode"], 1)

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
        merged = merge_state(state, update, 21, "제목")
        self.assertEqual(len(merged["history"]), 20)
        self.assertEqual(merged["history"][0]["episode"], 2)
        self.assertEqual(len(merged["new_facts"]), 60)
        self.assertEqual(merged["new_facts"][-1], "새 사실")

    def test_atomic_create_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "001.md"
            atomic_create_text(path, "first")
            with self.assertRaises(FileExistsError):
                atomic_create_text(path, "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "first")

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


if __name__ == "__main__":
    unittest.main()
