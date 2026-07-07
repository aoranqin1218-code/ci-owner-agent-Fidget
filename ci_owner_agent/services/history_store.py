from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from typing import Any

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.config import Settings
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice, FailureFact
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
        self.failure_facts = self.db["ci_failure_facts"]
        self.notifications = self.db["ci_notifications"]
        self.feedback = self.db["ci_feedback"]
        self.wecom_users = self.db["ci_wecom_users"]
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
        self.failure_facts.create_index([("job", 1), ("branch", 1), ("buildNumber", 1)])
        self.failure_facts.create_index([("job", 1), ("branch", 1), ("factId", 1)])
        self.failure_facts.create_index([("job", 1), ("branch", 1), ("signatureKey", 1)])
        self.failure_facts.create_index([("job", 1), ("branch", 1), ("historyEligible", 1), ("buildNumber", 1)])
        self.notifications.create_index([("job", 1), ("branch", 1), ("buildNumber", 1), ("noticeHash", 1), ("channel", 1)])
        self.feedback.create_index([("job", 1), ("branch", 1), ("buildNumber", 1), ("failureId", 1)])
        self.feedback.create_index([("job", 1), ("branch", 1), ("failureSignature", 1), ("isActive", 1)])
        self.wecom_users.create_index([("wecomUserId", 1)])
        self.wecom_users.create_index([("normalizedEmail", 1)])
        self.wecom_users.create_index([("authorName", 1)])
        self.wecom_users.create_index([("emailDomain", 1)])
        self.wecom_users.create_index([("searchText", 1)])

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

    def save_failure_facts(
        self,
        *,
        build_info: BuildInfo,
        notice: CiResponsibilityNotice,
        facts: list[FailureFact],
    ) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        key = {"job": build_info.job, "branch": build_info.branch, "buildNumber": build_info.buildNumber}
        self.failure_facts.delete_many(key)
        notice_doc = notice.model_dump(mode="json")
        for idx, fact in enumerate(facts):
            fact_doc = fact.model_dump(mode="json")
            fact_owner = _fact_owner_for_notice(fact, notice_doc)
            self.failure_facts.update_one(
                {**key, "factIndex": idx},
                {
                    "$set": {
                        **key,
                        "factIndex": idx,
                        "buildUrl": build_info.buildUrl,
                        "headCommit": notice.headCommit or build_info.commit,
                        "factId": fact.factId,
                        "signatureKey": fact.signatureKey,
                        "historyEligible": fact.historyEligible,
                        "isGenericWrapper": fact.isGenericWrapper,
                        "failureKind": fact.failureKind,
                        "fact": fact_doc,
                        "notice": notice_doc,
                        "ownerType": notice.owner.type,
                        "ownerName": notice.owner.name,
                        "ownerEmail": notice.owner.email,
                        "ownerCommit": notice.owner.commit,
                        "hasHighConfidenceOwner": notice.hasHighConfidenceOwner,
                        "factOwner": fact_owner,
                        "createdAt": now,
                    }
                },
                upsert=True,
            )
        return {"ok": True, "factsSaved": len(facts)}

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

    def find_historical_failure_facts(
        self,
        *,
        job: str,
        branch: str | None,
        current_build_number: int,
        last_successful_build_number: int | None,
        lookback_builds: int = 20,
        max_facts: int = 20,
    ) -> list[dict]:
        return self.find_historical_failure_facts_with_diagnostics(
            job=job,
            branch=branch,
            current_build_number=current_build_number,
            last_successful_build_number=last_successful_build_number,
            lookback_builds=lookback_builds,
            max_facts=max_facts,
        )["facts"]

    def find_historical_failure_facts_with_diagnostics(
        self,
        *,
        job: str,
        branch: str | None,
        current_build_number: int,
        last_successful_build_number: int | None,
        lookback_builds: int = 20,
        max_facts: int = 20,
    ) -> dict:
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
        diagnostics: dict[str, Any] = {
            "buildQuery": build_query,
            "historicalBuildsCount": len(builds),
            "historicalBuildNumbers": build_numbers[:20],
            "factQuery": None,
            "historicalFactsCount": 0,
            "queryStage": "build_query" if not build_numbers else "fact_query",
        }
        if not build_numbers:
            return {"facts": [], "diagnostics": diagnostics}

        fact_query: dict[str, Any] = {
            "job": job,
            "buildNumber": {"$in": build_numbers},
            "historyEligible": True,
            "isGenericWrapper": False,
        }
        if branch is not None:
            fact_query["branch"] = {"$in": [branch, None]}
        facts = list(self.failure_facts.find(fact_query))
        facts.sort(key=lambda item: (item.get("buildNumber") or 0, item.get("factIndex") or 0), reverse=True)
        diagnostics["factQuery"] = fact_query
        diagnostics["historicalFactsCount"] = len(facts)
        diagnostics["historicalFactBuildNumbers"] = [item.get("buildNumber") for item in facts[:20]]
        diagnostics["queryStage"] = "ok" if facts else "fact_query"
        return {"facts": facts[: max(1, max_facts)], "diagnostics": diagnostics}

    def notification_sent(self, *, job: str, branch: str | None, build_number: int, notice_hash: str, channel: str = "wecom") -> bool:
        return self.notifications.find_one(
            {"job": job, "branch": branch, "buildNumber": build_number, "noticeHash": notice_hash, "channel": channel, "status": "sent"}
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
        self.notifications.update_one(key, {"$set": doc, "$setOnInsert": {"createdAt": now}}, upsert=True)
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
        if owner_type != "no_high_confidence_owner" and owner_name and owner_name != NO_OWNER_NAME:
            responsible_names.append(owner_name)
            responsible_types.append(owner_type)
    return {
        "responsibilityItemCount": len(items),
        "responsibleOwnerNames": sorted(set(responsible_names)),
        "responsibleOwnerTypes": sorted(set(responsible_types)),
        "inheritedOwnerCount": inherited_count,
        "currentBuildOwnerCount": current_count,
    }


def _fact_owner_for_notice(fact: FailureFact, notice_doc: dict) -> dict[str, Any] | None:
    for item in notice_doc.get("responsibilityItems") or []:
        if not isinstance(item, dict):
            continue
        if item.get("failureSignature") == fact.signatureKey:
            owner = item.get("owner")
            return owner if isinstance(owner, dict) else None
    owner = notice_doc.get("owner")
    return owner if isinstance(owner, dict) else None


def notice_hash(notice: CiResponsibilityNotice) -> str:
    payload = json.dumps(notice.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _active_feedback_docs(store: MongoHistoryStore, job: str, branch: str | None) -> list[dict]:
    query: dict[str, Any] = {"job": job, "isActive": True}
    docs = list(store.feedback.find(query))
    if branch is None:
        return docs
    return [doc for doc in docs if doc.get("branch") in {branch, None}]


def find_feedback_override_for_failure_signature(
    store: MongoHistoryStore,
    *,
    job: str,
    branch: str | None,
    build_number: int | None,
    failure_signature: str | None,
    notice_doc: dict | None,
) -> dict | None:
    if not failure_signature:
        return None
    feedback_docs = _active_feedback_docs(store, job, branch)
    return _find_feedback_override(
        feedback_docs,
        job=job,
        branch=branch,
        build_number=build_number,
        signature_hash=None,
        signature={"signatureKey": failure_signature},
        notice_doc=notice_doc or {},
    )


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
