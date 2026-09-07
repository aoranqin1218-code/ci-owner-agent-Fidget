"""Feishu ``interactive`` card formatting for CiResponsibilityNotice.

Pure functions only: no network, no files, no environment variables and no
current-time side effects. The card uses official Markdown elements for bold
section headings, explicit margins for visual separation, and ``<at id=...>``
for real mentions. WeCom syntax and its 4096-byte budget never appear here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice, ResponsibilityItem
from ci_owner_agent.services.notification_formatter import (
    collect_notice_suggestions,
    failure_category,
    failure_reason_category,
    format_item_evidence,
    format_item_owner_label,
    format_item_title,
    is_continuing_coverage_item,
    is_coverage_failure_item,
    integration_failure_group_label,
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
FEISHU_ITEM_TITLE_MAX_CHARS = 90
FEISHU_REASON_POINT_MAX_CHARS = 150
FEISHU_EVIDENCE_POINT_MAX_CHARS = 160
FEISHU_SUGGESTION_POINT_MAX_CHARS = 160
FEISHU_REASON_MAX_POINTS = 3
FEISHU_EVIDENCE_MAX_POINTS = 2
FEISHU_SUGGESTION_MAX_POINTS = 3
FEISHU_LONG_TEXT_HINT = "（内容较长，详见 Jenkins）"
FEISHU_CARD_SECTION_MARGIN = "0px 0px 16px 0px"
FEISHU_CARD_ITEM_MARGIN = "0px 0px 12px 0px"

_SEMANTIC_POINT_BREAK_RE = re.compile(r"(?:\r?\n)+|[；;。]+")
_REASON_POINT_BREAK_RE = re.compile(
    r"(?:\r?\n)+|[；;。]+|[:：](?=[A-Za-z][A-Za-z0-9]*-\d+\b)"
)
_FAILURE_TITLE_RE = re.compile(r"^(?P<id>[A-Za-z][A-Za-z0-9]*-\d+)\s*[:：]\s*(?P<title>.+)$")
_FAILURE_CATEGORY_ORDER = ("build", "unit", "integration", "coverage", "other")
_FAILURE_CATEGORY_LABELS = {
    "build": "构建失败",
    "unit": "单元测试",
    "integration": "集成测试",
    "coverage": "覆盖率",
    "other": "其他失败",
}


@dataclass(frozen=True)
class FeishuFormatSummary:
    """What the rendered payload contains, for tests and audit records."""

    total_item_count: int
    shown_item_count: int
    omitted_item_count: int
    at_open_ids: tuple[str, ...] = field(default_factory=tuple)
    unmapped_owner_names: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FeishuMaintainerRoute:
    """Feishu-safe view of one unresolved item's maintainer route."""

    names: tuple[str, ...] = field(default_factory=tuple)
    used_fallback: bool = False
    reason: str = ""


def _markdown(content: str, *, margin: str = FEISHU_CARD_SECTION_MARGIN) -> dict:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": "normal_v2",
        "margin": margin,
    }


def _markdown_text(value: str) -> str:
    """Escape notice text while leaving formatter-owned Markdown intact."""

    cleaned = str(value or "")
    cleaned = cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\*_~`\[\]])", r"\\\1", cleaned)


def _payload_json_bytes(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _compact_title(notice: CiResponsibilityNotice) -> str:
    job = str(notice.job or "").strip().rstrip("/").split("/")[-1] or "CI"
    return f"{result_icon(notice.result)} CI 构建{result_label(notice.result)} | {job} #{notice.buildNumber}"


def _public_text(value: str) -> str:
    text = str(value or "")
    return public_single_line(text, max(1, len(text) + 1))


def _semantic_clip(value: str, max_chars: int) -> str:
    """Clip at a readable boundary and make the omission explicit."""

    cleaned = _public_text(value)
    if len(cleaned) <= max_chars:
        return cleaned
    keep = max(1, max_chars - len(FEISHU_LONG_TEXT_HINT))
    prefix = cleaned[:keep]
    boundary = max(prefix.rfind(mark) for mark in ("，", ",", "、", "：", ":"))
    if boundary < keep // 2:
        boundary = prefix.rfind(" ")
    if boundary >= keep // 2:
        prefix = prefix[:boundary]
    return prefix.rstrip("，,、：:；;。 ") + FEISHU_LONG_TEXT_HINT


def _semantic_points(
    value: str,
    *,
    max_points: int,
    max_chars: int,
    split_before_failure_id: bool = False,
) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    points: list[str] = []
    splitter = _REASON_POINT_BREAK_RE if split_before_failure_id else _SEMANTIC_POINT_BREAK_RE
    for part in splitter.split(raw):
        cleaned = _public_text(part).strip("：:；;。 ")
        if not cleaned:
            continue
        points.append(_semantic_clip(cleaned, max_chars))
        if len(points) >= max_points:
            break
    return points


def _compact_item_title(item: ResponsibilityItem) -> str:
    raw = format_item_title(item, max(1, len(str(item.failureTitle or "")) + 80))
    match = _FAILURE_TITLE_RE.match(raw)
    if match:
        description = match.group("title")
        if "(" in description:
            concise = description.split("(", 1)[0].rstrip()
            if concise:
                raw = f"{match.group('id')}：{concise}"
    return _semantic_clip(raw, FEISHU_ITEM_TITLE_MAX_CHARS)


def _evidence_points(value: str) -> list[str]:
    points = _semantic_points(
        value,
        max_points=FEISHU_EVIDENCE_MAX_POINTS,
        max_chars=FEISHU_EVIDENCE_POINT_MAX_CHARS,
    )
    normalized: list[str] = []
    prefixes = (
        (re.compile(r"^(?:日志失败证据|日志证据|日志)\s*[:：]\s*", re.IGNORECASE), "日志："),
        (re.compile(r"^(?:diff\s*证据|代码差异证据|代码证据|diff)\s*[:：]\s*", re.IGNORECASE), "代码："),
        (re.compile(r"^(?:测试断言|测试证据)\s*[:：]?\s*", re.IGNORECASE), "测试："),
    )
    for point in points:
        for pattern, label in prefixes:
            if pattern.search(point):
                point = label + pattern.sub("", point)
                break
        else:
            point = "依据：" + point
        normalized.append(point)
    return normalized


def _is_acceptance_retention_note(value: str) -> bool:
    text = _public_text(value).lower()
    acceptance_context = "验收场景" in text or ("验收" in text and "编排改动" in text)
    retention_evaluation = "是否保留" in text or ("评估" in text and "保留" in text)
    return acceptance_context and retention_evaluation


def _suggestion_points(values: list[str]) -> list[str]:
    points: list[str] = []
    for value in values:
        cleaned = _public_text(value)
        if not cleaned or _is_acceptance_retention_note(cleaned):
            continue
        points.append(_semantic_clip(cleaned, FEISHU_SUGGESTION_POINT_MAX_CHARS))
        if len(points) >= FEISHU_SUGGESTION_MAX_POINTS:
            break
    return points


def _failure_cause_point(item: ResponsibilityItem, category: str) -> str:
    if category == "coverage":
        return _semantic_clip(format_item_title(item, FEISHU_REASON_POINT_MAX_CHARS), FEISHU_REASON_POINT_MAX_CHARS)

    raw_title = _public_text(item.failureTitle)
    title_match = _FAILURE_TITLE_RE.match(raw_title)
    if title_match:
        label = title_match.group("id")
    elif category == "unit" and (item.testFilePath or item.failureFilePath):
        path = str(item.testFilePath or item.failureFilePath).replace("\\", "/")
        label = re.sub(r"\.[^.]+$", "", path.rsplit("/", 1)[-1]) or raw_title
    else:
        label = raw_title

    detail = _public_text(item.failureSummary or item.reason)
    if detail and detail not in {label, raw_title}:
        return _semantic_clip(f"{label}：{detail}", FEISHU_REASON_POINT_MAX_CHARS)
    return _semantic_clip(raw_title or detail, FEISHU_REASON_POINT_MAX_CHARS)


def _grouped_failure_causes(notice: CiResponsibilityNotice) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[ResponsibilityItem]] = {key: [] for key in _FAILURE_CATEGORY_ORDER}
    for item in notice.responsibilityItems:
        grouped[failure_category(item)].append(item)

    result: list[tuple[str, list[str]]] = []
    for category in _FAILURE_CATEGORY_ORDER:
        items = grouped[category]
        if not items:
            continue
        points = [_failure_cause_point(item, category) for item in items[:FEISHU_REASON_MAX_POINTS]]
        omitted = len(items) - len(points)
        if omitted > 0:
            points.append(f"另有 {omitted} 项，详见责任明细")
        label = _FAILURE_CATEGORY_LABELS[category]
        if category == "integration":
            label = integration_failure_group_label(notice, items, label)
        result.append((label, points))
    return result


def format_feishu_notice_payload(
    notice: CiResponsibilityNotice,
    *,
    jenkins_link: str,
    owner_open_ids: dict[str, str],
    maintainer_open_ids: dict[str, str],
    maintainer_routes: list[FeishuMaintainerRoute | None] | None = None,
    fallback_open_ids: tuple[str, ...] = (),
    max_item_details: int = FEISHU_MAX_ITEM_DETAILS,
) -> tuple[dict, FeishuFormatSummary]:
    """Build the Feishu ``interactive`` card and its display summary.

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
            maintainer_routes=maintainer_routes or [],
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
    maintainer_routes: list[FeishuMaintainerRoute | None],
    fallback_open_ids: tuple[str, ...],
) -> tuple[dict, FeishuFormatSummary]:
    rendered_at: set[str] = set()
    title = _compact_title(notice)
    elements: list[dict] = []
    unmapped_owners = _unmapped_owner_names(notice, owner_open_ids)

    if str(notice.result or "").upper() == "SUCCESS":
        overview = ["**📋 构建概览**"]
        if notice.repo:
            overview.append(f"• 项目：{_markdown_text(notice.repo)}")
        overview.append(f"• 流水线：{_markdown_text(notice.job)}")
        if notice.branch:
            overview.append(f"• 分支：{_markdown_text(notice.branch)}")
        elements.append(_markdown("\n".join(overview)))
        elements.append(_markdown(_jenkins_link(notice, jenkins_link), margin="0px"))
        return _package(title, elements, notice, shown, rendered_at, unmapped_owners)

    overview = ["**📋 构建概览**"]
    if notice.repo:
        overview.append(f"• 项目：{_markdown_text(notice.repo)}")
    overview.append(f"• 流水线：{_markdown_text(notice.job)}")
    if notice.branch:
        overview.append(f"• 分支：{_markdown_text(notice.branch)}")
    overview.append(_markdown_text(_stats_line(notice)))
    elements.append(_markdown("\n".join(overview)))

    if (notice.failureReason and notice.failureReason.strip()) or notice.responsibilityItems:
        reason_lines = ["**🔎 失败原因**"]
        grouped_causes = _grouped_failure_causes(notice)
        if grouped_causes:
            for category_label, points in grouped_causes:
                reason_lines.append(f"**{category_label}：**")
                reason_lines.extend(f"• {_markdown_text(point)}" for point in points)
        else:
            reason_points = _semantic_points(
                notice.failureReason,
                max_points=FEISHU_REASON_MAX_POINTS,
                max_chars=FEISHU_REASON_POINT_MAX_CHARS,
                split_before_failure_id=True,
            )
            if reason_points:
                fallback_category = failure_reason_category(notice.failureReason)
                reason_lines.append(f"**{_FAILURE_CATEGORY_LABELS[fallback_category]}：**")
                reason_lines.extend(f"• {_markdown_text(point)}" for point in reason_points)
        elements.append(_markdown("\n".join(reason_lines)))

    items = notice.responsibilityItems
    omitted = max(0, len(items) - shown)
    if not items:
        if notice.owner.type == "no_high_confidence_owner":
            item_lines = [
                "**👥 责任明细**",
                "**❓ 待确认｜未识别到独立责任项**",
                f"👤 责任人：{_markdown_text(NO_OWNER_NAME)}",
                "• 来源：-",
                "• 依据：证据不足，详见分析结果 JSON。",
            ]
            route = maintainer_routes[0] if maintainer_routes else None
            item_lines.extend(
                _maintainer_route_lines(
                    route,
                    maintainer_open_ids=maintainer_open_ids,
                    fallback_open_ids=fallback_open_ids,
                    rendered_at=rendered_at,
                )
            )
            elements.append(_markdown("\n".join(item_lines), margin=FEISHU_CARD_SECTION_MARGIN))
    else:
        shown_items = items[:shown]
        last_shown_index = len(shown_items) - 1
        for index, item in enumerate(shown_items):
            item_lines = []
            if index == 0:
                # 大区块标题并入首个责任项元素，确保与首个责任项标题直接相连。
                item_lines.append("**👥 责任明细**")
            item_lines.append(
                "**"
                + _markdown_text(
                    f"{responsibility_item_icon(item, notice)} "
                    f"{responsibility_item_label(item, notice)}｜{_compact_item_title(item)}"
                )
                + "**"
            )
            person_lines = _item_person_lines(
                item,
                owner_open_ids=owner_open_ids,
                maintainer_open_ids=maintainer_open_ids,
                maintainer_route=maintainer_routes[index] if index < len(maintainer_routes) else None,
                fallback_open_ids=fallback_open_ids,
                rendered_at=rendered_at,
            )
            item_lines.extend(person_lines)
            evidence = format_item_evidence(
                item,
                {e.id: e for e in notice.evidence},
                max(1, len(str(item.reason or "")) + 500),
            )
            if evidence:
                item_lines.extend(f"• {_markdown_text(point)}" for point in _evidence_points(evidence))
            # 区块最后一个内容元素承担区块间距；非末尾责任项用责任项间距。
            item_margin = (
                FEISHU_CARD_SECTION_MARGIN if omitted == 0 and index == last_shown_index else FEISHU_CARD_ITEM_MARGIN
            )
            elements.append(_markdown("\n".join(item_lines), margin=item_margin))

    if omitted > 0:
        elements.append(
            _markdown(
                f"📎 另有 {omitted} 项未展开（共 {len(items)} 项），详见 Jenkins",
                margin=FEISHU_CARD_SECTION_MARGIN,
            )
        )

    suggestions = _suggestion_points(collect_notice_suggestions(notice))
    if suggestions:
        suggestion_lines = ["**🛠️ 修复建议**"]
        suggestion_lines.extend(f"• {_markdown_text(suggestion)}" for suggestion in suggestions)
        elements.append(_markdown("\n".join(suggestion_lines)))

    elements.append(_markdown(_jenkins_link(notice, jenkins_link), margin="0px"))
    return _package(title, elements, notice, shown, rendered_at, unmapped_owners)


def _package(
    title: str,
    elements: list[dict],
    notice: CiResponsibilityNotice,
    shown: int,
    rendered_at: set[str],
    unmapped_owners: tuple[str, ...],
) -> tuple[dict, FeishuFormatSummary]:
    total = len(notice.responsibilityItems)
    payload = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "width_mode": "fill",
                "style": {
                    "text_size": {
                        "normal_v2": {"default": "normal", "pc": "normal", "mobile": "normal"}
                    }
                },
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": elements,
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
                "padding": "12px 12px 12px 12px",
            },
        },
    }
    summary = FeishuFormatSummary(
        total_item_count=total,
        shown_item_count=min(total, shown),
        omitted_item_count=max(0, total - shown),
        at_open_ids=tuple(sorted(rendered_at)),
        unmapped_owner_names=unmapped_owners,
    )
    return payload, summary


def _jenkins_link(notice: CiResponsibilityNotice, jenkins_link: str) -> str:
    return f"[查看 Jenkins 分析 #{notice.buildNumber}]({jenkins_link})"


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


def _item_person_lines(
    item: ResponsibilityItem,
    *,
    owner_open_ids: dict[str, str],
    maintainer_open_ids: dict[str, str],
    maintainer_route: FeishuMaintainerRoute | None,
    fallback_open_ids: tuple[str, ...],
    rendered_at: set[str],
) -> list[str]:
    owner = item.owner
    if item.responsibilityType != "no_high_confidence_owner" and owner.name and owner.name != NO_OWNER_NAME:
        role = format_item_owner_label(item)
        open_id = owner_open_ids.get(owner.name)
        person = f"👤 {role}：{_markdown_text(owner.name)}"
        if open_id and open_id not in rendered_at:
            rendered_at.add(open_id)
            return [f"{person} <at id={open_id}></at>"]
        if open_id:
            return [person]
        return [f"{person}（未完成身份映射）"]
    return [
        f"👤 责任人：{_markdown_text(NO_OWNER_NAME)}",
        *_maintainer_route_lines(
            maintainer_route,
            maintainer_open_ids=maintainer_open_ids,
            fallback_open_ids=fallback_open_ids,
            rendered_at=rendered_at,
        ),
    ]


def _maintainer_route_lines(
    route: FeishuMaintainerRoute | None,
    *,
    maintainer_open_ids: dict[str, str],
    fallback_open_ids: tuple[str, ...],
    rendered_at: set[str],
) -> list[str]:
    names = route.names if route else ()
    reason = route.reason if route and route.reason else "未生成维护人路由，使用默认兜底人"
    if names:
        people: list[str] = []
        has_reachable_maintainer = False
        for name in names:
            open_id = maintainer_open_ids.get(name)
            person = _markdown_text(name)
            if open_id:
                has_reachable_maintainer = True
                if open_id not in rendered_at:
                    rendered_at.add(open_id)
                    person += f" <at id={open_id}></at>"
            else:
                person += "（未完成身份映射）"
            people.append(person)
        lines = [f"📣 待确认维护人：{'、'.join(people)}"]
        if not has_reachable_maintainer and fallback_open_ids:
            lines.append(f"📣 兜底维护人：{_fallback_mentions(fallback_open_ids, rendered_at)}")
            reason = f"{reason}；维护人无法在飞书中 @，已使用默认兜底人"
        elif not has_reachable_maintainer:
            reason = f"{reason}；维护人无法在飞书中 @，且未配置默认兜底人"
        if reason:
            lines.append(f"• ℹ️ 路由说明：{_markdown_text(_semantic_clip(reason, FEISHU_EVIDENCE_POINT_MAX_CHARS))}")
        return lines

    fallback_mentions = _fallback_mentions(fallback_open_ids, rendered_at)
    lines = [f"📣 待确认维护人：{fallback_mentions or '未配置'}"]
    if not fallback_mentions:
        reason = reason.replace("使用默认兜底人", "但未配置默认兜底人")
    lines.append(f"• ℹ️ 路由说明：{_markdown_text(_semantic_clip(reason, FEISHU_EVIDENCE_POINT_MAX_CHARS))}")
    return lines


def _fallback_mentions(open_ids: tuple[str, ...], rendered_at: set[str]) -> str:
    mentions: list[str] = []
    for open_id in open_ids:
        if not open_id:
            continue
        rendered_at.add(open_id)
        mentions.append(f"<at id={open_id}></at>")
    return "、".join(mentions)


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
