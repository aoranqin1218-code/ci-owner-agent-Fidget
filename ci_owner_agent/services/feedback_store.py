from __future__ import annotations

import datetime as dt
from typing import Any

from ci_owner_agent.schemas import Owner
from ci_owner_agent.services.history_store import MongoHistoryStore
from ci_owner_agent.services.failure_identity import build_responsibility_signature

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
        job: str,
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
        note: str | None = None,
        source: str = "cli",
    ) -> dict[str, Any]:
        if action not in FEEDBACK_ACTIONS:
            raise ValueError(f"unsupported feedback action: {action}")
        if not failure_id and not failure_signature:
            raise ValueError("failure-id and failure-signature require at least one")
        if action == "correct_owner" and not owner_name:
            raise ValueError("owner-name is required for correct_owner")
        if action == "correct_owner" and owner_type not in CORRECT_OWNER_TYPES:
            raise ValueError("owner-type for correct_owner must be high_confidence or medium_confidence")

        failure_signature = build_responsibility_signature(
            failure_title=None,
            failure_summary=None,
            existing_signature=failure_signature,
        ) if failure_signature else None

        notice_doc, item = self._find_notice_item(job, build_number, failure_id, failure_signature)
        if item:
            failure_id = failure_id or item.get("failureId")
            failure_signature = failure_signature or item.get("failureSignature")
        branch = notice_doc.get("branch") if notice_doc else None
        repo = notice_doc.get("repo") if notice_doc else None
        build_url = (notice_doc.get("notice") or {}).get("buildUrl") if notice_doc else None
        original_owner = (item.get("owner") if item else None) or ((notice_doc.get("notice") or {}).get("owner") if notice_doc else None)
        corrected_owner = None
        if action == "correct_owner":
            corrected_owner = Owner(
                type=owner_type,  # type: ignore[arg-type]
                name=owner_name or "",
                email=owner_email,
                commit=owner_commit,
                confidence=1 if owner_type == "high_confidence" else 0.7,
            ).model_dump(mode="json")

        now = dt.datetime.now(dt.timezone.utc)
        deactivate_query = {"repo": repo, "job": job, "buildNumber": build_number, "isActive": True}
        existing = list(self.collection.find(deactivate_query))
        for doc in existing:
            same_id = failure_id and doc.get("failureId") == failure_id
            same_sig = failure_signature and doc.get("failureSignature") == failure_signature
            if same_id or same_sig:
                self.collection.update_one({"repo": doc.get("repo"), "job": doc.get("job"), "buildNumber": doc.get("buildNumber"), "failureId": doc.get("failureId"), "createdAt": doc.get("createdAt")}, {"$set": {"isActive": False, "updatedAt": now}}, upsert=False)

        doc = {
            "repo": repo,
            "job": job,
            "branch": branch,
            "buildNumber": build_number,
            "buildUrl": build_url,
            "failureId": failure_id,
            "failureSignature": failure_signature,
            "action": action,
            "originalOwner": original_owner,
            "correctedOwner": corrected_owner,
            "correctedOwnerWeComUserId": owner_wecom_userid,
            "correctedResponsibilityType": _responsibility_type_for_action(action),
            "sourceBuildNumber": source_build_number or build_number,
            "note": note,
            "reviewer": reviewer,
            "source": source,
            "isActive": True,
            "createdAt": now,
            "updatedAt": now,
        }
        self.collection.update_one(
            {"repo": repo, "job": job, "buildNumber": build_number, "failureId": failure_id, "failureSignature": failure_signature, "createdAt": now},
            {"$set": doc},
            upsert=True,
        )
        return {"ok": True, "feedback": doc}

    def list_feedback(self, *, job: str, build_number: int) -> list[dict[str, Any]]:
        return list(self.collection.find({"job": job, "buildNumber": build_number, "isActive": True}))

    def _find_notice_item(self, job: str, build_number: int, failure_id: str | None, failure_signature: str | None) -> tuple[dict | None, dict | None]:
        notice_doc = self.notices.find_one({"job": job, "buildNumber": build_number})
        if not notice_doc:
            return None, None
        notice = notice_doc.get("notice") or {}
        for item in notice.get("responsibilityItems") or []:
            if failure_id and item.get("failureId") == failure_id:
                return notice_doc, item
            if failure_signature and item.get("failureSignature") == failure_signature:
                return notice_doc, item
        return notice_doc, None


def _responsibility_type_for_action(action: str) -> str | None:
    if action == "correct_owner":
        return "current_build_owner"
    if action in {"mark_flaky", "mark_no_owner"}:
        return "no_high_confidence_owner"
    return None
