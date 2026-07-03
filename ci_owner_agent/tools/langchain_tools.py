from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.tools.keyword_tools import repo_keyword_search as keyword_search
from ci_owner_agent.tools.path_tools import repo_find_paths as find_paths
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
    trimmed = _trim_for_json(data)
    content_json = json.dumps(trimmed, ensure_ascii=False, default=str)
    if len(content_json) > max_chars:
        trimmed = _trim_for_json(data, max_items=8, max_string_chars=500, max_depth=4)
        content_json = json.dumps(trimmed, ensure_ascii=False, default=str)
    if len(content_json) > max_chars:
        content_json = json.dumps(
            {
                "ok": data.get("ok", True),
                "_truncated": True,
                "_original_type": type(data).__name__,
                "_original_keys": list(data.keys()),
            },
            ensure_ascii=False,
            default=str,
        )
    return {
        "ok": data.get("ok", True),
        "truncated": True,
        "originalKeys": list(data.keys()),
        "contentJson": content_json,
        "note": "Tool output exceeded max chars and was structurally truncated as valid JSON. Re-run with narrower arguments if needed.",
    }


def _trim_for_json(value: Any, max_items: int = 20, max_string_chars: int = 1000, max_depth: int = 6) -> Any:
    if max_depth <= 0:
        return {"_truncated": True, "_original_type": type(value).__name__}
    if isinstance(value, dict):
        trimmed: dict[str, Any] = {}
        truncated_fields: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            if isinstance(item, str) and len(item) > max_string_chars:
                trimmed[key_text] = item[:max_string_chars] + "\n...[truncated field]..."
                truncated_fields.append(key_text)
            else:
                trimmed[key_text] = _trim_for_json(item, max_items, max_string_chars, max_depth - 1)
        if truncated_fields:
            trimmed["_truncated_fields"] = truncated_fields
        return trimmed
    if isinstance(value, list):
        items = [_trim_for_json(item, max_items, max_string_chars, max_depth - 1) for item in value[:max_items]]
        if len(value) > max_items:
            return {"items": items, "_truncated_items": True, "_original_length": len(value)}
        return items
    if isinstance(value, str) and len(value) > max_string_chars:
        return value[:max_string_chars] + "\n...[truncated field]..."
    return value


def build_langchain_tools(context: AgentRuntimeContext) -> list[Any]:
    max_chars = context.settings.max_tool_output_chars
    tool_call_count = 0
    tool_call_seen: set[str] = set()
    budget = context.settings.max_tool_steps

    def _guard_tool_call(name: str, kwargs: dict) -> dict | None:
        nonlocal tool_call_count
        key = json.dumps({"name": name, "kwargs": kwargs}, ensure_ascii=False, sort_keys=True, default=str)
        if key in tool_call_seen:
            return {
                "ok": False,
                "error": "duplicate tool call blocked",
                "tool": name,
                "suggestion": "Do not call the same tool with the same arguments again. Produce final JSON based on existing evidence.",
            }
        if tool_call_count >= budget:
            return {
                "ok": False,
                "error": "tool call budget exhausted",
                "tool": name,
                "budget": budget,
                "suggestion": "Tool call budget exhausted. Produce final CiResponsibilityNotice JSON now. If evidence is insufficient, output no_high_confidence_owner.",
            }
        tool_call_seen.add(key)
        tool_call_count += 1
        return None

    def log_read_tail(lines: int = 500) -> dict:
        """Read the tail of the current build log."""
        blocked = _guard_tool_call("log_read_tail", {"lines": lines})
        if blocked:
            return blocked
        return _limit(context.log_provider.read_tail(lines).model_dump(), max_chars)

    def log_search(query: str, contextLines: int = 30, maxMatches: int = 10) -> dict:
        """Search the current build log and return context around matching lines."""
        blocked = _guard_tool_call("log_search", {"query": query, "contextLines": contextLines, "maxMatches": maxMatches})
        if blocked:
            return blocked
        return _limit(context.log_provider.search(query, contextLines, maxMatches), max_chars)

    def log_read_range(startLine: int, endLine: int) -> dict:
        """Read a line range from the current build log."""
        blocked = _guard_tool_call("log_read_range", {"startLine": startLine, "endLine": endLine})
        if blocked:
            return blocked
        return _limit(context.log_provider.read_range(startLine, endLine), max_chars)

    def log_find_error_chunks(chunkLines: int = 200, maxChunks: int = 5) -> dict:
        """Find high-signal error chunks in the current build log."""
        blocked = _guard_tool_call("log_find_error_chunks", {"chunkLines": chunkLines, "maxChunks": maxChunks})
        if blocked:
            return blocked
        return _limit(context.log_provider.find_error_chunks(chunkLines, maxChunks), max_chars)

    def log_detect_final_status() -> dict:
        """Detect the final Jenkins Finished status from the current build log."""
        blocked = _guard_tool_call("log_detect_final_status", {})
        if blocked:
            return blocked
        return {"status": context.log_provider.detect_final_status()}

    def repo_get_commits_between() -> dict:
        """Get commits between the context base and head commits."""
        blocked = _guard_tool_call("repo_get_commits_between", {})
        if blocked:
            return blocked
        return _limit(
            context.git_client.get_commits_between(context.repo, context.base_commit or "", context.head_commit or ""),
            max_chars,
        )

    def repo_get_diff_files() -> dict:
        """Get files changed between the context base and head commits."""
        blocked = _guard_tool_call("repo_get_diff_files", {})
        if blocked:
            return blocked
        return _limit(
            context.git_client.get_diff_files(context.repo, context.base_commit or "", context.head_commit or ""),
            max_chars,
        )

    def repo_get_file_diff(path: str, contextLines: int = 8) -> dict:
        """Get the Git diff for one path between the context base and head commits."""
        blocked = _guard_tool_call("repo_get_file_diff", {"path": path, "contextLines": contextLines})
        if blocked:
            return blocked
        return _limit(
            context.git_client.get_file_diff(context.repo, context.base_commit or "", context.head_commit or "", path, contextLines),
            max_chars,
        )

    def repo_get_file_content(commit: str, path: str, startLine: int | None = None, endLine: int | None = None) -> dict:
        """Get file content at a commit, optionally limited to a line range."""
        blocked = _guard_tool_call("repo_get_file_content", {"commit": commit, "path": path, "startLine": startLine, "endLine": endLine})
        if blocked:
            return blocked
        return _limit(context.git_client.get_file_content(context.repo, commit, path, startLine, endLine), max_chars)

    def repo_find_paths(
        query: str | None = None,
        paths: list[str] | None = None,
        suffixes: list[str] | None = None,
        maxMatches: int = 50,
    ) -> dict:
        """Find real file paths at the context head commit before reading file content. Use this before repo_get_file_content when the exact path is uncertain."""
        blocked = _guard_tool_call("repo_find_paths", {"query": query, "paths": paths or [], "suffixes": suffixes or [], "maxMatches": maxMatches})
        if blocked:
            return blocked
        return _limit(
            find_paths(
                context.git_client,
                context.repo,
                context.head_commit or "",
                query=query,
                paths=paths or [],
                suffixes=suffixes or [],
                maxMatches=maxMatches,
            ),
            max_chars,
        )

    def repo_keyword_search(keywords: list[str], scope: str = "changed_files", paths: list[str] | None = None, maxMatches: int = 50) -> dict:
        """Search keywords. Valid scope values: changed_files, paths, whole_repo. Use scope=paths with paths=[...] for specific directories/files. Aliases repo/repository/all are accepted as whole_repo."""
        blocked = _guard_tool_call("repo_keyword_search", {"keywords": keywords, "scope": scope, "paths": paths or [], "maxMatches": maxMatches})
        if blocked:
            return blocked
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
        """Analyze changed TypeScript or TSX functions for context changed files."""
        blocked = _guard_tool_call("ts_analyze_changed_functions", {"files": files or []})
        if blocked:
            return blocked
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
        """Find TypeScript definitions for symbols at the context head commit."""
        blocked = _guard_tool_call("ts_find_definitions", {"symbols": symbols})
        if blocked:
            return blocked
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
        """Find TypeScript callers of a symbol at the context head commit."""
        blocked = _guard_tool_call("ts_find_callers", {"symbol": symbol, "definitionFile": definitionFile, "maxResults": maxResults})
        if blocked:
            return blocked
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
        """Check node_modules, tsconfig, and dependency marker readiness for TypeScript analysis."""
        blocked = _guard_tool_call("check_node_dependencies_for_analysis", {})
        if blocked:
            return blocked
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
        (
            "repo_find_paths",
            "Find real file paths at the context head commit before reading file content. Use this before repo_get_file_content when the exact path is uncertain.",
            repo_find_paths,
        ),
        (
            "repo_keyword_search",
            "Search keywords. Valid scope values: changed_files, paths, whole_repo. Use scope=paths with paths=[...] to search specific directories/files. Use scope=whole_repo for the whole repository. Aliases repo/repository/all are accepted as whole_repo.",
            repo_keyword_search,
        ),
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
