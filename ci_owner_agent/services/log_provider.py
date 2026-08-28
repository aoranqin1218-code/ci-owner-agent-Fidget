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
    build_coverage_failure_summaries,
    build_integration_failure_summaries,
    build_integration_protocol_index,
    build_test_failure_summaries,
    classify_all_japa_failures,
    count_japa_failure_blocks,
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
        all_lines = self._lines()
        limit = max(0, max_chunks)
        coverage_chunks = build_coverage_failure_summaries(all_lines, max_chunks=None)

        # 集成协议 index + 全量分类（一次解析所有 Japa 失败块，逐块判定 kind）。
        protocol_index = build_integration_protocol_index(all_lines)
        preflight_failures = protocol_index["preflight_failures"]
        classifications = classify_all_japa_failures(all_lines)

        # 集成日志：code chunks 由 build_integration_failure_summaries 直接按行号
        # 生成，与 classifications 一一对应，不经过 build_test_failure_summaries
        # （后者在大量相邻 ✖ + 交错 ❯ 下会膨胀出重复 chunk）。
        # 非集成日志：走原有 build_test_failure_summaries，所有 Japa 块照常展示。
        summary_warning: str | None = None
        if protocol_index["is_integration"]:
            code_chunks = build_integration_failure_summaries(all_lines, max_chunks=None)
        else:
            focused = self.find_focused_failure_chunks(tail_lines=tail_lines, max_chunks=max(1, limit))
            summaries = build_test_failure_summaries(focused, max_chunks=max(1, limit))
            code_chunks = list(summaries.get("chunks") or [])
            summary_warning = summaries.get("warning")

        # 全量分类计数：assertion（代码）、connection（环境）、unknown（证据不足，no-owner）。
        connection_count = sum(1 for c in classifications if c["kind"] == "connection")
        unknown_count = sum(1 for c in classifications if c["kind"] == "unknown")

        # 统一预算：混合失败时优先保留尽可能多的 Japa，但 coverage 至少占 1 个展示位。
        if coverage_chunks and limit:
            displayed_japa = code_chunks[: max(0, limit - 1)]
            displayed_coverage = coverage_chunks[: limit - len(displayed_japa)]
        else:
            displayed_japa = code_chunks[:limit]
            displayed_coverage = []
        merged = [*displayed_japa, *displayed_coverage]
        for index, chunk in enumerate(merged):
            chunk["chunkIndex"] = index

        japa_total = count_japa_failure_blocks(all_lines)
        # 环境/unknown 失败事实全部 no-owner、history ineligible，不进 chunks，只进 totals 记账。
        integration_env_total = (
            len(preflight_failures)
            + connection_count
            + unknown_count
            + (1 if protocol_index["cleanup_failed"] else 0)
        )
        totals = {
            "japa": japa_total,
            "coverage": len(coverage_chunks),
            "integrationEnv": integration_env_total,
            "omittedJapa": max(0, japa_total - len(displayed_japa)),
            "omittedCoverage": max(0, len(coverage_chunks) - len(displayed_coverage)),
        }
        coverage_files = [
            {
                "packageName": (chunk.get("signature") or {}).get("packageName"),
                "rawCoveragePath": (chunk.get("signature") or {}).get("rawCoveragePath"),
                "metrics": (chunk.get("signature") or {}).get("metrics") or {},
                "signatureKey": (chunk.get("signature") or {}).get("signatureKey"),
                "content": chunk.get("content") or "",
            }
            for chunk in coverage_chunks
        ]
        result = {
            "chunks": merged,
            "totals": totals,
            "coverageFiles": coverage_files,
            "integrationClassifications": classifications,
            "integrationConflicts": protocol_index["conflicts"],
            # Persisting trusted suite checkpoints needs the protocol facts, not
            # only the display-oriented chunks.  The index contains no database
            # URI, credentials, or other runtime secrets.
            "integrationProtocolIndex": protocol_index,
        }
        if not merged and summary_warning:
            result["warning"] = summary_warning
        return result

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
            # JenkinsClient keeps normal ``get_console_text`` bounded.  Structured
            # Japa/c8 facts may occur long before the final Jenkins/Docker footer,
            # so use the analysis-specific full fetch when the client supports it.
            # Keep the fallback for lightweight fake clients used by callers/tests.
            load_for_analysis = getattr(self.client, "get_console_text_for_analysis", None)
            result = (
                load_for_analysis(self.job, self.build_number)
                if callable(load_for_analysis)
                else self.client.get_console_text(self.job, self.build_number)
            )
            self._cached_content = (
                result.get("analysisContent") or result.get("content", "")
                if result.get("ok")
                else ""
            )
        return self._cached_content

    def _lines(self) -> list[str]:
        return self._content().splitlines()
