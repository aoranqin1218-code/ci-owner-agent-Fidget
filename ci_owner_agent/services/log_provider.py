"""Log source adapters.

Pure console parsing lives in :mod:`ci_owner_agent.services.log_parsing`.
The imports below remain public compatibility exports for existing callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ci_owner_agent.schemas import LogTail
from ci_owner_agent.services.command_runner import truncate_text
from ci_owner_agent.services.log_parsing import (
    CheckoutCommitResolution,
    FinalStatus,
    FinalStatusResolution,
    build_test_failure_summaries,
    detect_checkout_revision_from_console_log,
    find_error_chunks,
    find_focused_failure_chunks,
    log_detect_final_status,
    resolve_checkout_revision_from_console_log,
    resolve_final_status_from_console_log,
)


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
        content, _ = truncate_text("\n".join(all_lines[-count:] if count else []), self.max_output_chars)
        return LogTail(startLine=start, endLine=len(all_lines), content=content)

    def search(self, query: str, context_lines: int = 30, max_matches: int = 10) -> dict:
        if not query:
            return {"matches": []}
        all_lines = self._lines()
        matches = []
        for idx, line in enumerate(all_lines, start=1):
            if query.lower() not in line.lower():
                continue
            start = max(1, idx - max(0, context_lines))
            end = min(len(all_lines), idx + max(0, context_lines))
            content, truncated = truncate_text("\n".join(all_lines[start - 1 : end]), self.max_output_chars)
            matches.append({"line": idx, "startLine": start, "endLine": end, "content": content, "truncated": truncated})
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
        return find_error_chunks(self._lines(), chunk_lines=chunk_lines, max_chunks=max_chunks, max_output_chars=self.max_output_chars)

    def find_focused_failure_chunks(self, tail_lines: int = 500, max_chunks: int = 3) -> dict:
        return find_focused_failure_chunks(self._lines(), tail_lines=tail_lines, max_chunks=max_chunks, max_output_chars=self.max_output_chars)

    def find_test_failure_summaries(self, tail_lines: int = 500, max_chunks: int = 5) -> dict:
        focused = self.find_focused_failure_chunks(tail_lines=tail_lines, max_chunks=1)
        return build_test_failure_summaries(focused, max_chunks=max_chunks)

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
            if chunk.get("chunkSource") in {"local_test_stage_tail", "local_make_docker_test_tail"}:
                chunk["chunkSource"] = "jenkins_test_stage_tail"
                chunk["stageName"] = "Test"
        return result
