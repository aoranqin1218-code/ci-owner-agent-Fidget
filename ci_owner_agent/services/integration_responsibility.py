"""Fidget 二期：集成测试环境/未知失败的责任保护 guard。

与 coverage 方案 X 一致：环境/基础设施失败是 no-owner、history-ineligible，
由本模块单一写入 canonical no-owner ResponsibilityItem；Agent 不得为环境失败
生成 owner，guard 会移除其意外生成的环境项。

镜像 coverage_responsibility.reconcile_coverage_responsibilities 的「移除 + 写
canonical item + 聚合顶层 owner」三步，但这里不查 Git/diff——环境失败没有代码
责任，直接写 no-owner。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice, EvidenceItem, Owner, ResponsibilityItem


def _env_signature(kind: str, detail: str) -> str:
    """环境/未知失败事实的稳定签名。"""
    return f"integration_{kind}|{detail or 'unresolved'}"


def _is_integration_env_item(
    item: ResponsibilityItem,
    *,
    preflight_steps: set[str] | None = None,
    cleanup_failed: bool = False,
) -> bool:
    signature = item.failureSignature or ""
    if (
        signature.startswith("integration_connection|")
        or signature.startswith("integration_env|")
        or signature.startswith("integration_unknown|")
        or signature.startswith("integration_pipeline|")
    ):
        return True
    if item.testFilePath or item.failureFilePath:
        return False
    text = " ".join(
        str(value or "").lower()
        for value in (item.failureSignature, item.failureTitle, item.failureSummary)
    )
    for step in preflight_steps or set():
        variants = {step.lower(), step.lower().replace("_", " "), step.lower().replace("_", "-")}
        if "preflight" in text and any(variant in text for variant in variants):
            return True
    return cleanup_failed and "cleanup" in text


def _no_owner_owner() -> Owner:
    return Owner(type="no_high_confidence_owner", name=NO_OWNER_NAME, email=None, commit=None, confidence=0.0)


def _env_item(
    *,
    signature: str,
    title: str,
    reason: str,
    evidence_ids: list[str],
) -> ResponsibilityItem:
    return ResponsibilityItem(
        failureId="integration-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12],
        failureTitle=title,
        failureSignature=signature,
        failureSummary=title,
        testFilePath=None,
        failureFilePath=None,
        owner=_no_owner_owner(),
        responsibilityType="no_high_confidence_owner",
        sourceCommit=None,
        matchType=None,
        relationship=None,
        confidence=0.0,
        reason=reason,
        evidenceIds=evidence_ids,
    )


def reconcile_integration_responsibilities(
    notice: CiResponsibilityNotice,
    *,
    failure_summaries: dict[str, Any] | None,
) -> CiResponsibilityNotice:
    """集成环境/未知失败责任项的唯一生产者：确定性写 canonical no-owner。

    规则：
    1. 从 failure_summaries 的 integrationClassifications、integrationConflicts 与
       integrationProtocolIndex 提取环境/未知事实，逐个生成 no-owner item；
    2. 移除 Agent 意外生成的所有集成环境项（按稳定签名识别）；
    3. 追加 canonical no-owner 项，并聚合顶层 owner。
    """
    if not isinstance(failure_summaries, dict):
        return notice
    classifications = failure_summaries.get("integrationClassifications")
    conflicts = failure_summaries.get("integrationConflicts")
    protocol_index = failure_summaries.get("integrationProtocolIndex")
    if (
        not isinstance(classifications, list)
        and not isinstance(conflicts, list)
        and not isinstance(protocol_index, dict)
    ):
        return notice
    classifications = classifications if isinstance(classifications, list) else []
    conflicts = conflicts if isinstance(conflicts, list) else []
    protocol_index = protocol_index if isinstance(protocol_index, dict) else {}
    preflight_failures = protocol_index.get("preflight_failures")
    preflight_failures = preflight_failures if isinstance(preflight_failures, list) else []
    preflight_steps = {
        str(item.get("step") or "unresolved")
        for item in preflight_failures
        if isinstance(item, dict)
    }
    cleanup_failed = protocol_index.get("cleanup_failed") is True

    # 1. 移除 Agent 意外生成的集成环境项（guard 是唯一生产者）
    removed_agent_env_item = any(
        _is_integration_env_item(
            item,
            preflight_steps=preflight_steps,
            cleanup_failed=cleanup_failed,
        )
        and (
            item.responsibilityType != "no_high_confidence_owner"
            or item.owner.type != "no_high_confidence_owner"
        )
        for item in notice.responsibilityItems
    )
    notice.responsibilityItems = [
        item
        for item in notice.responsibilityItems
        if not _is_integration_env_item(
            item,
            preflight_steps=preflight_steps,
            cleanup_failed=cleanup_failed,
        )
    ]

    # 2. 环境/未知失败事实 → canonical no-owner item。
    # 按 (kind, suite) 聚合：同一 suite 的数百次 connection 只生成一条 canonical item，
    # 行号不进入 signature，避免责任项随失败用例数爆炸。
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for classification in classifications:
        kind = classification.get("kind")
        if kind not in {"connection", "unknown"}:
            continue
        suite = classification.get("suite") or "unresolved"
        key = (kind, suite)
        if key not in grouped:
            grouped[key] = {
                "kind": kind,
                "suite": suite,
                "occurrences": 0,
                "first_line": classification.get("line"),
                "last_line": classification.get("line"),
            }
            order.append(key)
        group = grouped[key]
        group["occurrences"] += 1
        line = classification.get("line")
        if line is not None:
            if group["first_line"] is None:
                group["first_line"] = line
            group["last_line"] = line

    for key in order:
        group = grouped[key]
        kind = group["kind"]
        suite = group["suite"]
        signature = _env_signature(kind, suite)
        if kind == "connection":
            title = f"集成测试外部库/数据库不可达（{suite}）"
            reason = "外部库/数据库连接失败（ECONNREFUSED/超时等），属环境/基础设施问题，无代码责任可归。"
        else:
            title = f"集成测试失败分类不明确（{suite}）"
            reason = "失败证据不足或冲突，无法确定代码责任，保守判为无高可信责任人。"
        evidence_ids = _append_env_evidence(notice, signature, group, kind)
        notice.responsibilityItems.append(
            _env_item(signature=signature, title=title, reason=reason, evidence_ids=evidence_ids)
        )

    # 3. 协议可信时，preflight/cleanup marker 失败也必须形成可通知责任项，
    # 不能只停留在 totals.integrationEnv 计数。若协议本身冲突，则只保留下方的
    # 总体 protocol_conflict，避免把不可信 marker 再拆成多个事实。
    if not conflicts:
        for preflight_failure in preflight_failures:
            if not isinstance(preflight_failure, dict):
                continue
            step = str(preflight_failure.get("step") or "unresolved")
            line = preflight_failure.get("line")
            signature = f"integration_pipeline|preflight:{step}"
            title = f"集成测试前置检查失败（{step}）"
            detail = f"phase=preflight step={step} status=failed"
            if line is not None:
                detail += f" at line {line}"
            _append_protocol_env_item(
                notice,
                signature=signature,
                title=title,
                reason="集成测试前置环境/基础设施检查失败，无代码责任可归。",
                detail=detail,
            )

        if cleanup_failed:
            _append_protocol_env_item(
                notice,
                signature="integration_pipeline|cleanup",
                title="集成测试 cleanup 失败",
                reason="集成测试清理阶段失败，属环境/基础设施问题，无代码责任可归。",
                detail="phase=cleanup status=failed",
            )

    # 4. 协议冲突 → 一个总体的 no-owner 事实（不针对具体 suite）
    if conflicts:
        signature = "integration_unknown|protocol_conflict"
        evidence_id = "integration-conflict-evidence"
        if evidence_id not in {e.id for e in notice.evidence}:
            notice.evidence.append(
                EvidenceItem(
                    id=evidence_id,
                    type="reasoning",
                    summary="集成测试协议 marker 冲突/不完整",
                    detail="; ".join(conflicts)[:2000],
                    source="integration_protocol_index",
                )
            )
        existing_sigs = {item.failureSignature for item in notice.responsibilityItems}
        if signature not in existing_sigs:
            notice.responsibilityItems.append(
                _env_item(
                    signature=signature,
                    title="集成测试协议 marker 冲突/不完整",
                    reason="协议 marker 缺失/重复/乱序/计数不自洽，证据不可信，保守判为无高可信责任人。",
                    evidence_ids=[evidence_id],
                )
            )

    if removed_agent_env_item:
        notice.failureReason = (
            "构建存在代码/测试失败及集成测试环境失败；"
            "最终责任结论见 responsibilityItems。"
            if any(not _is_integration_env_item(item) for item in notice.responsibilityItems)
            else "构建失败：集成测试环境/基础设施失败，无代码责任可归。"
        )
    _aggregate_top_level_owner(notice)
    return notice


def _append_protocol_env_item(
    notice: CiResponsibilityNotice,
    *,
    signature: str,
    title: str,
    reason: str,
    detail: str,
) -> None:
    """把可信协议环境事实幂等写成 canonical no-owner item 与审计证据。"""
    suffix = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    evidence_id = f"integration-protocol-{suffix}"
    if evidence_id not in {e.id for e in notice.evidence}:
        notice.evidence.append(
            EvidenceItem(
                id=evidence_id,
                type="log",
                summary=title,
                detail=detail,
                source="integration_protocol_index",
            )
        )
    if signature not in {item.failureSignature for item in notice.responsibilityItems}:
        notice.responsibilityItems.append(
            _env_item(
                signature=signature,
                title=title,
                reason=reason,
                evidence_ids=[evidence_id],
            )
        )


def _append_env_evidence(
    notice: CiResponsibilityNotice,
    signature: str,
    group: dict[str, Any],
    kind: str,
) -> list[str]:
    suffix = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    evidence_id = f"integration-env-{suffix}"
    if evidence_id not in {e.id for e in notice.evidence}:
        suite = group.get("suite")
        occurrences = group.get("occurrences")
        first_line = group.get("first_line")
        last_line = group.get("last_line")
        line_desc = f"lines {first_line}-{last_line}" if first_line is not None else "n/a"
        notice.evidence.append(
            EvidenceItem(
                id=evidence_id,
                type="log",
                summary=f"integration {kind} failure ({suite})",
                detail=f"suite={suite}, occurrences={occurrences}, {line_desc}",
                source="integration_classifier",
            )
        )
    return [evidence_id]


def _aggregate_top_level_owner(notice: CiResponsibilityNotice) -> None:
    """按责任项聚合构建级 owner；schema 只降级不提升。"""
    current_items = [
        item
        for item in notice.responsibilityItems
        if item.responsibilityType == "current_build_owner"
        and item.owner.type in {"high_confidence", "medium_confidence"}
        and item.owner.name
        and item.owner.name != NO_OWNER_NAME
    ]
    identity_keys = {
        (item.owner.name, item.owner.email, item.owner.commit, item.responsibilityType)
        for item in current_items
    }
    if len(identity_keys) != 1:
        notice.owner = _no_owner_owner()
        notice.hasHighConfidenceOwner = False
        return
    selected = next(
        (item for item in current_items if item.owner.type == "high_confidence"),
        current_items[0],
    )
    notice.owner = Owner.model_validate(selected.owner.model_dump())
    notice.hasHighConfidenceOwner = selected.owner.type == "high_confidence"
