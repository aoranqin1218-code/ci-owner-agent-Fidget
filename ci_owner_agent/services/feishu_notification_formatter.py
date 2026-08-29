"""Feishu ``post`` (rich text) payload formatting for CiResponsibilityNotice.

Pure functions only: no network, no files, no environment variables and no
current-time side effects. This module builds the official Feishu ``post``
payload; WeCom markdown, the 4096-byte budget and ``<@userid>`` syntax never
appear here. Business copy may be reused, channel syntax and budget are local.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice, ResponsibilityItem
from ci_owner_agent.services.notification_formatter import (
    format_coverage_item_suggestion,
    format_item_evidence,
    format_item_owner_label,
    format_item_title,
    format_suggestions,
    is_continuing_coverage_item,
    public_single_line,
    responsibility_item_icon,
    responsibility_item_label,
    result_icon,
    result_label,
)

# 官方要求自定义机器人请求体不超过 20 KB；超限按确定性策略压缩，
# 仍超限由 service fail closed。使用二进制 KB 计算并按 UTF-8 实际字节校验。
FEISHU_POST_MAX_JSON_BYTES = 20 * 1024
FEISHU_MAX_ITEM_DETAILS = 5
FEISHU_ITEM_TITLE_MAX_CHARS = 120
FEISHU_ITEM_EVIDENCE_MAX_CHARS = 200
FEISHU_REASON_MAX_CHARS = 300


@dataclass(frozen=True)
class FeishuFormatSummary:
    """What the rendered payload contains, for tests and audit records."""

    total_item_count: int
    shown_item_count: int
    omitted_item_count: int
    at_open_ids: tuple[str, ...] = field(default_factory=tuple)
    unmapped_owner_names: tuple[str, ...] = field(default_factory=tuple)


def _text(value: str) -> dict:
    return {"tag": "text", "text": value}


def _at(open_id: str, name: str | None = None) -> dict:
    element: dict = {"tag": "at", "user_id": open_id}
    if name:
        element["user_name"] = name
    return element


def _payload_json_bytes(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def format_feishu_notice_payload(
    notice: CiResponsibilityNotice,
    *,
    jenkins_link: str,
    owner_open_ids: dict[str, str],
    maintainer_open_ids: dict[str, str],
    item_maintainer_names: list[str | None] | None = None,
    build_maintainer_names: list[str] | None = None,
    fallback_open_ids: tuple[str, ...] = (),
    max_item_details: int = FEISHU_MAX_ITEM_DETAILS,
) -> tuple[dict, FeishuFormatSummary]:
    """Build the Feishu ``post`` payload and its display summary.

    ``owner_open_ids`` / ``maintainer_open_ids`` map an exact person name to a
    valid Feishu open_id; only mapped names are rendered as real ``@``. Missing
    mapping degrades to plain name text and is reported in the summary so the
    caller can decide whether the target audience is actually reachable.
    """
    shown = max(1, max_item_details)
    while True:
        payload, summary = _build_payload(
            notice,
            shown=shown,
            jenkins_link=jenkins_link,
            owner_open_ids=owner_open_ids,
            maintainer_open_ids=maintainer_open_ids,
            item_maintainer_names=item_maintainer_names or [],
            build_maintainer_names=build_maintainer_names or [],
            fallback_open_ids=fallback_open_ids,
        )
        if _payload_json_bytes(payload) <= FEISHU_POST_MAX_JSON_BYTES or shown <= 1:
            return payload, summary
        shown = max(1, shown // 2)


def _build_payload(
    notice: CiResponsibilityNotice,
    *,
    shown: int,
    jenkins_link: str,
    owner_open_ids: dict[str, str],
    maintainer_open_ids: dict[str, str],
    item_maintainer_names: list[str | None],
    build_maintainer_names: list[str],
    fallback_open_ids: tuple[str, ...],
) -> tuple[dict, FeishuFormatSummary]:
    rendered_at: set[str] = set()
    title = f"{result_icon(notice.result)} {notice.repo or ''}/{notice.job} #{notice.buildNumber}"
    content: list[list[dict]] = []
    unmapped_owners = _unmapped_owner_names(notice, owner_open_ids)

    if str(notice.result or "").upper() == "SUCCESS":
        content.append([_text("✅ 构建成功")])
        content.append([_text(f"Job：{notice.job} | Build：#{notice.buildNumber}")])
        if notice.branch:
            content.append([_text(f"分支：{notice.branch}")])
        content.append([_link(notice, jenkins_link)])
        return _package(title, content, notice, shown, rendered_at, unmapped_owners)

    branch_text = f" | 分支 {notice.branch}" if notice.branch else ""
    content.append([_text(f"{result_label(notice.result)}{branch_text}")])
    content.append([_text(_stats_line(notice))])
    if notice.failureReason and notice.failureReason.strip():
        content.append([_text(f"原因：{public_single_line(notice.failureReason, FEISHU_REASON_MAX_CHARS)}")])

    items = notice.responsibilityItems
    if not items:
        for name in build_maintainer_names:
            line = _maintainer_person_line(name, maintainer_open_ids, rendered_at)
            if line:
                content.append(line)
    else:
        for index, item in enumerate(items[:shown]):
            content.append([_text(f"{responsibility_item_icon(item, notice)} {responsibility_item_label(item, notice)} | {format_item_title(item, FEISHU_ITEM_TITLE_MAX_CHARS)}")])
            line = _item_person_line(
                item,
                owner_open_ids=owner_open_ids,
                maintainer_open_ids=maintainer_open_ids,
                maintainer_name=item_maintainer_names[index] if index < len(item_maintainer_names) else None,
                rendered_at=rendered_at,
            )
            if line:
                content.append(line)
            evidence = format_item_evidence(item, {e.id: e for e in notice.evidence}, FEISHU_ITEM_EVIDENCE_MAX_CHARS)
            if evidence:
                content.append([_text(f"  证据：{evidence}")])
            suggestion = format_coverage_item_suggestion(item)
            if suggestion:
                content.append([_text(f"  建议：{suggestion}")])

    omitted = max(0, len(items) - shown)
    if omitted > 0:
        content.append([_text(f"…已省略 {omitted} 项，共 {len(items)} 项，详见下方链接")])

    suggestions = format_suggestions(notice.suggestions)
    if suggestions:
        content.append([_text("🛠️ 建议：")])
        content.extend([_text(f"  {suggestion}")] for suggestion in suggestions)

    # 兜底仅在无人可触达时启用：任何责任人或维护者已可靠 @ 时，不再追加兜底，避免误触达无关人员。
    if not rendered_at:
        for open_id in fallback_open_ids:
            content.append([_text("📢 兜底通知："), _at(open_id)])
            rendered_at.add(open_id)
    content.append([_link(notice, jenkins_link)])
    return _package(title, content, notice, shown, rendered_at, unmapped_owners)


def _package(
    title: str,
    content: list[list[dict]],
    notice: CiResponsibilityNotice,
    shown: int,
    rendered_at: set[str],
    unmapped_owners: tuple[str, ...],
) -> tuple[dict, FeishuFormatSummary]:
    total = len(notice.responsibilityItems)
    payload = {"msg_type": "post", "content": {"post": {"zh_cn": {"title": title, "content": content}}}}
    summary = FeishuFormatSummary(
        total_item_count=total,
        shown_item_count=min(total, shown),
        omitted_item_count=max(0, total - shown),
        at_open_ids=tuple(sorted(rendered_at)),
        unmapped_owner_names=unmapped_owners,
    )
    return payload, summary


def _link(notice: CiResponsibilityNotice, jenkins_link: str) -> dict:
    return {"tag": "a", "text": f"查看 Jenkins 分析 #{notice.buildNumber}", "href": jenkins_link}


def _unmapped_owner_names(notice: CiResponsibilityNotice, owner_open_ids: dict[str, str]) -> tuple[str, ...]:
    seen: set[str] = set()
    names: list[str] = []
    for item in notice.responsibilityItems:
        owner = item.owner
        if owner.type == "no_high_confidence_owner" or not owner.name or owner.name == NO_OWNER_NAME:
            continue
        if owner.name in seen:
            continue
        seen.add(owner.name)
        if owner.name not in owner_open_ids:
            names.append(owner.name)
    return tuple(names)


def _item_person_line(
    item: ResponsibilityItem,
    *,
    owner_open_ids: dict[str, str],
    maintainer_open_ids: dict[str, str],
    maintainer_name: str | None,
    rendered_at: set[str],
) -> list[dict] | None:
    owner = item.owner
    if item.responsibilityType != "no_high_confidence_owner" and owner.name and owner.name != NO_OWNER_NAME:
        role = format_item_owner_label(item)
        open_id = owner_open_ids.get(owner.name)
        if open_id and open_id not in rendered_at:
            rendered_at.add(open_id)
            return [_text(f"  {role}：{owner.name} "), _at(open_id, owner.name)]
        if open_id:
            return [_text(f"  {role}：{owner.name}")]
        return [_text(f"  {role}：{owner.name}（未完成身份映射）")]
    if maintainer_name:
        return _maintainer_person_line(maintainer_name, maintainer_open_ids, rendered_at)
    return None


def _maintainer_person_line(name: str, maintainer_open_ids: dict[str, str], rendered_at: set[str]) -> list[dict] | None:
    role = "待确认维护者"
    open_id = maintainer_open_ids.get(name)
    if open_id and open_id not in rendered_at:
        rendered_at.add(open_id)
        return [_text(f"  {role}：{name} "), _at(open_id, name)]
    if open_id:
        return [_text(f"  {role}：{name}")]
    return [_text(f"  {role}：{name}（未完成身份映射）")]


def _stats_line(notice: CiResponsibilityNotice) -> str:
    items = notice.responsibilityItems
    if not items:
        return "📌 责任项：未识别到独立责任项"
    current = sum(1 for item in items if item.responsibilityType == "current_build_owner" and not is_continuing_coverage_item(item, notice))
    continuing_coverage = sum(1 for item in items if is_continuing_coverage_item(item, notice))
    inherited = sum(1 for item in items if item.responsibilityType == "inherited_failure_owner")
    unresolved = sum(1 for item in items if item.responsibilityType == "no_high_confidence_owner")
    other = len(items) - current - continuing_coverage - inherited - unresolved
    parts = [f"共 {len(items)} 项"]
    if current:
        parts.append(f"当前引入 {current}")
    if continuing_coverage:
        parts.append(f"覆盖率持续 {continuing_coverage}")
    if inherited:
        parts.append(f"历史持续 {inherited}")
    if unresolved:
        parts.append(f"待确认 {unresolved}")
    if other:
        parts.append(f"其他 {other}")
    return f"📌 责任项：{'，'.join(parts)}"
