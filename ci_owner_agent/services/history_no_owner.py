"""Conservative inheritance of historical no-owner conclusions.

This module contains the domain rules that decide whether every current
failure can safely inherit an earlier, evidence-backed no-owner conclusion.
It intentionally has no database or Agent dependency: callers supply the
history precheck payload and retain responsibility for workflow orchestration.
"""
from __future__ import annotations

import json
import re

from ci_owner_agent.schemas import BuildInfo, ChangedFile, CiResponsibilityNotice, EvidenceItem
from ci_owner_agent.services.failure_identity import canonicalize_failure_signature
from ci_owner_agent.services.history_inheritance import build_no_owner_item_from_decision
from ci_owner_agent.services.responsibility_path_enricher import normalize_repository_path
from ci_owner_agent.services.scorer import no_owner, validate_notice


HISTORY_NO_OWNER_MATCH_TYPES = {"signature_exact", "signature_structural", "ai_fact_semantic"}


def select_build_level_no_owner_decision(
    history_precheck: dict | None,
    ai_history_precheck: dict | None,
) -> dict | None:
    history_items = (history_precheck or {}).get("currentChunks") or []
    if history_items:
        return _select_all_failures_no_owner_decision(history_items, source="historyPrecheck")
    ai_items = (ai_history_precheck or {}).get("currentFacts") or []
    if ai_items:
        return _select_all_failures_no_owner_decision(ai_items, source="aiHistoryPrecheck")
    return None


def validate_trusted_history_no_owner_decisions(
    decision: dict | None,
    *,
    current_build_number: int,
) -> dict | None:
    """Normalize a decision only when every historical source is trustworthy."""
    if not isinstance(decision, dict):
        return None
    raw_items = decision.get("decisions") if isinstance(decision.get("decisions"), list) else [decision]
    trusted_items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return None
        source_build = raw.get("sourceBuildNumber")
        failure_signature = canonicalize_failure_signature(raw.get("failureSignature"))
        match_type = str(raw.get("matchType") or "").strip()
        relationship = str(raw.get("relationship") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if not (
            type(source_build) is int
            and 0 < source_build < current_build_number
            and failure_signature
            and match_type in HISTORY_NO_OWNER_MATCH_TYPES
            and relationship
            and reason
        ):
            return None
        trusted_items.append(
            {
                **raw,
                "failureSignature": failure_signature,
                "sourceBuildNumber": source_build,
                "sourceBuildUrl": str(raw.get("sourceBuildUrl") or "").strip() or None,
                "matchType": match_type,
                "relationship": relationship,
                "reason": reason,
            }
        )
    if not trusted_items:
        return None
    return {
        **decision,
        **trusted_items[0],
        "decisions": trusted_items,
        "allFailuresNoOwnerDecision": len(trusted_items) > 1 or bool(decision.get("allFailuresNoOwnerDecision")),
        "coveredFailureCount": len(trusted_items),
    }


def has_new_strong_evidence(
    *,
    decision: dict,
    failure_summaries: dict | None,
    failure_facts: dict | None,
    changed_files: list[ChangedFile],
) -> bool:
    """Fail closed when current failure evidence cannot be uniquely aligned."""
    current_items = _current_failure_evidence_items(failure_summaries, failure_facts)
    item_decisions = decision.get("decisions") if isinstance(decision.get("decisions"), list) else [decision]
    aligned = _align_current_failures_to_decisions(current_items, item_decisions)
    if aligned is None:
        return True
    changed_paths = {
        normalized
        for item in changed_files
        if (normalized := normalize_repository_path(item.path))
    }
    for current, historical in aligned:
        if current["paths"] & changed_paths:
            return True
        current_markers = _strong_failure_markers(current["payload"])
        historical_markers = _strong_failure_markers(historical, historical=True)
        if not current_markers.issubset(historical_markers):
            return True
    return False


def build_no_owner_notice_from_history_decision(
    *,
    build_info: BuildInfo,
    base_commit: str,
    head_commit: str,
    decision: dict,
    failure_summaries: dict | None,
    failure_facts: dict | None,
) -> CiResponsibilityNotice:
    """Build a strict notice for already-validated historical decisions."""
    trusted = validate_trusted_history_no_owner_decisions(
        decision,
        current_build_number=build_info.buildNumber,
    )
    if trusted is None:
        raise ValueError("invalid historical no-owner decision")
    decision = trusted
    source_build = decision.get("sourceBuildNumber")
    if decision.get("allFailuresNoOwnerDecision"):
        covered = decision.get("coveredFailureCount") or 1
        reason = (
            f"所有当前失败项（共 {covered} 个）均与历史构建 #{source_build} 等已分析为无高可信责任人的同类失败一致，"
            "且当前构建没有新的强证据改变结论，因此直接继承历史 no-owner 判定。"
        )
    else:
        reason = (
            f"该失败与历史构建 #{source_build} 已分析为无高可信责任人的失败一致，"
            "当前构建没有新的强证据改变结论，因此直接继承历史 no-owner 判定。"
        )
    item_decisions = decision.get("decisions") if isinstance(decision.get("decisions"), list) else []
    if not item_decisions:
        item_decisions = [
            {
                **decision,
                "failureTitle": decision.get("failureTitle") or _failure_title(failure_summaries, failure_facts),
                "failureSignature": (
                    _current_failure_signature(failure_summaries, failure_facts)
                    or decision.get("failureSignature")
                    or _decision_signature(decision)
                ),
                "failureSummary": decision.get("failureSummary") or _failure_title(failure_summaries, failure_facts),
            }
        ]
    summary_evidence = EvidenceItem(
        id="E_HISTORY_NO_OWNER_SUMMARY",
        type="reasoning",
        summary=f"当前共 {len(item_decisions)} 个失败均命中历史 no-owner",
        detail="failures=" + json.dumps(
            [
                {
                    "failureSignature": item.get("failureSignature"),
                    "sourceBuildNumber": item.get("sourceBuildNumber"),
                }
                for item in item_decisions
            ],
            ensure_ascii=False,
        ),
        source="history_no_owner_decision",
    )
    item_evidence = [
        EvidenceItem(
            id=f"E_HISTORY_NO_OWNER_{index}",
            type="reasoning",
            summary="当前失败与历史无责任人失败一致",
            detail="; ".join(
                part
                for part in (
                    f"failureSignature={item['failureSignature']}",
                    f"sourceBuildNumber={item['sourceBuildNumber']}",
                    f"sourceBuildUrl={item['sourceBuildUrl']}" if item.get("sourceBuildUrl") else None,
                    f"matchType={item['matchType']}",
                    f"relationship={item['relationship']}",
                    f"feedbackAction={item['feedbackAction']}" if item.get("feedbackAction") else None,
                    f"reason={item['reason']}",
                    f"source={item['source']}" if item.get("source") else None,
                )
                if part
            ),
            source="history_no_owner_decision",
        )
        for index, item in enumerate(item_decisions, start=1)
    ]
    items = [
        build_no_owner_item_from_decision(
            item_decision,
            failure_title=str(item_decision.get("failureTitle") or "historical same failure"),
            failure_signature=item_decision.get("failureSignature"),
            failure_summary=item_decision.get("failureSummary"),
            evidence_id=item_evidence[index].id,
        )
        for index, item_decision in enumerate(item_decisions)
    ]
    notice = CiResponsibilityNotice(
        job=build_info.job,
        buildNumber=build_info.buildNumber,
        buildUrl=build_info.buildUrl,
        result=build_info.result,
        branch=build_info.branch,
        headCommit=head_commit,
        baseCommit=base_commit,
        owner=no_owner(),
        failureReason=reason,
        evidence=[summary_evidence, *item_evidence],
        suggestions=["如需强制重新分析，可设置 CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED=false。"],
        responsibilityItems=items,
        hasHighConfidenceOwner=False,
    )
    return apply_history_no_owner_sources(validate_notice(notice), item_decisions)


def apply_history_no_owner_sources(
    notice: CiResponsibilityNotice,
    trusted_decisions: list[dict],
) -> CiResponsibilityNotice:
    """Restore history source fields using only validated decisions."""
    decisions_by_signature = {
        str(item.get("failureSignature")): item
        for item in trusted_decisions
        if item.get("failureSignature")
    }
    for item in notice.responsibilityItems:
        trusted = decisions_by_signature.get(str(item.failureSignature))
        if not trusted:
            continue
        source_build = trusted.get("sourceBuildNumber")
        match_type = trusted.get("matchType")
        if not (
            isinstance(source_build, int)
            and 0 < source_build < notice.buildNumber
            and match_type in HISTORY_NO_OWNER_MATCH_TYPES
            and trusted.get("relationship")
        ):
            continue
        item.sourceBuildNumber = source_build
        item.sourceBuildUrl = trusted.get("sourceBuildUrl")
        item.matchType = match_type
        item.relationship = trusted.get("relationship")
        item.reason = trusted.get("reason") or item.reason
    return notice


def _select_all_failures_no_owner_decision(items: list, *, source: str) -> dict | None:
    decisions: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        inherited = item.get("inheritedOwner") if isinstance(item.get("inheritedOwner"), dict) else {}
        if inherited.get("found"):
            return None
        decision = item.get("noOwnerDecision") if isinstance(item.get("noOwnerDecision"), dict) else None
        if not decision or not decision.get("found"):
            return None
        decisions.append(
            {
                **decision,
                "source": source,
                "failureSignature": _current_item_signature(item, source),
                "failureTitle": _current_item_title(item, source),
                "failureSummary": _current_item_summary(item, source),
            }
        )
    if not decisions:
        return None
    ranked = sorted(
        decisions,
        key=lambda item: (
            1 if item.get("feedbackAction") in {"mark_flaky", "mark_no_owner"} else 0,
            -(item.get("sourceBuildNumber") or 0),
        ),
        reverse=True,
    )
    selected = dict(ranked[0])
    selected["allFailuresNoOwnerDecision"] = True
    selected["coveredFailureCount"] = len(decisions)
    selected["decisions"] = decisions
    selected["coveredSourceBuildNumbers"] = sorted(
        {number for number in (item.get("sourceBuildNumber") for item in decisions) if number is not None}
    )
    selected["coveredSignatures"] = [
        signature for signature in (_decision_signature(item) for item in decisions) if signature
    ]
    return selected


def _current_item_signature(item: dict, source: str) -> str | None:
    if source == "historyPrecheck":
        signature = item.get("signature") if isinstance(item.get("signature"), dict) else {}
        return signature.get("signatureKey") or signature.get("signatureHash") or item.get("normalizedHash")
    return item.get("signatureKey") or item.get("factId")


def _current_item_title(item: dict, source: str) -> str:
    if source == "historyPrecheck":
        signature = item.get("signature") if isinstance(item.get("signature"), dict) else {}
        return str(
            signature.get("testName")
            or signature.get("testCase")
            or signature.get("errorType")
            or _first_nonempty_line(item.get("preview"))
            or "historical same failure"
        )
    return " | ".join(
        str(value)
        for value in (item.get("failureKind"), item.get("errorCode"), item.get("symbol") or item.get("packageName"))
        if value
    ) or "historical same failure fact"


def _current_item_summary(item: dict, source: str) -> str | None:
    if source == "historyPrecheck":
        return str(item.get("preview") or "").strip() or None
    parts = [item.get("failureKind"), item.get("errorCode"), item.get("filePath"), item.get("symbol"), item.get("packageName")]
    text = " | ".join(str(value) for value in parts if value)
    return text or None


def _current_failure_evidence_items(
    failure_summaries: dict | None,
    failure_facts: dict | None,
) -> list[dict]:
    summaries = _failure_summary_signatures(failure_summaries)
    if summaries:
        return [
            {
                "identifiers": _failure_identifiers(summary),
                "payload": summary,
                "paths": _paths_from_payload(summary),
            }
            for summary in summaries
        ]
    facts = (failure_facts or {}).get("facts") if isinstance(failure_facts, dict) else []
    return [
        {
            "identifiers": _failure_identifiers(fact),
            "payload": fact,
            "paths": _paths_from_payload(fact),
        }
        for fact in facts or []
        if isinstance(fact, dict)
    ]


def _align_current_failures_to_decisions(
    current_items: list[dict],
    decisions: list[dict],
) -> list[tuple[dict, dict]] | None:
    if not current_items or len(current_items) != len(decisions):
        return None
    remaining = list(decisions)
    aligned: list[tuple[dict, dict]] = []
    for current in current_items:
        identifiers = current.get("identifiers") or set()
        matches = [item for item in remaining if identifiers & _failure_identifiers(item)]
        if len(matches) != 1:
            return None
        matched = matches[0]
        remaining.remove(matched)
        aligned.append((current, matched))
    return aligned if not remaining else None


def _failure_identifiers(value: dict) -> set[str]:
    signature = value.get("signature") if isinstance(value.get("signature"), dict) else {}
    candidates = (
        value.get("failureSignature"),
        value.get("signatureKey"),
        signature.get("signatureKey"),
        signature.get("signatureHash"),
        value.get("signatureHash"),
        value.get("normalizedHash"),
        value.get("factId"),
    )
    return {str(item).strip() for item in candidates if str(item or "").strip()}


def _paths_from_payload(value: dict) -> set[str]:
    signature = value.get("signature") if isinstance(value.get("signature"), dict) else {}
    result: set[str] = set()
    for raw_path in (
        value.get("testFile"),
        value.get("topStackFile"),
        value.get("filePath"),
        signature.get("testFile"),
        signature.get("topStackFile"),
        signature.get("filePath"),
    ):
        normalized = normalize_repository_path(raw_path)
        if normalized:
            result.add(normalized)
    return result


def _strong_failure_markers(value: dict, *, historical: bool = False) -> set[str]:
    signature = value.get("signature") if isinstance(value.get("signature"), dict) else {}
    explicit_values = (
        signature.get("errorCode"),
        signature.get("errorType"),
        value.get("errorCode"),
        value.get("errorType"),
        signature.get("failureKind"),
        value.get("failureKind"),
    )
    text = " ".join(
        str(part or "")
        for part in (
            None if historical else value.get("signatureKey"),
            None if historical else value.get("signatureHash"),
            None if historical else value.get("errorMessage"),
            signature.get("signatureKey"),
            signature.get("signatureHash"),
            signature.get("errorMessage"),
        )
    )
    markers = re.findall(
        r"\b(?:TS\d{4}|AssertionError|ReferenceError|TypeScriptCompileError|Timeout|MODULE_NOT_FOUND|ERR_[A-Z0-9_]+)\b",
        text,
        flags=re.IGNORECASE,
    )
    return {
        *(marker.lower() for marker in markers),
        *(str(marker).strip().lower() for marker in explicit_values if str(marker or "").strip()),
    }


def _current_failure_signature(
    failure_summaries: dict | None,
    failure_facts: dict | None,
) -> str | None:
    summary = _first_failure_summary_signature(failure_summaries)
    if summary:
        return summary.get("signatureKey") or summary.get("signatureHash")
    facts = (failure_facts or {}).get("facts") if isinstance(failure_facts, dict) else []
    for fact in facts or []:
        if isinstance(fact, dict) and fact.get("signatureKey"):
            return fact.get("signatureKey")
    return None


def _decision_signature(decision: dict) -> str | None:
    signature = decision.get("signature") if isinstance(decision.get("signature"), dict) else {}
    return signature.get("signatureKey") or decision.get("signatureHash")


def _failure_summary_signatures(failure_summaries: dict | None) -> list[dict]:
    chunks = (failure_summaries or {}).get("chunks") if isinstance(failure_summaries, dict) else []
    result: list[dict] = []
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        signature = chunk.get("signature") if isinstance(chunk.get("signature"), dict) else {}
        if signature:
            result.append({**signature, "signatureHash": chunk.get("signatureHash")})
    return result


def _first_failure_summary_signature(failure_summaries: dict | None) -> dict | None:
    summaries = _failure_summary_signatures(failure_summaries)
    return summaries[0] if summaries else None


def _failure_title(failure_summaries: dict | None, failure_facts: dict | None) -> str:
    summary = _first_failure_summary_signature(failure_summaries)
    if summary:
        return str(summary.get("testName") or summary.get("testCase") or summary.get("errorType") or "historical same failure")
    facts = (failure_facts or {}).get("facts") if isinstance(failure_facts, dict) else []
    for fact in facts or []:
        if isinstance(fact, dict):
            return str(fact.get("rootCauseSummary") or fact.get("message") or fact.get("failureKind") or "historical same failure")
    return "historical same failure"


def _first_nonempty_line(value: object) -> str | None:
    for line in str(value or "").splitlines():
        if line.strip():
            return line.strip()
    return None
