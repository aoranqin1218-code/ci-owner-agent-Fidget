from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

from ci_owner_agent.services.test_failure_stats import TestFailureStatsService
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from ci_owner_agent.services.weekly_test_report_formatter import classify_weekly_report_stats, format_weekly_test_report
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig
from ci_owner_agent.services.responsibility_path_enricher import is_test_file_path
from ci_owner_agent.services.wecom_notification_outbox import WeComNotificationOutbox
from ci_owner_agent.services.scope_normalization import normalize_branch_scope_values, normalize_scope_values


class WeeklyTestReportService:
    def __init__(self, store, config: WeeklyTestReportConfig, *, resolver: TestMaintainerResolver | None = None,
                 fallback_userids: tuple[str, ...] = (), notification_chat_id: str | None = None,
                 notification_dedup_enabled: bool = True, outbox_lease_seconds: int = 30,
                 outbox_max_attempts: int = 5, mention_mode: str = "userid") -> None:
        self.store, self.config = store, config
        self.resolver = resolver or TestMaintainerResolver()
        self.fallback_userids, self.notification_chat_id = fallback_userids, (notification_chat_id or "").strip() or None
        self.notification_dedup_enabled = notification_dedup_enabled
        self.outbox = WeComNotificationOutbox(store, lease_seconds=outbox_lease_seconds, max_attempts=outbox_max_attempts)
        self.mention_mode = mention_mode

    def generate(self, *, repo: str, jobs: list[str] | None, branches: list[str] | None,
                 period_start: dt.datetime, period_end: dt.datetime, top_n: int | None = None) -> dict:
        jobs, branches = normalize_scope_values(jobs), normalize_branch_scope_values(branches)
        stats = TestFailureStatsService(self.store, self.config).aggregate(repo, jobs, branches, period_start, period_end)
        groups = classify_weekly_report_stats(stats, self.config)
        routes = {}
        for stat in stats:
            if not stat.isImportant:
                continue
            match = self.resolver.resolve(repo=stat.repo, job=stat.job, test_file_path=stat.testFilePath,
                                          fallback_userids=self.fallback_userids)
            routes[(stat.repo, stat.job, stat.branch, stat.testFilePath)] = match.maintainers
        query = _scope_query(repo, jobs, branches)
        scoped_events = list(self.store.test_file_failures.find(query))
        missing = sum(1 for doc in scoped_events if doc.get("buildTimestamp") is None)
        failed_builds = len({(d.get("repo"), d.get("job"), d.get("branch"), d.get("buildNumber"))
                             for d in scoped_events if _in_period(d.get("buildTimestamp"), period_start, period_end)})
        completed_builds = sum(
            1 for doc in self.store.builds.find(query)
            if str(doc.get("result") or "").upper() in VALID_COMPLETED_RESULTS and _in_period(doc.get("buildTimestamp"), period_start, period_end)
        )
        unidentified = 0
        for doc in self.store.notices.find(query):
            if not _in_period(doc.get("buildTimestamp"), period_start, period_end):
                continue
            notice = doc.get("notice") if isinstance(doc.get("notice"), dict) else {}
            unidentified += sum(1 for item in notice.get("responsibilityItems") or []
                                if not is_test_file_path(item.get("testFilePath")))
        markdown = format_weekly_test_report(
            stats, period_start=period_start, period_end=period_end, config=self.config, maintainers=routes,
            repo=repo, jobs=jobs, branches=branches, missing_timestamp_count=missing,
            unidentified_item_count=unidentified, completed_build_count=completed_builds,
            failed_build_count=failed_builds, top_n=top_n,
            mention_mode=self.mention_mode,
        )
        important_count = len(groups.important)
        normal_count = len(groups.normal)
        ignored_count = len(groups.ignored)
        route_payload = sorted((list(key), [m.wecom_userid for m in value]) for key, value in routes.items())
        digest = hashlib.sha256(json.dumps({"markdown": markdown, "routes": route_payload,
                                           "config": self.config.model_dump(mode="json")}, ensure_ascii=False,
                                           sort_keys=True).encode("utf-8")).hexdigest()
        return {"stats": stats, "markdown": markdown, "digest": digest,
                "completedBuildCount": completed_builds, "failedBuildCount": failed_builds,
                "importantItemCount": important_count, "normalItemCount": normal_count, "ignoredItemCount": ignored_count}

    def notify(self, report: dict, *, repo: str, jobs: list[str] | None, branches: list[str] | None,
               period_start: dt.datetime, period_end: dt.datetime, dry_run: bool = False, force: bool = False) -> dict:
        jobs, branches = normalize_scope_values(jobs), normalize_branch_scope_values(branches)
        if report["importantItemCount"] == 0 and not self.config.notification.sendWhenNoImportantItems:
            return {"ok": True, "sent": False, "reason": "no_important_test_failures",
                    "importantItemCount": 0, "normalItemCount": report["normalItemCount"],
                    "ignoredItemCount": report.get("ignoredItemCount", 0)}
        if dry_run:
            return {"ok": True, "sent": False, "reason": "dry_run", "markdown": report["markdown"],
                    "importantItemCount": report["importantItemCount"], "normalItemCount": report["normalItemCount"],
                    "ignoredItemCount": report.get("ignoredItemCount", 0)}
        if not self.notification_chat_id:
            return {"ok": False, "error": "CI_AGENT_WECOM_BOT_NOTIFY_CHAT_ID is not configured"}
        try:
            queued = self.outbox.enqueue_markdown(notification_type="weekly_test_failure_report", target_chat_id=self.notification_chat_id,
                markdown=report["markdown"], dedup_key=report["digest"], force=force or not self.notification_dedup_enabled,
                metadata={"repo": repo, "jobs": jobs, "branches": branches, "periodStart": period_start,
                          "periodEnd": period_end, "digest": report["digest"]})
        except Exception as exc:
            logging.getLogger(__name__).warning("Weekly notification enqueue failed: %s", type(exc).__name__)
            return {"ok": False, "sent": False, "status": "enqueue_failed", "error": "notification outbox is unavailable"}
        if not queued["inserted"] and queued["status"] == "dead":
            return {"ok": False, "sent": False, "status": "dead", "inserted": False,
                    "reason": "existing_dead_delivery", "error": "existing notification delivery is dead; retry with --force",
                    "deliveryKey": queued["deliveryKey"]}
        return {"ok": True, "sent": False, "status": queued["status"], "inserted": queued["inserted"],
                "deliveryKey": queued["deliveryKey"], "importantItemCount": report["importantItemCount"],
                "normalItemCount": report["normalItemCount"], "ignoredItemCount": report.get("ignoredItemCount", 0)}


def _scope_query(repo: str, jobs: list[str] | None, branches: list[str] | None) -> dict:
    query: dict = {"repo": repo}
    if jobs:
        query["job"] = {"$in": jobs}
    if branches:
        query["branch"] = {"$in": branches}
    return query


VALID_COMPLETED_RESULTS = {"SUCCESS", "FAILURE", "UNSTABLE"}




def _in_period(value, start: dt.datetime, end: dt.datetime) -> bool:
    if value is None:
        return False
    parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return start <= parsed.astimezone(dt.timezone.utc) < end
