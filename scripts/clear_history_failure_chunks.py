from __future__ import annotations

from ci_owner_agent.config import load_settings


def main() -> int:
    # Run manually after switching history chunks to schemaVersion=3 test_failure_summary data.
    # This deletes old schemaVersion=2 focused tail chunks and earlier noisy windows.
    settings = load_settings()
    try:
        from pymongo import MongoClient
    except Exception as exc:
        print(f"pymongo import failed: {exc}")
        return 2

    client = MongoClient(settings.history_mongo_uri, serverSelectionTimeoutMS=2000)
    result = client[settings.history_mongo_db]["ci_failure_chunks"].delete_many({})
    print(f"deleted ci_failure_chunks: {result.deleted_count}")
    print("Manual equivalent:")
    print(f"use {settings.history_mongo_db}")
    print("db.ci_failure_chunks.deleteMany({})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
