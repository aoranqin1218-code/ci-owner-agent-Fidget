from __future__ import annotations

import datetime as dt
from typing import Any

from ci_owner_agent.services.history_store import MongoHistoryStore
from ci_owner_agent.services.wecom_user_mapping import NO_OWNER_NAME, WeComUserMapper, WeComUserMappingEntry, build_mapping_key, normalize_email


class MongoWeComUserMapper:
    def __init__(self, history_store: MongoHistoryStore, *, allowed_domain: str = "fanruan.com") -> None:
        self.history_store = history_store
        self.collection = history_store.wecom_users
        self.allowed_domain = allowed_domain.lower().lstrip("@")

    def resolve_owner(self, owner_name: str | None, owner_email: str | None) -> WeComUserMappingEntry | None:
        name = str(owner_name or "").strip()
        email = normalize_email(owner_email)
        if not name and not email:
            return None
        try:
            docs = list(self.collection.find({}))
        except Exception:
            return None
        candidates: list[tuple[tuple, dict[str, Any]]] = []
        mapping_key = build_mapping_key(name, email)
        for doc in docs:
            tier = _match_tier(doc, name=name, email=email, mapping_key=mapping_key)
            if tier is None:
                continue
            candidates.append((_sort_key(doc, tier, self.allowed_domain), doc))
        if not candidates:
            return None
        _, selected = min(candidates, key=lambda item: item[0])
        userid = str(selected.get("wecomUserId") or "").strip()
        if not userid:
            return None
        author_name = str(selected.get("authorName") or "").strip()
        normalized_email = normalize_email(selected.get("normalizedEmail") or selected.get("authorEmail"))
        return WeComUserMappingEntry(
            mapping_key=str(selected.get("mappingKey") or "").strip() or build_mapping_key(author_name, normalized_email),
            author_name=author_name,
            author_email=str(selected.get("authorEmail") or "").strip(),
            normalized_email=normalized_email,
            wecom_userid=userid,
            mapping_status=str(selected.get("mappingStatus") or "").strip(),
            note=str(selected.get("note") or "").strip(),
        )

    def mention_owner(self, owner_name: str | None, owner_email: str | None, *, mode: str = "userid") -> str:
        name = str(owner_name or "").strip()
        if not name or name == NO_OWNER_NAME:
            return NO_OWNER_NAME
        if mode == "name":
            return f"@{name}"
        entry = self.resolve_owner(name, owner_email)
        return f"<@{entry.wecom_userid}>" if entry and entry.wecom_userid else f"@{name}"


class CompositeWeComUserMapper:
    def __init__(self, mappers: list[Any]) -> None:
        self.mappers = [mapper for mapper in mappers if mapper is not None]

    def resolve_owner(self, owner_name: str | None, owner_email: str | None) -> Any | None:
        for mapper in self.mappers:
            try:
                entry = mapper.resolve_owner(owner_name, owner_email)
            except Exception:
                entry = None
            if _entry_userid(entry):
                return entry
        return None

    def mention_owner(self, owner_name: str | None, owner_email: str | None, *, mode: str = "userid") -> str:
        name = str(owner_name or "").strip()
        if not name or name == NO_OWNER_NAME:
            return NO_OWNER_NAME
        if mode == "name":
            return f"@{name}"
        entry = self.resolve_owner(name, owner_email)
        userid = _entry_userid(entry)
        return f"<@{userid}>" if userid else f"@{name}"


def _match_tier(doc: dict[str, Any], *, name: str, email: str, mapping_key: str) -> int | None:
    normalized_email = normalize_email(doc.get("normalizedEmail") or doc.get("authorEmail"))
    doc_mapping_key = str(doc.get("mappingKey") or "").strip()
    author_name = str(doc.get("authorName") or "").strip()
    if email and normalized_email == email:
        return 0
    if email and doc_mapping_key in {email, mapping_key}:
        return 1
    if name and doc_mapping_key == name:
        return 1
    if name and author_name == name:
        return 2
    return None


def _sort_key(doc: dict[str, Any], tier: int, allowed_domain: str) -> tuple:
    userid = str(doc.get("wecomUserId") or "").strip()
    domain = str(doc.get("emailDomain") or "").strip().lower()
    if not domain:
        email = normalize_email(doc.get("normalizedEmail") or doc.get("authorEmail"))
        domain = email.rsplit("@", 1)[1] if "@" in email else ""
    return (
        tier,
        0 if userid else 1,
        0 if domain == allowed_domain else 1,
        -_commit_count(doc),
        -_updated_at_timestamp(doc.get("updatedAt")),
    )


def _commit_count(doc: dict[str, Any]) -> int:
    try:
        return int(doc.get("commitCount") or 0)
    except Exception:
        return 0


def _updated_at_timestamp(value: Any) -> float:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.timestamp()
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _entry_userid(entry: Any) -> str | None:
    if not entry:
        return None
    if isinstance(entry, str):
        return entry.strip() or None
    userid = getattr(entry, "wecom_userid", None)
    if userid:
        return str(userid).strip() or None
    if isinstance(entry, dict):
        value = entry.get("wecomUserId") or entry.get("wecom_userid")
        return str(value).strip() if value else None
    return None


def build_wecom_notice_mapper(settings, store: MongoHistoryStore | None) -> CompositeWeComUserMapper | WeComUserMapper:
    csv_mapper = WeComUserMapper.from_csv(settings.wecom_user_mapping_file)
    if store is None:
        return csv_mapper
    return CompositeWeComUserMapper([MongoWeComUserMapper(store), csv_mapper])
