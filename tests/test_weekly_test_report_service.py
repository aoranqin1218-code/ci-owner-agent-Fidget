from __future__ import annotations

from datetime import datetime, timezone

from ci_owner_agent.schemas import TestFileFailureStat
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig
from ci_owner_agent.services.weekly_test_report_formatter import format_weekly_test_report
from ci_owner_agent.services.weekly_test_report_service import WeeklyTestReportService
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
    config = WeeklyTestReportConfig.model_validate({"normal": {"includeBelowThreshold": False}})
    markdown = format_weekly_test_report([_stat(important=False)], period_start=START, period_end=END, config=config)
    assert "其他失败测试" in markdown
    assert "共 1 个测试文件" in markdown
    assert "test/A.test.ts" not in markdown
    assert "<@" not in markdown


def test_formatter_top_n_reports_omitted_count_without_changing_priority():
    config = WeeklyTestReportConfig(topN=1)
    stats = [_stat("test/A.test.ts"), _stat("test/B.test.ts")]
    markdown = format_weekly_test_report(stats, period_start=START, period_end=END, config=config)
    assert "另有 1 项" in markdown
    assert all(item.isImportant for item in stats)


def test_formatter_length_limit_removes_whole_items_and_reports_omission():
    config = WeeklyTestReportConfig(topN=20)
    stats = [_stat(f"test/{'很长的测试路径' * 15}/{index}.test.ts") for index in range(20)]
    markdown = format_weekly_test_report(stats, period_start=START, period_end=END, config=config)
    assert len(markdown.encode("utf-8")) <= 4200
    assert "消息长度限制" in markdown


def test_service_skips_no_important_and_dry_run_does_not_write():
    store = make_store(); config = WeeklyTestReportConfig()
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


def test_notification_period_dedup_and_force(monkeypatch):
    store = make_store(); config = WeeklyTestReportConfig()
    service = WeeklyTestReportService(store, config, webhook_url="https://example.invalid")
    calls = []
    monkeypatch.setattr("ci_owner_agent.services.weekly_test_report_service.send_wecom_markdown",
                        lambda url, text: calls.append(text) or {"ok": True, "error": None})
    report = {"importantItemCount": 1, "normalItemCount": 0, "markdown": "x", "digest": "d"}
    first = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END)
    second = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END)
    forced = service.notify(report, repo="r", jobs=["j"], branches=["dev"], period_start=START, period_end=END, force=True)
    assert first["sent"] and second["reason"] == "already_sent" and forced["sent"]
    assert len(calls) == 2
