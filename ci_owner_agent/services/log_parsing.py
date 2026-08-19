"""Pure, fail-closed parsing for Jenkins console text and failure summaries.

This module deliberately has no file, Jenkins, or Agent dependency.  Log
providers own where text comes from; callers here own the parsed result.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Literal

from ci_owner_agent.services.command_runner import truncate_tail_text, truncate_text


FinalStatus = Literal["SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT", "UNKNOWN"]
FINAL_STATUS_RE = re.compile(
    r"(?mix)^\s*(?:\[\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\]\s*|\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*)?Finished:\s*(?P<status>[A-Za-z_]+)\b"
)
CHECKING_OUT_REVISION_RE = re.compile(r"(?mi)^\s*Checking out Revision\s+(?P<commit>[0-9a-f]{40})(?:\s+\((?P<ref>[^)]+)\))?\s*$")
GIT_CHECKOUT_FORCE_RE = re.compile(r"(?mi)^\s*(?:>\s*)?git\s+checkout\s+-f\s+(?P<commit>[0-9a-f]{40})(?:\s+#.*)?$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_TERMS = [
    "error", "exception", "assertionerror", "typeerror", "referenceerror", "fail", "failed", "npm err",
    "expected", "received", "cannot read", "timeout", "stack trace",
]
FAILURE_BLOCK_RE = re.compile(r"^\s*(\d+)\)\s+(.+?)\s*$")
XFAIL_BLOCK_RE = re.compile(r"^\s*✖\s+(.+?)\s*$")
ERROR_LINE_RE = re.compile(r"\b(AssertionError|Error|TypeError|ReferenceError):\s*(.*)")
PATH_RE = re.compile(
    r"((?:(?:[A-Za-z]:)?/?(?:var/app/)?)?(?:node_modules/|test/|server/|modules/|packages/)[^\s)'\",]+?\.(?:ts|tsx|js|jsx))(?:[:]\d+(?::\d+)?)?"
)
FOOTER_TERMS = [
    "------", "Dockerfile:", "ERROR: process", "ERROR: failed to solve:", "exit status 1", "make: ***",
    "[Pipeline] }", "[Pipeline] // stage",
]


@dataclass(frozen=True)
class FinalStatusResolution:
    status: FinalStatus
    detected: bool
    raw_status: str | None = None
    unsupported_status: str | None = None
    error: str | None = None


def resolve_final_status_from_console_log(log_content: str) -> FinalStatusResolution:
    matches = list(FINAL_STATUS_RE.finditer(ANSI_RE.sub("", log_content)))
    if not matches:
        return FinalStatusResolution("UNKNOWN", detected=False)
    raw = matches[-1].group("status").upper()
    if raw in {"SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT", "UNKNOWN"}:
        return FinalStatusResolution(raw, detected=True, raw_status=raw)  # type: ignore[arg-type]
    return FinalStatusResolution("UNKNOWN", detected=True, raw_status=raw, unsupported_status=raw, error=f"unsupported final status: {raw}")


def log_detect_final_status(log_content: str) -> FinalStatus:
    return resolve_final_status_from_console_log(log_content).status


@dataclass(frozen=True)
class CheckoutCommitResolution:
    commit: str | None
    ambiguous: bool = False
    refs: tuple[str, ...] = ()


def resolve_checkout_revision_from_console_log(text: str) -> CheckoutCommitResolution:
    clean = ANSI_RE.sub("", text)
    candidates = {
        match.group("commit").lower()
        for pattern in (CHECKING_OUT_REVISION_RE, GIT_CHECKOUT_FORCE_RE)
        for match in pattern.finditer(clean)
    }
    refs = tuple(sorted({match.group("ref").strip() for match in CHECKING_OUT_REVISION_RE.finditer(clean) if match.group("ref")}))
    if len(candidates) == 1:
        return CheckoutCommitResolution(candidates.pop(), refs=refs)
    return CheckoutCommitResolution(None, ambiguous=bool(candidates), refs=refs)


def detect_checkout_revision_from_console_log(text: str) -> str | None:
    """Return a unique checkout commit; security-sensitive callers inspect ambiguity."""
    return resolve_checkout_revision_from_console_log(text).commit


def find_error_chunks(
    all_lines: list[str],
    *,
    chunk_lines: int = 200,
    max_chunks: int = 5,
    max_output_chars: int = 20000,
) -> dict:
    size = max(1, chunk_lines)
    chunks = []
    for start_idx in range(0, len(all_lines), size):
        window = all_lines[start_idx : start_idx + size]
        lower = "\n".join(window).lower()
        hits = sum(lower.count(term) for term in ERROR_TERMS)
        if hits == 0:
            continue
        score = min(1.0, hits / 10)
        content, truncated = truncate_text("\n".join(window), max_output_chars)
        chunks.append(
            {
                "startLine": start_idx + 1,
                "endLine": start_idx + len(window),
                "score": score,
                "content": content,
                "truncated": truncated,
            }
        )
    chunks.sort(key=lambda item: item["score"], reverse=True)
    return {"chunks": chunks[:max_chunks]}


def find_focused_failure_chunks(
    all_lines: list[str],
    *,
    tail_lines: int = 500,
    max_chunks: int = 3,
    max_output_chars: int = 20000,
) -> dict:
    if not all_lines:
        return {"chunks": [], "warning": "log is empty"}
    count = max(50, tail_lines)
    test_start = None
    for idx, line in enumerate(all_lines):
        if "[Pipeline] { (Test)" in line:
            test_start = idx
    if test_start is not None:
        test_end = len(all_lines) - 1
        for idx in range(test_start + 1, len(all_lines)):
            if "[Pipeline] // stage" in all_lines[idx]:
                test_end = idx
                break
        else:
            for idx in range(test_start + 1, len(all_lines)):
                if all_lines[idx].strip() == "[Pipeline] }":
                    test_end = idx
                    break
        return {
            "chunks": [
                _focused_chunk(
                    all_lines,
                    test_start,
                    test_end,
                    count,
                    max_output_chars=max_output_chars,
                    chunk_source="local_test_stage_tail",
                    stage_name="Test",
                    step_name="make docker-test",
                    anchor_type="jenkins_test_stage",
                )
            ][:max_chunks]
        }

    make_start = None
    for idx, line in enumerate(all_lines):
        if "+ make docker-test" in line:
            make_start = idx
    if make_start is not None:
        return {
            "chunks": [
                _focused_chunk(
                    all_lines,
                    make_start,
                    len(all_lines) - 1,
                    count,
                    max_output_chars=max_output_chars,
                    chunk_source="local_make_docker_test_tail",
                    stage_name="Test",
                    step_name="make docker-test",
                    anchor_type="make_docker_test_tail",
                )
            ][:max_chunks]
        }

    start = max(0, len(all_lines) - count)
    content, truncated = truncate_tail_text("\n".join(all_lines[start:]), max_output_chars)
    return {
        "chunks": [
            {
                "chunkIndex": 0,
                "schemaVersion": 2,
                "chunkSource": "local_console_tail_fallback",
                "stageName": None,
                "stepName": None,
                "anchorType": "console_tail_fallback",
                "startLine": start + 1,
                "endLine": len(all_lines),
                "score": 0.2,
                "content": content,
                "truncated": truncated or start > 0,
            }
        ][:max_chunks],
        "warning": "focused Test stage and make docker-test anchors unavailable; returned console tail fallback",
    }


def find_test_failure_summaries(
    all_lines: list[str],
    *,
    tail_lines: int = 500,
    max_chunks: int = 5,
    max_output_chars: int = 20000,
) -> dict:
    focused = find_focused_failure_chunks(
        all_lines,
        tail_lines=tail_lines,
        max_chunks=1,
        max_output_chars=max_output_chars,
    )
    return build_test_failure_summaries(focused, max_chunks=max_chunks)


def build_test_failure_summaries(focused: dict, *, max_chunks: int = 5) -> dict:
    focused_chunks = focused.get("chunks", [])
    if not focused_chunks:
        return {"chunks": [], "warning": "focused failure chunks unavailable"}
    focused_chunk = focused_chunks[0]
    focused_source = focused_chunk.get("chunkSource")
    if focused_source == "local_console_tail_fallback":
        return {"chunks": [], "warning": "test failure summaries unavailable; focused chunk is console tail fallback"}
    lines = [_semantic_log_line(line) for line in str(focused_chunk.get("content") or "").splitlines()]
    chunk_source = _summary_source_for_focused_source(str(focused_source or ""))
    starts = [idx for idx, line in enumerate(lines) if FAILURE_BLOCK_RE.match(line)]
    if starts:
        return {
            "chunks": _build_summary_chunks(
                lines=lines,
                starts=starts,
                max_chunks=max_chunks,
                focused_chunk=focused_chunk,
                chunk_source=chunk_source,
                anchor_type="mocha_failure_block",
                signature_extractor=_extract_failure_signature,
                score=1.0,
            )
        }
    xfail_starts = [idx for idx, line in enumerate(lines) if XFAIL_BLOCK_RE.match(line) and "ERROR:" not in line]
    if xfail_starts:
        return {
            "chunks": _build_summary_chunks(
                lines=lines,
                starts=xfail_starts,
                max_chunks=max_chunks,
                focused_chunk=focused_chunk,
                chunk_source=chunk_source,
                anchor_type="japa_failure_block",
                signature_extractor=_extract_xfail_signature,
                score=0.9,
            )
        }
    # Docker, BuildKit, Jenkins, and shell wrapper errors are not stable enough
    # for deterministic historical inheritance.  Only structured Mocha/Japa
    # blocks are currently eligible; other failures remain current-log evidence.
    return {"chunks": [], "warning": "test failure summaries unavailable; no Mocha/Japa failure block found; history similarity skipped"}


def _focused_chunk(all_lines: list[str], start_idx: int, end_idx: int, tail_lines: int, *, max_output_chars: int, chunk_source: str, stage_name: str | None, step_name: str | None, anchor_type: str) -> dict:
    segment = all_lines[start_idx : end_idx + 1]
    tail_start = max(0, len(segment) - tail_lines)
    content, text_truncated = truncate_tail_text("\n".join(segment[tail_start:]), max_output_chars)
    return {"chunkIndex": 0, "schemaVersion": 2, "chunkSource": chunk_source, "stageName": stage_name, "stepName": step_name, "anchorType": anchor_type, "startLine": start_idx + tail_start + 1, "endLine": end_idx + 1, "score": 1.0, "content": content, "truncated": text_truncated or tail_start > 0}


def _build_summary_chunks(*, lines: list[str], starts: list[int], max_chunks: int, focused_chunk: dict, chunk_source: str, anchor_type: str, signature_extractor: Callable[[str], dict], score: float) -> list[dict]:
    chunks = []
    for chunk_index, start in enumerate(starts[:max_chunks]):
        end = starts[chunk_index + 1] if chunk_index + 1 < len(starts) else len(lines)
        chunks.append(_summary_chunk(chunk_index=chunk_index, lines=lines, start=start, end=end, focused_chunk=focused_chunk, chunk_source=chunk_source, anchor_type=anchor_type, signature_extractor=signature_extractor, score=score))
    return chunks


def _summary_chunk(*, chunk_index: int, lines: list[str], start: int, end: int, focused_chunk: dict, chunk_source: str, anchor_type: str, signature_extractor: Callable[[str], dict], score: float) -> dict:
    end = _trim_failure_block_end(lines, start, end)
    block_lines = lines[start:end][:120]
    content = "\n".join(block_lines)
    truncated = end - start > len(block_lines)
    if len(content) > 12000:
        content = content[:12000]
        truncated = True
    signature = signature_extractor(content)
    start_line = (focused_chunk.get("startLine") or 1) + start
    end_line = start_line + max(0, len(block_lines) - 1)
    return {"chunkIndex": chunk_index, "schemaVersion": 3, "chunkSource": chunk_source, "stageName": focused_chunk.get("stageName"), "stepName": focused_chunk.get("stepName"), "anchorType": anchor_type, "startLine": start_line, "endLine": end_line, "score": score, "content": content, "truncated": truncated, "signature": signature, "signatureHash": _signature_hash(signature)}


def _summary_source_for_focused_source(source: str) -> str:
    if source == "local_make_docker_test_tail":
        return "local_make_docker_test_failure_summary"
    if source == "jenkins_test_stage_tail":
        return "jenkins_test_failure_summary"
    if source == "jenkins_failed_stage_log":
        return "jenkins_failed_stage_failure_summary"
    return "local_test_failure_summary"


def _strip_docker_log_prefix(line: str) -> str:
    match = re.match(r"^#\d+\s+(?:\d+(?:\.\d+)?\s+)?(.*)$", line)
    return match.group(1) if match else line


def _semantic_log_line(line: str) -> str:
    return ANSI_RE.sub("", _strip_docker_log_prefix(line)).strip()


def _trim_failure_block_end(lines: list[str], start: int, end: int) -> int:
    for idx in range(start + 1, end):
        if any(lines[idx].strip().startswith(term) for term in FOOTER_TERMS):
            return idx
    return end


def _extract_failure_signature(content: str) -> dict:
    from ci_owner_agent.services.failure_identity import build_responsibility_signature

    lines = content.splitlines()
    first = FAILURE_BLOCK_RE.match(lines[0] if lines else "")
    title = first.group(2).strip() if first else ""
    test_name, test_case = _split_test_title(title)
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if ERROR_LINE_RE.search(stripped):
            break
        if not stripped.startswith(("at ", "+", "-", "expected", "actual")):
            test_case = test_case or stripped.rstrip(":")
            break
    error_type = None
    error_message = ""
    for line in lines:
        match = ERROR_LINE_RE.search(line)
        if match:
            error_type, error_message = match.group(1), match.group(2).strip()
            break
    files = _extract_stack_paths(lines)
    test_file = next((path for path in files if path.startswith("test/") or "/test/" in path), None)
    top_stack_file = files[0] if files else None
    normalized_error = _stable_error_message(error_message)
    signature_key = build_responsibility_signature(failure_title=" ".join(part for part in (test_name, test_case) if part), failure_summary="\n".join((content, normalized_error)), existing_signature="|".join([test_name or "", test_case or "", error_type or "", normalized_error, test_file or "", top_stack_file or ""]), error_type=error_type, test_file_path=test_file, failure_file_path=top_stack_file)
    return {"testName": test_name, "testCase": test_case, "errorType": error_type, "errorMessage": normalized_error, "testFile": test_file, "topStackFile": top_stack_file, "businessStackFiles": files, "signatureKey": signature_key}


def _extract_xfail_signature(content: str) -> dict:
    from ci_owner_agent.services.failure_identity import build_responsibility_signature

    lines = content.splitlines()
    first = XFAIL_BLOCK_RE.match(lines[0] if lines else "")
    title = first.group(1).strip() if first else ""
    error_type, error_message = "Error", ""
    joined = "\n".join(lines).lower()
    if "run" in joined and "awaitfunc" in joined and "timeout" in joined:
        error_type, error_message = "Timeout", "run awaitfunc timeout"
    else:
        for line in lines:
            match = ERROR_LINE_RE.search(line)
            if match:
                error_type, error_message = match.group(1), match.group(2).strip()
                break
        if not error_message:
            error_message = _first_meaningful_error_line(lines[1:]) or "error"
    files = _extract_stack_paths(lines)
    test_file = _pick_test_file(files)
    top_stack_file = files[0] if files else None
    normalized_error = _stable_error_message(error_message)
    signature_key = build_responsibility_signature(failure_title=title, failure_summary="\n".join((content, normalized_error)), existing_signature="|".join(["xfail", title, error_type, normalized_error, test_file or "", top_stack_file or ""]), failure_kind="xfail", error_type=error_type, test_file_path=test_file, failure_file_path=top_stack_file)
    return {"testName": title, "testCase": title, "errorType": error_type, "errorMessage": normalized_error, "testFile": test_file, "topStackFile": top_stack_file, "businessStackFiles": files, "signatureKey": signature_key}


def _extract_stack_paths(lines: list[str]) -> list[str]:
    files = []
    for line in lines:
        for match in PATH_RE.finditer(line.replace("\\", "/")):
            path = _clean_stack_path(match.group(1))
            if path not in files:
                files.append(path)
    return files


def _pick_test_file(files: list[str]) -> str | None:
    return next((path for path in files if path.startswith("test/") or "/test/" in path or "/tests/" in path), None)


def _first_meaningful_error_line(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith(("at ", "+", "-")):
            return stripped
    return None


def _split_test_title(title: str) -> tuple[str, str | None]:
    cleaned = title.strip().rstrip(":")
    parts = cleaned.split(maxsplit=1)
    if len(parts) == 2 and (parts[0].endswith("Test") or parts[0].endswith("Spec")):
        return parts[0], parts[1].rstrip(":")
    return cleaned, None


def _clean_stack_path(path: str) -> str:
    cleaned = re.sub(r":\d+(?::\d+)?$", "", path.replace("\\", "/")).strip()
    cleaned = re.sub(r"^[A-Za-z]:/", "", cleaned).lstrip("/")
    return cleaned[len("var/app/") :] if cleaned.startswith("var/app/") else cleaned


def _stable_error_message(message: str) -> str:
    from ci_owner_agent.services.failure_identity import canonicalize_failure_message

    return canonicalize_failure_message(message)[:200]


def _signature_hash(signature: dict) -> str:
    from ci_owner_agent.services.failure_identity import canonicalize_failure_signature

    canonical = canonicalize_failure_signature(str(signature.get("signatureKey") or ""))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
