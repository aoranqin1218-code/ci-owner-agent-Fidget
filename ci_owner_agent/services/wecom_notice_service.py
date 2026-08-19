"""Deliver CI notices through the configured WeCom transport.

The CLI owns command parsing; this module owns notification preparation, deduplication,
delivery and the rule that notification failures never invalidate an analysis notice.
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import nullcontext
from typing import Any

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
from ci_owner_agent.services.history_store import get_history_store
from ci_owner_agent.services.metrics import current_metrics_recorder
from ci_owner_agent.services.notification_formatter import format_wecom_markdown_notice
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from ci_owner_agent.services.wecom_notification_routing import notification_digest
from ci_owner_agent.services.wecom_mongo_user_mapping import build_wecom_notice_mapper
from ci_owner_agent.services.wecom_notification_outbox import WeComNotificationOutbox
from ci_owner_agent.services.wecom_notifier import send_wecom_markdown


def maybe_notify_notice(
    notice: CiResponsibilityNotice,
    settings: Any,
    cli_notify: bool,
    cli_dry_run: bool,
    force: bool,
) -> None:
    if not (cli_notify or settings.wecom_notify_enabled):
        return
    if notice.result == "SUCCESS" and not settings.wecom_notify_on_success:
        return
    if not settings.wecom_notify_on_no_owner and not _has_responsible_item_owner(notice):
        return
    try:
        result = notify_notice(
            notice,
            settings,
            dry_run=cli_dry_run or settings.wecom_notify_dry_run,
            force=force,
            feedback_base_url=settings.feedback_base_url,
        )
    except Exception:
        print("WARNING: notify failed unexpectedly", file=sys.stderr)
        return
    if not result.get("ok"):
        print(f"WARNING: notify failed: {result.get('error')}", file=sys.stderr)


def notify_notice(
    notice: CiResponsibilityNotice,
    settings: Any,
    *,
    dry_run: bool,
    force: bool,
    feedback_base_url: str | None,
) -> dict:
    recorder = current_metrics_recorder()
    stage = recorder.stage("notify") if recorder is not None else None
    with stage if stage is not None else nullcontext():
        store = get_history_store(settings)
        mapper = build_wecom_notice_mapper(settings, store)
        maintainer_resolver = TestMaintainerResolver.from_yaml(settings.test_maintainer_mapping_file)
        for warning in maintainer_resolver.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        feedback_code = None
        if store:
            try:
                store.upsert_notice_snapshot(notice, source="notification")
                context = FeedbackContextStore(store, settings.wecom_feedback_code_ttl_days).get_or_create_for_notice(notice)
                feedback_code = context.get("code") if context else None
            except Exception as exc:
                print(f"WARNING: feedback context unavailable: {type(exc).__name__}", file=sys.stderr)
        markdown = format_wecom_markdown_notice(
            notice,
            feedback_base_url=feedback_base_url,
            feedback_token=settings.feedback_shared_token,
            user_mapper=mapper,
            mention_mode=settings.wecom_mention_mode,
            fallback_userids=settings.wecom_fallback_userids,
            maintainer_resolver=maintainer_resolver,
            repo=notice.repo,
            feedback_code=feedback_code,
        )
        digest = notification_digest(
            notice,
            maintainer_resolver=maintainer_resolver,
            repo=notice.repo,
            fallback_userids=settings.wecom_fallback_userids,
            mention_mode=settings.wecom_mention_mode,
        )
        if dry_run:
            return {"ok": True, "status": "dry_run", "transport": settings.wecom_notify_transport, "markdown": markdown}
        if settings.wecom_notify_transport == "webhook":
            return _notify_notice_via_webhook(notice, settings, store=store, markdown=markdown, digest=digest, force=force)
        return _notify_notice_via_bot(notice, settings, store=store, markdown=markdown, digest=digest, force=force)


def _has_responsible_item_owner(notice: CiResponsibilityNotice) -> bool:
    return any(
        item.owner.type != "no_high_confidence_owner" and item.owner.name and item.owner.name != NO_OWNER_NAME
        for item in notice.responsibilityItems
    )


def _notify_notice_via_webhook(
    notice: CiResponsibilityNotice,
    settings: Any,
    *,
    store: Any,
    markdown: str,
    digest: str,
    force: bool,
) -> dict:
    if store is not None and settings.notification_dedup_enabled and not force:
        try:
            if store.notification_sent(
                repo=str(notice.repo or ""),
                job=notice.job,
                branch=notice.branch,
                build_number=notice.buildNumber,
                notice_hash=digest,
                channel="wecom",
            ):
                return {"ok": True, "status": "skipped", "transport": "webhook", "sent": False, "markdown": markdown}
        except Exception as exc:
            logging.getLogger(__name__).warning("WeCom notification dedup unavailable: %s", type(exc).__name__)
    if not settings.wecom_webhook_url:
        error = "CI_AGENT_WECOM_WEBHOOK_URL is not configured"
        _save_webhook_notification(store, notice, digest, markdown, status="failed", error=error)
        return {"ok": False, "status": "failed", "transport": "webhook", "sent": False, "error": error, "markdown": markdown}
    send_result = send_wecom_markdown(settings.wecom_webhook_url, markdown)
    status = "sent" if send_result.get("ok") else "failed"
    _save_webhook_notification(store, notice, digest, markdown, status=status, error=send_result.get("error"))
    return {
        **send_result,
        "status": status,
        "transport": "webhook",
        "sent": bool(send_result.get("ok")),
        "markdown": markdown,
    }


def _save_webhook_notification(
    store: Any,
    notice: CiResponsibilityNotice,
    digest: str,
    markdown: str,
    *,
    status: str,
    error: str | None,
) -> None:
    if store is None:
        return
    try:
        store.save_notification(notice=notice, notice_hash=digest, channel="wecom", status=status, message=markdown, error=error)
    except Exception as exc:
        logging.getLogger(__name__).warning("WeCom notification history unavailable: %s", type(exc).__name__)


def _notify_notice_via_bot(
    notice: CiResponsibilityNotice,
    settings: Any,
    *,
    store: Any,
    markdown: str,
    digest: str,
    force: bool,
) -> dict:
    if store is None:
        return {"ok": False, "transport": "bot", "error": "MongoDB history storage is required for WeCom bot notifications", "markdown": markdown}
    if not settings.wecom_bot_notify_chat_id:
        return {"ok": False, "transport": "bot", "error": "CI_AGENT_WECOM_BOT_NOTIFY_CHAT_ID is not configured", "markdown": markdown}
    dedup_key = json.dumps(
        {
            "notificationType": "ci_notice",
            "repo": notice.repo or "",
            "job": notice.job,
            "branch": notice.branch,
            "buildNumber": notice.buildNumber,
            "notificationDigest": digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        queued = WeComNotificationOutbox(
            store,
            lease_seconds=settings.wecom_bot_notify_lease_seconds,
            max_attempts=settings.wecom_bot_notify_max_attempts,
        ).enqueue_markdown(
            notification_type="ci_notice",
            target_chat_id=settings.wecom_bot_notify_chat_id,
            markdown=markdown,
            dedup_key=dedup_key,
            force=force or not settings.notification_dedup_enabled,
            metadata={
                "repo": notice.repo or "",
                "job": notice.job,
                "branch": notice.branch,
                "buildNumber": notice.buildNumber,
                "noticeHash": digest,
            },
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("WeCom notification enqueue failed: %s", type(exc).__name__)
        return {"ok": False, "status": "enqueue_failed", "transport": "bot", "error": "notification outbox is unavailable"}
    if not queued["inserted"] and queued["status"] == "dead":
        return {
            "ok": False,
            "status": "dead",
            "transport": "bot",
            "inserted": False,
            "reason": "existing_dead_delivery",
            "error": "existing notification delivery is dead; retry with --force",
            "deliveryKey": queued["deliveryKey"],
            "markdown": markdown,
        }
    return {
        "ok": True,
        "status": queued["status"],
        "transport": "bot",
        "inserted": queued["inserted"],
        "deliveryKey": queued["deliveryKey"],
        "markdown": markdown,
    }
