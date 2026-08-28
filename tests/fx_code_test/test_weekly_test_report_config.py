from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig, resolve_period


def test_config_rejects_unknown_fields_duplicate_names_and_bad_rate():
    base = {"important": {"rules": [{"name": "same", "thresholds": {"weeklyFailedBuildCount": 1}}]}}
    with pytest.raises(ValidationError):
        WeeklyTestReportConfig.model_validate({**base, "typo": True})
    with pytest.raises(ValidationError):
        WeeklyTestReportConfig.model_validate({"important": {"rules": base["important"]["rules"] * 2}})
    with pytest.raises(ValidationError):
        WeeklyTestReportConfig.model_validate({"important": {"rules": [{"name": "x", "thresholds": {"weeklyFailureRate": 1.1}}]}})


def test_previous_week_uses_shanghai_monday_boundaries_in_utc():
    start, end = resolve_period(timezone_name="Asia/Shanghai", period="previous-week",
                                now=datetime(2026, 7, 13, 2, tzinfo=timezone.utc))
    assert start.isoformat() == "2026-07-05T16:00:00+00:00"
    assert end.isoformat() == "2026-07-12T16:00:00+00:00"


def test_explicit_period_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone"):
        resolve_period(timezone_name="UTC", period_start="2026-01-01T00:00:00",
                       period_end="2026-01-02T00:00:00+00:00")
