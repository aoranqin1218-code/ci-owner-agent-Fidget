from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from ci_owner_agent.schemas import Owner
from ci_owner_agent.services.history_store import MongoHistoryStore
from ci_owner_agent.services.failure_identity import build_responsibility_signature
try:
    from pymongo.errors import DuplicateKeyError
except ImportError:  # pragma: no cover
    DuplicateKeyError = RuntimeError

FEEDBACK_ACTIONS = {"confirm_owner", "correct_owner", "mark_flaky", "mark_no_owner"}
CORRECT_OWNER_TYPES = {"high_confidence", "medium_confidence"}


class FeedbackStore:
    def __init__(self, history_store: MongoHistoryStore) -> None:
        self.history_store = history_store
        self.collection = history_store.feedback
        self.notices = history_store.notices

    def apply_feedback(
        self,
        *,
        repo: str,
        job: str,
        branch: str,
        build_number: int,
        failure_id: str | None,
        failure_signature: str | None,
        action: str,
        owner_name: str | None = None,
        owner_email: str | None = None,
        owner_type: str = "high_confidence",
        owner_commit: str | None = None,
        owner_wecom_userid: str | None = None,
        source_build_number: int | None = None,
        reviewer: str | None = None,
        reviewer_wecom_userid: str | None = None,
        note: str | None = None,
        source: str = "cli",
        operation_id: str | None = None,
        submitted_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        repo = str(repo or "").strip()
        branch = str(branch or "").strip()
        if not repo:
            raise ValueError("repo is required")
        if not branch:
            raise ValueError("branch is required")
        if action not in FEEDBACK_ACTIONS:
            raise ValueError(f"unsupported feedback action: {action}")
        if not failure_id and not failure_signature:
            raise ValueError("failure-id and failure-signature require at least one")
        if action == "correct_owner" and not owner_name:
            raise ValueError("owner-name is required for correct_owner")
        if action == "correct_owner" and owner_type not in CORRECT_OWNER_TYPES:
            raise ValueError("owner-type for correct_owner must be high_confidence or medium_confidence")

        notice_doc, item = self._find_notice_item(repo, job, branch, build_number, failure_id, failure_signature)
        if not notice_doc:
            raise ValueError(f"notice not found for repo={repo}, job={job}, branch={branch}, build={build_number}")
        if item is None:
            raise ValueError("failure item no longer exists in the current notice")
        failure_id = item.get("failureId")
        failure_signature = canonical_item_signature(item)
        feedback_item_key = build_feedback_item_key(item)
        operation_id = operation_id or uuid.uuid4().hex
        build_url = (notice_doc.get("notice") or {}).get("buildUrl")
        original_owner = (item.get("owner") if item else None) or ((notice_doc.get("notice") or {}).get("owner"))
        corrected_owner = None
        if action == "correct_owner":
            corrected_owner = Owner(
                type=owner_type,  # type: ignore[arg-type]
                name=owner_name or "",
                email=owner_email,
                commit=owner_commit,
                confidence=1 if owner_type == "high_confidence" else 0.7,
            ).model_dump(mode="json")

        scope = {"repo": repo, "job": job, "branch": branch, "buildNumber": build_number}
        base_doc = {
            "repo": repo,
            "job": job,
            "branch": branch,
            "buildNumber": build_number,
            "buildUrl": build_url,
            "failureId": failure_id,
            "failureSignature": failure_signature,
            "feedbackItemKey": feedback_item_key,
            "operationId": operation_id,
            "action": action,
            "originalOwner": original_owner,
            "correctedOwner": corrected_owner,
            "correctedOwnerWeComUserId": owner_wecom_userid,
            "correctedResponsibilityType": _responsibility_type_for_action(action),
            "sourceBuildNumber": source_build_number or build_number,
            "note": note,
            "reviewer": reviewer,
            "reviewerWeComUserId": reviewer_wecom_userid,
            "source": source,
        }
        now = dt.datetime.now(dt.timezone.utc)
        submitted_at = submitted_at or now
        payload_hash = hashlib.sha256(json.dumps(base_doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
        operation = {**base_doc, "_id": f"operation:{operation_id}", "recordType": "operation", "payloadHash": payload_hash, "submittedAt": submitted_at, "createdAt": now}
        try:
            self.collection.insert_one(operation)
        except DuplicateKeyError:
            stored = self.collection.find_one({"_id": operation["_id"]})
            if not stored or stored.get("payloadHash") != payload_hash:
                raise ValueError("operation id already exists with different feedback content")
            operation = stored
        return {"ok": True, "feedback": operation, "operation": operation, "isCurrent": self.is_current_operation(operation)}

    def list_feedback(self, *, repo: str, job: str, branch: str, build_number: int) -> list[dict[str, Any]]:
        repo = str(repo or "").strip()
        branch = str(branch or "").strip()
        if not repo:
            raise ValueError("repo is required")
        if not branch:
            raise ValueError("branch is required")
        return current_feedback_operations(self.collection, repo=repo, job=job, branch=branch, build_number=build_number)

    def find_by_operation_id(self, operation_id: str) -> dict | None:
        return self.collection.find_one({"_id": f"operation:{operation_id}", "recordType": "operation"})

    def is_current_operation(self, operation: dict) -> bool:
        current = current_feedback_operations(self.collection, repo=operation["repo"], job=operation["job"], branch=operation["branch"], build_number=operation["buildNumber"])
        return any(doc.get("operationId") == operation.get("operationId") for doc in current)

    def _find_notice_item(self, repo: str, job: str, branch: str, build_number: int, failure_id: str | None, failure_signature: str | None) -> tuple[dict | None, dict | None]:
        notice_doc = self.notices.find_one({"repo": repo, "job": job, "branch": branch, "buildNumber": build_number})
        if not notice_doc:
            return None, None
        notice = notice_doc.get("notice") or {}
        requested_signature = _canonical_signature(failure_signature)
        if failure_id and requested_signature:
            for item in notice.get("responsibilityItems") or []:
                if item.get("failureId") == failure_id and canonical_item_signature(item) == requested_signature:
                    return notice_doc, item
            raise ValueError("failure id and signature do not identify the same current notice item")
        for item in notice.get("responsibilityItems") or []:
            if failure_id and item.get("failureId") == failure_id:
                return notice_doc, item
            if failure_signature and item.get("failureSignature") == failure_signature:
                return notice_doc, item
            if requested_signature and canonical_item_signature(item) == requested_signature:
                return notice_doc, item
        return notice_doc, None

    def _deactivate_active(self, scope, key, failure_id, failure_signature, now):
        clauses = [{"feedbackItemKey": key}]
        if failure_id:
            clauses.append({"failureId": failure_id})
        if failure_signature:
            clauses.append({"failureSignature": failure_signature})
        query = {**scope, "isActive": True, "$or": clauses}
        self.collection.update_many(query, {"$set": {"isActive": False, "updatedAt": now}})


def _canonical_signature(value: Any) -> str | None:
    raw = str(value or "").strip()
    return build_responsibility_signature(failure_title=None, failure_summary=None, existing_signature=raw) if raw else None


def build_feedback_item_key(item: dict[str, Any]) -> str:
    failure_id = str(item.get("failureId") or "").strip()
    if failure_id:
        return f"id:{failure_id}"
    signature = canonical_item_signature(item)
    if signature:
        return f"sig:{signature}"
    raise ValueError("failure item has no stable identity")


def canonical_item_signature(item: dict[str, Any]) -> str | None:
    signature = build_responsibility_signature(
        failure_title=item.get("failureTitle"), failure_summary=item.get("failureSummary"),
        existing_signature=item.get("failureSignature"), error_code=item.get("errorCode"),
        error_type=item.get("errorType"), failure_kind=item.get("failureKind"),
        test_file_path=item.get("testFilePath"), failure_file_path=item.get("failureFilePath"),
    )
    return None if signature == "unknown_failure" else signature


def current_feedback_operations(collection: Any, *, repo: str, job: str, branch: str, build_number: int | None = None) -> list[dict]:
    query = {"recordType": "operation", "repo": repo, "job": job, "branch": branch}
    if build_number is not None:
        query["buildNumber"] = build_number
    docs = sorted(list(collection.find(query)), key=lambda doc: (doc.get("submittedAt"), doc.get("operationId")), reverse=True)
    seen, result = set(), []
    for doc in docs:
        key = (doc.get("buildNumber"), doc.get("feedbackItemKey"))
        if key not in seen:
            seen.add(key); result.append({**doc, "isActive": True})
    return result


def _responsibility_type_for_action(action: str) -> str | None:
    if action == "correct_owner":
        return "current_build_owner"
    if action in {"mark_flaky", "mark_no_owner"}:
        return "no_high_confidence_owner"
    return None
