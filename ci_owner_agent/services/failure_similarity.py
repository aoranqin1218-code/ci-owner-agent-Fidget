from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)
LINE_COL_RE = re.compile(r"(?<=\S):\d+(?::\d+)?\b")
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s)\b", re.I)
LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
TEMP_PATH_RE = re.compile(r"(?:[a-z]:)?/[^ \n\t]*?(?:tmp|temp|\.cache)[^ \n\t]*", re.I)
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
OBJECT_ID_RE = re.compile(r"\b[0-9a-f]{24}\b", re.I)
LONG_RANDOM_RE = re.compile(r"\b(?=[A-Za-z0-9_-]{32,}\b)(?=.*\d)(?=.*[_-])[A-Za-z0-9_-]+\b")


def normalize_error_chunk(text: str) -> str:
    normalized = ANSI_RE.sub("", text)
    normalized = normalized.replace("\\", "/").lower()
    normalized = LINE_COL_RE.sub("", normalized)
    normalized = UUID_RE.sub("<uuid>", normalized)
    normalized = OBJECT_ID_RE.sub("<object_id>", normalized)
    normalized = HEX_RE.sub("<hash>", normalized)
    normalized = DURATION_RE.sub("<duration>", normalized)
    normalized = TEMP_PATH_RE.sub("<tmp_path>", normalized)
    normalized = LONG_RANDOM_RE.sub("<random>", normalized)
    normalized = LONG_NUMBER_RE.sub("<num>", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


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
