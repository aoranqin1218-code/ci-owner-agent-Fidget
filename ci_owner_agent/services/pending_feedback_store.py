from __future__ import annotations

import datetime as dt
import secrets
import uuid
import uuid
from typing import Any

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class PendingFeedbackStore:
    def __init__(self, history_store: Any, ttl_seconds: int = 300) -> None:
        self.collection = history_store.wecom_pending_feedback
        self.confirm_ttl = dt.timedelta(seconds=ttl_seconds)
        self.ttl = dt.timedelta(days=7)
        self.apply_lease = dt.timedelta(seconds=60)

    def create(self, *, message: Any, context: dict[str, Any], item: dict[str, Any], intent: Any) -> dict[str, Any]:
        existing = self.collection.find_one({"eventKey": message.event_key})
        if existing:
            return existing
        now = _utcnow()
        operation_id = uuid.uuid4().hex
        card_task_id = f"ci-feedback-{operation_id}"
        for _ in range(8):
            code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
            doc = {
                "confirmationCode": code,
                "cardTaskId": card_task_id,
                "eventKey": message.event_key,
                "chatId": message.chat_id,
                "senderUserId": message.sender_userid,
                "senderName": message.sender_name,
                "feedbackContext": _safe_context(context, item),
                "intent": intent.model_dump(mode="json"),
                "operationId": uuid.uuid4().hex,
                "status": "pending",
                "operationId": operation_id,
                "createdAt": now,
                "updatedAt": now,
                "expiresAt": now + self.ttl,
                "confirmationExpiresAt": now + self.confirm_ttl,
            }
            try:
                self.collection.update_one({"confirmationCode": code}, {"$setOnInsert": doc}, upsert=True)
                stored = self.collection.find_one({"confirmationCode": code})
                if stored and stored.get("eventKey") == message.event_key:
                    return stored
            except Exception as exc:
                if "duplicate" not in str(exc).lower() and "e11000" not in str(exc).lower():
                    raise
                concurrent = self.collection.find_one({"eventKey": message.event_key})
                if concurrent:
                    return concurrent
        raise RuntimeError("unable to allocate a unique confirmation code")

    def claim(self, code: str, sender_userid: str) -> tuple[str, dict[str, Any] | None]:
        code = code.upper()
        now = _utcnow()
        current = self.collection.find_one({"confirmationCode": code})
        if current is None:
            return "not_found", None
        if current.get("senderUserId") != sender_userid:
            return "forbidden", current
        if current.get("status") == "pending" and current.get("confirmationExpiresAt", current.get("expiresAt", now)) <= now:
            self.expire_pending(code, sender_userid, now)
            refreshed = self.collection.find_one({"confirmationCode": code})
            return "expired", refreshed or current
        if current.get("status") == "applying":
            lease = current.get("applyLeaseUntil")
            if lease is not None and lease > now:
                return "applying", current
            token = secrets.token_urlsafe(24)
            query = {
                "confirmationCode": code,
                "status": "applying",
                "$or": [
                    {"applyLeaseUntil": {"$lte": now}},
                    {"applyLeaseUntil": None},
                    {"applyLeaseUntil": {"$exists": False}},
                ],
            }
            doc = self.collection.find_one_and_update(query, {"$set": {"applyToken": token, "applyLeaseUntil": now + self.apply_lease, "updatedAt": now}, "$inc": {"applyAttemptCount": 1}}, return_document=_return_after())
            return ("claimed", doc) if doc else ("applying", self.collection.find_one({"confirmationCode": code}))
        if current.get("status") != "pending":
            return str(current.get("status")), current
        query = {"confirmationCode": code, "senderUserId": sender_userid, "status": "pending", "confirmationExpiresAt": {"$gt": now}}
        token = secrets.token_urlsafe(24)
        update = {"$set": {"status": "applying", "applyToken": token, "applyLeaseUntil": now + self.apply_lease, "applyAttemptCount": 1, "operationSubmittedAt": now, "updatedAt": now}}
        try:
            from pymongo import ReturnDocument

            doc = self.collection.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
        except (AttributeError, TypeError):
            self.collection.update_one(query, update, upsert=False)
            doc = self.collection.find_one({"confirmationCode": code, "status": "applying"})
        return ("claimed", doc) if doc else ("already_processed", self.collection.find_one({"confirmationCode": code}))

    def cancel(self, code: str, sender_userid: str) -> str:
        code = code.upper()
        now = _utcnow()
        result = self.collection.update_one(
            {"confirmationCode": code, "senderUserId": sender_userid, "status": "pending", "confirmationExpiresAt": {"$gt": now}},
            {"$set": {"status": "cancelled", "updatedAt": now}},
            upsert=False,
        )
        if result.modified_count == 1:
            return "cancelled"
        doc = self.collection.find_one({"confirmationCode": code})
        if doc is None:
            return "not_found"
        if doc.get("senderUserId") != sender_userid:
            return "forbidden"
        if doc.get("status") != "pending":
            return str(doc.get("status"))
        if doc.get("confirmationExpiresAt", now) <= now:
            self.expire_pending(code, sender_userid, now)
            return "expired"
        return "already_processed"

    def get_by_card_task_id(self, task_id: str) -> dict[str, Any] | None:
        return self.collection.find_one({"cardTaskId": task_id})

    def claim_by_card_task_id(self, task_id: str, sender_userid: str) -> tuple[str, dict[str, Any] | None]:
        pending = self.collection.find_one({"cardTaskId": task_id})
        if pending is None:
            return "not_found", None
        return self.claim(pending["confirmationCode"], sender_userid)

    def cancel_by_card_task_id(self, task_id: str, sender_userid: str) -> str:
        pending = self.collection.find_one({"cardTaskId": task_id})
        if pending is None:
            return "not_found"
        return self.cancel(pending["confirmationCode"], sender_userid)


    def expire_pending(self, code: str, sender_userid: str, now: dt.datetime) -> bool:
        result = self.collection.update_one(
            {"confirmationCode": code.upper(), "senderUserId": sender_userid, "status": "pending", "confirmationExpiresAt": {"$lte": now}},
            {"$set": {"status": "expired", "updatedAt": now}},
            upsert=False,
        )
        return result.modified_count == 1

    def mark_applied(self, doc: dict[str, Any], apply_token: str) -> bool:
        return self._set_status(doc, "applied", apply_token)

    def mark_failed(self, doc: dict[str, Any], apply_token: str, error: str) -> bool:
        return self._terminal(doc, apply_token, "failed", error)

    def mark_stale(self, doc: dict[str, Any], apply_token: str, reason: str) -> bool:
        return self._terminal(doc, apply_token, "stale", reason)

    def reconcile_applied(self, confirmation_code: str, operation_id: str) -> bool:
        result = self.collection.update_one({"confirmationCode": confirmation_code, "status": "applying", "operationId": operation_id}, {"$set": {"status": "applied", "updatedAt": _utcnow(), "applyToken": None, "applyLeaseUntil": None}}, upsert=False)
        return result.modified_count == 1

    def activate_operation_if_pending_applied(
        self, confirmation_code: str, operation_id: str, feedback_collection
    ) -> str:
        """Atomically activate a prepared operation only if pending is in applied state.

        Returns one of: "committed", "already_committed", "not_applied", "not_found".
        """
        pending = self.collection.find_one(
            {"confirmationCode": confirmation_code, "status": "applied", "operationId": operation_id}
        )
        if pending is None:
            return "not_applied"
        op = feedback_collection.find_one(
            {"_id": f"operation:{operation_id}", "recordType": "operation"}
        )
        if op is None:
            return "not_found"
        if op.get("isCommitted"):
            return "already_committed"
        result = feedback_collection.update_one(
            {"_id": f"operation:{operation_id}", "recordType": "operation", "isCommitted": False},
            {"$set": {"isCommitted": True}},
            upsert=False,
        )
        if result.modified_count == 1:
            return "committed"
        refreshed = feedback_collection.find_one(
            {"_id": f"operation:{operation_id}", "recordType": "operation"}
        )
        if refreshed and refreshed.get("isCommitted"):
            return "already_committed"
        if refreshed is None:
            return "not_found"
        return "not_applied"

    def _terminal(self, doc, token, status, error):
        if not isinstance(token, str) or not token:
            return False
        result = self.collection.update_one({"confirmationCode": doc["confirmationCode"], "status": "applying", "applyToken": token}, {"$set": {"status": status, "error": str(error)[:500], "updatedAt": _utcnow(), "applyToken": None, "applyLeaseUntil": None}}, upsert=False)
        return result.modified_count == 1

    def _set_status(self, doc: dict[str, Any], status: str, apply_token: str | None = None) -> bool:
        if not isinstance(apply_token, str) or not apply_token:
            return False
        query = {"confirmationCode": doc["confirmationCode"], "status": "applying", "applyToken": apply_token}
        result = self.collection.update_one(
            query,
            {"$set": {"status": status, "updatedAt": _utcnow(), "applyToken": None, "applyLeaseUntil": None}},
            upsert=False,
        )
        return result.modified_count == 1





class WeComEventStore:
    """Deduplicate WeCom bot events with TTL."""

    def __init__(self, history_store: Any, ttl_days: int = 7) -> None:
        self.collection = history_store.wecom_bot_events
        self.ttl = dt.timedelta(days=ttl_days)
        self.processing_lease = dt.timedelta(seconds=30)

    def claim(self, message: Any) -> tuple[str, dict[str, Any] | None]:
        now = _utcnow()
        claim_token = secrets.token_urlsafe(24)
        doc = {
            "eventKey": message.event_key,
            "messageId": message.message_id,
            "requestId": message.request_id,
            "chatId": message.chat_id,
            "senderUserId": message.sender_userid,
            "status": "processing",
            "attemptCount": 1,
            "claimToken": claim_token,
            "leaseUntil": now + self.processing_lease,
            "replyText": None,
            "responseType": None,
            "responsePayload": None,
            "lastError": None,
            "createdAt": now,
            "updatedAt": now,
            "completedAt": None,
            "expiresAt": now + self.ttl,
        }
        try:
            before = self.collection.find_one_and_update(
                {"eventKey": message.event_key}, {"$setOnInsert": doc}, upsert=True, return_document=_return_before()
            )
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "e11000" in str(exc).lower():
                before = self.collection.find_one({"eventKey": message.event_key})
            elif isinstance(exc, AttributeError):
                before = self.collection.find_one({"eventKey": message.event_key})
                if before is None:
                    self.collection.update_one({"eventKey": message.event_key}, {"$setOnInsert": doc}, upsert=True)
                    return "claimed", self.collection.find_one({"eventKey": message.event_key}) or doc
            else:
                raise
        if before is None:
            return "claimed", self.collection.find_one({"eventKey": message.event_key}) or doc
        status = str(before.get("status") or "")
        if status == "completed":
            return "completed", before
        if status == "processing" and before.get("leaseUntil", now) > now:
            return "processing", before
        if status not in {"failed", "processing"}:
            return status, before
        retry_query: dict[str, Any] = {
            "eventKey": message.event_key,
            "$or": [
                {"status": "failed"},
                {"status": "processing", "leaseUntil": {"$lte": now}},
            ],
        }
        retry_update = {
            "$set": {"status": "processing", "leaseUntil": now + self.processing_lease, "claimToken": secrets.token_urlsafe(24), "updatedAt": now, "lastError": None},
            "$inc": {"attemptCount": 1},
        }
        claimed = self.collection.find_one_and_update(
            retry_query, retry_update, return_document=_return_after()
        )
        if claimed:
            return "claimed", claimed
        current = self.collection.find_one({"eventKey": message.event_key})
        if current and current.get("status") == "completed":
            return "completed", current
        return "processing", current

    def mark_completed(self, event_key: str, claim_token: str, reply_text: str) -> bool:
        now = _utcnow()
        self.collection.update_one(
            {"eventKey": event_key, "status": "processing", "claimToken": claim_token},
            {"$set": {"status": "completed", "replyText": reply_text, "completedAt": now, "updatedAt": now, "leaseUntil": None}},
            upsert=False,
        )
        return bool(self.collection.find_one({"eventKey": event_key, "status": "completed", "claimToken": claim_token}))

    def mark_completed_with_response(
        self, event_key: str, claim_token: str, response_type: str, response_payload: dict[str, Any]
    ) -> bool:
        now = _utcnow()
        self.collection.update_one(
            {"eventKey": event_key, "status": "processing", "claimToken": claim_token},
            {
                "$set": {
                    "status": "completed", "responseType": response_type, "responsePayload": response_payload,
                    "completedAt": now, "updatedAt": now, "leaseUntil": None,
                }
            },
            upsert=False,
        )
        return bool(self.collection.find_one({"eventKey": event_key, "status": "completed", "claimToken": claim_token}))

    def mark_failed(self, event_key: str, claim_token: str, error: str) -> bool:
        self.collection.update_one(
            {"eventKey": event_key, "status": "processing", "claimToken": claim_token},
            {"$set": {"status": "failed", "lastError": str(error)[:500], "updatedAt": _utcnow(), "leaseUntil": None}},
            upsert=False,
        )
        return bool(self.collection.find_one({"eventKey": event_key, "status": "failed", "claimToken": claim_token}))


def _safe_context(context: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": context.get("code"),
        "repo": context.get("repo"),
        "job": context.get("job"),
        "branch": context.get("branch"),
        "buildNumber": context.get("buildNumber"),
        "feedbackCode": context.get("code"),
        "itemIndex": item.get("itemIndex"),
        "failureId": item.get("failureId"),
        "failureSignature": item.get("failureSignature"),
        "contextUpdatedAt": context.get("updatedAt"),
        "item": dict(item),
    }


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _return_before():
    try:
        from pymongo import ReturnDocument

        return ReturnDocument.BEFORE
    except ImportError:
        return False


def _return_after():
    try:
        from pymongo import ReturnDocument

        return ReturnDocument.AFTER
    except ImportError:
        return True




def _safe_context(context: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": context.get("code"),
        "repo": context.get("repo"),
        "job": context.get("job"),
        "branch": context.get("branch"),
        "buildNumber": context.get("buildNumber"),
        "feedbackCode": context.get("code"),
        "itemIndex": item.get("itemIndex"),
        "failureId": item.get("failureId"),
        "failureSignature": item.get("failureSignature"),
        "contextUpdatedAt": context.get("updatedAt"),
        "item": dict(item),
    }


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _return_before():
    try:
        from pymongo import ReturnDocument

        return ReturnDocument.BEFORE
    except ImportError:
        return False


def _return_after():
    try:
        from pymongo import ReturnDocument

        return ReturnDocument.AFTER
    except ImportError:
        return True




def _safe_context(context: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": context.get("code"),
        "repo": context.get("repo"),
        "job": context.get("job"),
        "branch": context.get("branch"),
        "buildNumber": context.get("buildNumber"),
        "feedbackCode": context.get("code"),
        "itemIndex": item.get("itemIndex"),
        "failureId": item.get("failureId"),
        "failureSignature": item.get("failureSignature"),
        "contextUpdatedAt": context.get("updatedAt"),
        "item": dict(item),
    }


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _return_before():
    try:
        from pymongo import ReturnDocument

        return ReturnDocument.BEFORE
    except ImportError:
        return False


def _return_after():
    try:
        from pymongo import ReturnDocument

        return ReturnDocument.AFTER
    except ImportError:
        return True




def _safe_context(context: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": context.get("code"),
        "repo": context.get("repo"),
        "job": context.get("job"),
        "branch": context.get("branch"),
        "buildNumber": context.get("buildNumber"),
        "feedbackCode": context.get("code"),
        "itemIndex": item.get("itemIndex"),
        "failureId": item.get("failureId"),
        "failureSignature": item.get("failureSignature"),
        "contextUpdatedAt": context.get("updatedAt"),
        "item": dict(item),
    }


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _return_before():
    try:
        from pymongo import ReturnDocument

        return ReturnDocument.BEFORE
    except ImportError:
        return False


def _return_after():
    try:
        from pymongo import ReturnDocument

        return ReturnDocument.AFTER
    except ImportError:
        return True


class WeComEventStore:
    """Deduplicate WeCom bot events with TTL."""

    def __init__(self, history_store: Any, ttl_days: int = 7) -> None:
        self.collection = history_store.wecom_bot_events
        self.ttl = dt.timedelta(days=ttl_days)
        self.processing_lease = dt.timedelta(seconds=30)

    def claim(self, message: Any) -> tuple[str, dict[str, Any] | None]:
        now = _utcnow()
        claim_token = secrets.token_urlsafe(24)
        doc = {
            "eventKey": message.event_key,
            "messageId": message.message_id,
            "requestId": message.request_id,
            "chatId": message.chat_id,
            "senderUserId": message.sender_userid,
            "status": "processing",
            "attemptCount": 1,
            "claimToken": claim_token,
            "leaseUntil": now + self.processing_lease,
            "replyText": None,
            "responseType": None,
            "responsePayload": None,
            "lastError": None,
            "createdAt": now,
            "updatedAt": now,
            "completedAt": None,
            "expiresAt": now + self.ttl,
        }
        try:
            before = self.collection.find_one_and_update(
                {"eventKey": message.event_key}, {"$setOnInsert": doc}, upsert=True, return_document=_return_before()
            )
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "e11000" in str(exc).lower():
                before = self.collection.find_one({"eventKey": message.event_key})
            elif isinstance(exc, AttributeError):
                before = self.collection.find_one({"eventKey": message.event_key})
                if before is None:
                    self.collection.update_one({"eventKey": message.event_key}, {"$setOnInsert": doc}, upsert=True)
                    return "claimed", self.collection.find_one({"eventKey": message.event_key}) or doc
            else:
                raise
        if before is None:
            return "claimed", self.collection.find_one({"eventKey": message.event_key}) or doc
        status = str(before.get("status") or "")
        if status == "completed":
            return "completed", before
        if status == "processing" and before.get("leaseUntil", now) > now:
            return "processing", before
        if status not in {"failed", "processing"}:
            return status, before
        retry_query: dict[str, Any] = {
            "eventKey": message.event_key,
            "$or": [
                {"status": "failed"},
                {"status": "processing", "leaseUntil": {"$lte": now}},
            ],
        }
        retry_update = {
            "$set": {"status": "processing", "leaseUntil": now + self.processing_lease, "claimToken": secrets.token_urlsafe(24), "updatedAt": now, "lastError": None},
            "$inc": {"attemptCount": 1},
        }
        claimed = self.collection.find_one_and_update(
            retry_query, retry_update, return_document=_return_after()
        )
        if claimed:
            return "claimed", claimed
        current = self.collection.find_one({"eventKey": message.event_key})
        if current and current.get("status") == "completed":
            return "completed", current
        return "processing", current

    def mark_completed(self, event_key: str, claim_token: str, reply_text: str) -> bool:
        now = _utcnow()
        self.collection.update_one(
            {"eventKey": event_key, "status": "processing", "claimToken": claim_token},
            {"$set": {"status": "completed", "replyText": reply_text, "completedAt": now, "updatedAt": now, "leaseUntil": None}},
            upsert=False,
        )
        return bool(self.collection.find_one({"eventKey": event_key, "status": "completed", "claimToken": claim_token}))

    def mark_completed_with_response(
        self, event_key: str, claim_token: str, response_type: str, response_payload: dict[str, Any]
    ) -> bool:
        now = _utcnow()
        self.collection.update_one(
            {"eventKey": event_key, "status": "processing", "claimToken": claim_token},
            {
                "$set": {
                    "status": "completed", "responseType": response_type, "responsePayload": response_payload,
                    "completedAt": now, "updatedAt": now, "leaseUntil": None,
                }
            },
            upsert=False,
        )
        return bool(self.collection.find_one({"eventKey": event_key, "status": "completed", "claimToken": claim_token}))

    def mark_failed(self, event_key: str, claim_token: str, error: str) -> bool:
        self.collection.update_one(
            {"eventKey": event_key, "status": "processing", "claimToken": claim_token},
            {"$set": {"status": "failed", "lastError": str(error)[:500], "updatedAt": _utcnow(), "leaseUntil": None}},
            upsert=False,
        )
        return bool(self.collection.find_one({"eventKey": event_key, "status": "failed", "claimToken": claim_token}))