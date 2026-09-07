"""Deterministic history lookup and inheritance decisions."""

from __future__ import annotations

import hashlib
import re

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.services.failure_similarity import (
    chunk_similarity,
    hash_normalized_chunk,
    normalize_error_chunk,
    similarity_relationship,
)
from ci_owner_agent.services.failure_identity import build_failure_summary_signature
from ci_owner_agent.services.history_inheritance import (
    assess_failure_chain_continuity,
    build_inherited_owner,
    build_no_owner_decision_payload,
    continuity_previous_build_number,
    feedback_blocks_inheritance,
    find_no_owner_decision_from_notice,
    source_from_correct_owner_feedback,
)
from ci_owner_agent.services.history_store import MongoHistoryStore, get_history_store

CURRENT_ALLOWED_CHUNK_SOURCES = {
    "local_test_failure_summary",
    "local_make_docker_test_failure_summary",
    "jenkins_test_failure_summary",
    "jenkins_failed_stage_failure_summary",
    "notice_failure_summary",
}

_REPOSITORY_FILE_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.@+-]+/)+[A-Za-z0-9_.@+-]+\.(?:ts|tsx|js|jsx|json|ya?ml|sh))",
    re.IGNORECASE,
)


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
        current_chunks = []
        for idx, chunk in enumerate(current_chunks_raw):
            content = str(chunk.get("content") or "")
            if not content.strip():
                continue
            signature = dict(chunk.get("signature") or {})
            signature_key = build_failure_summary_signature(signature)
            signature["signatureKey"] = signature_key
            current_chunks.append({
                "chunkIndex": idx,
                "text": content,
                "normalized": normalize_error_chunk(content),
                "normalizedHash": hash_normalized_chunk(content),
                "preview": content[:500],
                "signature": signature,
                "signatureHash": hashlib.sha256(signature_key.encode("utf-8")).hexdigest(),
            })
        historical = history_store.find_historical_failure_chunks(
            repo=context.repo,
            job=context.job,
            branch=context.branch,
            current_build_number=context.build_number,
            last_successful_build_number=context.last_successful_build_number,
            lookback_builds=lookbackBuilds,
        )
        previous_build_number = continuity_previous_build_number(
            context.build_number,
            context.investigation_scope,
        )
        historical_chunk_count_by_build: dict[int, int] = {}
        for hist in historical:
            build_number = hist.get("buildNumber")
            if build_number is None:
                continue
            historical_chunk_count_by_build[build_number] = historical_chunk_count_by_build.get(build_number, 0) + 1

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
                    "historicalBuildFailureChunkCount": historical_chunk_count_by_build.get(hist.get("buildNumber")),
                    "similarity": round(score, 4),
                    "relationship": relationship,
                    "matchType": match_type,
                    "signature": current.get("signature"),
                    "historicalSignature": hist_signature,
                    "historicalSignatureHash": hist_signature_hash,
                    "ownerType": notice_doc.get("ownerType"),
                    "ownerName": notice_doc.get("ownerName"),
                    "ownerEmail": notice_doc.get("ownerEmail"),
                    "ownerCommit": notice_doc.get("ownerCommit"),
                    "hasHighConfidenceOwner": notice_doc.get("hasHighConfidenceOwner"),
                    "failureReason": notice_doc.get("failureReason"),
                    "matchedHistoricalChunk": hist_text[:1000],
                    "matchedCurrentChunk": current["text"][:1000],
                    "notice": notice,
                    "feedbackOverride": hist.get("feedbackOverride"),
                    "_noticeDoc": notice_doc,
                }
                candidates.append(candidate)
                candidates_by_chunk.setdefault(current["chunkIndex"], []).append(candidate)
        for candidate in candidates:
            candidate.update(
                _assess_candidate_continuity(
                    context,
                    candidate,
                    previous_build_number=previous_build_number,
                )
            )
        inherited_by_chunk = {
            chunk_index: _find_inherited_owner_for_chunk(
                chunk_candidates,
                context.build_number,
            )
            for chunk_index, chunk_candidates in candidates_by_chunk.items()
        }
        no_owner_by_chunk = {}
        for chunk_index, chunk_candidates in candidates_by_chunk.items():
            if inherited_by_chunk.get(chunk_index, {}).get("found"):
                no_owner_by_chunk[chunk_index] = {"found": False}
            else:
                no_owner_by_chunk[chunk_index] = _find_no_owner_decision_for_chunk(
                    chunk_candidates,
                    context.build_number,
                )
        for candidate in candidates:
            if candidate.get("continuityEligible"):
                candidate["inheritedOwner"] = inherited_by_chunk.get(
                    candidate.get("currentChunkIndex"),
                    {"found": False},
                )
                candidate["noOwnerDecision"] = no_owner_by_chunk.get(
                    candidate.get("currentChunkIndex"),
                    {"found": False},
                )
            else:
                candidate["inheritedOwner"] = {"found": False}
                candidate["noOwnerDecision"] = {"found": False}
            candidate.pop("_noticeDoc", None)
        candidates.sort(
            key=lambda item: (
                bool(item.get("continuityEligible")),
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
            "previousBuildNumber": previous_build_number,
            "lastSuccessfulBuildNumber": context.last_successful_build_number,
            "lastSuccessfulBuildNumberMissing": context.last_successful_build_number is None,
            "currentChunks": [
                {
                    "chunkIndex": item["chunkIndex"],
                    "normalizedHash": item["normalizedHash"],
                    "preview": item["preview"],
                    "signature": item["signature"],
                    "inheritedOwner": inherited_by_chunk.get(item["chunkIndex"], {"found": False}),
                    "noOwnerDecision": no_owner_by_chunk.get(item["chunkIndex"], {"found": False}),
                }
                for item in current_chunks
            ],
            "candidates": candidates[:max_candidates],
            "instruction": (
                "signature_exact/signature_structural + very_likely_same_failure 只有在 "
                "continuityEligible=true 时才表示历史持续失败。中间构建未执行该测试不会切断责任链；"
                "历史失败之后相关致因或测试文件被任何提交修改过时，必须按当前 build 重新分析。"
                "若 inheritedOwner.found=true，"
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


def _assess_candidate_continuity(
    context: AgentRuntimeContext,
    candidate: dict,
    *,
    previous_build_number: int | None,
) -> dict:
    if not (
        candidate.get("matchType") in {"signature_exact", "signature_structural"}
        and candidate.get("relationship") == "very_likely_same_failure"
    ):
        return {
            "continuityEligible": False,
            "continuityReason": "candidate is not a deterministic same-failure match",
            "continuityRelevantPaths": [],
            "continuityTouchedPaths": [],
        }
    source = _high_confidence_source_from_candidate(candidate)
    return assess_failure_chain_continuity(
        git_client=context.git_client,
        repo=context.repo,
        current_build_number=context.build_number,
        previous_build_number=previous_build_number,
        current_head_commit=context.head_commit,
        historical_build_number=candidate.get("buildNumber"),
        historical_head_commit=candidate.get("headCommit"),
        source_commit=source.get("ownerCommit") if isinstance(source, dict) else None,
        relevant_paths=_candidate_relevant_paths(candidate),
    )


def _candidate_relevant_paths(candidate: dict) -> list[str]:
    signature = candidate.get("historicalSignature") or {}
    paths = [
        signature.get("testFile"),
        signature.get("topStackFile"),
        *(signature.get("businessStackFiles") or []),
    ]
    notice = ((candidate.get("_noticeDoc") or {}).get("notice") or {})
    item = _matching_responsibility_item(notice, candidate)
    if item is not None:
        paths.extend([item.get("testFilePath"), item.get("failureFilePath")])
        evidence_by_id = {
            evidence.get("id"): evidence
            for evidence in notice.get("evidence") or []
            if isinstance(evidence, dict)
        }
        for evidence_id in item.get("evidenceIds") or []:
            evidence = evidence_by_id.get(evidence_id)
            if not isinstance(evidence, dict) or evidence.get("type") not in {
                "diff",
                "file_content",
                "keyword_match",
                "ts_symbol",
            }:
                continue
            for text in (evidence.get("source"), evidence.get("summary"), evidence.get("detail")):
                paths.extend(match.group("path") for match in _REPOSITORY_FILE_RE.finditer(str(text or "")))
    return list(dict.fromkeys(str(path) for path in paths if path))


def _find_inherited_owner_for_chunk(
    candidates: list[dict],
    current_build_number: int,
) -> dict:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.get("buildNumber") is not None
        and candidate.get("buildNumber") < current_build_number
        and candidate.get("continuityEligible") is True
        and candidate.get("matchType") in {"signature_exact", "signature_structural"}
        and candidate.get("relationship") == "very_likely_same_failure"
    ]
    eligible.sort(key=lambda item: item.get("buildNumber") or 0, reverse=True)
    for candidate in eligible:
        source = _trace_inherited_owner_source(candidate, candidates)
        if source is None:
            continue
        return build_inherited_owner(
            source,
            fallback_build_number=candidate.get("buildNumber"),
            fallback_build_url=candidate.get("buildUrl"),
            match_type=candidate.get("matchType"),
            relationship=candidate.get("relationship"),
        )
    return {"found": False}


def _trace_inherited_owner_source(continuity_candidate: dict, candidates: list[dict]) -> dict | None:
    source = _high_confidence_source_from_candidate(continuity_candidate)
    visited_builds: set[int] = set()
    while isinstance(source, dict) and source.get("ownerType") == "inherited_failure_owner":
        source_build = source.get("sourceBuildNumber")
        if not isinstance(source_build, int) or source_build in visited_builds:
            break
        visited_builds.add(source_build)
        source_candidate = next(
            (candidate for candidate in candidates if candidate.get("buildNumber") == source_build),
            None,
        )
        if source_candidate is None:
            break
        traced_source = _high_confidence_source_from_candidate(source_candidate)
        if traced_source is None:
            break
        traced_source.setdefault("sourceBuildNumber", source_build)
        traced_source.setdefault("sourceBuildUrl", source_candidate.get("buildUrl"))
        source = traced_source
    return source


def _find_no_owner_decision_for_chunk(
    candidates: list[dict],
    current_build_number: int,
) -> dict:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.get("buildNumber") is not None
        and candidate.get("buildNumber") < current_build_number
        and candidate.get("continuityEligible") is True
        and candidate.get("matchType") in {"signature_exact", "signature_structural"}
        and candidate.get("relationship") == "very_likely_same_failure"
    ]
    eligible.sort(key=lambda item: item.get("buildNumber") or 0, reverse=True)
    for candidate in eligible:
        feedback = candidate.get("feedbackOverride")
        if source_from_correct_owner_feedback(
            feedback,
            fallback_build_number=candidate.get("buildNumber"),
            fallback_build_url=candidate.get("buildUrl"),
        ):
            continue
        if feedback_blocks_inheritance(feedback):
            return build_no_owner_decision_payload(
                source_build_number=candidate.get("buildNumber"),
                source_build_url=candidate.get("buildUrl"),
                match_type=candidate.get("matchType"),
                relationship=candidate.get("relationship"),
                reason=(
                    f"historical same failure was blocked by feedback action {feedback.get('action')}; "
                    "treating current same failure as no_high_confidence_owner"
                ),
                feedback_action=feedback.get("action"),
                signature=candidate.get("historicalSignature") or {},
                signature_hash=candidate.get("historicalSignatureHash"),
            )
        notice_doc = candidate.get("_noticeDoc") or {}
        notice = notice_doc.get("notice") if isinstance(notice_doc, dict) else None
        historical_signature = candidate.get("historicalSignature") or {}
        item = find_no_owner_decision_from_notice(
            notice,
            historical_signature.get("signatureKey") or candidate.get("historicalSignatureHash"),
            allow_legacy_top_owner=_allow_legacy_top_owner_fallback(candidate),
        )
        if item is None and historical_signature.get("signatureHash"):
            item = find_no_owner_decision_from_notice(
                notice,
                historical_signature.get("signatureHash"),
                allow_legacy_top_owner=_allow_legacy_top_owner_fallback(candidate),
            )
        if item is None:
            continue
        return build_no_owner_decision_payload(
            source_build_number=candidate.get("buildNumber"),
            source_build_url=candidate.get("buildUrl"),
            match_type=candidate.get("matchType"),
            relationship=candidate.get("relationship"),
            reason="historical same failure was previously classified as no_high_confidence_owner",
            signature=candidate.get("historicalSignature") or {},
            signature_hash=candidate.get("historicalSignatureHash"),
        )
    return {"found": False}


def _high_confidence_source_from_candidate(candidate: dict) -> dict | None:
    feedback = candidate.get("feedbackOverride")
    if isinstance(feedback, dict):
        action = feedback.get("action")
        if feedback_blocks_inheritance(feedback):
            candidate["feedbackSuppressed"] = True
            candidate["feedbackReason"] = action
            return None
        if action == "confirm_owner":
            candidate["feedbackVerified"] = True
        source = source_from_correct_owner_feedback(
            feedback,
            fallback_build_number=candidate.get("buildNumber"),
            fallback_build_url=candidate.get("buildUrl"),
        )
        if source is not None:
            return source
    notice_doc = candidate.get("_noticeDoc") or {}
    notice = notice_doc.get("notice")
    items = notice.get("responsibilityItems") if isinstance(notice, dict) else None
    if isinstance(items, list) and items:
        item = _matching_responsibility_item(notice, candidate)
        if item is not None:
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            if item.get("responsibilityType") == "current_build_owner" and owner.get("type") == "high_confidence":
                source = {
                    "ownerType": owner.get("type"),
                    "ownerName": owner.get("name"),
                    "ownerEmail": owner.get("email"),
                    "ownerCommit": owner.get("commit"),
                    "confidence": item.get("confidence") or owner.get("confidence"),
                }
                if candidate.get("feedbackVerified"):
                    source["feedbackVerified"] = True
                return source
            if item.get("responsibilityType") == "inherited_failure_owner" and owner.get("type") == "inherited_failure_owner":
                source = {
                    "ownerType": owner.get("type"),
                    "ownerName": owner.get("name"),
                    "ownerEmail": owner.get("email"),
                    "ownerCommit": item.get("sourceCommit") or owner.get("commit"),
                    "confidence": item.get("confidence") or owner.get("confidence"),
                    "sourceBuildNumber": item.get("sourceBuildNumber"),
                    "sourceBuildUrl": item.get("sourceBuildUrl"),
                }
                if candidate.get("feedbackVerified"):
                    source["feedbackVerified"] = True
                return source
        return None

    if notice_doc.get("ownerType") == "high_confidence" and _allow_legacy_top_owner_fallback(candidate):
        source = {
            "ownerType": notice_doc.get("ownerType"),
            "ownerName": notice_doc.get("ownerName"),
            "ownerEmail": notice_doc.get("ownerEmail"),
            "ownerCommit": notice_doc.get("ownerCommit"),
            "confidence": _top_owner_confidence(notice),
        }
        if candidate.get("feedbackVerified"):
            source["feedbackVerified"] = True
        return source
    return None


def _matching_responsibility_item(notice: dict, candidate: dict) -> dict | None:
    for item in notice.get("responsibilityItems") or []:
        if isinstance(item, dict) and _responsibility_item_matches_candidate(item, candidate):
            return item
    return None


def _responsibility_item_matches_candidate(item: dict, candidate: dict) -> bool:
    failure_signature = item.get("failureSignature")
    if not failure_signature:
        return False
    historical_signature = candidate.get("historicalSignature") or {}
    possible = {
        historical_signature.get("signatureKey"),
        candidate.get("historicalSignatureHash"),
        historical_signature.get("signatureHash"),
    }
    return failure_signature in {str(value) for value in possible if value}


def _allow_legacy_top_owner_fallback(candidate: dict) -> bool:
    notice = (candidate.get("_noticeDoc") or {}).get("notice")
    if isinstance(notice, dict):
        items = notice.get("responsibilityItems")
        if isinstance(items, list) and items:
            return False
    # Old notices had one top-level owner and no item-to-signature mapping. This fallback is only
    # safe when the history chunk set for that build appears to contain a single failure.
    return candidate.get("historicalBuildFailureChunkCount") in {None, 1}


def _top_owner_confidence(notice: dict | None) -> float | None:
    if not isinstance(notice, dict):
        return None
    owner = notice.get("owner")
    if not isinstance(owner, dict):
        return None
    return owner.get("confidence")
