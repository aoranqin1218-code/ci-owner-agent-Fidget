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
from ci_owner_agent.services.failure_identity import build_failure_summary_signature, canonicalize_failure_message
from ci_owner_agent.services.history_inheritance import (
    active_feedback_docs as _active_feedback_docs,
    find_feedback_override as _find_feedback_override,
    find_feedback_override_for_failure_signature,
)
from ci_owner_agent.services.responsibility_signature_enricher import enrich_responsibility_item_signatures
from ci_owner_agent.services.branch_normalization import normalize_branch_name

HISTORY_CHUNK_SCHEMA_VERSION = 3
ALLOWED_HISTORY_CHUNK_SOURCES = {
    "local_test_failure_summary",
    "local_make_docker_test_failure_summary",
    "jenkins_test_failure_summary",
    "jenkins_failed_stage_failure_summary",
    "notice_failure_summary",
}
DEFAULT_EXCLUDED_CHUNK_SOURCES = {"local_console_tail_fallback"}


def _create_index(collection: Any, spec: list[tuple[str, int]], **options: Any) -> Any:
    """Keep lightweight test doubles compatible while preserving real Mongo options."""
    try:
        return collection.create_index(spec, **options)
    except TypeError:
        return collection.create_index(spec, unique=bool(options.get("unique", False)))


def _canonical_history_chunk(chunk: dict) -> dict:
    result = dict(chunk)
    signature = dict(chunk.get("signature") or {})
    signature_key = build_failure_summary_signature(signature)
    signature["signatureKey"] = signature_key
    if signature.get("errorMessage"):
        signature["errorMessage"] = canonicalize_failure_message(signature["errorMessage"])
    result["signature"] = signature
    result["signatureHash"] = hashlib.sha256(signature_key.encode("utf-8")).hexdigest()
    return result


class MongoHistoryStore:
    def __init__(self, uri: str, db_name: str, client: Any | None = None) -> None:
        if client is None:
            try:
                from pymongo import MongoClient
            except Exception as exc:
                raise RuntimeError(f"pymongo is not installed: {exc}") from exc
            client = MongoClient(uri, serverSelectionTimeoutMS=2000, tz_aware=True, tzinfo=dt.timezone.utc)
        self.client = client
        self.db = client[db_name]
        self.builds = self.db["ci_builds"]
        self.notices = self.db["ci_notices"]
        self.failure_chunks = self.db["ci_failure_chunks"]
        self.failure_facts = self.db["ci_failure_facts"]
        self.notifications = self.db["ci_notifications"]
        self.test_file_failures = self.db["ci_test_file_failures"]
        self.report_notifications = self.db["ci_report_notifications"]
        self.feedback = self.db["ci_feedback"]
        self.wecom_users = self.db["ci_wecom_users"]
        self.feedback_contexts = self.db["ci_feedback_contexts"]
        self.wecom_pending_feedback = self.db["ci_wecom_pending_feedback"]
        self.wecom_bot_events = self.db["ci_wecom_bot_events"]
        self.wecom_notification_outbox = self.db["ci_wecom_notification_outbox"]
        self.ensure_indexes()

    @classmethod
    def from_settings(cls, settings: Settings) -> "MongoHistoryStore | None":
        if not settings.history_enabled:
            return None
        return cls(settings.history_mongo_uri, settings.history_mongo_db)

    def ensure_indexes(self) -> None:
        self.builds.create_index([("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1)], unique=True)
        self.builds.create_index([("repo", 1), ("job", 1), ("branch", 1), ("result", 1), ("buildNumber", -1)])
        self.notices.create_index([("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1)], unique=True)
        self.failure_chunks.create_index([("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1)])
        self.failure_chunks.create_index([("repo", 1), ("job", 1), ("branch", 1), ("chunkHash", 1)])
        self.failure_facts.create_index([("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1)])
        self.failure_facts.create_index([("repo", 1), ("job", 1), ("branch", 1), ("factId", 1)])
        self.failure_facts.create_index([("repo", 1), ("job", 1), ("branch", 1), ("signatureKey", 1)])
        self.failure_facts.create_index([("repo", 1), ("job", 1), ("branch", 1), ("historyEligible", 1), ("buildNumber", 1)])
        self.notifications.create_index([("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1), ("noticeHash", 1), ("channel", 1)])
        self.test_file_failures.create_index(
            [("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1), ("testFilePath", 1)], unique=True
        )
        self.test_file_failures.create_index(
            [("repo", 1), ("job", 1), ("branch", 1), ("testFilePath", 1), ("buildNumber", -1)]
        )
        self.test_file_failures.create_index([("buildTimestamp", -1), ("job", 1), ("branch", 1)])
        self.report_notifications.create_index(
            [
                ("notificationType", 1), ("repo", 1), ("job", 1), ("branch", 1),
                ("periodStart", 1), ("periodEnd", 1), ("channel", 1),
            ],
            unique=True,
        )
        self.feedback.create_index([("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1), ("failureId", 1)])
        _create_index(self.feedback, [("operationId", 1)], unique=True)
        _create_index(self.feedback, [("recordType", 1), ("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1), ("feedbackItemKey", 1), ("submittedAt", -1), ("operationId", -1)])
        self.wecom_users.create_index([("wecomUserId", 1)])
        self.wecom_users.create_index([("normalizedEmail", 1)])
        self.wecom_users.create_index([("authorName", 1)])
        self.wecom_users.create_index([("emailDomain", 1)])
        self.wecom_users.create_index([("searchText", 1)])
        self.feedback_contexts.create_index([("code", 1)], unique=True)
        self.feedback_contexts.create_index(
            [("repo", 1), ("job", 1), ("branch", 1), ("buildNumber", 1)], unique=True
        )
        _create_index(self.feedback_contexts, [("expiresAt", 1)], expireAfterSeconds=0)
        self.wecom_pending_feedback.create_index([("confirmationCode", 1)], unique=True)
        self.wecom_pending_feedback.create_index([("cardTaskId", 1)], unique=True)
        self.wecom_pending_feedback.create_index([("eventKey", 1)], unique=True)
        _create_index(self.wecom_pending_feedback, [("status", 1), ("applyLeaseUntil", 1)])
        _create_index(self.wecom_pending_feedback, [("expiresAt", 1)], expireAfterSeconds=0)
        self.wecom_bot_events.create_index([("eventKey", 1)], unique=True)
        self.wecom_bot_events.create_index([("status", 1), ("leaseUntil", 1)])
        _create_index(self.wecom_bot_events, [("expiresAt", 1)], expireAfterSeconds=0)
        _create_index(self.wecom_notification_outbox, [("deliveryKey", 1)], unique=True)
        self.wecom_notification_outbox.create_index([("status", 1), ("nextAttemptAt", 1), ("createdAt", 1)])
        self.wecom_notification_outbox.create_index([("status", 1), ("leaseUntil", 1)])

    def upsert_notice_snapshot(self, notice: CiResponsibilityNotice, *, source: str | None = None) -> dict[str, Any]:
        """Persist the minimum notice state required for feedback without analysis side effects."""
        now = dt.datetime.now(dt.timezone.utc)
        repo = _required_repo(notice)
        key = {"repo": repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
        doc = {**key, "notice": notice.model_dump(mode="json"), "source": source, "updatedAt": now}
        self.notices.update_one(key, {"$set": doc, "$setOnInsert": {"createdAt": now}}, upsert=True)
        return self.notices.find_one(key) or {**doc, "createdAt": now}

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
        build_timestamp = _utc_datetime(build_info.timestamp)
        canonical_chunks = [_canonical_history_chunk(chunk) for chunk in error_chunks]
        enrich_responsibility_item_signatures(notice, {"chunks": canonical_chunks}, None)
        branch = build_info.branch
        repo = _required_repo(notice)
        key = {"repo": repo, "job": build_info.job, "branch": branch, "buildNumber": build_info.buildNumber}
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
                    "buildTimestamp": build_timestamp,
                    "analyzedAt": now,
                    "updatedAt": now,
                },
                "$setOnInsert": {"createdAt": now},
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
                    "buildTimestamp": build_timestamp,
                    "analyzedAt": now,
                    **item_summary,
                    "updatedAt": now,
                }
                , "$setOnInsert": {"createdAt": now}},
            upsert=True,
        )
        self.failure_chunks.delete_many(key)
        chunks_to_save = [chunk for chunk in canonical_chunks if _is_allowed_history_chunk(chunk)]
        for idx, chunk in enumerate(chunks_to_save):
            text = canonicalize_failure_message(str(chunk.get("content") or chunk.get("chunkText") or ""))
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
        event_result = self.replace_test_file_failures(build_info=build_info, notice=notice)
        return {"ok": True, "chunksSaved": len(chunks_to_save), "inputChunks": len(error_chunks), **event_result}

    def replace_test_file_failures(self, *, build_info: BuildInfo, notice: CiResponsibilityNotice) -> dict:
        from ci_owner_agent.services.responsibility_path_enricher import is_test_file_path

        repo = _required_repo(notice)
        key = {"repo": repo, "job": build_info.job, "branch": build_info.branch, "buildNumber": build_info.buildNumber}
        self.test_file_failures.delete_many(key)
        if str(build_info.result).upper() == "SUCCESS":
            return {"testFileFailuresSaved": 0, "unidentifiedTestFileItems": 0}
        grouped, unidentified = group_test_file_failure_items(notice)
        now = dt.datetime.now(dt.timezone.utc)
        timestamp = _utc_datetime(build_info.timestamp)
        for path, items in grouped.items():
            failure_ids = list(dict.fromkeys(item.failureId for item in items if item.failureId))
            signatures = list(dict.fromkeys(item.failureSignature for item in items if item.failureSignature))
            responsibility_types = list(dict.fromkeys(item.responsibilityType for item in items))
            owner_names = list(dict.fromkeys(
                item.owner.name for item in items
                if item.owner.name and item.owner.name != NO_OWNER_NAME
            ))
            event_key = {**key, "testFilePath": path}
            doc = {
                **event_key,
                "buildUrl": build_info.buildUrl,
                "buildTimestamp": timestamp,
                "failureItemCount": len(items),
                "failureIds": failure_ids,
                "failureSignatures": signatures,
                "responsibilityTypes": responsibility_types,
                "ownerNames": owner_names,
                "updatedAt": now,
            }
            self.test_file_failures.update_one(
                event_key, {"$set": doc, "$setOnInsert": {"createdAt": now}}, upsert=True
            )
        return {"testFileFailuresSaved": len(grouped), "unidentifiedTestFileItems": unidentified}

    def save_failure_facts(
        self,
        *,
        build_info: BuildInfo,
        notice: CiResponsibilityNotice,
        facts: list[FailureFact],
    ) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        enrich_responsibility_item_signatures(
            notice,
            None,
            {"ok": True, "facts": [fact.model_dump(mode="json") for fact in facts]},
        )
        key = {"repo": _required_repo(notice), "job": build_info.job, "branch": build_info.branch, "buildNumber": build_info.buildNumber}
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
        repo: str,
        job: str,
        branch: str | None,
        current_build_number: int,
        last_successful_build_number: int | None,
        lookback_builds: int = 20,
    ) -> list[dict]:
        branch = normalize_branch_name(branch)
        build_query: dict[str, Any] = {
            "repo": repo,
            "job": job,
            "buildNumber": {"$lt": current_build_number},
            "result": {"$in": ["FAILURE", "UNSTABLE", "UNKNOWN"]},
        }
        build_query["branch"] = branch
        if last_successful_build_number is not None:
            build_query["buildNumber"]["$gt"] = last_successful_build_number

        builds = list(self.builds.find(build_query).sort("buildNumber", -1).limit(max(1, lookback_builds)))
        build_numbers = [item.get("buildNumber") for item in builds if item.get("buildNumber") is not None]
        if not build_numbers:
            return []

        chunk_query: dict[str, Any] = {
            "repo": repo,
            "job": job,
            "buildNumber": {"$in": build_numbers},
            "schemaVersion": {"$gte": HISTORY_CHUNK_SCHEMA_VERSION},
            "chunkSource": {"$in": sorted(ALLOWED_HISTORY_CHUNK_SOURCES)},
        }
        chunk_query["branch"] = branch
        chunks = list(self.failure_chunks.find(chunk_query))
        notice_query: dict[str, Any] = {"repo": repo, "job": job, "buildNumber": {"$in": build_numbers}}
        notice_query["branch"] = branch
        notices = {item.get("buildNumber"): item for item in self.notices.find(notice_query)}
        build_by_number = {item.get("buildNumber"): item for item in builds}
        feedback_docs = _active_feedback_docs(self, repo, job, branch)
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

    def find_previous_build(
        self,
        *,
        repo: str,
        job: str,
        branch: str | None,
        current_build_number: int,
    ) -> dict | None:
        branch = normalize_branch_name(branch)
        query: dict[str, Any] = {
            "repo": repo,
            "job": job,
            "buildNumber": {"$lt": current_build_number},
            "headCommit": {"$exists": True, "$ne": None},
        }
        query["branch"] = branch
        docs = list(self.builds.find(query).sort("buildNumber", -1).limit(1))
        return docs[0] if docs else None

    def find_historical_failure_facts(
        self,
        *,
        repo: str,
        job: str,
        branch: str | None,
        current_build_number: int,
        last_successful_build_number: int | None,
        lookback_builds: int = 20,
        max_facts: int = 20,
    ) -> list[dict]:
        return self.find_historical_failure_facts_with_diagnostics(
            repo=repo,
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
        repo: str,
        job: str,
        branch: str | None,
        current_build_number: int,
        last_successful_build_number: int | None,
        lookback_builds: int = 20,
        max_facts: int = 20,
    ) -> dict:
        branch = normalize_branch_name(branch)
        build_query: dict[str, Any] = {
            "repo": repo,
            "job": job,
            "buildNumber": {"$lt": current_build_number},
            "result": {"$in": ["FAILURE", "UNSTABLE", "UNKNOWN"]},
        }
        build_query["branch"] = branch
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
            "repo": repo,
            "job": job,
            "buildNumber": {"$in": build_numbers},
            "historyEligible": True,
            "isGenericWrapper": False,
        }
        fact_query["branch"] = branch
        facts = list(self.failure_facts.find(fact_query))
        facts.sort(key=lambda item: (item.get("buildNumber") or 0, item.get("factIndex") or 0), reverse=True)
        diagnostics["factQuery"] = fact_query
        diagnostics["historicalFactsCount"] = len(facts)
        diagnostics["historicalFactBuildNumbers"] = [item.get("buildNumber") for item in facts[:20]]
        diagnostics["queryStage"] = "ok" if facts else "fact_query"
        return {"facts": facts[: max(1, max_facts)], "diagnostics": diagnostics}

    def notification_sent(self, *, repo: str, job: str, branch: str | None, build_number: int, notice_hash: str, channel: str = "wecom") -> bool:
        return self.notifications.find_one(
            {"repo": repo, "job": job, "branch": normalize_branch_name(branch), "buildNumber": build_number, "noticeHash": notice_hash, "channel": channel, "status": "sent"}
        ) is not None

    def save_notification(self, *, notice: CiResponsibilityNotice, notice_hash: str, channel: str, status: str, message: str, error: str | None = None) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        key = {
            "repo": _required_repo(notice),
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


def _utc_datetime(value: str | dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("build timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _required_repo(notice: CiResponsibilityNotice) -> str:
    repo = str(notice.repo or "").strip()
    if not repo:
        raise ValueError("notice.repo is required for history persistence")
    return repo


def group_test_file_failure_items(notice: CiResponsibilityNotice) -> tuple[dict[str, list], int]:
    from ci_owner_agent.services.responsibility_path_enricher import is_test_file_path

    grouped: dict[str, list] = {}
    unidentified = 0
    for item in notice.responsibilityItems:
        if not is_test_file_path(item.testFilePath):
            unidentified += 1
            continue
        grouped.setdefault(str(item.testFilePath), []).append(item)
    return grouped, unidentified


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
