from __future__ import annotations

import re
from typing import Any

from ci_owner_agent.services.history_store import MongoHistoryStore
from ci_owner_agent.services.wecom_user_mapping import normalize_email


class WeComUserDirectory:
    def __init__(self, history_store: MongoHistoryStore, allowed_domain: str = "fanruan.com") -> None:
        self.history_store = history_store
        self.collection = history_store.wecom_users
        self.allowed_domain = allowed_domain.lower().lstrip("@")

    def search_users(self, query: str, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit or 20), 50))
        query_text = str(query or "").strip().lower()
        docs = [doc for doc in self.collection.find({}) if str(doc.get("wecomUserId") or "").strip()]
        if query_text:
            pattern = re.compile(re.escape(query_text), re.I)
            matched_userids = {str(doc.get("wecomUserId") or "").strip() for doc in docs if _matches_query(doc, pattern)}
            docs = [doc for doc in docs if str(doc.get("wecomUserId") or "").strip() in matched_userids]
        grouped = _group_by_userid(docs, self.allowed_domain)
        grouped.sort(key=lambda item: item.get("commitCount") or 0, reverse=True)
        return grouped[:limit]

    def resolve_preferred_email(self, *, wecom_userid: str | None = None, author_name: str | None = None) -> str | None:
        userid = str(wecom_userid or "").strip()
        name = str(author_name or "").strip()
        docs = [doc for doc in self.collection.find({}) if str(doc.get("wecomUserId") or "").strip()]
        if userid:
            docs = [doc for doc in docs if str(doc.get("wecomUserId") or "") == userid]
        elif name:
            docs = [doc for doc in docs if str(doc.get("authorName") or "") == name]
        else:
            return None
        grouped = _group_by_userid(docs, self.allowed_domain)
        return grouped[0].get("preferredEmail") if grouped else None


def _matches_query(doc: dict[str, Any], pattern: re.Pattern[str]) -> bool:
    fields = [
        doc.get("wecomUserId"),
        doc.get("authorName"),
        doc.get("normalizedEmail"),
        doc.get("authorEmail"),
        doc.get("searchText"),
    ]
    return any(pattern.search(str(value or "")) for value in fields)


def _group_by_userid(docs: list[dict[str, Any]], allowed_domain: str) -> list[dict]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        userid = str(doc.get("wecomUserId") or "").strip()
        if not userid:
            continue
        groups.setdefault(userid, []).append(doc)
    return [_group_to_item(userid, items, allowed_domain) for userid, items in groups.items()]


def _group_to_item(userid: str, docs: list[dict[str, Any]], allowed_domain: str) -> dict:
    names = _unique(str(doc.get("authorName") or "").strip() for doc in docs if str(doc.get("authorName") or "").strip())
    emails = _unique(_display_email(doc) for doc in docs if _display_email(doc))
    display_name = _display_name(docs, names, userid)
    preferred_email = _preferred_email(docs, allowed_domain)
    return {
        "wecomUserId": userid,
        "displayName": display_name,
        "preferredEmail": preferred_email,
        "emails": emails,
        "authorNames": names,
        "commitCount": sum(_commit_count(doc) for doc in docs),
    }


def _display_name(docs: list[dict[str, Any]], names: list[str], userid: str) -> str:
    for name in names:
        if re.search(r"[\u4e00-\u9fff]", name):
            return name
    if not docs:
        return userid
    best = max(docs, key=_commit_count)
    return str(best.get("authorName") or "").strip() or userid


def _preferred_email(docs: list[dict[str, Any]], allowed_domain: str) -> str | None:
    with_email = [doc for doc in docs if normalize_email(doc.get("normalizedEmail") or doc.get("authorEmail"))]
    if not with_email:
        return None
    allowed = [
        doc
        for doc in with_email
        if normalize_email(doc.get("normalizedEmail") or doc.get("authorEmail")).endswith(f"@{allowed_domain}")
    ]
    selected = max(allowed or with_email, key=_commit_count)
    return _display_email(selected)


def _display_email(doc: dict[str, Any]) -> str:
    return normalize_email(doc.get("normalizedEmail") or doc.get("authorEmail"))


def _commit_count(doc: dict[str, Any]) -> int:
    try:
        return int(doc.get("commitCount") or 0)
    except Exception:
        return 0


def _unique(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
