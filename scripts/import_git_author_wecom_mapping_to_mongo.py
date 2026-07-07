from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def parse_bool(value: Any, default: bool = False) -> bool:
    if value in {True, False}:
        return bool(value)
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def parse_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def row_to_doc(row: dict[str, Any], *, allowed_domain: str = "fanruan.com", source: str = "git_author_wecom_mapping.csv") -> dict | None:
    if not row or not any(str(value or "").strip() for value in row.values()):
        return None
    author_name = str(row.get("authorName") or "").strip()
    author_email = str(row.get("authorEmail") or "").strip()
    normalized_email = normalize_email(row.get("normalizedEmail") or author_email)
    mapping_key = str(row.get("mappingKey") or "").strip()
    if not mapping_key and (author_name or normalized_email):
        mapping_key = f"{author_name} <{normalized_email}>"
    wecom_userid = str(row.get("wecomUserId") or "").strip()
    if not (normalized_email or mapping_key or author_name or wecom_userid):
        return None
    email_domain = str(row.get("emailDomain") or "").strip().lower()
    if not email_domain and "@" in normalized_email:
        email_domain = normalized_email.rsplit("@", 1)[1]
    is_allowed = parse_bool(row.get("isAllowedDomain"), email_domain == allowed_domain)
    search_text = " ".join(
        value
        for value in [
            wecom_userid.lower(),
            author_name.lower(),
            normalized_email,
            author_email.lower(),
            email_domain,
        ]
        if value
    )
    return {
        "mappingKey": mapping_key,
        "authorName": author_name,
        "authorEmail": author_email,
        "normalizedEmail": normalized_email,
        "emailDomain": email_domain,
        "isAllowedDomain": is_allowed,
        "commitCount": parse_int(row.get("commitCount")),
        "firstCommitTime": str(row.get("firstCommitTime") or "").strip(),
        "firstCommit": str(row.get("firstCommit") or "").strip(),
        "lastCommitTime": str(row.get("lastCommitTime") or "").strip(),
        "lastCommit": str(row.get("lastCommit") or "").strip(),
        "wecomUserId": wecom_userid,
        "mappingStatus": str(row.get("mappingStatus") or "").strip(),
        "note": str(row.get("note") or "").strip(),
        "searchText": search_text,
        "source": source,
        "updatedAt": dt.datetime.now(dt.timezone.utc),
    }


def read_mapping_csv(path: Path, *, allowed_domain: str = "fanruan.com", limit: int | None = None) -> tuple[list[dict], int]:
    docs: list[dict] = []
    skipped = 0
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        for idx, row in enumerate(csv.DictReader(file)):
            if limit is not None and len(docs) >= limit:
                break
            try:
                doc = row_to_doc(row, allowed_domain=allowed_domain, source=path.name)
            except Exception:
                doc = None
            if doc is None:
                skipped += 1
                continue
            docs.append(doc)
    return docs, skipped


def upsert_docs(collection, docs: list[dict], *, clear: bool = False, dry_run: bool = False) -> dict[str, int]:
    if dry_run:
        return {"upserted": 0, "skipped": 0}
    if clear:
        collection.delete_many({})
    upserted = 0
    skipped = 0
    for doc in docs:
        key = _upsert_key(doc)
        if key is None:
            skipped += 1
            continue
        collection.update_one(key, {"$set": doc}, upsert=True)
        upserted += 1
    return {"upserted": upserted, "skipped": skipped}


def _upsert_key(doc: dict) -> dict | None:
    if doc.get("normalizedEmail"):
        return {"normalizedEmail": doc["normalizedEmail"]}
    if doc.get("mappingKey"):
        return {"mappingKey": doc["mappingKey"]}
    if doc.get("authorName") or doc.get("wecomUserId"):
        return {"authorName": doc.get("authorName"), "wecomUserId": doc.get("wecomUserId")}
    return None


def stats_for_docs(docs: list[dict], skipped: int, collection_name: str, *, allowed_domain: str = "fanruan.com") -> dict:
    mapped_users = {doc.get("wecomUserId") for doc in docs if doc.get("wecomUserId")}
    allowed = [doc for doc in docs if doc.get("emailDomain") == allowed_domain]
    return {
        "rowsRead": len(docs) + skipped,
        "rowsImported": len(docs),
        "rowsSkipped": skipped,
        "mappedUsersCount": len(mapped_users),
        "fanruanEmailsCount": len(allowed),
        "nonFanruanEmailsCount": len(docs) - len(allowed),
        "collection": collection_name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-file", required=True)
    parser.add_argument("--mongo-uri", default=None)
    parser.add_argument("--mongo-db", default=None)
    parser.add_argument("--collection", default="ci_wecom_users")
    parser.add_argument("--allowed-domain", default="fanruan.com")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    if load_dotenv is not None:
        load_dotenv(override=False)

    csv_file = Path(args.csv_file).expanduser().resolve()
    docs, skipped = read_mapping_csv(csv_file, allowed_domain=args.allowed_domain.lower().lstrip("@"), limit=args.limit)
    allowed_domain = args.allowed_domain.lower().lstrip("@")
    stats = stats_for_docs(docs, skipped, args.collection, allowed_domain=allowed_domain)

    if not args.dry_run:
        try:
            from pymongo import MongoClient
        except Exception as exc:
            raise SystemExit(f"pymongo is required: {exc}") from exc
        mongo_uri = args.mongo_uri or os.getenv("CI_AGENT_HISTORY_MONGO_URI") or "mongodb://localhost:27017"
        mongo_db = args.mongo_db or os.getenv("CI_AGENT_HISTORY_MONGO_DB") or "ci_owner_agent"
        client = MongoClient(mongo_uri)
        result = upsert_docs(client[mongo_db][args.collection], docs, clear=args.clear, dry_run=False)
        stats["rowsUpserted"] = result["upserted"]
        stats["rowsSkipped"] += result["skipped"]
    else:
        stats["rowsUpserted"] = 0

    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
