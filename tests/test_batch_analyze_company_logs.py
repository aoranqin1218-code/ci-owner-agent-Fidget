from __future__ import annotations

import pytest

from scripts.batch_analyze_company_logs import (
    build_analyze_command,
    extract_history_stats_from_notice,
    extract_responsibility_stats_from_notice,
    filter_logs_by_build_range,
    load_logs,
    main,
)


def write_log(log_dir, build: int, commit: str, status: str) -> None:
    log_dir.joinpath(f"company-unittest-{build}.log").write_text(
        f"Checking out Revision {commit}\nAssertionError\nFinished: {status}\n",
        encoding="utf-8",
    )


def test_batch_analyze_passes_last_success_build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    success_commit = "1" * 40
    failure_commit = "2" * 40
    (log_dir / "company-unittest-5075.log").write_text(
        f"Checking out Revision {success_commit}\nFinished: SUCCESS\n",
        encoding="utf-8",
    )
    (log_dir / "company-unittest-5076.log").write_text(
        f"Checking out Revision {failure_commit}\nAssertionError\nFinished: FAILURE\n",
        encoding="utf-8",
    )
    logs = load_logs(log_dir, "*.log")
    failure = next(item for item in logs if item.build == 5076)
    assert failure.base_commit == success_commit
    assert failure.last_success_build_number == 5075

    command = build_analyze_command(
        item=failure,
        repo="fx-code",
        job="services/fx-code-unittest",
        branch="dev",
        build_url_prefix="local://services/fx-code-unittest",
    )
    assert "--last-success-build" in command
    assert command[command.index("--last-success-build") + 1] == "5075"


def test_build_range_does_not_break_base_commit_calculation(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    success_commit = "1" * 40
    failure_commit = "2" * 40
    write_log(log_dir, 5068, success_commit, "SUCCESS")
    write_log(log_dir, 5072, failure_commit, "FAILURE")

    logs_all = load_logs(log_dir, "*.log")
    selected = filter_logs_by_build_range(logs_all, 5072, None)
    failure = selected[0]
    assert failure.build == 5072
    assert failure.base_commit == success_commit
    assert failure.last_success_build_number == 5068


def test_build_from_to_filters_selected_logs(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for build in [5072, 5073, 5076, 5080]:
        write_log(log_dir, build, str(build % 10) * 40, "FAILURE")

    selected = filter_logs_by_build_range(load_logs(log_dir, "*.log", initial_base_commit="a" * 40), 5072, 5076)
    assert [item.build for item in selected] == [5072, 5073, 5076]


def test_limit_applies_after_build_range_filter(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for build in [5072, 5073, 5076, 5080]:
        write_log(log_dir, build, str(build % 10) * 40, "FAILURE")

    selected = filter_logs_by_build_range(load_logs(log_dir, "*.log", initial_base_commit="a" * 40), 5072, 5080)
    runnable = [item for item in selected if item.status == "FAILURE" and not item.skip_reason]
    limited = runnable[:2]
    assert [item.build for item in limited] == [5072, 5073]


def test_build_from_greater_than_build_to_errors(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        [
            "batch_analyze_company_logs.py",
            "--log-dir",
            str(log_dir),
            "--build-from",
            "5076",
            "--build-to",
            "5072",
            "--dry-run",
        ],
    )
    with pytest.raises(SystemExit):
        main()


def test_extract_history_stats_from_structured_notice():
    stats = extract_history_stats_from_notice(
        {
            "history_search_similar_failures": {
                "ok": True,
                "candidates": [
                    {
                        "buildNumber": 5111,
                        "similarity": 1.0,
                        "matchType": "signature_exact",
                        "relationship": "very_likely_same_failure",
                    },
                    {"buildNumber": 5110, "similarity": 1.0},
                ],
            }
        }
    )
    assert stats["historicalMatchCount"] == 2
    assert stats["topHistoricalMatchBuild"] == 5111
    assert stats["topHistoricalMatchSimilarity"] == 1.0
    assert stats["topHistoricalMatchType"] == "signature_exact"
    assert stats["topHistoricalRelationship"] == "very_likely_same_failure"


def test_extract_history_stats_no_candidates_and_warning():
    no_candidates = extract_history_stats_from_notice({"history_search_similar_failures": {"ok": True, "candidates": []}})
    assert no_candidates["historicalMatchCount"] == 0
    assert no_candidates["topHistoricalMatchBuild"] is None

    warning = extract_history_stats_from_notice(
        {"history_search_similar_failures": {"ok": True, "warning": "test failure summaries unavailable; history similarity skipped", "candidates": []}}
    )
    assert warning["historicalMatchCount"] == 0
    assert warning["topHistoricalMatchBuild"] is None


def test_extract_history_stats_from_notice_evidence_text():
    stats = extract_history_stats_from_notice(
        {
            "evidence": [
                {
                    "source": "history_search_similar_failures",
                    "summary": "历史相似失败检查命中",
                    "detail": (
                        "history_search_similar_failures 返回 5 个候选，"
                        "top build=5111，similarity=1.0，matchType=signature_exact，"
                        "relationship=very_likely_same_failure"
                    ),
                }
            ]
        }
    )
    assert stats["historicalMatchCount"] == 5
    assert stats["topHistoricalMatchBuild"] == 5111
    assert stats["topHistoricalMatchSimilarity"] == 1.0
    assert stats["topHistoricalMatchType"] == "signature_exact"
    assert stats["topHistoricalRelationship"] == "very_likely_same_failure"


def test_extract_responsibility_stats_from_notice_items():
    stats = extract_responsibility_stats_from_notice(
        {
            "responsibilityItems": [
                {
                    "failureId": "F1",
                    "failureTitle": "historical failure",
                    "owner": {
                        "type": "inherited_failure_owner",
                        "name": "Tang.Tangerine-唐嘉伟",
                        "email": "tang@example.com",
                        "commit": "ab286e5",
                        "confidence": 0.9,
                    },
                    "responsibilityType": "inherited_failure_owner",
                    "sourceBuildNumber": 5104,
                    "confidence": 1.0,
                    "reason": "历史持续失败。",
                },
                {
                    "failureId": "F2",
                    "failureTitle": "new failure",
                    "owner": {
                        "type": "high_confidence",
                        "name": "Li Si",
                        "email": "lisi@example.com",
                        "commit": "f" * 40,
                        "confidence": 0.88,
                    },
                    "responsibilityType": "current_build_owner",
                    "confidence": 0.88,
                    "reason": "新失败可定责。",
                },
                {
                    "failureId": "F3",
                    "failureTitle": "unknown failure",
                    "owner": {
                        "type": "no_high_confidence_owner",
                        "name": "无高可信责任人",
                        "email": None,
                        "commit": None,
                        "confidence": 0,
                    },
                    "responsibilityType": "no_high_confidence_owner",
                    "confidence": 0,
                    "reason": "证据不足。",
                },
            ]
        }
    )
    assert stats["responsibilityItemCount"] == 3
    assert stats["responsibleOwners"] == "Tang.Tangerine-唐嘉伟(inherited from #5104); Li Si(high_confidence)"
    assert stats["inheritedOwners"] == "Tang.Tangerine-唐嘉伟"
    assert stats["currentBuildOwners"] == "Li Si"
    assert stats["unresolvedFailureCount"] == 1


def test_extract_responsibility_stats_backward_compatible_without_items():
    stats = extract_responsibility_stats_from_notice({"owner": {"type": "high_confidence", "name": "Zhang San"}})
    assert stats["responsibilityItemCount"] == 0
    assert stats["responsibleOwners"] == ""
    assert stats["unresolvedFailureCount"] == 0
