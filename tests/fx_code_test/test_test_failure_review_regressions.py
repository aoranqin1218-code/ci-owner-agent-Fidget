from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ci_owner_agent.config import load_settings
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice, Owner, ResponsibilityItem
from ci_owner_agent.services.history_store import MongoHistoryStore, get_history_store
from ci_owner_agent.services.test_failure_stats import TestFailureStatsService
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig, load_weekly_test_report_config
from ci_owner_agent.services.weekly_test_report_formatter import classify_weekly_report_stats
from scripts.backfill_test_file_failures import backfill
from tests.fx_code_test.test_history_store import make_store


def _notice(repo: str, build: int, paths: list[str | None]) -> CiResponsibilityNotice:
    owner = Owner(type="no_high_confidence_owner", name="暂无高置信责任人", confidence=0)
    items = [ResponsibilityItem(failureId=f"f-{index}", failureTitle="failure", failureSignature=f"sig-{index}",
             testFilePath=path, owner=owner, responsibilityType="no_high_confidence_owner", confidence=0, reason="x")
             for index, path in enumerate(paths)]
    return CiResponsibilityNotice(repo=repo, job="job-x", buildNumber=build, buildUrl="u", result="FAILURE",
                                 branch="dev", owner=owner, failureReason="x", responsibilityItems=items,
                                 hasHighConfidenceOwner=False)


def test_get_history_store_handles_disabled_success_and_failure(monkeypatch, capsys):
    settings = load_settings()
    assert get_history_store(replace(settings, history_enabled=False)) is None
    expected = object()
    monkeypatch.setattr(MongoHistoryStore, "from_settings", classmethod(lambda cls, value: expected))
    assert get_history_store(replace(settings, history_enabled=True)) is expected
    monkeypatch.setattr(MongoHistoryStore, "from_settings", classmethod(lambda cls, value: (_ for _ in ()).throw(RuntimeError("mongo down"))))
    assert get_history_store(replace(settings, history_enabled=True)) is None
    assert "history store unavailable: mongo down" in capsys.readouterr().err


def test_repo_scopes_history_queries_and_allows_same_build_number():
    store = make_store()
    for repo in ("repo-a", "repo-b"):
        notice = _notice(repo, 100, ["test/A.test.ts"])
        build = BuildInfo(job="job-x", buildNumber=100, result="FAILURE", buildUrl="u", branch="dev", commit=repo)
        store.save_analysis(build, notice, None, repo, None, None, [{"schemaVersion": 3, "chunkSource": "notice_failure_summary", "content": repo}])
    assert len(store.builds.docs) == 2
    assert store.find_previous_build(repo="repo-a", job="job-x", branch="dev", current_build_number=101)["headCommit"] == "repo-a"
    chunks = store.find_historical_failure_chunks("repo-a", "job-x", "dev", 101, None)
    assert {item["repo"] for item in chunks} == {"repo-a"}


def test_backfill_uses_failure_facts_and_dry_run_counts_grouped_paths():
    store = make_store(); notice = _notice("repo-a", 1, [None, None, None])
    for item in notice.responsibilityItems:
        item.failureSignature = "sig"
    store.notices.docs.append({"repo": "repo-a", "job": "job-x", "branch": "dev", "buildNumber": 1,
                               "notice": notice.model_dump(mode="json")})
    store.failure_facts.docs.append({"repo": "repo-a", "job": "job-x", "branch": "dev", "buildNumber": 1,
                                     "fact": {"signatureKey": "sig", "filePath": "test/service/A.test.ts"}})
    dry = backfill(store, repo="repo-a", dry_run=True)
    assert dry["createdEventCount"] == 1
    actual = backfill(store, repo="repo-a")
    assert actual["createdEventCount"] == 1
    assert store.test_file_failures.docs[0]["testFilePath"] == "test/service/A.test.ts"
    assert store.test_file_failures.docs[0]["failureItemCount"] == 3


def test_empty_important_rules_are_rejected_but_disabled_rules_are_allowed():
    try:
        WeeklyTestReportConfig.model_validate({"important": {"enabled": True, "rules": []}})
    except ValueError:
        pass
    else:
        raise AssertionError("empty enabled rules must be rejected")
    WeeklyTestReportConfig.model_validate({"important": {"enabled": False, "rules": []}})
    assert load_weekly_test_report_config(Path("config/weekly-test-report.yml")).important.rules


def test_weekly_groups_exclude_historical_only_normal_items():
    config = WeeklyTestReportConfig.model_validate({"important": {"enabled": False}, "normal": {"minimumWeeklyFailedBuildCount": 1}})
    store = make_store(); now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    for index in range(52):
        store.test_file_failures.docs.append({"repo": "r", "job": "j", "branch": "dev", "buildNumber": index,
                                              "testFilePath": f"test/{index}.test.ts", "failureItemCount": 1,
                                              "buildTimestamp": now if index < 2 else datetime(2026, 1, 1, tzinfo=timezone.utc)})
    stats = TestFailureStatsService(store, config).aggregate("r", ["j"], ["dev"], datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc))
    groups = classify_weekly_report_stats(stats, config)
    assert len(groups.normal) == 2 and len(groups.ignored) == 50
