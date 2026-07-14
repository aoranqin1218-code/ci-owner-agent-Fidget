from __future__ import annotations

import datetime as dt
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
        failure_signature = item.get("failureSignature") or _canonical_signature(item.get("failureSignature"))
        feedback_item_key = build_feedback_item_key(item)
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
        for attempt in range(4):
            now = dt.datetime.now(dt.timezone.utc)
            self._deactivate_active(scope, feedback_item_key, failure_id, failure_signature, now)
            doc = {**base_doc, "isActive": True, "createdAt": now, "updatedAt": now}
            try:
                self.collection.insert_one(doc)
                return {"ok": True, "feedback": doc}
            except DuplicateKeyError:
                if attempt == 3:
                    raise
        raise RuntimeError("unable to serialize feedback write")

    def list_feedback(self, *, repo: str, job: str, branch: str, build_number: int) -> list[dict[str, Any]]:
        repo = str(repo or "").strip()
        branch = str(branch or "").strip()
        if not repo:
            raise ValueError("repo is required")
        if not branch:
            raise ValueError("branch is required")
        return list(self.collection.find({"repo": repo, "job": job, "branch": branch, "buildNumber": build_number, "isActive": True}))

    def _find_notice_item(self, repo: str, job: str, branch: str, build_number: int, failure_id: str | None, failure_signature: str | None) -> tuple[dict | None, dict | None]:
        notice_doc = self.notices.find_one({"repo": repo, "job": job, "branch": branch, "buildNumber": build_number})
        if not notice_doc:
            return None, None
        notice = notice_doc.get("notice") or {}
        requested_signature = _canonical_signature(failure_signature)
        if failure_id and requested_signature:
            for item in notice.get("responsibilityItems") or []:
                if item.get("failureId") == failure_id and _canonical_signature(item.get("failureSignature")) == requested_signature:
                    return notice_doc, item
            raise ValueError("failure id and signature do not identify the same current notice item")
        for item in notice.get("responsibilityItems") or []:
            if failure_id and item.get("failureId") == failure_id:
                return notice_doc, item
            if requested_signature and _canonical_signature(item.get("failureSignature")) == requested_signature:
                return notice_doc, item
        return notice_doc, None

    def _deactivate_active(self, scope, key, failure_id, failure_signature, now):
        query = {**scope, "isActive": True, "$or": [{"feedbackItemKey": key}, {"failureId": failure_id}, {"failureSignature": failure_signature}]}
        self.collection.update_many(query, {"$set": {"isActive": False, "updatedAt": now}})


def _canonical_signature(value: Any) -> str | None:
    raw = str(value or "").strip()
    return build_responsibility_signature(failure_title=None, failure_summary=None, existing_signature=raw) if raw else None


def build_feedback_item_key(item: dict[str, Any]) -> str:
    failure_id = str(item.get("failureId") or "").strip()
    if failure_id:
        return f"id:{failure_id}"
    signature = _canonical_signature(item.get("failureSignature"))
    if signature:
        return f"sig:{signature}"
    raise ValueError("failure item has no stable identity")


def _responsibility_type_for_action(action: str) -> str | None:
    if action == "correct_owner":
        return "current_build_owner"
    if action in {"mark_flaky", "mark_no_owner"}:
        return "no_high_confidence_owner"
    return None
