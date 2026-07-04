from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from ci_owner_agent.schemas import LogTail
from ci_owner_agent.services.command_runner import truncate_text

FinalStatus = Literal["SUCCESS", "FAILURE", "ABORTED", "UNKNOWN"]
FINAL_STATUS_RE = re.compile(r"Finished:\s*(SUCCESS|FAILURE|ABORTED)\b", re.IGNORECASE)
CHECKING_OUT_REVISION_RE = re.compile(r"\bChecking out Revision\s+([0-9a-f]{7,40})\b", re.IGNORECASE)
GIT_CHECKOUT_FORCE_RE = re.compile(r"\bgit\s+checkout\s+-f\s+([0-9a-f]{7,40})\b", re.IGNORECASE)
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
        content, truncated = truncate_text("\n".join(all_lines[start:]), self.max_output_chars)
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
        content, text_truncated = truncate_text("\n".join(selected), self.max_output_chars)
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
            elif chunk.get("chunkSource") == "local_make_docker_test_tail":
                chunk["chunkSource"] = "jenkins_test_stage_tail"
        return result
