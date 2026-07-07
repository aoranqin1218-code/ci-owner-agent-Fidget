from __future__ import annotations

import sys

from scripts.batch_analyze_jenkins_builds import (
    build_analyze_command,
    extract_ai_history_stats_from_trace_or_notice,
    extract_first_json_object,
    extract_responsibility_stats_from_notice,
    metadata_matches_jenkins,
    parse_builds,
)


def test_parse_builds_list_range_and_union():
    assert parse_builds("7,13", None, None) == [7, 13]
    assert parse_builds(None, 7, 9) == [7, 8, 9]
    assert parse_builds("7,13", 8, 10) == [7, 8, 9, 10, 13]


def test_build_analyze_command_flags():
    command = build_analyze_command(
        build=13,
        repo="fx-code",
        job="CI-test/unintest-MatureLeek",
        log_tail_lines=200,
        notify=False,
        notify_dry_run=False,
        force_notify=False,
    )
    assert command[:4] == [sys.executable, "-m", "ci_owner_agent", "analyze"]
    assert "--notify" not in command
    assert "--branch" not in command
    assert "--base-commit" not in command
    assert "--head-commit" not in command
    assert "--console-file" not in command
    assert "--build-url" not in command
    assert "--last-success-build" not in command

    command = build_analyze_command(
        build=13,
        repo="fx-code",
        job="CI-test/unintest-MatureLeek",
        log_tail_lines=200,
        notify=True,
        notify_dry_run=True,
        force_notify=True,
    )
    assert "--notify" in command
    assert "--notify-dry-run" in command
    assert "--force-notify" in command


def test_metadata_matches_jenkins_uses_job_repo_build_only():
    assert metadata_matches_jenkins(
        {
            "job": "CI-test/unintest-MatureLeek",
            "repo": "fx-code",
            "buildNumber": 13,
            "baseCommit": "different",
            "headCommit": "different",
        },
        build=13,
        repo="fx-code",
        job="CI-test/unintest-MatureLeek",
    )
    assert not metadata_matches_jenkins(
        {"job": "CI-test/unintest-MatureLeek", "repo": "fx-code", "buildNumber": 12},
        build=13,
        repo="fx-code",
        job="CI-test/unintest-MatureLeek",
    )


def test_extract_first_json_object_from_noisy_stdout():
    parsed = extract_first_json_object('noise before\n{"job":"j","buildNumber":13}\nnoise after')
    assert parsed == {"job": "j", "buildNumber": 13}


def test_extract_ai_history_stats_from_nested_trace():
    trace = {
        "child_runs": [
            {
                "inputs": {
                    "payload": {
                        "aiHistoryPrecheck": {
                            "diagnostics": {
                                "eligibleCurrentFactsCount": 1,
                                "historicalBuildsCount": 1,
                                "historicalFactsCount": 1,
                                "rankedPairsCount": 1,
                                "comparedPairsCount": 1,
                                "acceptedCandidatesCount": 1,
                                "queryStage": "ok",
                                "skipped": {"compareNotSameFailure": 0},
                            }
                        }
                    }
                }
            }
        ]
    }
    stats = extract_ai_history_stats_from_trace_or_notice({}, trace)
    assert stats["aiHistoryEligibleCurrentFactsCount"] == 1
    assert stats["aiHistoryHistoricalBuildsCount"] == 1
    assert stats["aiHistoryHistoricalFactsCount"] == 1
    assert stats["aiHistoryRankedPairsCount"] == 1
    assert stats["aiHistoryComparedPairsCount"] == 1
    assert stats["aiHistoryAcceptedCandidatesCount"] == 1
    assert stats["aiHistoryQueryStage"] == "ok"
    assert "compareNotSameFailure" in stats["aiHistorySkipped"]


def test_summary_responsibility_stats_for_mixed_items():
    stats = extract_responsibility_stats_from_notice(
        {
            "responsibilityItems": [
                {
                    "owner": {"type": "inherited_failure_owner", "name": "Tang"},
                    "responsibilityType": "inherited_failure_owner",
                    "sourceBuildNumber": 7,
                },
                {
                    "owner": {"type": "high_confidence", "name": "Li"},
                    "responsibilityType": "current_build_owner",
                },
                {
                    "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人"},
                    "responsibilityType": "no_high_confidence_owner",
                },
            ]
        }
    )
    assert stats["responsibilityItemCount"] == 3
    assert stats["responsibleOwners"] == "Tang(inherited from #7); Li(high_confidence)"
    assert stats["inheritedOwners"] == "Tang"
    assert stats["currentBuildOwners"] == "Li"
    assert stats["unresolvedFailureCount"] == 1
