from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

def normalize_error_chunk(text: str) -> str:
    from ci_owner_agent.services.failure_identity import canonicalize_failure_message

    return canonicalize_failure_message(text)


def hash_normalized_chunk(text: str) -> str:
    return hashlib.sha256(normalize_error_chunk(text).encode("utf-8")).hexdigest()


def _tokens(text: str) -> set[str]:
    return {item for item in re.split(r"[^a-z0-9_\u4e00-\u9fff./:-]+", text) if item}


def _ngrams(text: str, n: int = 5) -> set[str]:
    if len(text) <= n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def chunk_similarity(a: str, b: str) -> float:
    left = normalize_error_chunk(a)
    right = normalize_error_chunk(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    sequence = SequenceMatcher(None, left, right).ratio()
    token_score = _jaccard(_tokens(left), _tokens(right))
    gram_score = _jaccard(_ngrams(left), _ngrams(right))
    return max(0.0, min(1.0, (sequence * 0.5) + (token_score * 0.3) + (gram_score * 0.2)))


def similarity_relationship(score: float) -> str:
    if score >= 0.92:
        return "very_likely_same_failure"
    if score >= 0.75:
        return "possible_same_failure"
    return "weak_match"
