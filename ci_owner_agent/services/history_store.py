from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from typing import Any

from ci_owner_agent.config import Settings
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice
from ci_owner_agent.services.failure_similarity import hash_normalized_chunk, normalize_error_chunk

HISTORY_CHUNK_SCHEMA_VERSION = 3
ALLOWED_HISTORY_CHUNK_SOURCES = {
    "local_test_failure_summary",
    "local_make_docker_test_failure_summary",
    "jenkins_test_failure_summary",
    "jenkins_failed_stage_failure_summary",
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
        self.notifications = self.db["ci_notifications"]
        self.feedback = self.db["ci_feedback"]
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
        self.notifications.create_index([("job", 1), ("branch", 1), ("buildNumber", 1), ("noticeHash", 1), ("channel", 1)])
        self.feedback.create_index([("job", 1), ("branch", 1), ("buildNumber", 1), ("failureId", 1)])
        self.feedback.create_index([("job", 1), ("branch", 1), ("failureSignature", 1), ("isActive", 1)])

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
        item_summary = _responsibility_item_summary(notice_doc.get("responsibilityItems") or [])
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
                    **item_summary,
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
                        "signature": chunk.get("signature"),
                        "signatureHash": chunk.get("signatureHash"),
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
        feedback_docs = _active_feedback_docs(self, job, branch)
        for chunk in chunks:
            build_number = chunk.get("buildNumber")
            chunk["build"] = build_by_number.get(build_number, {})
            chunk["noticeDoc"] = notices.get(build_number, {})
            chunk["feedbackOverride"] = _find_feedback_override(
                feedback_docs,
                job=job,
                branch=chunk.get("branch"),
                build_number=build_number,
                signature_hash=chunk.get("signatureHash"),
                signature=chunk.get("signature") or {},
                notice_doc=chunk.get("noticeDoc") or {},
            )
        return chunks

    def notification_sent(self, *, job: str, branch: str | None, build_number: int, notice_hash: str, channel: str = "wecom") -> bool:
        return self.notifications.find_one(
            {"job": job, "branch": branch, "buildNumber": build_number, "noticeHash": notice_hash, "channel": channel, "status": {"$in": ["sent", "skipped"]}}
        ) is not None

    def save_notification(self, *, notice: CiResponsibilityNotice, notice_hash: str, channel: str, status: str, message: str, error: str | None = None) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        key = {
            "job": notice.job,
            "branch": notice.branch,
            "buildNumber": notice.buildNumber,
            "noticeHash": notice_hash,
            "channel": channel,
        }
        doc = {
            **key,
            "status": status,
            "messagePreview": message[:1000],
            "error": error,
            "updatedAt": now,
        }
        self.notifications.update_one(key, {"$set": {**doc, "createdAt": now}}, upsert=True)
        return {"ok": True, **doc}


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


def _responsibility_item_summary(items: list[dict]) -> dict[str, Any]:
    responsible_names: list[str] = []
    responsible_types: list[str] = []
    inherited_count = 0
    current_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        responsibility_type = str(item.get("responsibilityType") or "")
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        owner_type = str(owner.get("type") or "")
        owner_name = str(owner.get("name") or "")
        if responsibility_type == "inherited_failure_owner":
            inherited_count += 1
        if responsibility_type == "current_build_owner":
            current_count += 1
        if owner_type != "no_high_confidence_owner" and owner_name and owner_name != "无高可信责任人":
            responsible_names.append(owner_name)
            responsible_types.append(owner_type)
    return {
        "responsibilityItemCount": len(items),
        "responsibleOwnerNames": sorted(set(responsible_names)),
        "responsibleOwnerTypes": sorted(set(responsible_types)),
        "inheritedOwnerCount": inherited_count,
        "currentBuildOwnerCount": current_count,
    }


def notice_hash(notice: CiResponsibilityNotice) -> str:
    payload = json.dumps(notice.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _active_feedback_docs(store: MongoHistoryStore, job: str, branch: str | None) -> list[dict]:
    query: dict[str, Any] = {"job": job, "isActive": True}
    docs = list(store.feedback.find(query))
    if branch is None:
        return docs
    return [doc for doc in docs if doc.get("branch") in {branch, None}]


def _find_feedback_override(
    feedback_docs: list[dict],
    *,
    job: str,
    branch: str | None,
    build_number: int | None,
    signature_hash: str | None,
    signature: dict,
    notice_doc: dict,
) -> dict | None:
    possible_signatures = {signature_hash, signature.get("signatureKey"), signature.get("signatureHash")}
    notice = notice_doc.get("notice") if isinstance(notice_doc, dict) else None
    failure_ids: set[str] = set()
    for item in (notice.get("responsibilityItems") if isinstance(notice, dict) else []) or []:
        if item.get("failureSignature") in possible_signatures and item.get("failureId"):
            failure_ids.add(item.get("failureId"))
    for doc in feedback_docs:
        if doc.get("job") != job:
            continue
        if branch is not None and doc.get("branch") not in {branch, None}:
            continue
        if doc.get("failureSignature") and doc.get("failureSignature") in possible_signatures:
            return doc
    for doc in feedback_docs:
        if doc.get("job") == job and doc.get("buildNumber") == build_number and doc.get("failureId") in failure_ids:
            return doc
    return None
