from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from ci_owner_agent.schemas import TestFileFailureStat
from ci_owner_agent.services.test_failure_priority import apply_priority
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig
from ci_owner_agent.services.scope_normalization import normalize_branch_scope_values


VALID_RESULTS = {"SUCCESS", "FAILURE", "UNSTABLE"}


class TestFailureStatsService:
    __test__ = False

    def __init__(self, store, config: WeeklyTestReportConfig | None = None) -> None:
        self.store = store
        self.config = config

    def aggregate(self, repo: str | None, jobs: list[str] | None, branches: list[str] | None,
                  period_start: datetime, period_end: datetime) -> list[TestFileFailureStat]:
        start, end = _utc(period_start), _utc(period_end)
        query: dict = {}
        if repo is not None:
            query["repo"] = repo
        if jobs:
            query["job"] = {"$in": jobs}
        normalized_branches = normalize_branch_scope_values(branches)
        if normalized_branches is not None:
            query["branch"] = {"$in": normalized_branches}
        events = list(self.store.test_file_failures.find(query))
        builds = list(self.store.builds.find(query))

        events_by_key: dict[tuple, list[dict]] = defaultdict(list)
        for event in events:
            events_by_key[_file_key(event)].append(event)
        build_by_scope: dict[tuple, list[dict]] = defaultdict(list)
        for build in builds:
            build_by_scope[_scope_key(build)].append(build)

        result: list[TestFileFailureStat] = []
        for key, file_events in events_by_key.items():
            scope = key[:3]
            sequence = sorted(build_by_scope.get(scope, []), key=lambda b: int(b.get("buildNumber") or 0), reverse=True)
            event_builds = {int(e["buildNumber"]): e for e in file_events}
            period_events = [e for e in file_events if _in_period(e.get("buildTimestamp"), start, end)]
            completed = sum(1 for b in sequence if str(b.get("result") or "").upper() in VALID_RESULTS and _in_period(b.get("buildTimestamp"), start, end))
            failed_builds = len({int(e["buildNumber"]) for e in period_events})
            known_times = [_as_datetime(e.get("buildTimestamp")) for e in file_events if e.get("buildTimestamp")]
            last_event = max(file_events, key=lambda e: int(e.get("buildNumber") or 0))
            stat = TestFileFailureStat(
                repo=str(key[0]), job=str(key[1]), branch=key[2], testFilePath=str(key[3]),
                totalFailedBuildCount=len(event_builds),
                totalFailureItemCount=sum(int(e.get("failureItemCount") or 0) for e in file_events),
                currentConsecutiveFailureCount=_consecutive(sequence, event_builds),
                periodFailedBuildCount=failed_builds,
                periodFailureItemCount=sum(int(e.get("failureItemCount") or 0) for e in period_events),
                periodCompletedBuildCount=completed,
                periodFailureRate=(failed_builds / completed if completed else 0.0),
                firstFailureAt=min(known_times).isoformat() if known_times else None,
                lastFailureAt=max(known_times).isoformat() if known_times else None,
                lastFailureBuildNumber=int(last_event.get("buildNumber")) if last_event.get("buildNumber") is not None else None,
            )
            result.append(apply_priority(stat, self.config) if self.config else stat)
        return sorted(result, key=lambda s: (
            -int(s.isImportant), -s.periodFailedBuildCount, -s.currentConsecutiveFailureCount,
            -s.periodFailureRate, -s.totalFailedBuildCount, s.testFilePath,
        ))


def _consecutive(builds: list[dict], events: dict[int, dict]) -> int:
    count = 0
    for build in builds:
        status = str(build.get("result") or "UNKNOWN").upper()
        number = int(build.get("buildNumber") or 0)
        failed = number in events
        if status in {"ABORTED", "NOT_BUILT"} or (status == "UNKNOWN" and not failed):
            continue
        if failed and status in {"FAILURE", "UNSTABLE", "UNKNOWN"}:
            count += 1
            continue
        if status in {"SUCCESS", "FAILURE", "UNSTABLE"}:
            break
    return count


def _scope_key(doc: dict) -> tuple:
    return (doc.get("repo"), doc.get("job"), doc.get("branch"))


def _file_key(doc: dict) -> tuple:
    return (*_scope_key(doc), doc.get("testFilePath"))


def _as_datetime(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _in_period(value, start: datetime, end: datetime) -> bool:
    return bool(value is not None and start <= _as_datetime(value) < end)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("period boundary must include a timezone")
    return value.astimezone(timezone.utc)
