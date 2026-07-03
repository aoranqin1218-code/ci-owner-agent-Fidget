from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.services.command_runner import truncate_text
from ci_owner_agent.tools.keyword_tools import repo_keyword_search as keyword_search
from ci_owner_agent.tools.typescript_tools import (
    check_node_dependencies_for_analysis as check_ts_deps,
    ts_analyze_changed_functions as analyze_changed_functions,
    ts_find_callers as find_callers,
    ts_find_definitions as find_definitions,
)


@dataclass
class SimpleTool:
    name: str
    description: str
    func: Callable[..., dict]

    def invoke(self, kwargs: dict[str, Any] | None = None) -> dict:
        return self.func(**(kwargs or {}))


def _limit(data: dict, max_chars: int) -> dict:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return data
    truncated, _ = truncate_text(text, max_chars)
    return {
        "ok": data.get("ok", True),
        "truncated": True,
        "originalKeys": list(data.keys()),
        "contentJson": truncated,
        "note": "Tool output exceeded max chars and was truncated as JSON text. Re-run with narrower arguments if needed.",
    }


def build_langchain_tools(context: AgentRuntimeContext) -> list[Any]:
    max_chars = context.settings.max_tool_output_chars

    def log_read_tail(lines: int = 500) -> dict:
        return _limit(context.log_provider.read_tail(lines).model_dump(), max_chars)

    def log_search(query: str, contextLines: int = 30, maxMatches: int = 10) -> dict:
        return _limit(context.log_provider.search(query, contextLines, maxMatches), max_chars)

    def log_read_range(startLine: int, endLine: int) -> dict:
        return _limit(context.log_provider.read_range(startLine, endLine), max_chars)

    def log_find_error_chunks(chunkLines: int = 200, maxChunks: int = 5) -> dict:
        return _limit(context.log_provider.find_error_chunks(chunkLines, maxChunks), max_chars)

    def log_detect_final_status() -> dict:
        return {"status": context.log_provider.detect_final_status()}

    def repo_get_commits_between() -> dict:
        return _limit(
            context.git_client.get_commits_between(context.repo, context.base_commit or "", context.head_commit or ""),
            max_chars,
        )

    def repo_get_diff_files() -> dict:
        return _limit(
            context.git_client.get_diff_files(context.repo, context.base_commit or "", context.head_commit or ""),
            max_chars,
        )

    def repo_get_file_diff(path: str, contextLines: int = 8) -> dict:
        return _limit(
            context.git_client.get_file_diff(context.repo, context.base_commit or "", context.head_commit or "", path, contextLines),
            max_chars,
        )

    def repo_get_file_content(commit: str, path: str, startLine: int | None = None, endLine: int | None = None) -> dict:
        return _limit(context.git_client.get_file_content(context.repo, commit, path, startLine, endLine), max_chars)

    def repo_keyword_search(keywords: list[str], scope: str = "changed_files", paths: list[str] | None = None, maxMatches: int = 50) -> dict:
        return _limit(
            keyword_search(
                context.git_client,
                context.repo,
                context.head_commit or "",
                keywords,
                scope=scope,
                baseCommit=context.base_commit,
                headCommit=context.head_commit,
                paths=paths or [],
                maxMatches=maxMatches,
            ),
            max_chars,
        )

    def ts_analyze_changed_functions(files: list[str] | None = None) -> dict:
        target_files = files or [item.path for item in context.changed_files if item.path.endswith((".ts", ".tsx"))]
        return analyze_changed_functions(
            repo=context.repo,
            baseCommit=context.base_commit,
            headCommit=context.head_commit,
            files=target_files,
            repo_cache_dir=context.settings.repo_cache_dir,
            analyzer_dir=context.settings.ts_analyzer_dir,
            max_output_chars=max_chars,
        )

    def ts_find_definitions(symbols: list[str]) -> dict:
        return find_definitions(
            repo=context.repo,
            commit=context.head_commit,
            symbols=symbols,
            repo_cache_dir=context.settings.repo_cache_dir,
            analyzer_dir=context.settings.ts_analyzer_dir,
            force_checkout=True,
            max_output_chars=max_chars,
        )

    def ts_find_callers(symbol: str, definitionFile: str, maxResults: int = 50) -> dict:
        return find_callers(
            repo=context.repo,
            commit=context.head_commit,
            symbol=symbol,
            definitionFile=definitionFile,
            maxResults=maxResults,
            repo_cache_dir=context.settings.repo_cache_dir,
            analyzer_dir=context.settings.ts_analyzer_dir,
            force_checkout=True,
            max_output_chars=max_chars,
        )

    def check_node_dependencies_for_analysis() -> dict:
        return check_ts_deps(context.repo, repo_cache_dir=context.settings.repo_cache_dir)

    specs = [
        ("log_read_tail", "Read the tail of the current build log.", log_read_tail),
        ("log_search", "Search current build log with context.", log_search),
        ("log_read_range", "Read a line range from current build log.", log_read_range),
        ("log_find_error_chunks", "Recall high-signal error chunks from the log.", log_find_error_chunks),
        ("log_detect_final_status", "Detect final Jenkins Finished status from log.", log_detect_final_status),
        ("repo_get_commits_between", "Get commits in base..head.", repo_get_commits_between),
        ("repo_get_diff_files", "Get changed files in base..head.", repo_get_diff_files),
        ("repo_get_file_diff", "Get diff for a changed file.", repo_get_file_diff),
        ("repo_get_file_content", "Get file content at a commit.", repo_get_file_content),
        ("repo_keyword_search", "Search keywords in changed files, paths, or whole repo.", repo_keyword_search),
        ("ts_analyze_changed_functions", "Analyze changed TS/TSX functions.", ts_analyze_changed_functions),
        ("ts_find_definitions", "Find TypeScript definitions for symbols.", ts_find_definitions),
        ("ts_find_callers", "Find TypeScript callers of a symbol.", ts_find_callers),
        ("check_node_dependencies_for_analysis", "Check TS node_modules and tsconfig dependencies.", check_node_dependencies_for_analysis),
    ]
    simple_tools = [SimpleTool(name=name, description=description, func=func) for name, description, func in specs]
    try:
        from langchain_core.tools import StructuredTool

        return [StructuredTool.from_function(tool.func, name=tool.name, description=tool.description) for tool in simple_tools]
    except Exception:
        return simple_tools
