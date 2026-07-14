from __future__ import annotations

import datetime as dt
from typing import Any

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.services.failure_identity import canonicalize_failure_signature

INHERITABLE_OWNER_TYPES = {"high_confidence", "medium_confidence", "inherited_failure_owner"}
BLOCKING_FEEDBACK_ACTIONS = {"mark_flaky", "mark_no_owner"}


def active_feedback_docs(store: Any, repo: str, job: str, branch: str | None) -> list[dict]:
    return sort_feedback_docs(list(store.feedback.find({"repo": repo, "job": job, "branch": branch, "isActive": True})))


def sort_feedback_docs(docs: list[dict]) -> list[dict]:
    return sorted(
        docs,
        key=lambda doc: (_time_sort_value(doc.get("updatedAt")), _time_sort_value(doc.get("createdAt"))),
        reverse=True,
    )


def find_feedback_override_for_failure_signature(
    store: Any,
    *,
    repo: str,
    job: str,
    branch: str | None,
    build_number: int | None,
    failure_signature: str | None,
    notice_doc: dict | None,
) -> dict | None:
    failure_signature = canonicalize_failure_signature(failure_signature)
    if not failure_signature:
        return None
    feedback_docs = active_feedback_docs(store, repo, job, branch)
    return find_feedback_override(
        feedback_docs,
        job=job,
        branch=branch,
        build_number=build_number,
        signature_hash=None,
        signature={"signatureKey": failure_signature},
        notice_doc=notice_doc or {},
    )


def find_feedback_override(
    feedback_docs: list[dict],
    *,
    job: str,
    branch: str | None,
    build_number: int | None,
    signature_hash: str | None,
    signature: dict,
    notice_doc: dict,
) -> dict | None:
    possible_signatures = {
        canonical
        for value in (signature_hash, signature.get("signatureKey"), signature.get("signatureHash"))
        if (canonical := canonicalize_failure_signature(value))
    }
    notice = notice_doc.get("notice") if isinstance(notice_doc, dict) else None
    failure_ids: set[str] = set()
    for item in (notice.get("responsibilityItems") if isinstance(notice, dict) else []) or []:
        if item.get("failureSignature") in possible_signatures and item.get("failureId"):
            failure_ids.add(item.get("failureId"))
    for doc in feedback_docs:
        if doc.get("job") != job:
            continue
        if doc.get("branch") != branch:
            continue
        if doc.get("failureSignature") and doc.get("failureSignature") in possible_signatures:
            return doc
    for doc in feedback_docs:
        if doc.get("job") == job and doc.get("buildNumber") == build_number and doc.get("failureId") in failure_ids:
            return doc
    return None


def feedback_blocks_inheritance(feedback: dict | None) -> bool:
    return isinstance(feedback, dict) and feedback.get("action") in BLOCKING_FEEDBACK_ACTIONS


def feedback_preview(feedback: dict | None) -> dict | None:
    if not isinstance(feedback, dict):
        return None
    return {
        "action": feedback.get("action"),
        "reviewer": feedback.get("reviewer"),
        "note": feedback.get("note"),
        "correctedOwner": feedback.get("correctedOwner"),
    }


def valid_inherited_owner(owner: dict | None) -> bool:
    if not isinstance(owner, dict):
        return False
    name = str(owner.get("name") or "").strip()
    owner_type = str(owner.get("type") or "")
    confidence = float(owner.get("confidence") or 0)
    return bool(name and name != NO_OWNER_NAME and owner_type in INHERITABLE_OWNER_TYPES and confidence > 0)


def build_inherited_owner(
    source: dict,
    *,
    fallback_build_number: int | None,
    fallback_build_url: str | None,
    match_type: str,
    relationship: str,
) -> dict:
    return {
        "found": True,
        "sourceBuildNumber": source.get("sourceBuildNumber") or fallback_build_number,
        "sourceBuildUrl": source.get("sourceBuildUrl") or fallback_build_url,
        "ownerType": source.get("ownerType"),
        "ownerName": source.get("ownerName"),
        "ownerEmail": source.get("ownerEmail"),
        "ownerCommit": source.get("ownerCommit"),
        "confidence": source.get("confidence"),
        "matchType": match_type,
        "relationship": relationship,
        **({"feedbackVerified": True} if source.get("feedbackVerified") else {}),
        **({"feedbackOverride": True} if source.get("feedbackOverride") else {}),
        **({"feedbackCorrected": True} if source.get("feedbackCorrected") else {}),
    }


def source_from_correct_owner_feedback(
    feedback: dict | None,
    *,
    fallback_build_number: int | None,
    fallback_build_url: str | None,
) -> dict | None:
    if not isinstance(feedback, dict) or feedback.get("action") != "correct_owner":
        return None
    owner = feedback.get("correctedOwner")
    if not isinstance(owner, dict):
        return None
    return {
        "ownerType": owner.get("type"),
        "ownerName": owner.get("name"),
        "ownerEmail": owner.get("email"),
        "ownerCommit": owner.get("commit"),
        "confidence": owner.get("confidence"),
        "sourceBuildNumber": feedback.get("sourceBuildNumber") or fallback_build_number,
        "sourceBuildUrl": feedback.get("buildUrl") or fallback_build_url,
        "feedbackOverride": True,
        "feedbackCorrected": True,
    }


def is_no_owner_decision_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    responsibility_type = str(item.get("responsibilityType") or "")
    owner_type = str(owner.get("type") or "")
    return (
        responsibility_type == "no_high_confidence_owner"
        or owner_type == "no_high_confidence_owner"
        or (responsibility_type == "unknown" and owner_type == "no_high_confidence_owner")
    )


def find_no_owner_decision_from_notice(
    notice: dict | None,
    failure_signature: str | None = None,
    *,
    allow_legacy_top_owner: bool = False,
) -> dict | None:
    if not isinstance(notice, dict):
        return None
    items = notice.get("responsibilityItems")
    if isinstance(items, list) and items:
        if failure_signature:
            for item in items:
                if isinstance(item, dict) and item.get("failureSignature") == failure_signature and is_no_owner_decision_item(item):
                    return item
            return None
        for item in items:
            if isinstance(item, dict) and is_no_owner_decision_item(item):
                return item
        return None
    owner = notice.get("owner") if isinstance(notice.get("owner"), dict) else {}
    if allow_legacy_top_owner and owner.get("type") == "no_high_confidence_owner":
        return {
            "failureId": "legacy-top-owner",
            "failureTitle": "historical failure",
            "failureSignature": failure_signature,
            "owner": owner,
            "responsibilityType": "no_high_confidence_owner",
            "confidence": 0,
            "reason": notice.get("failureReason") or "historical notice top owner is no_high_confidence_owner",
        }
    return None


def build_no_owner_decision_payload(
    *,
    source_build_number: int | None,
    source_build_url: str | None,
    match_type: str | None,
    relationship: str | None,
    reason: str,
    feedback_action: str | None = None,
    signature: dict | None = None,
    signature_hash: str | None = None,
    failure_metadata: dict | None = None,
) -> dict:
    structured_signature = dict(signature or {})
    for key in ("signatureKey", "errorCode", "errorType", "failureKind", "filePath", "packageName", "symbol"):
        value = (failure_metadata or {}).get(key)
        if value is not None and key not in structured_signature:
            structured_signature[key] = value
    return {
        "found": True,
        "sourceBuildNumber": source_build_number,
        "sourceBuildUrl": source_build_url,
        "matchType": match_type,
        "relationship": relationship,
        "reason": reason,
        "feedbackAction": feedback_action,
        "signature": structured_signature,
        "signatureHash": signature_hash,
    }


def build_no_owner_item_from_decision(
    decision: dict,
    *,
    failure_title: str,
    failure_signature: str | None,
    failure_summary: str | None = None,
    evidence_id: str,
) -> dict:
    return {
        "failureId": "auto",
        "failureTitle": failure_title,
        "failureSignature": failure_signature,
        "failureSummary": failure_summary or failure_title,
        "owner": {
            "type": "no_high_confidence_owner",
            "name": NO_OWNER_NAME,
            "email": None,
            "commit": None,
            "confidence": 0,
        },
        "responsibilityType": "no_high_confidence_owner",
        "sourceBuildNumber": decision.get("sourceBuildNumber"),
        "sourceBuildUrl": decision.get("sourceBuildUrl"),
        "sourceCommit": None,
        "matchType": decision.get("matchType"),
        "relationship": decision.get("relationship"),
        "confidence": 0,
        "reason": decision.get("reason") or "历史同类失败已判定为无高可信责任人。",
        "evidenceIds": [evidence_id],
    }


def _time_sort_value(value: Any) -> float:
    if isinstance(value, dt.datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            text = value.replace("Z", "+00:00")
            return dt.datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0
    return 0
