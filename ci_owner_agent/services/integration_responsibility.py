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
from ci_owner_agent.schemas import CiResponsibilityNotice, Owner, ResponsibilityItem


def _env_signature(kind: str, detail: str) -> str:
    """环境/未知失败事实的稳定签名。"""
    return f"integration_{kind}|{detail or 'unresolved'}"


def _is_integration_env_item(item: ResponsibilityItem) -> bool:
    signature = item.failureSignature or ""
    return (
        signature.startswith("integration_connection|")
        or signature.startswith("integration_env|")
        or signature.startswith("integration_unknown|")
        or signature.startswith("integration_pipeline|")
    )


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
    1. 从 failure_summaries 的 integrationClassifications + integrationConflicts 提取
       环境/未知事实，逐个生成 no-owner item；
    2. 移除 Agent 意外生成的所有集成环境项（按稳定签名识别）；
    3. 追加 canonical no-owner 项，并聚合顶层 owner。
    """
    if not isinstance(failure_summaries, dict):
        return notice
    classifications = failure_summaries.get("integrationClassifications")
    conflicts = failure_summaries.get("integrationConflicts")
    if not isinstance(classifications, list) and not isinstance(conflicts, list):
        return notice
    classifications = classifications if isinstance(classifications, list) else []
    conflicts = conflicts if isinstance(conflicts, list) else []

    # 1. 移除 Agent 意外生成的集成环境项（guard 是唯一生产者）
    notice.responsibilityItems = [
        item for item in notice.responsibilityItems if not _is_integration_env_item(item)
    ]

    # 2. 环境/未知失败事实 → canonical no-owner item
    seen: set[str] = set()
    for classification in classifications:
        kind = classification.get("kind")
        if kind not in {"connection", "unknown"}:
            continue
        suite = classification.get("suite")
        line = classification.get("line")
        detail = f"{suite or 'unresolved'}@L{line}"
        signature = _env_signature(kind, detail)
        if signature in seen:
            continue
        seen.add(signature)
        if kind == "connection":
            title = f"集成测试外部库/数据库不可达（{suite or 'unresolved'}）"
            reason = "外部库/数据库连接失败（ECONNREFUSED/超时等），属环境/基础设施问题，无代码责任可归。"
        else:
            title = f"集成测试失败分类不明确（{suite or 'unresolved'}）"
            reason = "失败证据不足或冲突，无法确定代码责任，保守判为无高可信责任人。"
        evidence_ids = _append_env_evidence(notice, signature, classification, kind)
        notice.responsibilityItems.append(
            _env_item(signature=signature, title=title, reason=reason, evidence_ids=evidence_ids)
        )

    # 3. 协议冲突 → 一个总体的 no-owner 事实（不针对具体 suite）
    if conflicts:
        signature = "integration_unknown|protocol_conflict"
        evidence_id = f"integration-conflict-evidence"
        if evidence_id not in {e.id for e in notice.evidence}:
            from ci_owner_agent.schemas import EvidenceItem

            notice.evidence.append(
                EvidenceItem(
                    id=evidence_id,
                    type="reasoning",
                    summary="集成测试协议 marker 冲突/不完整",
                    detail="; ".join(conflicts)[:2000],
                    source="integration_protocol_index",
                )
            )
        if signature not in seen:
            notice.responsibilityItems.append(
                _env_item(
                    signature=signature,
                    title="集成测试协议 marker 冲突/不完整",
                    reason="协议 marker 缺失/重复/乱序/计数不自洽，证据不可信，保守判为无高可信责任人。",
                    evidence_ids=[evidence_id],
                )
            )

    _aggregate_top_level_owner(notice)
    return notice


def _append_env_evidence(
    notice: CiResponsibilityNotice,
    signature: str,
    classification: dict[str, Any],
    kind: str,
) -> list[str]:
    from ci_owner_agent.schemas import EvidenceItem

    suffix = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    evidence_id = f"integration-env-{suffix}"
    if evidence_id not in {e.id for e in notice.evidence}:
        notice.evidence.append(
            EvidenceItem(
                id=evidence_id,
                type="log",
                summary=f"integration {kind} failure",
                detail=f"line={classification.get('line')}, suite={classification.get('suite')}",
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
