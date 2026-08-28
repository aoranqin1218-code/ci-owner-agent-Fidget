from __future__ import annotations

from datetime import datetime, timezone

from ci_owner_agent.services.test_failure_stats import TestFailureStatsService
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig
from tests.fx_code_test.test_history_store import make_store


def _build(store, number, result, timestamp, branch="dev"):
    store.builds.docs.append({"repo": "r", "job": "j", "branch": branch, "buildNumber": number,
                              "result": result, "buildTimestamp": timestamp})


def _event(store, number, timestamp, items=1, path="test/A.test.ts", branch="dev"):
    store.test_file_failures.docs.append({"repo": "r", "job": "j", "branch": branch, "buildNumber": number,
                                          "testFilePath": path, "failureItemCount": items,
                                          "buildTimestamp": timestamp})


def test_aggregate_counts_items_builds_period_and_consecutive_sequence():
    store = make_store()
    t = datetime(2026, 7, 10, tzinfo=timezone.utc)
    _build(store, 110, "FAILURE", t); _event(store, 110, t, 3)
    _build(store, 109, "ABORTED", t)
    _build(store, 108, "FAILURE", t); _event(store, 108, t)
    _build(store, 107, "UNSTABLE", t); _event(store, 107, t, 2)
    _build(store, 106, "SUCCESS", t)
    _event(store, 80, None, 4)
    config = WeeklyTestReportConfig.model_validate({"important": {"rules": [
        {"name": "frequent", "thresholds": {"weeklyFailedBuildCount": 3}},
        {"name": "streak", "thresholds": {"consecutiveFailureCount": 3}},
    ]}})
    stat = TestFailureStatsService(store, config).aggregate("r", ["j"], ["dev"],
        datetime(2026, 7, 6, tzinfo=timezone.utc), datetime(2026, 7, 13, tzinfo=timezone.utc))[0]
    assert stat.totalFailedBuildCount == 4
    assert stat.totalFailureItemCount == 10
    assert stat.periodFailedBuildCount == 3
    assert stat.periodCompletedBuildCount == 4
    assert stat.periodFailureRate == .75
    assert stat.currentConsecutiveFailureCount == 3
    assert stat.isImportant is True
    assert stat.matchedRuleNames == ["frequent", "streak"]


def test_other_failure_breaks_streak_unknown_without_event_skips_and_branches_isolate():
    store = make_store(); t = datetime(2026, 7, 10, tzinfo=timezone.utc)
    _build(store, 10, "FAILURE", t); _event(store, 10, t)
    _build(store, 9, "UNKNOWN", t)
    _build(store, 8, "FAILURE", t)  # another test failed
    _build(store, 7, "FAILURE", t); _event(store, 7, t)
    _build(store, 99, "FAILURE", t, branch="other"); _event(store, 99, t, branch="other")
    stat = TestFailureStatsService(store).aggregate("r", ["j"], ["dev"],
        datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc))[0]
    assert stat.currentConsecutiveFailureCount == 1
    assert stat.totalFailedBuildCount == 2


def test_aggregate_normalizes_branch_scope_like_weekly_reports():
    store = make_store(); t = datetime(2026, 7, 10, tzinfo=timezone.utc)
    _build(store, 1, "FAILURE", t); _event(store, 1, t)
    assert len(TestFailureStatsService(store).aggregate("r", ["j"], ["refs/remotes/origin/dev"], t.replace(day=1), t.replace(day=20))) == 1


def test_aggregate_empty_or_invalid_branch_filter_matches_no_branches():
    store = make_store(); t = datetime(2026, 7, 10, tzinfo=timezone.utc)
    _build(store, 1, "FAILURE", t, branch="dev"); _event(store, 1, t, branch="dev")
    _build(store, 2, "FAILURE", t, branch="release"); _event(store, 2, t, branch="release")
    service = TestFailureStatsService(store)
    assert service.aggregate("r", ["j"], ["refs/tags/v1"], t.replace(day=1), t.replace(day=20)) == []
    assert service.aggregate("r", ["j"], [], t.replace(day=1), t.replace(day=20)) == []
    assert len(service.aggregate("r", ["j"], None, t.replace(day=1), t.replace(day=20))) == 2
