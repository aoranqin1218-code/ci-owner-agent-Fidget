from __future__ import annotations

from ci_owner_agent.schemas import TestFileFailureStat
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig


_FIELDS = {
    "weeklyFailedBuildCount": "periodFailedBuildCount",
    "consecutiveFailureCount": "currentConsecutiveFailureCount",
    "weeklyFailureRate": "periodFailureRate",
    "totalFailedBuildCount": "totalFailedBuildCount",
    "minimumCompletedBuildCount": "periodCompletedBuildCount",
}


def apply_priority(stat: TestFileFailureStat, config: WeeklyTestReportConfig) -> TestFileFailureStat:
    stat.isImportant = False
    stat.matchedRuleNames = []
    stat.matchedThresholds = []
    if not config.important.enabled:
        return stat
    for rule in config.important.rules:
        checks: list[tuple[bool, str]] = []
        for threshold_name, threshold in rule.thresholds.model_dump().items():
            if threshold is None:
                continue
            actual = getattr(stat, _FIELDS[threshold_name])
            checks.append((actual >= threshold, f"{threshold_name} >= {threshold}"))
        matched = all(ok for ok, _ in checks) if rule.matchMode == "all" else any(ok for ok, _ in checks)
        if matched:
            stat.isImportant = True
            stat.matchedRuleNames.append(rule.name)
            stat.matchedThresholds.extend(text for ok, text in checks if ok)
    stat.matchedThresholds = list(dict.fromkeys(stat.matchedThresholds))
    return stat
