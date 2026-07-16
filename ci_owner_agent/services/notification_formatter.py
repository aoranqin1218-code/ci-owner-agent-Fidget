from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlencode

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice, EvidenceItem, Owner, ResponsibilityItem
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainer, TestMaintainerMatch, TestMaintainerResolver
from ci_owner_agent.services.wecom_user_mapping import WeComUserMapper


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
    lines = [
        f"### {result_icon(notice.result)} CI 单测{result_label(notice.result)} | {notice.job} #{notice.buildNumber}",
        "",
        f"👤 **责任人**：{format_responsible_mentions(owners, mapper, mention_mode)}",
    ]
    if any(match is not None for match in maintainer_matches):
        lines.append(f"📣 **待确认维护人**：{format_test_maintainer_mentions(pending_maintainers, mention_mode) or '未配置'}")
    lines.extend(
        [
            f"🧭 **原因**：{public_single_line(notice.failureReason, max_reason_chars)}",
            responsibility_item_stats(notice.responsibilityItems),
            "",
            "#### 🧩 责任项",
            "",
        ]
    )
    if notice.responsibilityItems:
        for idx, item in enumerate(notice.responsibilityItems, start=1):
            owner_name = format_item_owner(item.owner, mapper, mention_mode)
            item_type = str(item.responsibilityType or "unknown")
            item_lines = [
                f"{idx}. {responsibility_type_icon(item_type)} {responsibility_type_label(item_type)} | {public_single_line(item.failureTitle, 120)}",
                f"   - 👤 责任人：{owner_name}",
                f"   - 🧷 来源：{source_build_label(item, notice)}",
                f"   - 🔎 证据：{format_item_evidence(item, evidence_by_id, max_evidence_chars)}",
            ]
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
            lines.extend(item_lines)
    else:
        lines.extend(["1. 🧩 unknown | 未识别到独立责任项", f"   - 👤 责任人：{NO_OWNER_NAME}", "   - 🧷 来源：-", "   - 🔎 证据：证据不足，详见分析结果 JSON。", ""])

    suggestions = format_suggestions(notice.suggestions)
    if suggestions:
        lines.extend(["#### 🛠️ 修复建议", ""])
        lines.extend(f"- {suggestion}" for suggestion in suggestions)
        lines.append("")

    lines.extend(["#### 🔗 相关链接", ""])
    lines.append(f"- 🏗️ [查看 Jenkins 构建]({notice.buildUrl})" if notice.buildUrl else "- 🏗️ Jenkins 构建：无")
    feedback_url = build_feedback_url(feedback_base_url, notice, feedback_token)
    lines.append(f"- 📝 [提交反馈]({feedback_url})" if feedback_url else "- 📝 反馈：未配置")
    if feedback_code:
        lines.extend(
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
    return "\n".join(lines)


def resolve_test_maintainer_matches(
    notice: CiResponsibilityNotice,
    *,
    maintainer_resolver: TestMaintainerResolver | None,
    repo: str | None,
    fallback_userids: tuple[str, ...],
) -> list[TestMaintainerMatch | None]:
    resolver = maintainer_resolver or TestMaintainerResolver()
    matches: list[TestMaintainerMatch | None] = []
    if (
        str(notice.result or "").upper() in {"FAILURE", "UNSTABLE", "UNKNOWN"}
        and not notice.responsibilityItems
        and notice.owner.type == "no_high_confidence_owner"
    ):
        return [
            resolver.resolve(
                repo=repo or notice.repo,
                job=notice.job,
                test_file_path=None,
                fallback_userids=fallback_userids,
            )
        ]
    for item in notice.responsibilityItems:
        if item.responsibilityType != "no_high_confidence_owner":
            matches.append(None)
            continue
        matches.append(
            resolver.resolve(
                repo=repo or notice.repo,
                job=notice.job,
                test_file_path=item.testFilePath,
                fallback_userids=fallback_userids,
            )
        )
    return matches


def notification_digest(
    notice: CiResponsibilityNotice,
    *,
    maintainer_resolver: TestMaintainerResolver | None = None,
    repo: str | None = None,
    fallback_userids: tuple[str, ...] = (),
    mention_mode: str = "userid",
) -> str:
    matches = resolve_test_maintainer_matches(
        notice,
        maintainer_resolver=maintainer_resolver,
        repo=repo,
        fallback_userids=fallback_userids,
    )
    routes = []
    for index, match in enumerate(matches):
        if match is None:
            continue
        routes.append(
            {
                "itemIndex": index,
                "testFilePath": match.test_file_path,
                "matchedPattern": match.matched_pattern,
                "maintainerUserids": [item.wecom_userid for item in match.maintainers],
                "maintainerNames": [item.name for item in match.maintainers],
                "usedFallback": match.used_fallback,
                "reason": match.reason,
            }
        )
    payload = {
        "notice": notice.model_dump(mode="json"),
        "mentionMode": mention_mode,
        "testMaintainerRoutes": routes,
    }
    from ci_owner_agent.services.failure_identity import canonicalize_failure_message

    raw = canonicalize_failure_message(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def _collect_pending_maintainers(matches: list[TestMaintainerMatch | None]) -> tuple[TestMaintainer, ...]:
    result: list[TestMaintainer] = []
    seen: set[str] = set()
    for match in matches:
        if match is None:
            continue
        for maintainer in match.maintainers:
            if maintainer.wecom_userid in seen:
                continue
            seen.add(maintainer.wecom_userid)
            result.append(maintainer)
    return tuple(result)


def collect_responsible_owners(notice: CiResponsibilityNotice) -> list[Owner]:
    owners: list[Owner] = []
    seen: set[str] = set()
    for item in notice.responsibilityItems:
        owner = item.owner
        if owner.type == "no_high_confidence_owner" or not owner.name or owner.name == NO_OWNER_NAME:
            continue
        key = owner.email.lower().strip() if owner.email else owner.name
        if key in seen:
            continue
        seen.add(key)
        owners.append(owner)
    return owners


def collect_responsible_display_names(notice: CiResponsibilityNotice) -> list[str]:
    return [owner.name for owner in collect_responsible_owners(notice)]


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


def format_item_evidence(item: ResponsibilityItem, evidence_by_id: dict[str, EvidenceItem], max_chars: int = 500) -> str:
    if item.reason.strip():
        return public_single_line(item.reason, max_chars)
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
    }.get(str(value or ""), "🧩")


def responsibility_type_label(value: str) -> str:
    return {
        "current_build_owner": "当前引入",
        "inherited_failure_owner": "历史持续",
        "no_high_confidence_owner": "待确认",
    }.get(str(value or ""), str(value or "unknown"))


def responsibility_item_stats(items: list[ResponsibilityItem]) -> str:
    if not items:
        return "📌 **责任项**：未识别到独立责任项"
    current = sum(1 for item in items if item.responsibilityType == "current_build_owner")
    inherited = sum(1 for item in items if item.responsibilityType == "inherited_failure_owner")
    unresolved = sum(1 for item in items if item.responsibilityType == "no_high_confidence_owner")
    other = len(items) - current - inherited - unresolved
    parts = [f"共 {len(items)} 项"]
    if current:
        parts.append(f"当前引入 {current}")
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
