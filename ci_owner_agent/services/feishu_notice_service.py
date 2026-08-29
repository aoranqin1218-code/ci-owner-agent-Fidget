"""Deliver CI notices through the configured Feishu transport.

The CLI owns command parsing; this module owns notification preparation,
deduplication, delivery and the rule that notification failures never
invalidate an analysis notice. WeCom and Feishu are independent downstream
side effects; one failing never blocks the other or rewrites the notice.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import sys
from contextlib import nullcontext
from typing import Any

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.failure_identity import (
    _find_plaintext_secrets,
    canonicalize_failure_message,
)
from ci_owner_agent.services.feishu_notification_formatter import (
    FEISHU_POST_MAX_JSON_BYTES,
    FeishuFormatSummary,
    format_feishu_notice_payload,
)
from ci_owner_agent.services.feishu_notifier import send_feishu_payload
from ci_owner_agent.services.feishu_user_mapping import FeishuUserMapper
from ci_owner_agent.services.history_store import get_history_store
from ci_owner_agent.services.metrics import current_metrics_recorder
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from ci_owner_agent.services.wecom_notification_routing import resolve_test_maintainer_matches


def maybe_notify_notice(notice: CiResponsibilityNotice, settings: Any) -> None:
    """Auto-notify only when Feishu is explicitly enabled; never via ``--notify``.

    Keeps the existing ``--notify`` meaning WeCom-only, so an upgrade cannot
    suddenly start sending Feishu messages.
    """
    if not settings.feishu_notify_enabled:
        return
    if notice.result == "SUCCESS" and not settings.feishu_notify_on_success:
        return
    if not settings.feishu_notify_on_no_owner and not _has_responsible_item_owner(notice):
        return
    try:
        result = notify_notice(notice, settings, dry_run=settings.feishu_notify_dry_run, force=False)
    except Exception:
        print("WARNING: feishu notify failed unexpectedly", file=sys.stderr)
        return
    if not result.get("ok"):
        print(f"WARNING: feishu notify failed: {result.get('error')}", file=sys.stderr)


def notify_notice(
    notice: CiResponsibilityNotice,
    settings: Any,
    *,
    dry_run: bool,
    force: bool,
) -> dict:
    recorder = current_metrics_recorder()
    stage = recorder.stage("notify") if recorder is not None else None
    with stage if stage is not None else nullcontext():
        # 敏感信息 fail-closed 与 dry-run 必须发生在任何 MongoDB 副作用之前：
        # store 初始化会创建索引（Mongo 写副作用），只在真实发送需要去重时才初始化。
        residual = _find_plaintext_secrets(notice.model_dump(mode="json"))
        if residual:
            return {
                "ok": False,
                "status": "blocked",
                "transport": "feishu",
                "error": f"notice contains plaintext secrets: {len(residual)} hit(s)",
                "payload": None,
            }
        mapper = FeishuUserMapper.from_yaml(settings.feishu_user_mapping_file, settings.feishu_fallback_userids)
        maintainer_resolver = TestMaintainerResolver.from_yaml(settings.test_maintainer_mapping_file)
        owner_open_ids = _resolve_owner_open_ids(notice, mapper)
        item_maintainer_names, build_maintainer_names = _resolve_maintainer_names(notice, maintainer_resolver)
        maintainer_open_ids = _resolve_maintainer_open_ids(item_maintainer_names, build_maintainer_names, mapper)
        payload, summary = format_feishu_notice_payload(
            notice,
            jenkins_link=notice.buildUrl,
            owner_open_ids=owner_open_ids,
            maintainer_open_ids=maintainer_open_ids,
            item_maintainer_names=item_maintainer_names,
            build_maintainer_names=build_maintainer_names,
            fallback_open_ids=mapper.fallback_open_ids,
        )
        digest = _feishu_digest(notice, owner_open_ids, maintainer_open_ids, mapper.fallback_open_ids)
        payload_json = json.dumps(payload, ensure_ascii=False)
        if len(payload_json.encode("utf-8")) > FEISHU_POST_MAX_JSON_BYTES:
            return {
                "ok": False,
                "status": "blocked",
                "transport": "feishu",
                "error": f"feishu payload exceeds {FEISHU_POST_MAX_JSON_BYTES} UTF-8 bytes",
                "payload": payload,
            }
        if dry_run:
            return {
                "ok": True,
                "status": "dry_run",
                "transport": "feishu",
                "payload": payload,
                "summary": _summary_dict(summary),
            }
        store = get_history_store(settings)
        if store is not None and settings.notification_dedup_enabled and not force:
            try:
                if store.notification_sent(
                    repo=str(notice.repo or ""),
                    job=notice.job,
                    branch=notice.branch,
                    build_number=notice.buildNumber,
                    notice_hash=digest,
                    channel="feishu",
                ):
                    return {"ok": True, "status": "skipped", "transport": "feishu", "sent": False, "payload": payload, "summary": _summary_dict(summary)}
            except Exception as exc:
                logging.getLogger(__name__).warning("Feishu notification dedup unavailable: %s", type(exc).__name__)
        if not settings.feishu_webhook_url:
            error = "CI_AGENT_FEISHU_WEBHOOK_URL is not configured"
            _save_feishu_notification(store, notice, digest, payload, status="failed", error=error)
            return {"ok": False, "status": "failed", "transport": "feishu", "sent": False, "error": error, "payload": payload, "summary": _summary_dict(summary)}
        send_result = send_feishu_payload(settings.feishu_webhook_url, payload, secret=settings.feishu_webhook_secret)
        status = "sent" if send_result.get("ok") else "failed"
        _save_feishu_notification(store, notice, digest, payload, status=status, error=send_result.get("error"))
        return {
            **send_result,
            "status": status,
            "transport": "feishu",
            "sent": bool(send_result.get("ok")),
            "payload": payload,
            "summary": _summary_dict(summary),
        }


def _has_responsible_item_owner(notice: CiResponsibilityNotice) -> bool:
    return any(
        item.owner.type != "no_high_confidence_owner" and item.owner.name and item.owner.name != NO_OWNER_NAME
        for item in notice.responsibilityItems
    )


def _resolve_owner_open_ids(notice: CiResponsibilityNotice, mapper: FeishuUserMapper) -> dict[str, str]:
    result: dict[str, str] = {}
    seen: set[str] = set()
    for item in notice.responsibilityItems:
        owner = item.owner
        if owner.type == "no_high_confidence_owner" or not owner.name or owner.name == NO_OWNER_NAME:
            continue
        if owner.name in seen:
            continue
        seen.add(owner.name)
        open_id = mapper.resolve_open_id(owner.name, owner.email)
        if open_id:
            result[owner.name] = open_id
    return result


def _resolve_maintainer_names(
    notice: CiResponsibilityNotice,
    maintainer_resolver: TestMaintainerResolver,
) -> tuple[list[str | None], list[str]]:
    """Return (per-item maintainer name, build-level maintainer names).

    Reuses the WeCom routing match only for the *names* of the current
    maintainer route; Feishu never consumes WeCom userids here.
    """
    matches = resolve_test_maintainer_matches(
        notice,
        maintainer_resolver=maintainer_resolver,
        repo=notice.repo,
        fallback_userids=(),
    )
    if not notice.responsibilityItems:
        build_names: list[str] = []
        if matches and matches[0]:
            build_names = [m.name for m in matches[0].maintainers if m.name]
        return [], build_names
    item_names: list[str | None] = []
    for match in matches:
        if match is None:
            item_names.append(None)
            continue
        names = [m.name for m in match.maintainers if m.name]
        item_names.append(names[0] if names else None)
    return item_names, []


def _resolve_maintainer_open_ids(
    item_names: list[str | None],
    build_names: list[str],
    mapper: FeishuUserMapper,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in list(item_names) + list(build_names):
        if not name or name in result:
            continue
        open_id = mapper.resolve_open_id(name)
        if open_id:
            result[name] = open_id
    return result


def _feishu_digest(
    notice: CiResponsibilityNotice,
    owner_open_ids: dict[str, str],
    maintainer_open_ids: dict[str, str],
    fallback_open_ids: tuple[str, ...],
) -> str:
    """Digest includes the notice and the current Feishu routing result, so a
    mapping change yields a new delivery identity instead of being skipped."""
    payload = {
        "notice": notice.model_dump(mode="json"),
        "ownerOpenIds": owner_open_ids,
        "maintainerOpenIds": maintainer_open_ids,
        "fallbackOpenIds": fallback_open_ids,
    }
    raw = canonicalize_failure_message(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _save_feishu_notification(
    store: Any,
    notice: CiResponsibilityNotice,
    digest: str,
    payload: dict,
    *,
    status: str,
    error: str | None,
) -> None:
    if store is None:
        return
    try:
        # 只保存脱敏后的消息预览，at 的 user_id（open_id）属 PRD R4 敏感标识，不得持久化明文。
        store.save_notification(notice=notice, notice_hash=digest, channel="feishu", status=status, message=_redacted_payload_preview(payload), error=error)
    except Exception as exc:
        logging.getLogger(__name__).warning("Feishu notification history unavailable: %s", type(exc).__name__)


def _redacted_payload_preview(payload: dict) -> str:
    """Return a JSON preview with at user_id values replaced, safe to persist."""
    preview = copy.deepcopy(payload)
    for paragraph in preview.get("content", {}).get("post", {}).get("zh_cn", {}).get("content", []):
        for element in paragraph:
            if isinstance(element, dict) and element.get("tag") == "at" and "user_id" in element:
                element["user_id"] = "***"
    return json.dumps(preview, ensure_ascii=False)


def _summary_dict(summary: FeishuFormatSummary) -> dict[str, Any]:
    return {
        "totalItemCount": summary.total_item_count,
        "shownItemCount": summary.shown_item_count,
        "omittedItemCount": summary.omitted_item_count,
        "atOpenIdCount": len(summary.at_open_ids),
        "unmappedOwnerNames": list(summary.unmapped_owner_names),
    }
