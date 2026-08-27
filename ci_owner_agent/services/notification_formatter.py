from __future__ import annotations

import re
from urllib.parse import urlencode

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice, EvidenceItem, Owner, ResponsibilityItem
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainer, TestMaintainerResolver
from ci_owner_agent.services.wecom_notification_routing import (
    collect_pending_maintainers as _collect_pending_maintainers,
    collect_responsible_display_names,
    collect_responsible_owners,
    notification_digest,
    resolve_test_maintainer_matches,
)
from ci_owner_agent.services.wecom_user_mapping import WeComUserMapper


COVERAGE_FAILURE_SIGNATURE_PREFIX = "coverage_threshold_failure|"
COVERAGE_FAILURE_TITLE_RE = re.compile(
    r"^coverage\s+(?P<metrics>.+?)\s+below threshold for\s+(?P<path>.+)$",
    re.IGNORECASE,
)
SINGLE_AUTHOR_COVERAGE_REASON_RE = re.compile(
    r"^single author (?P<name>.+?) modified (?P<path>.+?) in (?P<scope>focus|full) range$",
    re.IGNORECASE,
)
COVERAGE_METRIC_LABELS = {
    "branches": "分支",
    "functions": "函数",
    "lines": "行",
    "statements": "语句",
}


WECOM_MAX_UTF8_BYTES = 4096


def _utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节截断，不切断半个中文字符。"""
    if _utf8_bytes(text) <= max_bytes:
        return text
    truncated = text
    while _utf8_bytes(truncated) > max_bytes:
        truncated = truncated[:-1]
    return truncated


def _finalize_within_budget(
    parts: list[str],
    protected_tail: list[str] | None = None,
    protected_head: list[str] | None = None,
) -> str:
    """最终统一收口：拼接所有段落并强制 ≤4096 UTF-8 字节。

    超限时只截断前部可变内容，保留尾部 Jenkins/反馈链接与省略提示；
    按字符边界截断，保证不切断半个中文字符。
    """
    head = list(protected_head or [])
    tail = list(protected_tail or [])
    full = "\n".join(head + parts + tail)
    if _utf8_bytes(full) <= WECOM_MAX_UTF8_BYTES:
        return full
    omission = "…消息过长已截断，请查看分析结果 JSON。"
    head_text = "\n".join(head)
    tail_text = "\n".join(tail)
    protected_bytes = _utf8_bytes(head_text) + _utf8_bytes(tail_text)
    separators = int(bool(head_text)) + int(bool(tail_text)) + 1
    middle_budget = WECOM_MAX_UTF8_BYTES - _utf8_bytes(omission) - protected_bytes - separators
    if middle_budget < 0:
        # 极端情况下受保护字段本身已超预算：优先保留头部责任/@信息和省略提示。
        head_budget = WECOM_MAX_UTF8_BYTES - _utf8_bytes(omission) - 1
        return _truncate_utf8(head_text, max(0, head_budget)) + "\n" + omission
    middle_text = _truncate_utf8("\n".join(parts), middle_budget)
    output = list(head)
    if middle_text:
        output.append(middle_text)
    output.append(omission)
    output.extend(tail)
    return "\n".join(output)


def _append_within_budget(lines: list[str], tail_lines: list[str], addition: list[str]) -> bool:
    """在不超过 4096 UTF-8 字节预算的前提下追加内容。

    尾部（链接/反馈）始终保留；若追加 ``addition`` 会超预算，返回 False 且不追加。
    按字符边界拼接，不截断半个中文字符。
    """
    current = "\n".join(lines)
    tail = "\n".join(tail_lines)
    candidate = current + ("\n" + "\n".join(addition) if addition else "")
    if _utf8_bytes(candidate + ("\n" + tail if tail else "")) <= WECOM_MAX_UTF8_BYTES:
        lines.extend(addition)
        return True
    return False


def format_wecom_markdown_notice(
    notice: CiResponsibilityNotice,
    feedback_base_url: str | None = None,
    feedback_token: str | None = None,
    max_reason_chars: int = 800,
    max_evidence_chars: int = 500,
    user_mapper: WeComUserMapper | None = None,
    mention_mode: str = "userid",
    fallback_userids: tuple[str, ...] = (),
    maintainer_resolver: TestMaintainerResolver | None = None,
    repo: str | None = None,
    feedback_code: str | None = None,
) -> str:
    display_job = truncate_single_line(notice.job, 240)
    job_truncated = display_job != str(notice.job or "")
    title = f"### {result_icon(notice.result)} CI 单测{result_label(notice.result)} | {display_job} #{notice.buildNumber}"
    if str(notice.result or "").upper() == "SUCCESS":
        tail_lines: list[str] = []
        if job_truncated:
            tail_lines.extend(["", "…Job 名称过长已截断。"])
        if notice.buildUrl:
            tail_lines.extend(["", f"🏗️ [查看 Jenkins 构建]({notice.buildUrl})"])
        return _finalize_within_budget([], tail_lines, [title])

    mapper = user_mapper or WeComUserMapper([])
    owners = collect_responsible_owners(notice)
    maintainer_matches = resolve_test_maintainer_matches(
        notice,
        maintainer_resolver=maintainer_resolver,
        repo=repo,
        fallback_userids=fallback_userids,
    )
    pending_maintainers = _collect_pending_maintainers(maintainer_matches)
    evidence_by_id = {item.id: item for item in notice.evidence}
    protected_head = [
        title,
        "",
        f"👤 **责任人**：{format_responsible_mentions(owners, mapper, mention_mode)}",
    ]
    if any(match is not None for match in maintainer_matches):
        protected_head.append(f"📣 **待确认维护人**：{format_test_maintainer_mentions(pending_maintainers, mention_mode) or '未配置'}")
    lines = protected_head + [
        f"**原因**：{public_single_line(notice.failureReason, max_reason_chars)}",
        responsibility_item_stats(notice),
        "",
        "#### 📌 责任项",
        "",
    ]

    # 尾部（建议 + 链接 + 反馈）先构建，始终保留，不计入责任项的预算竞争。
    tail_lines: list[str] = []
    if job_truncated:
        tail_lines.extend(["…Job 名称过长已截断。", ""])
    suggestions = format_suggestions(notice.suggestions)
    if suggestions:
        tail_lines.extend(["#### 🛠️ 修复建议", ""])
        tail_lines.extend(f"- {suggestion}" for suggestion in suggestions)
        tail_lines.append("")
    tail_lines.extend(["#### 🔗 相关链接", ""])
    tail_lines.append(f"- 🏗️ [查看 Jenkins 构建]({notice.buildUrl})" if notice.buildUrl else "- 🏗️ Jenkins 构建：无")
    feedback_url = build_feedback_url(feedback_base_url, notice, feedback_token)
    tail_lines.append(f"- 📝 [提交反馈]({feedback_url})" if feedback_url else "- 📝 反馈：未配置")
    if feedback_code:
        tail_lines.extend(
            [
                "",
                f"**反馈码：{feedback_code}**",
                "",
                "群内反馈：",
                f"@机器人 {feedback_code} 1 判断正确",
                f"@机器人 {feedback_code} 1 责任人改为 @某人",
                f"@机器人 {feedback_code} 1 标记偶发",
            ]
        )

    # 责任项逐个追加，逼近 4096 UTF-8 字节；超限项不静默丢弃，末尾标注省略。
    shown = 0
    if notice.responsibilityItems:
        for idx, item in enumerate(notice.responsibilityItems, start=1):
            owner_name = format_item_owner(item.owner, mapper, mention_mode)
            item_lines = [
                f"{idx}. {responsibility_item_icon(item, notice)} {responsibility_item_label(item, notice)} | {format_item_title(item, 120)}",
                f"   - 👤 {format_item_owner_label(item)}：{owner_name}",
                f"   - 来源：{source_build_label(item, notice)}",
                f"   - 🔎 证据：{format_item_evidence(item, evidence_by_id, max_evidence_chars)}",
            ]
            coverage_suggestion = format_coverage_item_suggestion(item)
            if coverage_suggestion:
                item_lines.append(f"   - 🛠️ 建议：{coverage_suggestion}")
            match = maintainer_matches[idx - 1]
            if match is not None:
                item_lines.extend(
                    [
                        f"   - 📁 测试文件：{match.test_file_path or '未识别'}",
                        f"   - 📣 待确认维护人：{format_test_maintainer_mentions(match.maintainers, mention_mode) or '未配置'}",
                    ]
                )
                if match.used_fallback:
                    item_lines.append(f"   - ℹ️ 路由说明：{match.reason}")
            item_lines.append("")
            if not _append_within_budget(lines, tail_lines, item_lines):
                break
            shown += 1
        omitted = len(notice.responsibilityItems) - shown
        if omitted > 0:
            tail_lines[0:0] = ["", f"…本消息展示 {shown} 项，其余 {omitted} 项请查看分析结果 JSON。", ""]
    else:
        lines.extend(["1. ❓ unknown | 未识别到独立责任项", f"   - 👤 责任人：{NO_OWNER_NAME}", "   - 来源：-", "   - 🔎 证据：证据不足，详见分析结果 JSON。", ""])

    return _finalize_within_budget(lines[len(protected_head):], tail_lines, protected_head)


def format_test_maintainer_mentions(maintainers: tuple[TestMaintainer, ...] | list[TestMaintainer], mention_mode: str) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for maintainer in maintainers:
        userid = str(maintainer.wecom_userid or "").strip()
        if not userid or userid in seen:
            continue
        seen.add(userid)
        if mention_mode == "name" and maintainer.name:
            result.append(f"@{maintainer.name}")
        else:
            result.append(f"<@{userid}>")
    return "、".join(result)


def format_fallback_userid_mentions(userids: tuple[str, ...] | list[str] | None) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for userid in userids or ():
        value = str(userid or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(f"<@{value}>")
    return "、".join(result)


def format_responsible_mentions(
    owners: list[Owner],
    mapper: WeComUserMapper | None = None,
    mention_mode: str = "userid",
    fallback_userids: tuple[str, ...] = (),
) -> str:
    mapper = mapper or WeComUserMapper([])
    if owners:
        return "、".join(mapper.mention_owner(owner.name, owner.email, mode=mention_mode) for owner in owners)
    fallback_mentions = format_fallback_userid_mentions(fallback_userids)
    if fallback_mentions:
        return f"{NO_OWNER_NAME}，兜底通知 {fallback_mentions}"
    return NO_OWNER_NAME


def format_item_owner(owner: Owner, mapper: WeComUserMapper, mention_mode: str) -> str:
    if owner.type == "no_high_confidence_owner" or not owner.name or owner.name == NO_OWNER_NAME:
        return NO_OWNER_NAME
    return mapper.mention_owner(owner.name, owner.email, mode=mention_mode)


def format_item_owner_label(item: ResponsibilityItem) -> str:
    if item.owner.type == "medium_confidence":
        return "责任人（中等置信）"
    return "责任人"


def is_coverage_failure_item(item: ResponsibilityItem) -> bool:
    return str(item.failureSignature or "").startswith(COVERAGE_FAILURE_SIGNATURE_PREFIX)


def is_continuing_coverage_item(item: ResponsibilityItem, notice: CiResponsibilityNotice) -> bool:
    return (
        item.responsibilityType == "current_build_owner"
        and is_coverage_failure_item(item)
        and bool(item.sourceCommit)
        and bool(notice.headCommit)
        and item.sourceCommit != notice.headCommit
    )


def responsibility_item_icon(item: ResponsibilityItem, notice: CiResponsibilityNotice) -> str:
    if is_continuing_coverage_item(item, notice):
        return "♻️"
    return responsibility_type_icon(item.responsibilityType)


def responsibility_item_label(item: ResponsibilityItem, notice: CiResponsibilityNotice) -> str:
    if is_continuing_coverage_item(item, notice):
        return "覆盖率持续"
    return responsibility_type_label(item.responsibilityType)


def coverage_title_parts(item: ResponsibilityItem) -> tuple[str, tuple[str, ...]]:
    match = COVERAGE_FAILURE_TITLE_RE.match(str(item.failureTitle or "").strip())
    if not match:
        return str(item.failureFilePath or "").strip(), ()
    metrics = tuple(
        COVERAGE_METRIC_LABELS.get(metric.strip().lower(), metric.strip())
        for metric in match.group("metrics").split(",")
        if metric.strip()
    )
    return match.group("path"), metrics


def format_item_title(item: ResponsibilityItem, max_chars: int) -> str:
    title = public_single_line(item.failureTitle, max_chars)
    if not is_coverage_failure_item(item):
        return title
    path, metrics = coverage_title_parts(item)
    if not path:
        return f"覆盖率未达标：{title}"
    return truncate_single_line(
        f"覆盖率未达标：{path}（{'、'.join(metrics) or '指标'}）",
        max_chars,
    )


def format_item_reason(item: ResponsibilityItem, max_chars: int) -> str:
    reason = item.reason.strip()
    if is_coverage_failure_item(item):
        match = SINGLE_AUTHOR_COVERAGE_REASON_RE.match(reason)
        if match:
            return truncate_single_line(
                f"在本次责任排查范围内，仅 {match.group('name')} 修改了 {match.group('path')}。",
                max_chars,
            )
    return public_single_line(reason, max_chars)


def format_coverage_item_suggestion(item: ResponsibilityItem) -> str | None:
    if not is_coverage_failure_item(item):
        return None
    path, metrics = coverage_title_parts(item)
    target = path or "相关源码文件"
    metric_text = "、".join(metrics) or "覆盖率"
    return f"补充 {target} 中未覆盖分支的测试，使 {metric_text} 达到配置阈值。"


def format_item_evidence(item: ResponsibilityItem, evidence_by_id: dict[str, EvidenceItem], max_chars: int = 500) -> str:
    if item.reason.strip():
        return format_item_reason(item, max_chars)
    if item.responsibilityType == "inherited_failure_owner":
        build = item.sourceBuildNumber or "-"
        name = item.owner.name or NO_OWNER_NAME
        return truncate_single_line(
            f"当前失败与历史构建 #{build} 的失败表现一致，属于历史持续失败；责任继承自首次失败责任人 {name}，不是当前 build 新引入。",
            max_chars,
        )
    snippets: list[str] = []
    for evidence_id in item.evidenceIds[:2]:
        evidence = evidence_by_id.get(evidence_id)
        if not evidence:
            continue
        text = evidence.summary or evidence.detail[:200]
        if text:
            snippets.append(public_single_line(text, 220))
    return public_single_line("；".join(snippets), max_chars) if snippets else "证据不足，详见分析结果 JSON。"


def result_icon(result: str) -> str:
    return {
        "SUCCESS": "✅",
        "FAILURE": "❌",
        "UNSTABLE": "⚠️",
        "ABORTED": "⏹️",
        "UNKNOWN": "❔",
    }.get(str(result or "").upper(), "❔")


def result_label(result: str) -> str:
    return {
        "SUCCESS": "成功",
        "FAILURE": "失败",
        "UNSTABLE": "不稳定",
        "ABORTED": "中止",
        "UNKNOWN": "未知",
    }.get(str(result or "").upper(), "未知")


def responsibility_type_icon(value: str) -> str:
    return {
        "current_build_owner": "🔥",
        "inherited_failure_owner": "♻️",
        "no_high_confidence_owner": "❓",
    }.get(str(value or ""), "❓")


def responsibility_type_label(value: str) -> str:
    return {
        "current_build_owner": "当前引入",
        "inherited_failure_owner": "历史持续",
        "no_high_confidence_owner": "待确认",
    }.get(str(value or ""), str(value or "unknown"))


def responsibility_item_stats(notice: CiResponsibilityNotice) -> str:
    items = notice.responsibilityItems
    if not items:
        return "📌 **责任项**：未识别到独立责任项"
    current = sum(
        1
        for item in items
        if item.responsibilityType == "current_build_owner"
        and not is_continuing_coverage_item(item, notice)
    )
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
    return f"📌 **责任项**：{'，'.join(parts)}"


def format_suggestions(suggestions: list[str], max_items: int = 3) -> list[str]:
    result: list[str] = []
    for item in suggestions[:max_items]:
        cleaned = public_single_line(item, 200)
        if cleaned:
            result.append(cleaned)
    return result


def build_feedback_url(base_url: str | None, notice: CiResponsibilityNotice, token: str | None = None) -> str | None:
    if not base_url or not str(notice.repo or "").strip() or not str(notice.branch or "").strip():
        return None
    query_params = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "build": notice.buildNumber}
    if token:
        query_params["token"] = token
    query = urlencode(query_params)
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{query}"


def truncate_single_line(text: str | None, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)] + "..."


def public_single_line(text: str | None, limit: int) -> str:
    cleaned = truncate_single_line(text, limit)
    cleaned = cleaned.replace("failureSignature", "failure feature")
    cleaned = cleaned.replace("signatureHash", "feature hash")
    cleaned = cleaned.replace("failureId", "failure item")
    cleaned = cleaned.replace("签名", "特征")
    return cleaned


def source_build_label(item: ResponsibilityItem, notice: CiResponsibilityNotice) -> str:
    if item.sourceBuildNumber:
        return f"#{item.sourceBuildNumber}"
    if item.responsibilityType == "current_build_owner":
        return f"#{notice.buildNumber}"
    return "-"
