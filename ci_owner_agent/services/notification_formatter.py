from __future__ import annotations

import re
from urllib.parse import urlencode

from ci_owner_agent.schemas import CiResponsibilityNotice, EvidenceItem, ResponsibilityItem


def format_wecom_markdown_notice(
    notice: CiResponsibilityNotice,
    feedback_base_url: str | None = None,
    max_reason_chars: int = 800,
    max_evidence_chars: int = 500,
) -> str:
    names = collect_responsible_display_names(notice)
    evidence_by_id = {item.id: item for item in notice.evidence}
    lines = [
        f"### 单测失败 | {notice.job} #{notice.buildNumber}",
        "",
        f"**责任人**：{format_responsible_mentions(names)}",
        f"**原因**：{public_single_line(notice.failureReason, max_reason_chars)}",
        "",
        "#### 责任项",
    ]
    if notice.responsibilityItems:
        for idx, item in enumerate(notice.responsibilityItems, start=1):
            owner_name = item.owner.name if item.owner.name and item.owner.name != "无高可信责任人" else "无高可信责任人"
            lines.extend(
                [
                    f"{idx}. {public_single_line(item.failureTitle, 120)}",
                    f"   - 类型：{item.responsibilityType}",
                    f"   - 责任人：{owner_name}",
                    f"   - 来源：{source_build_label(item, notice)}",
                    f"   - 证据：{format_item_evidence(item, evidence_by_id, max_evidence_chars)}",
                    "",
                ]
            )
    else:
        lines.extend(["1. 未识别到独立责任项", "   - 类型：unknown", "   - 责任人：无高可信责任人", "   - 来源：-", "   - 证据：证据不足，详见分析结果 JSON。", ""])

    lines.extend(["#### 构建链接", f"[查看 Jenkins 构建]({notice.buildUrl})" if notice.buildUrl else "无", "", "#### 反馈链接"])
    feedback_url = build_feedback_url(feedback_base_url, notice)
    lines.append(f"[提交反馈]({feedback_url})" if feedback_url else "未配置")
    return "\n".join(lines)


def collect_responsible_display_names(notice: CiResponsibilityNotice) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in notice.responsibilityItems:
        owner = item.owner
        if owner.type == "no_high_confidence_owner" or not owner.name or owner.name == "无高可信责任人":
            continue
        if owner.name in seen:
            continue
        seen.add(owner.name)
        names.append(owner.name)
    return names


def format_responsible_mentions(names: list[str]) -> str:
    return "、".join(f"@{name}" for name in names) if names else "无高可信责任人"


def format_item_evidence(item: ResponsibilityItem, evidence_by_id: dict[str, EvidenceItem], max_chars: int = 500) -> str:
    if item.reason.strip():
        return public_single_line(item.reason, max_chars)
    if item.responsibilityType == "inherited_failure_owner":
        build = item.sourceBuildNumber or "-"
        name = item.owner.name or "无高可信责任人"
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


def build_feedback_url(base_url: str | None, notice: CiResponsibilityNotice) -> str | None:
    if not base_url:
        return None
    query = urlencode({"job": notice.job, "build": notice.buildNumber})
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
