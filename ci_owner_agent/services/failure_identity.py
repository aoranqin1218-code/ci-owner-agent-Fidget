from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OBJECT_ID_WRAPPER_RE = re.compile(r"(?:new\s+)?objectid\s*\(\s*(['\"]?)[0-9a-f]{24}\1\s*\)", re.I)
OBJECT_ID_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-f]{24}(?![A-Za-z0-9])", re.I)
UUID_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![A-Za-z0-9])", re.I)
ISO_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?\b", re.I)
MILLI_TIMESTAMP_RE = re.compile(r"(?<!\d)1\d{11,13}(?!\d)")
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|sec(?:onds?)?|minutes?|mins?)\b", re.I)
LINE_COL_RE = re.compile(r"(?<=[A-Za-z0-9_.\-/\\]):\d+(?::\d+)?\b")
PORT_RE = re.compile(r"(?i)(?:(?<=localhost:)|(?<=127\.0\.0\.1:)|(?<=\bport\s))\d{2,5}\b")
CONTEXT_ID_RE = re.compile(
    r"(?i)\b(request[_-]?id|trace[_-]?id|session[_-]?id|correlation[_-]?id)\s*[:=]\s*['\"]?[A-Za-z0-9._:/+-]{6,}['\"]?"
)
TEMP_PATH_RE = re.compile(
    r"(?i)(?:[A-Za-z]:)?(?:[/\\](?:users[/\\][^/\\\s]+[/\\]appdata[/\\]local[/\\]temp|tmp|var[/\\]tmp))[/\\][^\s'\"]+"
)
HASH_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-f]{7,64}(?![A-Za-z0-9])", re.I)

DYNAMIC_PLACEHOLDER_RE = re.compile(
    r"(?:(?:request|trace|session|correlation)id[=:_-]*)?"
    r"<(?:object_id|uuid|timestamp|duration|tmp_path|port|requestid|traceid|sessionid|correlationid|hash|random)>",
    re.I,
)
UNUSABLE_IDENTITY_VALUES = {"unknown_failure", "unusable_failure_identity", "generic_wrapper"}
GENERIC_WRAPPER_WORDS = {
    "build",
    "buildkit",
    "code",
    "command",
    "complete",
    "did",
    "docker",
    "error",
    "exit",
    "failed",
    "failure",
    "generic",
    "jenkins",
    "make",
    "not",
    "only",
    "outer",
    "process",
    "returned",
    "script",
    "shell",
    "successfully",
    "wrapper",
}

MONGO_COLLECTION_RE = re.compile(r"(?i)\bcollection\s*:\s*([A-Za-z0-9_.-]+)")
MONGO_INDEX_RE = re.compile(r"(?i)\bindex(?:\s+name)?\s*:\s*([^\s,}]+)")
MONGO_ON_RE = re.compile(r"(?i)\bon\s+([A-Za-z0-9_.-]+)\.((?:_id_|[A-Za-z0-9_-]+_\d))\b")
MONGO_SIGNATURE_RE = re.compile(
    r"(?i)e11000[_|\s-]+duplicate[_|\s-]+key[_|\s-]+(?P<collection>[A-Za-z0-9_.-]+?)(?:__|\|)(?P<index>_id_|[A-Za-z0-9_-]+_\d)(?:[_|]|$)"
)


def canonicalize_failure_message(value: str | None) -> str:
    return _normalize_failure_message(value, lowercase=True)


def sanitize_failure_message(value: str | None) -> str:
    """Redact dynamic identity values without changing display-text casing."""
    return _normalize_failure_message(value, lowercase=False)


def _normalize_failure_message(value: str | None, *, lowercase: bool) -> str:
    text = ANSI_RE.sub("", str(value or ""))
    if not text.strip():
        return ""
    text = OBJECT_ID_WRAPPER_RE.sub("<object_id>", text)
    text = OBJECT_ID_RE.sub("<object_id>", text)
    text = UUID_RE.sub("<uuid>", text)
    text = ISO_TIMESTAMP_RE.sub("<timestamp>", text)
    text = MILLI_TIMESTAMP_RE.sub("<timestamp>", text)
    text = DURATION_RE.sub("<duration>", text)
    text = CONTEXT_ID_RE.sub(lambda match: f"{_context_placeholder(match.group(1))}=<{_context_placeholder(match.group(1))}>", text)
    text = PORT_RE.sub("<port>", text)
    text = TEMP_PATH_RE.sub("<tmp_path>", text)
    text = LINE_COL_RE.sub("", text)
    text = HASH_RE.sub("<hash>", text)
    text = text.replace("\\", "/")
    if lowercase:
        text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def canonicalize_failure_signature(value: str | None) -> str:
    text = canonicalize_failure_message(value)
    if not text:
        return ""
    text = re.sub(r"\s*\|\s*", "|", text)
    text = re.sub(r"[\s:;,=]+", "_", text)
    text = re.sub(r"_+", "_", text)
    text = re.sub(r"\|+", "|", text)
    return text.strip("_| ")


def build_failure_fact_signature(fact: Any) -> str:
    values = _fact_values(fact)
    mongo = _mongodb_duplicate_key_signature(values)
    if mongo:
        return mongo
    if not has_meaningful_failure_identity(fact):
        return "unknown_failure"
    structured = [
        values.get("failureKind"),
        values.get("errorCode"),
        values.get("errorType"),
        values.get("packageName"),
        values.get("filePath"),
        values.get("symbol"),
        values.get("rootCauseSummary"),
        values.get("message"),
    ]
    parts = [canonicalize_failure_signature(str(value)) for value in structured if str(value or "").strip()]
    return "|".join(part for part in parts if part) or "unknown_failure"


def has_meaningful_failure_identity(fact: Any) -> bool:
    if bool(_fact_value(fact, "isGenericWrapper")):
        return False

    for key in ("failureKind", "errorCode", "errorType", "packageName", "filePath", "symbol"):
        if _has_meaningful_identity_text(_fact_value(fact, key)):
            return True
    return any(
        _has_meaningful_identity_text(_fact_value(fact, key))
        for key in ("rootCauseSummary", "message")
    )


def build_failure_summary_signature(signature: Mapping[str, Any] | None) -> str:
    values = dict(signature or {})
    mongo = _mongodb_duplicate_key_signature(values)
    if mongo:
        return mongo
    existing = canonicalize_failure_signature(values.get("signatureKey"))
    if existing:
        return existing
    components = (
        values.get("testName"),
        values.get("testCase"),
        values.get("errorCode"),
        values.get("errorType"),
        values.get("errorMessage"),
        values.get("testFile"),
        values.get("topStackFile"),
    )
    parts = [canonicalize_failure_signature(value) for value in components if str(value or "").strip()]
    return "|".join(part for part in parts if part) or "unknown_failure"


def build_responsibility_signature(
    *,
    failure_title: str | None,
    failure_summary: str | None,
    existing_signature: str | None,
    error_code: str | None = None,
    error_type: str | None = None,
    failure_kind: str | None = None,
    test_file_path: str | None = None,
    failure_file_path: str | None = None,
) -> str:
    values = {
        "signatureKey": existing_signature,
        "failureTitle": failure_title,
        "failureSummary": failure_summary,
        "errorCode": error_code,
        "errorType": error_type,
        "failureKind": failure_kind,
        "testFilePath": test_file_path,
        "failureFilePath": failure_file_path,
        "message": failure_summary,
        "rootCauseSummary": failure_title,
    }
    mongo = _mongodb_duplicate_key_signature(values)
    if mongo:
        return mongo
    existing = canonicalize_failure_signature(existing_signature)
    if existing:
        return existing
    components = [failure_kind, error_code, error_type, failure_title, failure_summary, test_file_path, failure_file_path]
    parts = [canonicalize_failure_signature(value) for value in components if str(value or "").strip()]
    return "|".join(part for part in parts if part) or "unknown_failure"


def _mongodb_duplicate_key_signature(values: Mapping[str, Any]) -> str | None:
    combined = "\n".join(str(value) for value in values.values() if value is not None)
    lowered = combined.lower()
    error_code = str(values.get("errorCode") or "").lower()
    if "e11000" not in lowered and error_code != "e11000" and "duplicate key error" not in lowered:
        return None
    collection = _first_match(MONGO_COLLECTION_RE, combined)
    index = _first_match(MONGO_INDEX_RE, combined)
    if not (collection and index):
        on_match = MONGO_ON_RE.search(combined)
        if on_match:
            collection = collection or on_match.group(1)
            index = index or on_match.group(2)
    if not (collection and index):
        signature_match = MONGO_SIGNATURE_RE.search(combined)
        if signature_match:
            collection = collection or signature_match.group("collection")
            index = index or signature_match.group("index")
            if index.lower() == "id_":
                index = "_id_"
    if collection and index:
        return f"mongodb_duplicate_key|e11000|{collection.lower()}|{index.lower()}"
    return None


def _fact_values(fact: Any) -> dict[str, Any]:
    keys = (
        "signatureKey",
        "failureKind",
        "errorCode",
        "errorType",
        "packageName",
        "filePath",
        "symbol",
        "message",
        "rootCauseSummary",
    )
    if isinstance(fact, Mapping):
        return {key: fact.get(key) for key in keys}
    return {key: getattr(fact, key, None) for key in keys}


def _fact_value(fact: Any, key: str) -> Any:
    return fact.get(key) if isinstance(fact, Mapping) else getattr(fact, key, None)


def _has_meaningful_identity_text(value: Any) -> bool:
    canonical = canonicalize_failure_signature(str(value or ""))
    if not canonical or canonical in UNUSABLE_IDENTITY_VALUES:
        return False
    without_dynamic_values = DYNAMIC_PLACEHOLDER_RE.sub("", canonical).strip("_| -")
    if not without_dynamic_values:
        return False
    words = set(re.findall(r"[a-z0-9]+", without_dynamic_values))
    return bool(words) and not words.issubset(GENERIC_WRAPPER_WORDS)


def _first_match(pattern: re.Pattern[str], value: str) -> str | None:
    match = pattern.search(value)
    return match.group(1).strip("'\" ,") if match else None


def _context_placeholder(key: str) -> str:
    return re.sub(r"[_-]", "", key).lower()
