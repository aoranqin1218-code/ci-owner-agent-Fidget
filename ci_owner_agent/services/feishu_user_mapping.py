"""Feishu open_id mapping for real member @ in notifications.

Feishu custom bots can only @ a member by open_id (``ou_`` prefix) or ``"all"``,
and have no directory access, so open_ids must come from an explicit mapping.
This module loads a small YAML mapping file and resolves exact name/email matches
only; there is never similarity guessing. Entries whose open_id is missing or
malformed are treated as unmapped so callers degrade to plain name text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9]+$")


def is_valid_open_id(value: str | None) -> bool:
    """Return True only for a Feishu open_id shape (``ou_`` prefix)."""
    return bool(value) and bool(_OPEN_ID_RE.fullmatch(str(value).strip()))


@dataclass(frozen=True)
class FeishuUserMappingEntry:
    """One explicit author/maintainer to Feishu open_id mapping."""

    name: str
    email: str | None = None
    open_id: str | None = None


class FeishuUserMapper:
    """Resolve a git author or maintainer name/email to a Feishu open_id.

    Email match takes priority over name match. Only mappings whose open_id is a
    valid ``ou_`` id participate; everything else resolves to ``None``.
    """

    def __init__(
        self,
        entries: list[FeishuUserMappingEntry] | None = None,
        fallback_open_ids: tuple[str, ...] = (),
    ) -> None:
        self._by_email: dict[str, str] = {}
        self._by_name: dict[str, str] = {}
        conflicted_email: set[str] = set()
        conflicted_name: set[str] = set()
        for entry in entries or []:
            if not is_valid_open_id(entry.open_id):
                continue
            if entry.email:
                key = entry.email.strip().lower()
                existing = self._by_email.get(key)
                if existing is not None and existing != entry.open_id:
                    # 同一邮箱映射到不同 open_id：身份冲突，不得任选一个 @。
                    conflicted_email.add(key)
                elif existing is None:
                    self._by_email[key] = entry.open_id
            if entry.name:
                key = entry.name.strip()
                existing = self._by_name.get(key)
                if existing is not None and existing != entry.open_id:
                    conflicted_name.add(key)
                elif existing is None:
                    self._by_name[key] = entry.open_id
        # 冲突键从映射移除，使解析降级为 None（调用方展示姓名文本，不猜身份）。
        for key in conflicted_email:
            self._by_email.pop(key, None)
        for key in conflicted_name:
            self._by_name.pop(key, None)
        self.fallback_open_ids = tuple(dict.fromkeys(oid for oid in fallback_open_ids if is_valid_open_id(oid)))

    @classmethod
    def from_yaml(cls, path: Path | None, fallback_open_ids: tuple[str, ...] = ()) -> "FeishuUserMapper":
        """Build a mapper from a YAML file (``users: [{name, email?, openId}]``)."""
        if not path:
            return cls(fallback_open_ids=fallback_open_ids)
        try:
            raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except Exception:
            return cls(fallback_open_ids=fallback_open_ids)
        users = raw.get("users", []) if isinstance(raw, dict) else []
        entries: list[FeishuUserMappingEntry] = []
        for item in users if isinstance(users, list) else []:
            if not isinstance(item, dict):
                continue
            entries.append(
                FeishuUserMappingEntry(
                    name=str(item.get("name") or "").strip(),
                    email=str(item.get("email") or "").strip() or None,
                    open_id=str(item.get("openId") or "").strip() or None,
                )
            )
        return cls(entries, fallback_open_ids)

    def resolve_open_id(self, name: str | None, email: str | None = None) -> str | None:
        """Return the mapped open_id for exact email/name match, else None."""
        if email:
            hit = self._by_email.get(str(email).strip().lower())
            if hit:
                return hit
        if name:
            return self._by_name.get(str(name).strip())
        return None
