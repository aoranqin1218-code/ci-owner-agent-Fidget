from __future__ import annotations

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.services.failure_similarity import (
    chunk_similarity,
    hash_normalized_chunk,
    normalize_error_chunk,
    similarity_relationship,
)
from ci_owner_agent.services.history_store import MongoHistoryStore, get_history_store

CURRENT_ALLOWED_CHUNK_SOURCES = {
    "local_test_failure_summary",
    "local_make_docker_test_failure_summary",
    "jenkins_test_failure_summary",
    "jenkins_failed_stage_failure_summary",
    "notice_failure_summary",
}


def history_search_similar_failures(
    context: AgentRuntimeContext,
    maxCandidates: int = 5,
    lookbackBuilds: int = 20,
    store: MongoHistoryStore | None = None,
) -> dict:
    if not context.settings.history_enabled and store is None:
        return {"ok": False, "historyEnabled": False, "error": "history store disabled"}
    try:
        history_store = store or get_history_store(context.settings)
        if history_store is None:
            return {"ok": False, "historyEnabled": False, "error": "history store disabled or unavailable"}

        summaries = context.failure_summaries
        if summaries is None:
            summaries = context.log_provider.find_test_failure_summaries(
                tail_lines=context.settings.failure_chunk_tail_lines,
                max_chunks=5,
            )
        current_chunks_raw = [
            chunk
            for chunk in summaries.get("chunks", [])
            if chunk.get("schemaVersion", 3) >= 3 and chunk.get("chunkSource") in CURRENT_ALLOWED_CHUNK_SOURCES
        ]
        if not current_chunks_raw:
            return {
                "ok": True,
                "historyEnabled": True,
                "currentBuild": context.build_number,
                "lastSuccessfulBuildNumber": context.last_successful_build_number,
                "lastSuccessfulBuildNumberMissing": context.last_successful_build_number is None,
                "currentChunks": [],
                "candidates": [],
                "warning": "test failure summaries unavailable; history similarity skipped",
            }
        current_chunks = [
            {
                "chunkIndex": idx,
                "text": str(chunk.get("content") or ""),
                "normalized": normalize_error_chunk(str(chunk.get("content") or "")),
                "normalizedHash": hash_normalized_chunk(str(chunk.get("content") or "")),
                "preview": str(chunk.get("content") or "")[:500],
                "signature": chunk.get("signature") or {},
                "signatureHash": chunk.get("signatureHash"),
            }
            for idx, chunk in enumerate(current_chunks_raw)
            if str(chunk.get("content") or "").strip()
        ]
        historical = history_store.find_historical_failure_chunks(
            job=context.job,
            branch=context.branch,
            current_build_number=context.build_number,
            last_successful_build_number=context.last_successful_build_number,
            lookback_builds=lookbackBuilds,
        )

        candidates: list[dict] = []
        candidates_by_chunk: dict[int, list[dict]] = {}
        for current in current_chunks:
            for hist in historical:
                hist_text = str(hist.get("chunkText") or "")
                hist_signature = hist.get("signature") or {}
                hist_signature_hash = hist.get("signatureHash")
                match_type = "text_similarity"
                if current.get("signatureHash") and current.get("signatureHash") == hist_signature_hash:
                    score = 1.0
                    relationship = "very_likely_same_failure"
                    match_type = "signature_exact"
                elif _signature_structural_match(current.get("signature") or {}, hist_signature):
                    score = 0.96
                    relationship = "very_likely_same_failure"
                    match_type = "signature_structural"
                else:
                    score = chunk_similarity(current["text"], hist_text)
                    relationship = similarity_relationship(score)
                if score < 0.75:
                    continue
                notice_doc = hist.get("noticeDoc") or {}
                build_doc = hist.get("build") or {}
                notice = notice_doc.get("notice")
                candidate = {
                    "currentChunkIndex": current["chunkIndex"],
                    "buildNumber": hist.get("buildNumber"),
                    "buildUrl": build_doc.get("buildUrl"),
                    "headCommit": build_doc.get("headCommit"),
                    "similarity": round(score, 4),
                    "relationship": relationship,
                    "matchType": match_type,
                    "signature": current.get("signature"),
                    "historicalSignature": hist_signature,
                    "ownerType": notice_doc.get("ownerType"),
                    "ownerName": notice_doc.get("ownerName"),
                    "ownerEmail": notice_doc.get("ownerEmail"),
                    "ownerCommit": notice_doc.get("ownerCommit"),
                    "hasHighConfidenceOwner": notice_doc.get("hasHighConfidenceOwner"),
                    "failureReason": notice_doc.get("failureReason"),
                    "matchedHistoricalChunk": hist_text[:1000],
                    "matchedCurrentChunk": current["text"][:1000],
                    "notice": notice,
                    "_noticeDoc": notice_doc,
                }
                candidates.append(candidate)
                candidates_by_chunk.setdefault(current["chunkIndex"], []).append(candidate)
        inherited_by_chunk = {
            chunk_index: _find_inherited_owner_for_chunk(chunk_candidates, context.build_number)
            for chunk_index, chunk_candidates in candidates_by_chunk.items()
        }
        for candidate in candidates:
            candidate["inheritedOwner"] = inherited_by_chunk.get(
                candidate.get("currentChunkIndex"),
                {"found": False},
            )
            candidate.pop("_noticeDoc", None)
        candidates.sort(
            key=lambda item: (
                item.get("similarity") or 0,
                item.get("buildNumber") or 0,
            ),
            reverse=True,
        )
        max_candidates = max(1, min(maxCandidates or context.settings.history_max_candidates, 50))
        return {
            "ok": True,
            "historyEnabled": True,
            "currentBuild": context.build_number,
            "lastSuccessfulBuildNumber": context.last_successful_build_number,
            "lastSuccessfulBuildNumberMissing": context.last_successful_build_number is None,
            "currentChunks": [
                {
                    "chunkIndex": item["chunkIndex"],
                    "normalizedHash": item["normalizedHash"],
                    "preview": item["preview"],
                    "signature": item["signature"],
                    "inheritedOwner": inherited_by_chunk.get(item["chunkIndex"], {"found": False}),
                }
                for item in current_chunks
            ],
            "candidates": candidates[:max_candidates],
            "instruction": (
                "如果 signature_exact/signature_structural + very_likely_same_failure 出现在当前 build 之前，"
                "当前 failure item 应视为历史持续失败。若 inheritedOwner.found=true，"
                "在 responsibilityItems 中使用 inherited_failure_owner 表达首次失败责任人；"
                "不要将后续提交判为该持续失败的首次责任人。"
            ),
            **(
                {
                    "warning": (
                        "lastSuccessfulBuildNumberMissing=true; using recent failed builds fallback. "
                        "Lower confidence for historical continuity decisions."
                    )
                }
                if context.last_successful_build_number is None
                else {}
            ),
        }
    except Exception as exc:
        return {"ok": False, "historyEnabled": True, "error": str(exc), "candidates": []}


def _signature_structural_match(current: dict, historical: dict) -> bool:
    required = ["testName", "errorType", "testFile"]
    if any(not current.get(key) or current.get(key) != historical.get(key) for key in required):
        return False
    left_message = str(current.get("errorMessage") or "")
    right_message = str(historical.get("errorMessage") or "")
    if not left_message and not right_message:
        return True
    return chunk_similarity(left_message, right_message) >= 0.9


def _find_inherited_owner_for_chunk(candidates: list[dict], current_build_number: int) -> dict:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.get("buildNumber") is not None
        and candidate.get("buildNumber") < current_build_number
        and candidate.get("matchType") in {"signature_exact", "signature_structural"}
        and candidate.get("relationship") == "very_likely_same_failure"
    ]
    eligible.sort(key=lambda item: item.get("buildNumber") or 0)
    for candidate in eligible:
        source = _high_confidence_source_from_candidate(candidate)
        if source is None:
            continue
        return {
            "found": True,
            "sourceBuildNumber": candidate.get("buildNumber"),
            "sourceBuildUrl": candidate.get("buildUrl"),
            "ownerType": source.get("ownerType"),
            "ownerName": source.get("ownerName"),
            "ownerEmail": source.get("ownerEmail"),
            "ownerCommit": source.get("ownerCommit"),
            "confidence": source.get("confidence"),
            "matchType": candidate.get("matchType"),
            "relationship": candidate.get("relationship"),
        }
    return {"found": False}


def _high_confidence_source_from_candidate(candidate: dict) -> dict | None:
    notice_doc = candidate.get("_noticeDoc") or {}
    if notice_doc.get("ownerType") == "high_confidence":
        return {
            "ownerType": notice_doc.get("ownerType"),
            "ownerName": notice_doc.get("ownerName"),
            "ownerEmail": notice_doc.get("ownerEmail"),
            "ownerCommit": notice_doc.get("ownerCommit"),
            "confidence": _top_owner_confidence(notice_doc.get("notice")),
        }

    notice = notice_doc.get("notice")
    items = notice.get("responsibilityItems") if isinstance(notice, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        if item.get("responsibilityType") == "current_build_owner" and owner.get("type") == "high_confidence":
            return {
                "ownerType": owner.get("type"),
                "ownerName": owner.get("name"),
                "ownerEmail": owner.get("email"),
                "ownerCommit": owner.get("commit"),
                "confidence": item.get("confidence") or owner.get("confidence"),
            }
    return None


def _top_owner_confidence(notice: dict | None) -> float | None:
    if not isinstance(notice, dict):
        return None
    owner = notice.get("owner")
    if not isinstance(owner, dict):
        return None
    return owner.get("confidence")
