from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.generate_episode import (
    atomic_create_text,
    calculate_next_episode,
    call_with_retry,
    extract_title_and_body,
    list_generation_models,
    merge_state,
    parse_response,
    select_model,
)


def catalog(*names: str) -> list[dict[str, object]]:
    return [{"name": name} for name in names]


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
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        )
        self.assertEqual(select_model(models), "gemini-2.5-flash")
        self.assertEqual(
            select_model(models, "models/gemini-2.5-pro"),
            "gemini-2.5-pro",
        )

    def test_stable_fallback_beats_preview(self) -> None:
        models = catalog("gemini-flash-preview", "gemini-pro-stable")
        self.assertEqual(select_model(models), "gemini-pro-stable")

    def test_unavailable_configured_model_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "GEMINI_MODEL"):
            select_model(catalog("gemini-2.5-flash"), "missing")


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
