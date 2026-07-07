from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ci_owner_agent.constants import NO_OWNER_NAME


@dataclass(frozen=True)
class WeComUserMappingEntry:
    mapping_key: str
    author_name: str
    author_email: str
    normalized_email: str
    wecom_userid: str
    mapping_status: str
    note: str


class WeComUserMapper:
    def __init__(self, entries: list[WeComUserMappingEntry]) -> None:
        self.entries = entries
        self.by_email: dict[str, str] = {}
        self.by_mapping_key: dict[str, str] = {}
        self.by_name: dict[str, str] = {}
        for entry in entries:
            userid = entry.wecom_userid.strip()
            if not userid:
                continue
            if entry.normalized_email and entry.normalized_email not in self.by_email:
                self.by_email[entry.normalized_email] = userid
            if entry.mapping_key and entry.mapping_key not in self.by_mapping_key:
                self.by_mapping_key[entry.mapping_key] = userid
            if entry.author_name and entry.author_name not in self.by_name:
                self.by_name[entry.author_name] = userid

    @classmethod
    def from_csv(cls, path: str | Path | None) -> "WeComUserMapper":
        if not path:
            return cls([])
        csv_path = Path(path)
        if not csv_path.exists():
            return cls([])
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
        except Exception:
            return cls([])
        entries: list[WeComUserMappingEntry] = []
        for row in rows:
            if not row or not any(str(value or "").strip() for value in row.values()):
                continue
            author_name = str(row.get("authorName") or "").strip()
            author_email = str(row.get("authorEmail") or "").strip()
            normalized_email = normalize_email(row.get("normalizedEmail") or author_email)
            mapping_key = str(row.get("mappingKey") or "").strip() or build_mapping_key(author_name, normalized_email)
            entries.append(
                WeComUserMappingEntry(
                    mapping_key=mapping_key,
                    author_name=author_name,
                    author_email=author_email,
                    normalized_email=normalized_email,
                    wecom_userid=str(row.get("wecomUserId") or "").strip(),
                    mapping_status=str(row.get("mappingStatus") or "").strip(),
                    note=str(row.get("note") or "").strip(),
                )
            )
        return cls(entries)

    def resolve_owner(self, owner_name: str | None, owner_email: str | None) -> str | None:
        name = str(owner_name or "").strip()
        email = normalize_email(owner_email)
        if email and email in self.by_email:
            return self.by_email[email]
        mapping_key = build_mapping_key(name, email)
        if mapping_key in self.by_mapping_key:
            return self.by_mapping_key[mapping_key]
        if name and name in self.by_name:
            return self.by_name[name]
        return None

    def mention_owner(self, owner_name: str | None, owner_email: str | None, *, mode: str = "userid") -> str:
        name = str(owner_name or "").strip()
        if not name or name == NO_OWNER_NAME:
            return NO_OWNER_NAME
        if mode == "name":
            return f"@{name}"
        userid = self.resolve_owner(name, owner_email)
        return f"<@{userid}>" if userid else f"@{name}"


def normalize_email(email: str | None) -> str:
    return str(email or "").strip().lower()


def build_mapping_key(author_name: str | None, normalized_email: str | None) -> str:
    return f"{str(author_name or '').strip()} <{normalize_email(normalized_email)}>"
