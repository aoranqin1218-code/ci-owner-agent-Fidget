from __future__ import annotations

import datetime as dt
import hashlib
import json

from ci_owner_agent.services.test_failure_stats import TestFailureStatsService
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from ci_owner_agent.services.weekly_test_report_formatter import classify_weekly_report_stats, format_weekly_test_report
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig
from ci_owner_agent.services.wecom_notifier import send_wecom_markdown
from ci_owner_agent.services.responsibility_path_enricher import is_test_file_path


class WeeklyTestReportService:
    def __init__(self, store, config: WeeklyTestReportConfig, *, resolver: TestMaintainerResolver | None = None,
                 fallback_userids: tuple[str, ...] = (), webhook_url: str | None = None,
                 mention_mode: str = "userid") -> None:
        self.store, self.config = store, config
        self.resolver = resolver or TestMaintainerResolver()
        self.fallback_userids, self.webhook_url = fallback_userids, webhook_url
        self.mention_mode = mention_mode

    def generate(self, *, repo: str, jobs: list[str] | None, branches: list[str] | None,
                 period_start: dt.datetime, period_end: dt.datetime, top_n: int | None = None) -> dict:
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
            unidentified_item_count=unidentified, failed_build_count=failed_builds, top_n=top_n,
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
                "importantItemCount": important_count, "normalItemCount": normal_count, "ignoredItemCount": ignored_count}

    def notify(self, report: dict, *, repo: str, jobs: list[str] | None, branches: list[str] | None,
               period_start: dt.datetime, period_end: dt.datetime, dry_run: bool = False, force: bool = False) -> dict:
        if report["importantItemCount"] == 0 and not self.config.notification.sendWhenNoImportantItems:
            return {"ok": True, "sent": False, "reason": "no_important_test_failures",
                    "importantItemCount": 0, "normalItemCount": report["normalItemCount"],
                    "ignoredItemCount": report.get("ignoredItemCount", 0)}
        scope_job = ",".join(jobs or [])
        scope_branch = ",".join(branches or []) or None
        key = {"notificationType": "weekly_test_failure_report", "repo": repo or "", "job": scope_job,
               "branch": scope_branch, "periodStart": period_start, "periodEnd": period_end, "channel": "wecom"}
        existing = self.store.report_notifications.find_one({**key, "status": "sent"})
        if existing and not force:
            return {"ok": True, "sent": False, "reason": "already_sent", "importantItemCount": report["importantItemCount"],
                    "normalItemCount": report["normalItemCount"], "ignoredItemCount": report.get("ignoredItemCount", 0)}
        if dry_run:
            return {"ok": True, "sent": False, "reason": "dry_run", "markdown": report["markdown"],
                    "importantItemCount": report["importantItemCount"], "normalItemCount": report["normalItemCount"],
                    "ignoredItemCount": report.get("ignoredItemCount", 0)}
        if not self.webhook_url:
            send_result = {"ok": False, "error": "CI_AGENT_WECOM_WEBHOOK_URL is not configured"}
        else:
            send_result = send_wecom_markdown(self.webhook_url, report["markdown"])
        now = dt.datetime.now(dt.timezone.utc)
        doc = {**key, "digest": report["digest"], "status": "sent" if send_result.get("ok") else "failed",
               "messagePreview": report["markdown"][:1000], "error": send_result.get("error"), "updatedAt": now}
        self.store.report_notifications.update_one(key, {"$set": doc, "$setOnInsert": {"createdAt": now}}, upsert=True)
        return {**send_result, "sent": bool(send_result.get("ok")), "importantItemCount": report["importantItemCount"],
                "normalItemCount": report["normalItemCount"], "ignoredItemCount": report.get("ignoredItemCount", 0)}


def _scope_query(repo: str, jobs: list[str] | None, branches: list[str] | None) -> dict:
    query: dict = {"repo": repo}
    if jobs:
        query["job"] = {"$in": jobs}
    if branches:
        query["branch"] = {"$in": branches}
    return query


def _in_period(value, start: dt.datetime, end: dt.datetime) -> bool:
    if value is None:
        return False
    parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return start <= parsed.astimezone(dt.timezone.utc) < end
