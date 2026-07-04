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
                candidates.append(
                    {
                        "buildNumber": hist.get("buildNumber"),
                        "headCommit": build_doc.get("headCommit"),
                        "similarity": round(score, 4),
                        "relationship": relationship,
                        "matchType": match_type,
                        "signature": current.get("signature"),
                        "historicalSignature": hist_signature,
                        "ownerType": notice_doc.get("ownerType"),
                        "ownerName": notice_doc.get("ownerName"),
                        "ownerCommit": notice_doc.get("ownerCommit"),
                        "hasHighConfidenceOwner": notice_doc.get("hasHighConfidenceOwner"),
                        "failureReason": notice_doc.get("failureReason"),
                        "matchedHistoricalChunk": hist_text[:1000],
                        "matchedCurrentChunk": current["text"][:1000],
                        "notice": notice,
                    }
                )
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
                }
                for item in current_chunks
            ],
            "candidates": candidates[:max_candidates],
            "instruction": (
                "如果 very_likely_same_failure 出现在当前 build 之前，当前失败应视为历史持续失败，"
                "不要将后续提交判为首次责任人。"
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
