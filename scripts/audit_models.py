from __future__ import annotations

import argparse
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

try:
    from scripts.generate_episode import (
        atomic_write_text,
        call_with_retry,
        create_gemini_client,
        error_status_code,
        is_transient_error,
        list_models,
        log,
        model_catalog_document,
        ordered_model_candidates,
        probe_model,
        safe_error_reason,
        utc_now,
    )
except ModuleNotFoundError:
    from generate_episode import (  # type: ignore[no-redef]
        atomic_write_text,
        call_with_retry,
        create_gemini_client,
        error_status_code,
        is_transient_error,
        list_models,
        log,
        model_catalog_document,
        ordered_model_candidates,
        probe_model,
        safe_error_reason,
        utc_now,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gemini model catalog and generateContent compatibility audit"
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def audit_candidates(
    client: Any,
    candidates: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    succeeded: list[str] = []
    failed: list[dict[str, Any]] = []
    for index, model in enumerate(candidates, start=1):
        log(f"반기 모델 감사 probe: {model} ({index}/{len(candidates)})")
        try:
            probe_model(client, model)
        except Exception as exc:
            failure = {
                "name": model,
                "status_code": error_status_code(exc) or "UNKNOWN",
                "reason": safe_error_reason(exc),
                "transient": is_transient_error(exc),
            }
            failed.append(failure)
            log(
                f"반기 모델 감사 실패: {model} "
                f"status={failure['status_code']} reason={failure['reason']}"
            )
        else:
            succeeded.append(model)
            log(f"반기 모델 감사 성공: {model}")
    return succeeded, failed


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 2

    checked_at = utc_now()
    client = create_gemini_client(api_key)
    catalog = call_with_retry(lambda: list_models(client), "감사 모델 목록 조회")
    candidates, initial_failures = ordered_model_candidates(
        catalog, os.environ.get("GEMINI_MODEL")
    )
    succeeded, probe_failures = audit_candidates(client, candidates)
    failures = initial_failures + probe_failures
    selected = succeeded[0] if succeeded else None
    report = model_catalog_document(
        catalog,
        selected,
        succeeded,
        failures,
        checked_at,
    )
    report.update(
        {
            "audit_kind": "semiannual_model_api_compatibility",
            "google_genai_version": version("google-genai"),
            "candidate_count": len(candidates),
            "successful_candidate_count": len(succeeded),
            "all_ordered_candidates": candidates,
        }
    )
    atomic_write_text(
        args.output,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )
    log(
        f"반기 모델 감사 완료: 후보 {len(candidates)}개, "
        f"성공 {len(succeeded)}개, 실패 {len(failures)}개"
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
