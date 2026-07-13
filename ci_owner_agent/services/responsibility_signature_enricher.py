from __future__ import annotations

from typing import Any

from ci_owner_agent.schemas import CiResponsibilityNotice, stable_failure_id
from ci_owner_agent.services.failure_identity import (
    build_failure_fact_signature,
    build_failure_summary_signature,
    build_responsibility_signature,
    canonicalize_failure_message,
    canonicalize_failure_signature,
    sanitize_failure_message,
)
from ci_owner_agent.services.responsibility_path_enricher import normalize_repository_path


def enrich_responsibility_item_signatures(
    notice: CiResponsibilityNotice,
    failure_summaries: dict | None,
    failure_facts: dict | None,
) -> CiResponsibilityNotice:
    candidates = _identity_candidates(failure_summaries, failure_facts)
    for item in notice.responsibilityItems:
        own_signature = build_responsibility_signature(
            failure_title=item.failureTitle,
            failure_summary=item.failureSummary,
            existing_signature=item.failureSignature,
            test_file_path=item.testFilePath,
            failure_file_path=item.failureFilePath,
        )
        matched = _unique_candidate(item, own_signature, candidates)
        item.failureSignature = matched or own_signature
        item.failureId = stable_failure_id(item)
        item.failureTitle = sanitize_failure_message(item.failureTitle)
        item.failureSummary = sanitize_failure_message(item.failureSummary) or None
        item.reason = sanitize_failure_message(item.reason)
    notice.failureReason = sanitize_failure_message(notice.failureReason)
    for evidence in notice.evidence:
        evidence.summary = sanitize_failure_message(evidence.summary)
        evidence.detail = sanitize_failure_message(evidence.detail)
    return notice


def _identity_candidates(failure_summaries: dict | None, failure_facts: dict | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    chunks = failure_summaries.get("chunks", []) if isinstance(failure_summaries, dict) else []
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        signature = chunk.get("signature") if isinstance(chunk.get("signature"), dict) else {}
        signature_key = build_failure_summary_signature(signature)
        if signature_key:
            result.append(
                {
                    "signature": signature_key,
                    "paths": _paths(signature),
                    "terms": _terms(signature),
                }
            )
    facts = failure_facts.get("facts", []) if isinstance(failure_facts, dict) else []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        signature_key = build_failure_fact_signature(fact)
        if signature_key:
            result.append(
                {
                    "signature": signature_key,
                    "paths": _paths(fact),
                    "terms": _terms(fact),
                }
            )
    return result


def _unique_candidate(item: Any, own_signature: str, candidates: list[dict[str, Any]]) -> str | None:
    exact = {candidate["signature"] for candidate in candidates if candidate["signature"] == own_signature}
    if len(exact) == 1:
        return next(iter(exact))

    item_paths = {
        path
        for raw in (item.testFilePath, item.failureFilePath)
        if (path := normalize_repository_path(raw))
    }
    if item_paths:
        path_matches = {candidate["signature"] for candidate in candidates if item_paths & candidate["paths"]}
        if len(path_matches) == 1:
            return next(iter(path_matches))

    item_text = canonicalize_failure_signature(" ".join((item.failureTitle or "", item.failureSummary or "")))
    term_matches = {
        candidate["signature"]
        for candidate in candidates
        if candidate["terms"] and any(term in item_text for term in candidate["terms"])
    }
    return next(iter(term_matches)) if len(term_matches) == 1 else None


def _paths(value: dict) -> set[str]:
    result: set[str] = set()
    for raw in (value.get("testFile"), value.get("topStackFile"), value.get("filePath")):
        path = normalize_repository_path(raw)
        if path:
            result.add(path)
    return result


def _terms(value: dict) -> set[str]:
    return {
        canonicalize_failure_signature(str(raw))
        for raw in (
            value.get("errorCode"),
            value.get("errorType"),
            value.get("failureKind"),
            value.get("testName"),
            value.get("testCase"),
            value.get("symbol"),
            value.get("packageName"),
        )
        if canonicalize_failure_signature(str(raw or ""))
    }
