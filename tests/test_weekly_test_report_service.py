from __future__ import annotations

from datetime import datetime, timezone

from ci_owner_agent.schemas import TestFileFailureStat
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig
from ci_owner_agent.services.weekly_test_report_formatter import format_weekly_test_report
from ci_owner_agent.services.weekly_test_report_service import WeeklyTestReportService
from ci_owner_agent.services.scope_normalization import normalize_branch_scope_values, normalize_scope_values
from tests.test_history_store import make_store


START = datetime(2026, 7, 6, tzinfo=timezone.utc)
END = datetime(2026, 7, 13, tzinfo=timezone.utc)


def _stat(path="test/A.test.ts", important=True):
    return TestFileFailureStat(repo="r", job="j", branch="dev", testFilePath=path,
        totalFailedBuildCount=5, totalFailureItemCount=8, currentConsecutiveFailureCount=2,
        periodFailedBuildCount=3, periodFailureItemCount=4, periodCompletedBuildCount=10,
        periodFailureRate=.3, lastFailureBuildNumber=10, isImportant=important,
        matchedRuleNames=["frequent"] if important else [])


def test_formatter_strictly_separates_normal_items_without_mentions_and_hides_details():
    config = WeeklyTestReportConfig.model_validate({"important": {"enabled": False}, "normal": {"includeBelowThreshold": False}})
    markdown = format_weekly_test_report([_stat(important=False)], period_start=START, period_end=END, config=config)
    assert "其他失败测试" in markdown
    assert "共 1 个测试文件" in markdown
    assert "test/A.test.ts" not in markdown
    assert "<@" not in markdown


def test_formatter_top_n_reports_omitted_count_without_changing_priority():
    config = WeeklyTestReportConfig.model_validate({"important": {"enabled": False}, "topN": 1})
    stats = [_stat("test/A.test.ts"), _stat("test/B.test.ts")]
    markdown = format_weekly_test_report(stats, period_start=START, period_end=END, config=config)
    assert "另有 1 项" in markdown
    assert all(item.isImportant for item in stats)


def test_formatter_length_limit_removes_whole_items_and_reports_omission():
    config = WeeklyTestReportConfig.model_validate({"important": {"enabled": False}, "topN": 20})
    stats = [_stat(f"test/{'很长的测试路径' * 15}/{index}.test.ts") for index in range(20)]
    markdown = format_weekly_test_report(stats, period_start=START, period_end=END, config=config)
    assert len(markdown.encode("utf-8")) <= 4200
    assert "消息长度限制" in markdown


def test_service_skips_no_important_and_dry_run_does_not_write():
    store = make_store(); config = WeeklyTestReportConfig.model_validate({"important": {"enabled": False}})
    service = WeeklyTestReportService(store, config)
    report = {"importantItemCount": 0, "normalItemCount": 2, "markdown": "x", "digest": "d"}
    result = service.notify(report, repo="r", jobs=["j"], branches=["dev"],
                            period_start=START, period_end=END)
    assert result["reason"] == "no_important_test_failures"
    assert store.report_notifications.docs == []
    config.notification.sendWhenNoImportantItems = True
    result = service.notify(report, repo="r", jobs=["j"], branches=["dev"],
                            period_start=START, period_end=END, dry_run=True)
    assert result["reason"] == "dry_run"
    assert store.report_notifications.docs == []


def test_notification_period_dedup_and_force():
    store = make_store(); config = WeeklyTestReportConfig.model_validate({"important": {"enabled": False}})
    service = WeeklyTestReportService(store, config, notification_chat_id="chat")
    report = {"importantItemCount": 1, "normalItemCount": 0, "markdown": "x", "digest": "d"}
    first = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END)
    second = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END)
    forced = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END, force=True)
    assert first["status"] == "pending" and second["inserted"] is False and forced["inserted"] is True
    assert len(store.wecom_notification_outbox.docs) == 2


def test_weekly_notify_handles_outbox_exception(monkeypatch):
    store = make_store(); service = WeeklyTestReportService(store, WeeklyTestReportConfig.model_validate({"important": {"enabled": False}}), notification_chat_id="chat")
    monkeypatch.setattr(service.outbox, "enqueue_markdown", lambda **_: (_ for _ in ()).throw(RuntimeError("mongodb://user:password@secret-host")))
    report = {"importantItemCount": 1, "normalItemCount": 0, "markdown": "private", "digest": "d"}
    result = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END)
    assert result["ok"] is False and result["sent"] is False and result["status"] == "enqueue_failed"
    assert all(value not in result["error"] for value in ("password", "secret-host", "mongodb://"))


def test_weekly_existing_dead_returns_error_and_force_creates_new_delivery():
    store = make_store(); service = WeeklyTestReportService(store, WeeklyTestReportConfig.model_validate({"important": {"enabled": False}}), notification_chat_id="chat")
    report = {"importantItemCount": 1, "normalItemCount": 0, "markdown": "x", "digest": "d"}
    first = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END)
    store.wecom_notification_outbox.docs[0]["status"] = "dead"
    repeat = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END)
    forced = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END, force=True)
    assert first["ok"] is True and repeat["ok"] is False and repeat["reason"] == "existing_dead_delivery"
    assert forced["ok"] is True and forced["inserted"] is True and len(store.wecom_notification_outbox.docs) == 2


def _save_build(store, *, job: str, number: int, result, timestamp: datetime, branch: str = "dev"):
    store.builds.update_one(
        {"repo": "r", "job": job, "branch": branch, "buildNumber": number},
        {"$set": {"repo": "r", "job": job, "branch": branch, "buildNumber": number, "result": result, "buildTimestamp": timestamp}},
        upsert=True,
    )


def test_weekly_completed_builds_include_success_only_scope_and_normalize_result_case():
    store = make_store()
    for number, result in enumerate(["SUCCESS", "success", "Failure", "unstable", "ABORTED", "NOT_BUILT", "UNKNOWN", None], start=1):
        _save_build(store, job="job-a", number=number, result=result, timestamp=START)
    service = WeeklyTestReportService(store, WeeklyTestReportConfig.model_validate({"important": {"enabled": False}}))
    report = service.generate(repo="r", jobs=["job-a"], branches=["dev"], period_start=START, period_end=END)
    assert report["completedBuildCount"] == 4
    assert report["failedBuildCount"] == 0


def test_weekly_completed_builds_multi_job_and_period_boundaries():
    store = make_store()
    for number in range(30):
        _save_build(store, job="job-a", number=number, result="SUCCESS", timestamp=START)
    for number, result in enumerate(["SUCCESS"] * 5 + ["FAILURE"] * 3, start=100):
        _save_build(store, job="job-b", number=number, result=result, timestamp=START)
    _save_build(store, job="job-b", number=200, result="SUCCESS", timestamp=END)
    _save_build(store, job="job-b", number=201, result="SUCCESS", timestamp=START.replace(year=2025))
    store.test_file_failures.update_one(
        {"repo": "r", "job": "job-b", "branch": "dev", "buildNumber": 105, "testFilePath": "a"},
        {"$set": {"repo": "r", "job": "job-b", "branch": "dev", "buildNumber": 105, "testFilePath": "a", "buildTimestamp": START}}, upsert=True,
    )
    store.test_file_failures.update_one(
        {"repo": "r", "job": "job-b", "branch": "dev", "buildNumber": 106, "testFilePath": "b"},
        {"$set": {"repo": "r", "job": "job-b", "branch": "dev", "buildNumber": 106, "testFilePath": "b", "buildTimestamp": START}}, upsert=True,
    )
    service = WeeklyTestReportService(store, WeeklyTestReportConfig.model_validate({"important": {"enabled": False}}))
    report = service.generate(repo="r", jobs=["job-b", "job-a", "job-a"], branches=["dev", "dev"], period_start=START, period_end=END)
    assert report["completedBuildCount"] == 38
    assert report["failedBuildCount"] == 2


def test_weekly_notification_dedup_is_scope_order_independent():
    store = make_store()
    service = WeeklyTestReportService(store, WeeklyTestReportConfig.model_validate({"important": {"enabled": False}}), notification_chat_id="chat")
    report = {"importantItemCount": 1, "normalItemCount": 0, "markdown": "x", "digest": "d"}
    first = service.notify(report, repo="r", jobs=["job-b", "job-a", "job-a"], branches=["release", "dev", "dev"], period_start=START, period_end=END)
    second = service.notify(report, repo="r", jobs=["job-a", "job-b"], branches=["dev", "release"], period_start=START, period_end=END)
    assert first["inserted"] is True
    assert second["inserted"] is False


def test_scope_normalization_keeps_jobs_and_normalizes_branches():
    assert normalize_scope_values([" origin/dev ", "refs/tags/release", "feature/a", "origin/dev"]) == ["feature/a", "origin/dev", "refs/tags/release"]
    assert normalize_branch_scope_values(["refs/remotes/origin/dev", "*/dev"]) == ["dev"]


def test_branch_scope_normalization_preserves_explicit_empty_filter():
    assert normalize_branch_scope_values(None) is None
    assert normalize_branch_scope_values([]) == []
    assert normalize_branch_scope_values(["refs/tags/v1"]) == []
    assert normalize_branch_scope_values(["refs/tags/v1", "*/dev"]) == ["dev"]
    assert normalize_branch_scope_values(normalize_branch_scope_values(["refs/tags/v1"])) == []


def test_weekly_scope_query_preserves_job_refs():
    store = make_store()
    _save_build(store, job="origin/dev", number=1, result="SUCCESS", timestamp=START)
    service = WeeklyTestReportService(store, WeeklyTestReportConfig.model_validate({"important": {"enabled": False}}))
    report = service.generate(repo="r", jobs=["origin/dev"], branches=["refs/remotes/origin/dev"], period_start=START, period_end=END)
    assert report["completedBuildCount"] == 1


def test_weekly_invalid_or_empty_branch_filter_matches_no_data():
    store = make_store()
    _save_build(store, job="job", number=1, result="SUCCESS", timestamp=START, branch="dev")
    _save_build(store, job="job", number=2, result="SUCCESS", timestamp=START, branch="release")
    service = WeeklyTestReportService(store, WeeklyTestReportConfig.model_validate({"important": {"enabled": False}}), notification_chat_id="chat")
    for branches in (["refs/tags/v1"], []):
        report = service.generate(repo="r", jobs=["job"], branches=branches, period_start=START, period_end=END)
        assert report["stats"] == [] and report["completedBuildCount"] == 0 and report["failedBuildCount"] == 0
        queued = service.notify({"importantItemCount": 1, "normalItemCount": 0, "markdown": "x", "digest": str(branches)}, repo="r", jobs=["job"], branches=branches, period_start=START, period_end=END)
        assert queued["ok"] is True
        assert store.wecom_notification_outbox.docs[-1]["metadata"]["branches"] == []
