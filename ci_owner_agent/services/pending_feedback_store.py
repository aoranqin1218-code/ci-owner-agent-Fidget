from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class PendingFeedbackStore:
    def __init__(self, history_store: Any, ttl_seconds: int = 300) -> None:
        self.collection = history_store.wecom_pending_feedback
        self.ttl = dt.timedelta(seconds=ttl_seconds)

    def create(self, *, message: Any, context: dict[str, Any], item: dict[str, Any], intent: Any) -> dict[str, Any]:
        existing = self.collection.find_one({"eventKey": message.event_key})
        if existing:
            return existing
        now = _utcnow()
        for _ in range(8):
            code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
            doc = {
                "confirmationCode": code,
                "eventKey": message.event_key,
                "chatId": message.chat_id,
                "senderUserId": message.sender_userid,
                "senderName": message.sender_name,
                "feedbackContext": _safe_context(context, item),
                "intent": intent.model_dump(mode="json"),
                "status": "pending",
                "createdAt": now,
                "updatedAt": now,
                "expiresAt": now + self.ttl,
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
        if current.get("expiresAt", now) <= now:
            self._set_status(current, "expired")
            return "expired", current
        if current.get("status") != "pending":
            return str(current.get("status")), current
        query = {"confirmationCode": code, "senderUserId": sender_userid, "status": "pending", "expiresAt": {"$gt": now}}
        update = {"$set": {"status": "applying", "updatedAt": now}}
        try:
            from pymongo import ReturnDocument

            doc = self.collection.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
        except (AttributeError, TypeError):
            self.collection.update_one(query, update, upsert=False)
            doc = self.collection.find_one({"confirmationCode": code, "status": "applying"})
        return ("claimed", doc) if doc else ("already_processed", self.collection.find_one({"confirmationCode": code}))

    def cancel(self, code: str, sender_userid: str) -> str:
        status, doc = self.claim(code, sender_userid)
        if status != "claimed" or doc is None:
            return status
        self._set_status(doc, "cancelled")
        return "cancelled"

    def mark_applied(self, doc: dict[str, Any]) -> None:
        self._set_status(doc, "applied")

    def mark_failed(self, doc: dict[str, Any], error: str) -> None:
        self.collection.update_one(
            {"confirmationCode": doc["confirmationCode"], "status": "applying"},
            {"$set": {"status": "failed", "error": str(error)[:500], "updatedAt": _utcnow()}},
            upsert=False,
        )

    def mark_stale(self, doc: dict[str, Any], reason: str) -> None:
        self.collection.update_one(
            {"confirmationCode": doc["confirmationCode"], "status": "applying"},
            {"$set": {"status": "stale", "error": reason[:500], "updatedAt": _utcnow()}},
            upsert=False,
        )

    def _set_status(self, doc: dict[str, Any], status: str) -> None:
        self.collection.update_one(
            {"confirmationCode": doc["confirmationCode"]},
            {"$set": {"status": status, "updatedAt": _utcnow()}},
            upsert=False,
        )


class WeComEventStore:
    def __init__(self, history_store: Any, ttl_days: int = 7, processing_lease_seconds: int = 60) -> None:
        self.collection = history_store.wecom_bot_events
        self.ttl = dt.timedelta(days=ttl_days)
        self.processing_lease = dt.timedelta(seconds=processing_lease_seconds)

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
                    return "claimed", self.collection.find_one({"eventKey": message.event_key})
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
