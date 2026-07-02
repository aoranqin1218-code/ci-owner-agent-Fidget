from __future__ import annotations

from ci_owner_agent.services.log_provider import LogProvider


def log_read_tail(provider: LogProvider, lines: int = 500) -> dict:
    return provider.read_tail(lines).model_dump()


def log_search(provider: LogProvider, query: str, contextLines: int = 30, maxMatches: int = 10) -> dict:
    return provider.search(query, contextLines, maxMatches)


def log_read_range(provider: LogProvider, startLine: int, endLine: int) -> dict:
    return provider.read_range(startLine, endLine)


def log_find_error_chunks(provider: LogProvider, chunkLines: int = 200, maxChunks: int = 5) -> dict:
    return provider.find_error_chunks(chunkLines, maxChunks)
