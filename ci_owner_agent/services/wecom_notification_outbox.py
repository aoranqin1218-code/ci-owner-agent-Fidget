from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import uuid
from collections.abc import Mapping
from typing import Any


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc(value: dt.datetime | None) -> dt.datetime:
    value = value or _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _safe_error(error: BaseException) -> str:
    return " ".join(str(error).split())[:300]


class WeComNotificationOutbox:
    def __init__(self, history_store: Any, *, lease_seconds: int = 30, max_attempts: int = 5) -> None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("lease_seconds and max_attempts must be positive")
        self.collection = history_store.wecom_notification_outbox
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def enqueue_markdown(self, *, notification_type: str, target_chat_id: str, markdown: str,
                         dedup_key: str, metadata: Mapping[str, Any] | None = None,
                         force: bool = False) -> dict[str, Any]:
        if not all(isinstance(v, str) and v.strip() for v in (notification_type, target_chat_id, markdown, dedup_key)):
            raise ValueError("notification_type, target_chat_id, markdown, and dedup_key are required")
        stable = {"channel": "wecom_bot", "notificationType": notification_type,
                  "targetChatId": target_chat_id.strip(), "dedupKey": dedup_key}
        if force:
            stable["forceNonce"] = uuid.uuid4().hex
        delivery_key = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        now = _utc_now()
        doc = {"deliveryKey": delivery_key, "dedupKey": dedup_key, "notificationType": notification_type,
               "channel": "wecom_bot", "targetChatId": target_chat_id.strip(), "messageType": "markdown",
               "payload": {"content": markdown}, "metadata": dict(metadata or {}), "status": "pending",
               "attemptCount": 0, "nextAttemptAt": now, "leaseToken": None, "leaseUntil": None,
               "lastErrorType": None, "lastError": None, "createdAt": now, "updatedAt": now,
               "sentAt": None, "deadAt": None}
        try:
            self.collection.insert_one(doc)
            return {**doc, "inserted": True}
        except Exception as exc:
            if exc.__class__.__name__ != "DuplicateKeyError":
                raise
            existing = self.collection.find_one({"deliveryKey": delivery_key})
            if existing is None:
                raise
            return {**existing, "inserted": False}

    def claim_next(self, *, now: dt.datetime | None = None) -> dict[str, Any] | None:
        now = _utc(now)
        query = {"$or": [{"status": "pending", "nextAttemptAt": {"$lte": now}},
                          {"status": "sending", "leaseUntil": {"$lte": now}}]}
        candidates = sorted(list(self.collection.find(query)), key=lambda d: (d.get("nextAttemptAt") or d.get("leaseUntil") or now, d.get("createdAt") or now))
        for candidate in candidates:
            old_status = candidate.get("status")
            condition: dict[str, Any] = {"deliveryKey": candidate["deliveryKey"], "status": old_status}
            if old_status == "pending":
                condition["nextAttemptAt"] = {"$lte": now}
            else:
                condition["leaseUntil"] = {"$lte": now}
            token = secrets.token_urlsafe(24)
            claimed = self.collection.find_one_and_update(condition, {"$set": {"status": "sending", "leaseToken": token,
                "leaseUntil": now + dt.timedelta(seconds=self.lease_seconds), "updatedAt": now}, "$inc": {"attemptCount": 1}}, return_document=True)
            if claimed is not None:
                return claimed
        return None

    def mark_sent(self, *, delivery_key: str, lease_token: str, now: dt.datetime | None = None) -> bool:
        now = _utc(now)
        result = self.collection.update_one({"deliveryKey": delivery_key, "status": "sending", "leaseToken": lease_token},
            {"$set": {"status": "sent", "sentAt": now, "updatedAt": now, "leaseToken": None, "leaseUntil": None,
                      "lastErrorType": None, "lastError": None}})
        return bool(getattr(result, "modified_count", 0))

    def mark_failed(self, *, delivery_key: str, lease_token: str, error: BaseException,
                    now: dt.datetime | None = None) -> str:
        now = _utc(now)
        current = self.collection.find_one({"deliveryKey": delivery_key, "status": "sending", "leaseToken": lease_token})
        if current is None:
            return "lost"
        attempt_count = int(current.get("attemptCount") or 0)
        fields = {"updatedAt": now, "leaseToken": None, "leaseUntil": None,
                  "lastErrorType": type(error).__name__, "lastError": _safe_error(error)}
        if attempt_count >= self.max_attempts:
            fields.update({"status": "dead", "deadAt": now, "nextAttemptAt": None})
            outcome = "dead"
        else:
            fields.update({"status": "pending", "nextAttemptAt": now + dt.timedelta(seconds=min(2 ** max(attempt_count - 1, 0), 300))})
            outcome = "retry"
        result = self.collection.update_one({"deliveryKey": delivery_key, "status": "sending", "leaseToken": lease_token}, {"$set": fields})
        return outcome if getattr(result, "modified_count", 0) else "lost"
