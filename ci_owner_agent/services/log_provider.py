from __future__ import annotations

import re
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from ci_owner_agent.schemas import LogTail
from ci_owner_agent.services.command_runner import truncate_tail_text, truncate_text

FinalStatus = Literal["SUCCESS", "FAILURE", "ABORTED", "UNKNOWN"]
FINAL_STATUS_RE = re.compile(r"Finished:\s*(SUCCESS|FAILURE|ABORTED)\b", re.IGNORECASE)
CHECKING_OUT_REVISION_RE = re.compile(r"\bChecking out Revision\s+([0-9a-f]{7,40})\b", re.IGNORECASE)
GIT_CHECKOUT_FORCE_RE = re.compile(r"\bgit\s+checkout\s+-f\s+([0-9a-f]{7,40})\b", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_TERMS = [
    "error",
    "exception",
    "assertionerror",
    "typeerror",
    "referenceerror",
    "fail",
    "failed",
    "npm err",
    "expected",
    "received",
    "cannot read",
    "timeout",
    "stack trace",
]
FAILURE_BLOCK_RE = re.compile(r"^\s*(\d+)\)\s+(.+?)\s*$")
XFAIL_BLOCK_RE = re.compile(r"^\s*✖\s+(.+?)\s*$")
FATAL_ERROR_RE = re.compile(r"(?:✖\s+ERROR:|\bERROR:|\bError\s+\[ERR_|\bERR_[A-Z0-9_]+)")
ERROR_CODE_RE = re.compile(r"\b(ERR_[A-Z0-9_]+)\b")
ERROR_LINE_RE = re.compile(r"\b(AssertionError|Error|TypeError|ReferenceError):\s*(.*)")
PATH_RE = re.compile(
    r"((?:(?:[A-Za-z]:)?/?(?:var/app/)?)?(?:node_modules/|test/|server/|modules/|packages/)[^\s)'\",]+?\.(?:ts|tsx|js|jsx))(?:[:]\d+(?::\d+)?)?"
)
FOOTER_TERMS = [
    "------",
    "Dockerfile:",
    "ERROR: process",
    "ERROR: failed to solve:",
    "exit status 1",
    "make: ***",
    "[Pipeline] }",
    "[Pipeline] // stage",
]


def log_detect_final_status(log_content: str) -> FinalStatus:
    matches = list(FINAL_STATUS_RE.finditer(log_content))
    if not matches:
        return "UNKNOWN"
    return matches[-1].group(1).upper()  # type: ignore[return-value]


def detect_checkout_revision_from_console_log(text: str) -> str | None:
    checkout_revision = CHECKING_OUT_REVISION_RE.search(text)
    if checkout_revision:
        return checkout_revision.group(1)
    forced_checkout = GIT_CHECKOUT_FORCE_RE.search(text)
    if forced_checkout:
        return forced_checkout.group(1)
    return None


class LogProvider(ABC):
    @abstractmethod
    def read_tail(self, lines: int) -> LogTail:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, context_lines: int, max_matches: int) -> dict:
        raise NotImplementedError

    @abstractmethod
    def read_range(self, start_line: int, end_line: int) -> dict:
        raise NotImplementedError

    @abstractmethod
    def find_error_chunks(self, chunk_lines: int, max_chunks: int) -> dict:
        raise NotImplementedError

    @abstractmethod
    def find_focused_failure_chunks(self, tail_lines: int = 500, max_chunks: int = 3) -> dict:
        raise NotImplementedError

    @abstractmethod
    def find_test_failure_summaries(self, tail_lines: int = 500, max_chunks: int = 5) -> dict:
        raise NotImplementedError

    @abstractmethod
    def detect_final_status(self) -> FinalStatus:
        raise NotImplementedError


class TextLogProvider(LogProvider):
    def __init__(self, max_output_chars: int = 20000) -> None:
        self.max_output_chars = max_output_chars

    @abstractmethod
    def _lines(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def _content(self) -> str:
        raise NotImplementedError

    def read_tail(self, lines: int) -> LogTail:
        all_lines = self._lines()
        count = max(0, lines)
        start = max(1, len(all_lines) - count + 1) if all_lines else 1
        selected = all_lines[-count:] if count else []
        content, _ = truncate_text("\n".join(selected), self.max_output_chars)
        return LogTail(startLine=start, endLine=len(all_lines), content=content)

    def search(self, query: str, context_lines: int = 30, max_matches: int = 10) -> dict:
        if not query:
            return {"matches": []}
        all_lines = self._lines()
        needle = query.lower()
        matches = []
        for idx, line in enumerate(all_lines, start=1):
            if needle not in line.lower():
                continue
            start = max(1, idx - max(0, context_lines))
            end = min(len(all_lines), idx + max(0, context_lines))
            content, truncated = truncate_text("\n".join(all_lines[start - 1 : end]), self.max_output_chars)
            matches.append(
                {
                    "line": idx,
                    "startLine": start,
                    "endLine": end,
                    "content": content,
                    "truncated": truncated,
                }
            )
            if len(matches) >= max_matches:
                break
        return {"matches": matches}

    def read_range(self, start_line: int, end_line: int) -> dict:
        all_lines = self._lines()
        start = max(1, start_line)
        end = min(len(all_lines), max(start, end_line))
        content, truncated = truncate_text("\n".join(all_lines[start - 1 : end]), self.max_output_chars)
        return {"startLine": start, "endLine": end, "content": content, "truncated": truncated}

    def find_error_chunks(self, chunk_lines: int = 200, max_chunks: int = 5) -> dict:
        all_lines = self._lines()
        size = max(1, chunk_lines)
        chunks = []
        for start_idx in range(0, len(all_lines), size):
            window = all_lines[start_idx : start_idx + size]
            lower = "\n".join(window).lower()
            hits = sum(lower.count(term) for term in ERROR_TERMS)
            if hits == 0:
                continue
            score = min(1.0, hits / 10)
            content, truncated = truncate_text("\n".join(window), self.max_output_chars)
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

    def find_focused_failure_chunks(self, tail_lines: int = 500, max_chunks: int = 3) -> dict:
        all_lines = self._lines()
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
                    self._focused_chunk(
                        all_lines,
                        test_start,
                        test_end,
                        count,
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
                    self._focused_chunk(
                        all_lines,
                        make_start,
                        len(all_lines) - 1,
                        count,
                        chunk_source="local_make_docker_test_tail",
                        stage_name="Test",
                        step_name="make docker-test",
                        anchor_type="make_docker_test_tail",
                    )
                ][:max_chunks]
            }

        start = max(0, len(all_lines) - count)
        content, truncated = truncate_tail_text("\n".join(all_lines[start:]), self.max_output_chars)
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

    def find_test_failure_summaries(self, tail_lines: int = 500, max_chunks: int = 5) -> dict:
        focused = self.find_focused_failure_chunks(tail_lines=tail_lines, max_chunks=1)
        focused_chunks = focused.get("chunks", [])
        if not focused_chunks:
            return {"chunks": [], "warning": "focused failure chunks unavailable"}
        focused_chunk = focused_chunks[0]
        focused_source = focused_chunk.get("chunkSource")
        if focused_source == "local_console_tail_fallback":
            return {"chunks": [], "warning": "test failure summaries unavailable; focused chunk is console tail fallback"}
        raw_lines = str(focused_chunk.get("content") or "").splitlines()
        lines = [_semantic_log_line(line) for line in raw_lines]
        starts = [idx for idx, line in enumerate(lines) if FAILURE_BLOCK_RE.match(line)]
        chunk_source = _summary_source_for_focused_source(str(focused_source or ""))
        if starts:
            return {
                "chunks": self._build_summary_chunks(
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
                "chunks": self._build_summary_chunks(
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

        # Do not treat Docker / BuildKit / Jenkins / shell wrapper failures as stable
        # deterministic history signatures. Current deterministic similarity is only
        # for structured Mocha/Japa test failures; non-structured build failures are
        # analyzed by the agent from the current log and diff context.
        return {
            "chunks": [],
            "warning": "test failure summaries unavailable; no Mocha/Japa failure block found; history similarity skipped",
        }

    def _build_summary_chunks(
        self,
        *,
        lines: list[str],
        starts: list[int],
        max_chunks: int,
        focused_chunk: dict,
        chunk_source: str,
        anchor_type: str,
        signature_extractor,
        score: float,
    ) -> list[dict]:
        chunks = []
        for chunk_index, start in enumerate(starts[:max_chunks]):
            end = starts[chunk_index + 1] if chunk_index + 1 < len(starts) else len(lines)
            chunks.append(
                self._summary_chunk(
                    chunk_index=chunk_index,
                    lines=lines,
                    start=start,
                    end=end,
                    focused_chunk=focused_chunk,
                    chunk_source=chunk_source,
                    anchor_type=anchor_type,
                    signature_extractor=signature_extractor,
                    score=score,
                )
            )
        return chunks

    def _summary_chunk(
        self,
        *,
        chunk_index: int,
        lines: list[str],
        start: int,
        end: int,
        focused_chunk: dict,
        chunk_source: str,
        anchor_type: str,
        signature_extractor,
        score: float,
    ) -> dict:
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
        return {
            "chunkIndex": chunk_index,
            "schemaVersion": 3,
            "chunkSource": chunk_source,
            "stageName": focused_chunk.get("stageName"),
            "stepName": focused_chunk.get("stepName"),
            "anchorType": anchor_type,
            "startLine": start_line,
            "endLine": end_line,
            "score": score,
            "content": content,
            "truncated": truncated,
            "signature": signature,
            "signatureHash": _signature_hash(signature),
        }

    def _focused_chunk(
        self,
        all_lines: list[str],
        start_idx: int,
        end_idx: int,
        tail_lines: int,
        *,
        chunk_source: str,
        stage_name: str | None,
        step_name: str | None,
        anchor_type: str,
    ) -> dict:
        segment = all_lines[start_idx : end_idx + 1]
        tail_start = max(0, len(segment) - tail_lines)
        selected = segment[tail_start:]
        content, text_truncated = truncate_tail_text("\n".join(selected), self.max_output_chars)
        return {
            "chunkIndex": 0,
            "schemaVersion": 2,
            "chunkSource": chunk_source,
            "stageName": stage_name,
            "stepName": step_name,
            "anchorType": anchor_type,
            "startLine": start_idx + tail_start + 1,
            "endLine": end_idx + 1,
            "score": 1.0,
            "content": content,
            "truncated": text_truncated or tail_start > 0,
        }

    def detect_final_status(self) -> FinalStatus:
        return log_detect_final_status(self._content())


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
    line = _strip_docker_log_prefix(line)
    line = ANSI_RE.sub("", line)
    return line.strip()


def _trim_failure_block_end(lines: list[str], start: int, end: int) -> int:
    for idx in range(start + 1, end):
        stripped = lines[idx].strip()
        if any(stripped.startswith(term) for term in FOOTER_TERMS):
            return idx
    return end


def _extract_failure_signature(content: str) -> dict:
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
            error_type = match.group(1)
            error_message = match.group(2).strip()
            break

    files = []
    for line in lines:
        for match in PATH_RE.finditer(line.replace("\\", "/")):
            path = _clean_stack_path(match.group(1))
            if path not in files:
                files.append(path)
    test_file = next((path for path in files if path.startswith("test/") or "/test/" in path), None)
    top_stack_file = files[0] if files else None
    normalized_error = _stable_error_message(error_message)
    signature_key = "|".join(
        [
            test_name or "",
            test_case or "",
            error_type or "",
            normalized_error,
            test_file or "",
            top_stack_file or "",
        ]
    )
    return {
        "testName": test_name,
        "testCase": test_case,
        "errorType": error_type,
        "errorMessage": normalized_error,
        "testFile": test_file,
        "topStackFile": top_stack_file,
        "businessStackFiles": files,
        "signatureKey": signature_key,
    }


def _extract_xfail_signature(content: str) -> dict:
    lines = content.splitlines()
    first = XFAIL_BLOCK_RE.match(lines[0] if lines else "")
    title = first.group(1).strip() if first else ""
    error_type = "Error"
    error_message = ""
    joined = "\n".join(lines).lower()
    if "run" in joined and "awaitfunc" in joined and "timeout" in joined:
        error_type = "Timeout"
        error_message = "run awaitfunc timeout"
    else:
        for line in lines:
            match = ERROR_LINE_RE.search(line)
            if match:
                error_type = match.group(1)
                error_message = match.group(2).strip()
                break
        if not error_message:
            error_message = _first_meaningful_error_line(lines[1:]) or "error"

    files = _extract_stack_paths(lines)
    test_file = _pick_test_file(files)
    top_stack_file = files[0] if files else None
    normalized_error = _stable_error_message(error_message)
    signature_key = "|".join(["xfail", title, error_type, normalized_error, test_file or "", top_stack_file or ""])
    return {
        "testName": title,
        "testCase": title,
        "errorType": error_type,
        "errorMessage": normalized_error,
        "testFile": test_file,
        "topStackFile": top_stack_file,
        "businessStackFiles": files,
        "signatureKey": signature_key,
    }


def _extract_fatal_error_signature(content: str) -> dict:
    lines = content.splitlines()
    text = "\n".join(lines)
    code_match = ERROR_CODE_RE.search(text)
    error_code = code_match.group(1) if code_match else "fatal error"
    message = ""
    for line in lines:
        if "Directory import" in line or ERROR_LINE_RE.search(line) or error_code in line:
            message = line
            break
    if error_code and error_code not in message:
        message = f"{error_code} {message}".strip()
    files = _extract_stack_paths(lines)
    test_file = _pick_test_file(files)
    top_stack_file = test_file or (files[0] if files else None)
    normalized_error = _stable_error_message(message or error_code)
    signature_key = "|".join(["fatal", error_code, normalized_error, test_file or "", top_stack_file or ""])
    return {
        "testName": "test initialization",
        "testCase": error_code,
        "errorType": "Error",
        "errorMessage": normalized_error,
        "testFile": test_file,
        "topStackFile": top_stack_file,
        "businessStackFiles": files,
        "signatureKey": signature_key,
    }


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
        if not stripped or stripped.startswith(("at ", "+", "-")):
            continue
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
    cleaned = re.sub(r"^[A-Za-z]:/", "", cleaned)
    cleaned = cleaned.lstrip("/")
    if cleaned.startswith("var/app/"):
        cleaned = cleaned[len("var/app/") :]
    return cleaned


def _stable_error_message(message: str) -> str:
    from ci_owner_agent.services.failure_similarity import normalize_error_chunk

    return normalize_error_chunk(message)[:200]


def _signature_hash(signature: dict) -> str:
    return hashlib.sha256(str(signature.get("signatureKey") or "").encode("utf-8")).hexdigest()


class LocalFileLogProvider(TextLogProvider):
    def __init__(self, path: str | Path, max_output_chars: int = 20000) -> None:
        super().__init__(max_output_chars=max_output_chars)
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"log file does not exist: {self.path}")

    def _lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8", errors="replace").splitlines()

    def _content(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")


class JenkinsLogProvider(TextLogProvider):
    def __init__(self, client, job: str, build_number: int, max_output_chars: int = 20000) -> None:
        super().__init__(max_output_chars=max_output_chars)
        self.client = client
        self.job = job
        self.build_number = build_number
        self._cached_content: str | None = None

    def _content(self) -> str:
        if self._cached_content is None:
            result = self.client.get_console_text(self.job, self.build_number)
            self._cached_content = result.get("content", "") if result.get("ok") else ""
        return self._cached_content

    def _lines(self) -> list[str]:
        return self._content().splitlines()

    def find_focused_failure_chunks(self, tail_lines: int = 500, max_chunks: int = 3) -> dict:
        result = super().find_focused_failure_chunks(tail_lines=tail_lines, max_chunks=max_chunks)
        for chunk in result.get("chunks", []):
            if chunk.get("chunkSource") == "local_test_stage_tail":
                chunk["chunkSource"] = "jenkins_test_stage_tail"
                chunk["stageName"] = "Test"
            elif chunk.get("chunkSource") == "local_make_docker_test_tail":
                chunk["chunkSource"] = "jenkins_test_stage_tail"
                chunk["stageName"] = "Test"
        return result
