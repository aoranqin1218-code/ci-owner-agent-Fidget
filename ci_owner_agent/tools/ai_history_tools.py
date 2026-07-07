from __future__ import annotations

from typing import Any

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import FailureFact, FailureFactComparison
from ci_owner_agent.services.failure_fact_compare_ai import compare_failure_facts_with_ai
from ci_owner_agent.services.history_store import (
    MongoHistoryStore,
    find_feedback_override_for_failure_signature,
    get_history_store,
)

OWNER_TYPES = {"high_confidence", "medium_confidence", "inherited_failure_owner"}


def history_search_similar_failure_facts(
    context: AgentRuntimeContext,
    maxCandidates: int | None = None,
    store: MongoHistoryStore | None = None,
) -> dict:
    if not context.settings.history_enabled:
        return {
            "ok": False,
            "historyEnabled": False,
            "mode": "ai_failure_facts",
            "candidates": [],
            "currentFacts": [],
            "diagnostics": _new_diagnostics(),
            "warning": "history disabled",
        }
    if not context.settings.ai_history_compare_enabled:
        return {
            "ok": False,
            "historyEnabled": True,
            "mode": "ai_failure_facts",
            "candidates": [],
            "currentFacts": [],
            "diagnostics": _new_diagnostics(),
            "warning": "AI history compare disabled",
        }

    history_store = store or get_history_store(context.settings)
    if history_store is None:
        return {
            "ok": False,
            "historyEnabled": True,
            "mode": "ai_failure_facts",
            "candidates": [],
            "currentFacts": [],
            "diagnostics": _new_diagnostics(),
            "warning": "history store disabled or unavailable",
        }

    diagnostics = _new_diagnostics()
    current_facts = _current_facts(context.failure_facts)
    if not current_facts:
        return _base_result(context, current_facts=[], candidates=[], diagnostics=diagnostics, warning="no current failure facts")

    current_views = [_current_fact_view(fact, context.settings.ai_failure_fact_min_confidence) for fact in current_facts]
    eligible_current = [item for item in current_views if item["_eligible"]]
    diagnostics["eligibleCurrentFactsCount"] = len(eligible_current)
    diagnostics["skipped"]["currentFactIneligible"] = len(current_views) - len(eligible_current)
    if not eligible_current:
        return _base_result(context, current_facts=_public_current_views(current_views), candidates=[], diagnostics=diagnostics)

    historical_docs, store_diagnostics = _find_historical_facts_with_diagnostics(history_store, context)
    diagnostics.update(store_diagnostics)
    if not historical_docs:
        return _base_result(context, current_facts=_public_current_views(current_views), candidates=[], diagnostics=diagnostics)

    pairs = _ranked_pairs(eligible_current, historical_docs, diagnostics)
    diagnostics["rankedPairsCount"] = len(pairs)
    compare_limit = max(1, context.settings.ai_history_max_compare_calls)
    candidate_limit = max(1, min(maxCandidates or context.settings.history_max_candidates, context.settings.ai_history_max_fact_candidates, 50))
    candidates: list[dict] = []
    warning: str | None = None

    for current_view, historical_doc, historical_fact in pairs[:compare_limit]:
        try:
            comparison = compare_failure_facts_with_ai(
                settings=context.settings,
                current_fact=current_view["_fact"],
                historical_fact=historical_fact,
            )
            diagnostics["comparedPairsCount"] += 1
        except Exception as exc:
            diagnostics["comparedPairsCount"] += 1
            diagnostics["skipped"]["compareError"] += 1
            warning = f"AI failure fact compare error: {exc}"
            _append_compare_result(diagnostics, current_view["_fact"], historical_fact, historical_doc, None, False, "compare_error", str(exc))
            continue

        if not _is_same_failure(comparison, context.settings.ai_history_compare_threshold):
            skip_reason = _comparison_skip_reason(comparison, context.settings.ai_history_compare_threshold)
            if skip_reason == "compare_below_threshold":
                diagnostics["skipped"]["compareBelowThreshold"] += 1
            else:
                diagnostics["skipped"]["compareNotSameFailure"] += 1
            _append_compare_result(diagnostics, current_view["_fact"], historical_fact, historical_doc, comparison, False, skip_reason, comparison.reason)
            continue

        feedback = find_feedback_override_for_failure_signature(
            history_store,
            job=context.job,
            branch=context.branch,
            build_number=historical_doc.get("buildNumber"),
            failure_signature=historical_fact.signatureKey,
            notice_doc={"notice": historical_doc.get("notice") or {}},
        )
        if _feedback_blocks(feedback):
            diagnostics["skipped"]["blockedByFeedback"] += 1
            current_view["blockedReason"] = f"blocked_by_feedback_{feedback.get('action')}"
            _append_compare_result(diagnostics, current_view["_fact"], historical_fact, historical_doc, comparison, False, f"blocked_by_feedback_{feedback.get('action')}", comparison.reason)
            continue

        inherited_owner = _inherited_owner_from_historical_doc(
            historical_doc,
            comparison,
            feedback,
        )
        if not inherited_owner.get("found"):
            diagnostics["skipped"]["invalidOwner"] += 1
            _append_compare_result(diagnostics, current_view["_fact"], historical_fact, historical_doc, comparison, False, "invalid_owner", comparison.reason)
            continue

        candidate = _candidate_dict(current_view["_fact"], historical_fact, historical_doc, comparison, inherited_owner, feedback)
        candidates.append(candidate)
        current_view.setdefault("_candidateOwners", []).append(inherited_owner)
        _append_compare_result(diagnostics, current_view["_fact"], historical_fact, historical_doc, comparison, True, None, comparison.reason)

    for current_view in current_views:
        owners = current_view.pop("_candidateOwners", [])
        if owners:
            current_view.pop("blockedReason", None)
            current_view["inheritedOwner"] = _best_inherited_owner(owners)

    candidates.sort(
        key=lambda item: (
            1 if (item.get("feedbackOverride") or {}).get("action") in {"correct_owner", "confirm_owner"} else 0,
            item.get("confidence") or 0,
            -(item.get("inheritedOwner") or {}).get("sourceBuildNumber", item.get("buildNumber") or 0),
        ),
        reverse=True,
    )
    diagnostics["acceptedCandidatesCount"] = len(candidates)
    return _base_result(
        context,
        current_facts=_public_current_views(current_views),
        candidates=candidates[:candidate_limit],
        diagnostics=diagnostics,
        warning=warning,
    )


def _base_result(
    context: AgentRuntimeContext,
    *,
    current_facts: list[dict],
    candidates: list[dict],
    diagnostics: dict,
    warning: str | None = None,
) -> dict:
    return {
        "ok": True,
        "historyEnabled": True,
        "mode": "ai_failure_facts",
        "currentBuild": context.build_number,
        "lastSuccessfulBuildNumber": context.last_successful_build_number,
        "threshold": context.settings.ai_history_compare_threshold,
        "currentFacts": current_facts,
        "candidates": candidates,
        "diagnostics": diagnostics,
        "warning": warning,
    }


def _new_diagnostics() -> dict:
    return {
        "eligibleCurrentFactsCount": 0,
        "historicalBuildsCount": None,
        "historicalFactsCount": 0,
        "rankedPairsCount": 0,
        "comparedPairsCount": 0,
        "acceptedCandidatesCount": 0,
        "skipped": {
            "currentFactIneligible": 0,
            "invalidHistoricalFact": 0,
            "compareNotSameFailure": 0,
            "compareBelowThreshold": 0,
            "blockedByFeedback": 0,
            "invalidOwner": 0,
            "compareError": 0,
        },
        "compareResults": [],
    }


def _find_historical_facts_with_diagnostics(history_store: MongoHistoryStore, context: AgentRuntimeContext) -> tuple[list[dict], dict]:
    kwargs = {
        "job": context.job,
        "branch": context.branch,
        "current_build_number": context.build_number,
        "last_successful_build_number": context.last_successful_build_number,
        "lookback_builds": context.settings.ai_history_max_fact_candidates,
        "max_facts": context.settings.ai_history_max_fact_candidates,
    }
    if hasattr(history_store, "find_historical_failure_facts_with_diagnostics"):
        result = history_store.find_historical_failure_facts_with_diagnostics(**kwargs)
        return list(result.get("facts") or []), _compact_store_diagnostics(result.get("diagnostics") or {})
    facts = history_store.find_historical_failure_facts(**kwargs)
    return list(facts), {"historicalFactsCount": len(facts)}


def _compact_store_diagnostics(value: dict) -> dict:
    return {
        "historicalBuildsCount": value.get("historicalBuildsCount"),
        "historicalBuildNumbers": value.get("historicalBuildNumbers"),
        "historicalFactsCount": value.get("historicalFactsCount", 0),
        "historicalFactBuildNumbers": value.get("historicalFactBuildNumbers"),
        "queryStage": value.get("queryStage"),
    }


def _current_facts(facts_result: dict[str, Any] | None) -> list[FailureFact]:
    raw_facts = (facts_result or {}).get("facts") or []
    facts: list[FailureFact] = []
    for item in raw_facts:
        try:
            facts.append(item if isinstance(item, FailureFact) else FailureFact.model_validate(item))
        except Exception:
            continue
    return facts


def _current_fact_view(fact: FailureFact, min_confidence: float) -> dict:
    blocked_reason = _blocked_reason(fact, min_confidence)
    return {
        "factId": fact.factId,
        "signatureKey": fact.signatureKey,
        "failureKind": fact.failureKind,
        "errorCode": fact.errorCode,
        "packageName": fact.packageName,
        "filePath": fact.filePath,
        "symbol": fact.symbol,
        "confidence": fact.confidence,
        "inheritedOwner": {"found": False},
        **({"blockedReason": blocked_reason} if blocked_reason else {}),
        "_eligible": blocked_reason is None,
        "_fact": fact,
    }


def _public_current_views(items: list[dict]) -> list[dict]:
    public: list[dict] = []
    for item in items:
        clean = dict(item)
        clean.pop("_fact", None)
        clean.pop("_eligible", None)
        clean.pop("_candidateOwners", None)
        public.append(clean)
    return public


def _blocked_reason(fact: FailureFact, min_confidence: float) -> str | None:
    if not fact.historyEligible or fact.isGenericWrapper:
        return "blocked_by_generic_wrapper"
    if fact.confidence < min_confidence:
        return "blocked_by_low_confidence"
    return None


def _ranked_pairs(current_views: list[dict], historical_docs: list[dict], diagnostics: dict) -> list[tuple[dict, dict, FailureFact]]:
    pairs: list[tuple[int, dict, dict, FailureFact]] = []
    for current in current_views:
        current_fact = current["_fact"]
        for historical_doc in historical_docs:
            try:
                historical_fact = FailureFact.model_validate(historical_doc.get("fact") or {})
            except Exception:
                diagnostics["skipped"]["invalidHistoricalFact"] += 1
                continue
            score = _pair_score(current_fact, historical_fact)
            build_number = int(historical_doc.get("buildNumber") or 0)
            pairs.append((score * 100000 + build_number, current, historical_doc, historical_fact))
    pairs.sort(key=lambda item: item[0], reverse=True)
    return [(current, doc, fact) for _, current, doc, fact in pairs]


def _comparison_skip_reason(comparison: FailureFactComparison, threshold: float) -> str:
    if comparison.sameFailure and comparison.relationship == "same_root_cause" and comparison.confidence < threshold:
        return "compare_below_threshold"
    return "compare_not_same_failure"


def _append_compare_result(
    diagnostics: dict,
    current_fact: FailureFact,
    historical_fact: FailureFact,
    historical_doc: dict,
    comparison: FailureFactComparison | None,
    accepted: bool,
    skip_reason: str | None,
    reason: str,
) -> None:
    if len(diagnostics["compareResults"]) >= 5:
        return
    diagnostics["compareResults"].append(
        {
            "currentFactId": current_fact.factId,
            "currentSignatureKey": current_fact.signatureKey,
            "historicalFactId": historical_fact.factId,
            "historicalSignatureKey": historical_fact.signatureKey,
            "historicalBuildNumber": historical_doc.get("buildNumber"),
            "sameFailure": comparison.sameFailure if comparison else False,
            "confidence": comparison.confidence if comparison else 0,
            "relationship": comparison.relationship if comparison else "unclear",
            "accepted": accepted,
            "skipReason": skip_reason,
            "reason": reason,
        }
    )


def _pair_score(current: FailureFact, historical: FailureFact) -> int:
    score = 0
    if current.signatureKey == historical.signatureKey:
        score += 100
    if current.failureKind == historical.failureKind:
        score += 30
    if current.errorCode and current.errorCode == historical.errorCode:
        score += 20
    if current.packageName and current.packageName == historical.packageName:
        score += 15
    if current.filePath and current.filePath == historical.filePath:
        score += 15
    if current.symbol and current.symbol == historical.symbol:
        score += 15
    return score


def _is_same_failure(comparison: FailureFactComparison, threshold: float) -> bool:
    return (
        comparison.sameFailure is True
        and comparison.relationship == "same_root_cause"
        and comparison.confidence >= threshold
    )


def _feedback_blocks(feedback: dict | None) -> bool:
    return isinstance(feedback, dict) and feedback.get("action") in {"mark_flaky", "mark_no_owner"}


def _inherited_owner_from_historical_doc(
    historical_doc: dict,
    comparison: FailureFactComparison,
    feedback: dict | None,
) -> dict:
    feedback_corrected = False
    feedback_verified = False
    source_build_number = historical_doc.get("buildNumber")
    source_build_url = historical_doc.get("buildUrl")
    owner: dict[str, Any] | None = None

    if isinstance(feedback, dict):
        action = feedback.get("action")
        if action == "correct_owner" and isinstance(feedback.get("correctedOwner"), dict):
            owner = feedback["correctedOwner"]
            source_build_number = feedback.get("sourceBuildNumber") or source_build_number
            source_build_url = feedback.get("buildUrl") or source_build_url
            feedback_corrected = True
        elif action == "confirm_owner":
            feedback_verified = True

    if owner is None:
        owner = historical_doc.get("factOwner") if isinstance(historical_doc.get("factOwner"), dict) else None
    if owner is None:
        owner = {
            "type": historical_doc.get("ownerType"),
            "name": historical_doc.get("ownerName"),
            "email": historical_doc.get("ownerEmail"),
            "commit": historical_doc.get("ownerCommit"),
            "confidence": 1 if historical_doc.get("ownerType") == "high_confidence" else 0,
        }

    item_source = _matching_notice_item_source(historical_doc)
    if item_source:
        source_build_number = item_source.get("sourceBuildNumber") or source_build_number
        source_build_url = item_source.get("sourceBuildUrl") or source_build_url

    if not _valid_owner(owner):
        return {"found": False}

    return {
        "found": True,
        "sourceBuildNumber": source_build_number,
        "sourceBuildUrl": source_build_url,
        "ownerType": owner.get("type"),
        "ownerName": owner.get("name"),
        "ownerEmail": owner.get("email"),
        "ownerCommit": owner.get("commit"),
        "confidence": comparison.confidence,
        "matchType": "ai_fact_semantic",
        "relationship": comparison.relationship,
        "feedbackVerified": feedback_verified,
        "feedbackCorrected": feedback_corrected,
    }


def _valid_owner(owner: dict | None) -> bool:
    if not isinstance(owner, dict):
        return False
    name = str(owner.get("name") or "").strip()
    owner_type = str(owner.get("type") or "")
    confidence = float(owner.get("confidence") or 0)
    return bool(name and name != NO_OWNER_NAME and owner_type in OWNER_TYPES and confidence > 0)


def _matching_notice_item_source(historical_doc: dict) -> dict | None:
    notice = historical_doc.get("notice")
    if not isinstance(notice, dict):
        return None
    signature_key = historical_doc.get("signatureKey")
    for item in notice.get("responsibilityItems") or []:
        if isinstance(item, dict) and item.get("failureSignature") == signature_key:
            return item
    return None


def _best_inherited_owner(owners: list[dict]) -> dict:
    owners.sort(
        key=lambda item: (
            1 if item.get("feedbackCorrected") or item.get("feedbackVerified") else 0,
            item.get("confidence") or 0,
            -(item.get("sourceBuildNumber") or 0),
        ),
        reverse=True,
    )
    return owners[0]


def _candidate_dict(
    current_fact: FailureFact,
    historical_fact: FailureFact,
    historical_doc: dict,
    comparison: FailureFactComparison,
    inherited_owner: dict,
    feedback: dict | None,
) -> dict:
    return {
        "currentFactId": current_fact.factId,
        "historicalFactId": historical_fact.factId,
        "currentSignatureKey": current_fact.signatureKey,
        "historicalSignatureKey": historical_fact.signatureKey,
        "buildNumber": historical_doc.get("buildNumber"),
        "buildUrl": historical_doc.get("buildUrl"),
        "headCommit": historical_doc.get("headCommit"),
        "sameFailure": comparison.sameFailure,
        "confidence": comparison.confidence,
        "relationship": comparison.relationship,
        "matchType": "ai_fact_semantic",
        "samePoints": comparison.samePoints,
        "differentPoints": comparison.differentPoints,
        "reason": comparison.reason,
        "ownerName": inherited_owner.get("ownerName"),
        "ownerEmail": inherited_owner.get("ownerEmail"),
        "ownerCommit": inherited_owner.get("ownerCommit"),
        "inheritedOwner": inherited_owner,
        "feedbackOverride": _feedback_preview(feedback),
    }


def _feedback_preview(feedback: dict | None) -> dict | None:
    if not isinstance(feedback, dict):
        return None
    return {
        "action": feedback.get("action"),
        "reviewer": feedback.get("reviewer"),
        "note": feedback.get("note"),
        "correctedOwner": feedback.get("correctedOwner"),
    }
