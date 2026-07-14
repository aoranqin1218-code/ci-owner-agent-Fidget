from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class FeedbackContextStore:
    def __init__(self, history_store: Any, ttl_days: int = 30) -> None:
        self.collection = history_store.feedback_contexts
        self.notices = history_store.notices
        self.ttl = dt.timedelta(days=ttl_days)

    def get_or_create_for_notice(self, notice: Any) -> dict[str, Any] | None:
        repo = str(notice.repo or "").strip()
        if not repo or not notice.branch or not notice.responsibilityItems:
            return None
        now = _utcnow()
        key = {"repo": repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
        existing = self.collection.find_one(key)
        if existing and existing.get("isActive", True) and existing.get("expiresAt", now) > now:
            return existing
        items = [_context_item(index, item) for index, item in enumerate(notice.responsibilityItems, 1)]
        for _ in range(8):
            code = "CI-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))
            doc = {
                **key,
                "code": code,
                "responsibilityItems": items,
                "createdAt": now,
                "updatedAt": now,
                "expiresAt": now + self.ttl,
                "isActive": True,
            }
            try:
                if existing:
                    self.collection.update_one(
                        {**key, "code": existing.get("code")}, {"$set": doc}, upsert=False
                    )
                else:
                    self.collection.update_one(key, {"$setOnInsert": doc}, upsert=True)
                stored = self.collection.find_one(key)
                if stored and stored.get("isActive", True) and stored.get("expiresAt", now) > now:
                    return stored
            except Exception as exc:
                if "duplicate" not in str(exc).lower() and "e11000" not in str(exc).lower():
                    raise
                concurrent = self.collection.find_one(key)
                if concurrent and concurrent.get("isActive", True) and concurrent.get("expiresAt", now) > now:
                    return concurrent
        raise RuntimeError("unable to allocate a unique feedback code")

    def get_active(self, code: str) -> dict[str, Any] | None:
        now = _utcnow()
        doc = self.collection.find_one({"code": str(code).upper(), "isActive": True})
        if not doc or doc.get("expiresAt", now) <= now:
            return None
        return doc

    def resolve_item(self, code: str, item_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
        context = self.get_active(code)
        if context is None:
            raise ValueError("反馈码不存在或已过期。")
        items = context.get("responsibilityItems") or []
        if item_index < 1 or item_index > len(items):
            raise ValueError(f"责任项序号越界，有效范围为 1-{len(items)}。")
        return context, items[item_index - 1]


def _context_item(index: int, item: Any) -> dict[str, Any]:
    owner = item.owner.model_dump(mode="json") if getattr(item, "owner", None) else None
    return {
        "itemIndex": index,
        "failureId": item.failureId,
        "failureSignature": item.failureSignature,
        "failureTitle": item.failureTitle,
        "owner": owner,
    }


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
