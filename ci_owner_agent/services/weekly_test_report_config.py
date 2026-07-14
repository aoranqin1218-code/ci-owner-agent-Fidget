from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Thresholds(_StrictModel):
    weeklyFailedBuildCount: int | None = Field(default=None, ge=0)
    consecutiveFailureCount: int | None = Field(default=None, ge=0)
    weeklyFailureRate: float | None = Field(default=None, ge=0, le=1)
    totalFailedBuildCount: int | None = Field(default=None, ge=0)
    minimumCompletedBuildCount: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def non_empty(self) -> "Thresholds":
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("thresholds must contain at least one threshold")
        return self


class ImportantRule(_StrictModel):
    name: str
    matchMode: Literal["all", "any"] = "all"
    thresholds: Thresholds

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rule name must not be empty")
        return value.strip()


class ImportantConfig(_StrictModel):
    enabled: bool = True
    rules: list[ImportantRule] = Field(default_factory=list)


class NormalConfig(_StrictModel):
    includeBelowThreshold: bool = True
    minimumWeeklyFailedBuildCount: int = Field(default=1, ge=0)


class NotificationConfig(_StrictModel):
    sendWhenNoImportantItems: bool = False
    mentionMaintainersForImportantItems: bool = True


class WeeklyTestReportConfig(_StrictModel):
    timezone: str = "Asia/Shanghai"
    topN: int = Field(default=20, gt=0)
    important: ImportantConfig = Field(default_factory=ImportantConfig)
    normal: NormalConfig = Field(default_factory=NormalConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"invalid IANA timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def unique_rule_names(self) -> "WeeklyTestReportConfig":
        if self.important.enabled and not self.important.rules:
            raise ValueError("important.rules must contain at least one rule when important.enabled is true")
        names = [rule.name for rule in self.important.rules]
        if len(names) != len(set(names)):
            raise ValueError("important rule names must be unique")
        return self


def load_weekly_test_report_config(path: str | Path) -> WeeklyTestReportConfig:
    source = Path(path)
    if not source.exists():
        raise ValueError(f"weekly test report config file not found: {source}")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid weekly test report YAML: {exc}") from exc
    try:
        return WeeklyTestReportConfig.model_validate(data)
    except Exception as exc:
        raise ValueError(f"invalid weekly test report config: {exc}") from exc


def parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO 8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def resolve_period(
    *, timezone_name: str, period: str = "previous-week", period_start: str | None = None,
    period_end: str | None = None, now: datetime | None = None,
) -> tuple[datetime, datetime]:
    if period_start or period_end:
        if not period_start or not period_end:
            raise ValueError("--period-start and --period-end must be provided together")
        start, end = parse_aware_datetime(period_start), parse_aware_datetime(period_end)
    else:
        if period not in {"current-week", "previous-week"}:
            raise ValueError("period must be current-week or previous-week")
        zone = ZoneInfo(timezone_name)
        local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
        monday = (local_now - timedelta(days=local_now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        local_start = monday if period == "current-week" else monday - timedelta(days=7)
        local_end = monday + timedelta(days=7) if period == "current-week" else monday
        start, end = local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
    if start >= end:
        raise ValueError("period start must be earlier than period end")
    return start, end
