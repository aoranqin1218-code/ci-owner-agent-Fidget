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

    def _set_status(self, doc: dict[str, Any], status: str) -> None:
        self.collection.update_one(
            {"confirmationCode": doc["confirmationCode"]},
            {"$set": {"status": status, "updatedAt": _utcnow()}},
            upsert=False,
        )


class WeComEventStore:
    def __init__(self, history_store: Any, ttl_days: int = 7) -> None:
        self.collection = history_store.wecom_bot_events
        self.ttl = dt.timedelta(days=ttl_days)

    def mark_once(self, message: Any) -> bool:
        if self.collection.find_one({"eventKey": message.event_key}):
            return False
        now = _utcnow()
        doc = {
            "eventKey": message.event_key,
            "messageId": message.message_id,
            "requestId": message.request_id,
            "chatId": message.chat_id,
            "senderUserId": message.sender_userid,
            "createdAt": now,
            "expiresAt": now + self.ttl,
        }
        try:
            result = self.collection.update_one({"eventKey": message.event_key}, {"$setOnInsert": doc}, upsert=True)
            return getattr(result, "upserted_id", True) is not None
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "e11000" in str(exc).lower():
                return False
            raise


def _safe_context(context: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": context.get("code"),
        "repo": context.get("repo"),
        "job": context.get("job"),
        "branch": context.get("branch"),
        "buildNumber": context.get("buildNumber"),
        "item": dict(item),
    }


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
