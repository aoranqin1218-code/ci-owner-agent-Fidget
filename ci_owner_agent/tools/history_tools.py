from __future__ import annotations

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.services.failure_similarity import (
    chunk_similarity,
    hash_normalized_chunk,
    normalize_error_chunk,
    similarity_relationship,
)
from ci_owner_agent.services.history_store import MongoHistoryStore, get_history_store


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

        current_chunks_raw = context.log_provider.find_error_chunks(chunk_lines=200, max_chunks=5).get("chunks", [])
        current_chunks = [
            {
                "chunkIndex": idx,
                "text": str(chunk.get("content") or ""),
                "normalized": normalize_error_chunk(str(chunk.get("content") or "")),
                "normalizedHash": hash_normalized_chunk(str(chunk.get("content") or "")),
                "preview": str(chunk.get("content") or "")[:500],
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
                score = chunk_similarity(current["text"], hist_text)
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
                        "relationship": similarity_relationship(score),
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
        candidates.sort(key=lambda item: item["similarity"], reverse=True)
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
