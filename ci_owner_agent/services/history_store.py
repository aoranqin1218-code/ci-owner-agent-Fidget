from __future__ import annotations

import datetime as dt
import sys
from typing import Any

from ci_owner_agent.config import Settings
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice
from ci_owner_agent.services.failure_similarity import hash_normalized_chunk, normalize_error_chunk

HISTORY_CHUNK_SCHEMA_VERSION = 2
ALLOWED_HISTORY_CHUNK_SOURCES = {
    "local_test_stage_tail",
    "local_make_docker_test_tail",
    "jenkins_test_stage_tail",
    "jenkins_failed_stage_log",
    "notice_failure_summary",
}
DEFAULT_EXCLUDED_CHUNK_SOURCES = {"local_console_tail_fallback"}


class MongoHistoryStore:
    def __init__(self, uri: str, db_name: str, client: Any | None = None) -> None:
        if client is None:
            try:
                from pymongo import MongoClient
            except Exception as exc:
                raise RuntimeError(f"pymongo is not installed: {exc}") from exc
            client = MongoClient(uri, serverSelectionTimeoutMS=2000)
        self.client = client
        self.db = client[db_name]
        self.builds = self.db["ci_builds"]
        self.notices = self.db["ci_notices"]
        self.failure_chunks = self.db["ci_failure_chunks"]
        self.ensure_indexes()

    @classmethod
    def from_settings(cls, settings: Settings) -> "MongoHistoryStore | None":
        if not settings.history_enabled:
            return None
        return cls(settings.history_mongo_uri, settings.history_mongo_db)

    def ensure_indexes(self) -> None:
        self.builds.create_index([("job", 1), ("branch", 1), ("buildNumber", 1)], unique=True)
        self.builds.create_index([("job", 1), ("branch", 1), ("result", 1), ("buildNumber", -1)])
        self.notices.create_index([("job", 1), ("branch", 1), ("buildNumber", 1)], unique=True)
        self.failure_chunks.create_index([("job", 1), ("branch", 1), ("buildNumber", 1)])
        self.failure_chunks.create_index([("job", 1), ("branch", 1), ("chunkHash", 1)])

    def save_analysis(
        self,
        build_info: BuildInfo,
        notice: CiResponsibilityNotice,
        base_commit: str | None,
        head_commit: str | None,
        last_successful_build_number: int | None,
        last_successful_commit: str | None,
        error_chunks: list[dict],
    ) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        branch = build_info.branch
        key = {"job": build_info.job, "branch": branch, "buildNumber": build_info.buildNumber}
        self.builds.update_one(
            key,
            {
                "$set": {
                    **key,
                    "result": build_info.result,
                    "baseCommit": base_commit,
                    "headCommit": head_commit or build_info.commit,
                    "lastSuccessfulBuildNumber": last_successful_build_number,
                    "lastSuccessfulCommit": last_successful_commit,
                    "buildUrl": build_info.buildUrl,
                    "createdAt": now,
                }
            },
            upsert=True,
        )
        notice_doc = notice.model_dump(mode="json")
        self.notices.update_one(
            key,
            {
                "$set": {
                    **key,
                    "notice": notice_doc,
                    "ownerType": notice.owner.type,
                    "ownerName": notice.owner.name,
                    "ownerEmail": notice.owner.email,
                    "ownerCommit": notice.owner.commit,
                    "hasHighConfidenceOwner": notice.hasHighConfidenceOwner,
                    "failureReason": notice.failureReason,
                    "createdAt": now,
                }
            },
            upsert=True,
        )
        self.failure_chunks.delete_many(key)
        chunks_to_save = [chunk for chunk in error_chunks if _is_allowed_history_chunk(chunk)]
        for idx, chunk in enumerate(chunks_to_save):
            text = str(chunk.get("content") or chunk.get("chunkText") or "")
            normalized = normalize_error_chunk(text)
            self.failure_chunks.update_one(
                {**key, "chunkIndex": idx},
                {
                    "$set": {
                        **key,
                        "chunkIndex": idx,
                        "schemaVersion": int(chunk.get("schemaVersion") or HISTORY_CHUNK_SCHEMA_VERSION),
                        "chunkSource": chunk.get("chunkSource"),
                        "stageName": chunk.get("stageName"),
                        "stepName": chunk.get("stepName"),
                        "anchorType": chunk.get("anchorType"),
                        "startLine": chunk.get("startLine"),
                        "endLine": chunk.get("endLine"),
                        "score": chunk.get("score"),
                        "chunkText": text,
                        "normalizedChunk": normalized,
                        "chunkHash": hash_normalized_chunk(text),
                        "createdAt": now,
                    }
                },
                upsert=True,
            )
        return {"ok": True, "chunksSaved": len(chunks_to_save), "inputChunks": len(error_chunks)}

    def find_historical_failure_chunks(
        self,
        job: str,
        branch: str | None,
        current_build_number: int,
        last_successful_build_number: int | None,
        lookback_builds: int = 20,
    ) -> list[dict]:
        build_query: dict[str, Any] = {
            "job": job,
            "buildNumber": {"$lt": current_build_number},
            "result": {"$in": ["FAILURE", "UNSTABLE", "UNKNOWN"]},
        }
        if branch is not None:
            build_query["branch"] = {"$in": [branch, None]}
        if last_successful_build_number is not None:
            build_query["buildNumber"]["$gt"] = last_successful_build_number

        builds = list(self.builds.find(build_query).sort("buildNumber", -1).limit(max(1, lookback_builds)))
        build_numbers = [item.get("buildNumber") for item in builds if item.get("buildNumber") is not None]
        if not build_numbers:
            return []

        chunk_query: dict[str, Any] = {
            "job": job,
            "buildNumber": {"$in": build_numbers},
            "schemaVersion": {"$gte": HISTORY_CHUNK_SCHEMA_VERSION},
            "chunkSource": {"$in": sorted(ALLOWED_HISTORY_CHUNK_SOURCES)},
        }
        if branch is not None:
            chunk_query["branch"] = {"$in": [branch, None]}
        chunks = list(self.failure_chunks.find(chunk_query))
        notice_query: dict[str, Any] = {"job": job, "buildNumber": {"$in": build_numbers}}
        if branch is not None:
            notice_query["branch"] = {"$in": [branch, None]}
        notices = {item.get("buildNumber"): item for item in self.notices.find(notice_query)}
        build_by_number = {item.get("buildNumber"): item for item in builds}
        for chunk in chunks:
            build_number = chunk.get("buildNumber")
            chunk["build"] = build_by_number.get(build_number, {})
            chunk["noticeDoc"] = notices.get(build_number, {})
        return chunks


def get_history_store(settings: Settings) -> MongoHistoryStore | None:
    if not settings.history_enabled:
        return None
    try:
        return MongoHistoryStore.from_settings(settings)
    except Exception as exc:
        print(f"history store unavailable: {exc}", file=sys.stderr)
        return None


def _is_allowed_history_chunk(chunk: dict) -> bool:
    return (
        int(chunk.get("schemaVersion") or HISTORY_CHUNK_SCHEMA_VERSION) >= HISTORY_CHUNK_SCHEMA_VERSION
        and chunk.get("chunkSource") in ALLOWED_HISTORY_CHUNK_SOURCES
        and chunk.get("chunkSource") not in DEFAULT_EXCLUDED_CHUNK_SOURCES
        and bool(str(chunk.get("content") or chunk.get("chunkText") or "").strip())
    )

