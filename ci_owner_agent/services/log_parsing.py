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
XFAIL_BLOCK_RE = re.compile(r"^\s*✖\s+(.+?)\s*$")
ERROR_LINE_RE = re.compile(r"\b(AssertionError|Error|TypeError|ReferenceError):\s*(.*)")
# Japa prints an early ``✖ <test>`` result and, after other test output, a
# detailed ``❯ <group> / <test>`` error section.  The two can be separated by
# hundreds of lines in Docker/BuildKit's merged stream.
JAPA_ERROR_HEADER_RE = re.compile(r"^\s*❯\s+(?P<label>.+?)\s*$")
JAPA_DURATION_SUFFIX_RE = re.compile(r"\s*\(\d+(?:\.\d+)?(?:ms|s)\)\s*$", re.IGNORECASE)
# Nx task 输出块边界（Fidget 二期，用于把覆盖率报错归属到具体包）：
#   nx 包头：  > nx run @fx/fidget-sql:"test:coverage"
#   npm 行：   > @fx/fidget-sql@1.8.0-dev.0 test:coverage
NX_RUN_HEADER_RE = re.compile(r">\s*nx\s+run\s+@fx/(?P<pkg>[\w-]+):\"(?P<target>[\w:-]+)\"")
NPM_PKG_HEADER_RE = re.compile(r">\s*@fx/(?P<pkg>[\w-]+)@")
# c8 check-coverage 门槛失败识别（Fidget 二期）：
#   per-file 形态带目标文件：   Coverage for statements (87%) does not meet threshold (100%) for src/foo.ts
#   global 形态不带文件：      Coverage for statements (87%) does not meet global threshold (100%)
COVERAGE_ERROR_RE = re.compile(
    r"^\s*ERROR:\s*Coverage for (?P<metric>\w+)\s+\(\s*(?P<pct>[\d.]+)%\s*\)\s+"
    r"does\s+not\s+meet\s+(?P<thr_kind>global\s+)?threshold\s+\(\s*(?P<thr>[\d.]+)%\s*\)"
    r"(?:\s+for\s+(?P<file>\S+))?\s*$",
    re.IGNORECASE,
)
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
    from_explicit_checkout_stage: bool = False


def resolve_checkout_revision_from_console_log(text: str) -> CheckoutCommitResolution:
    clean = ANSI_RE.sub("", text)
    candidates: set[str] = set()
    refs_by_commit: dict[str, set[str]] = {}
    checkout_stage_candidates: set[str] = set()
    current_stage: str | None = None

    for line in clean.splitlines():
        stage_match = re.match(r"^\s*\[Pipeline\]\s+\{\s+\((?P<stage>.+)\)\s*$", line)
        if stage_match:
            current_stage = stage_match.group("stage").strip()
            continue
        if re.match(r"^\s*\[Pipeline\]\s+//\s+stage\s*$", line):
            current_stage = None
            continue

        revision_match = CHECKING_OUT_REVISION_RE.match(line)
        force_match = GIT_CHECKOUT_FORCE_RE.match(line)
        match = revision_match or force_match
        if match is None:
            continue
        commit = match.group("commit").lower()
        candidates.add(commit)
        if revision_match and revision_match.group("ref"):
            refs_by_commit.setdefault(commit, set()).add(revision_match.group("ref").strip())
        if current_stage and current_stage.strip().lower() == "checkout":
            checkout_stage_candidates.add(commit)

    # “Pipeline script from SCM” may checkout an older Jenkinsfile before any stage.
    # When the log later has one distinct SHA inside the explicit Checkout stage,
    # that later checkout is the source tree that the pipeline actually tests.
    selected = checkout_stage_candidates if len(checkout_stage_candidates) == 1 and len(candidates) > 1 else candidates
    if len(selected) == 1:
        commit = next(iter(selected))
        return CheckoutCommitResolution(
            commit,
            refs=tuple(sorted(refs_by_commit.get(commit, set()))),
            from_explicit_checkout_stage=selected is checkout_stage_candidates,
        )
    refs = tuple(sorted({ref for values in refs_by_commit.values() for ref in values}))
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


def find_coverage_errors(all_lines: list[str]) -> list[dict]:
    """扫描日志，找出所有 c8 check-coverage 门槛失败，并在可归属性时记录包名。

    返回每条：{line, metric, pct, threshold, kind, file, package_name}。
    package_name 采用「受约束 B」：仅当报错行落在某个 Nx task 输出块内、且该块的
    nx 包头与 npm 生命周期行包名一致时才写入；否则为 None（由调用方用 C 消歧）。
    仅 pct < threshold 才算门槛失败（对齐 c8）。
    """
    # 预扫描：逐行推进，记录每一行属于哪个"已确认"的包（None=无确认归属）。
    # 逻辑：nx 包头开始一个新 task 块；块内的 npm 生命周期行包名与之一致则确认该块归属；
    # coverage 报错继承当前块已确认的包名；nx 包头后未出现一致的 npm 行则归属存疑(None)。
    confirmed_pkg: str | None = None
    confirmed_in_block: bool = False
    pkg_by_line: dict[int, str | None] = {}

    for idx, raw in enumerate(all_lines):
        clean = _semantic_log_line(raw)
        nx_m = NX_RUN_HEADER_RE.match(clean) if clean.startswith(">") else None
        npm_m = NPM_PKG_HEADER_RE.match(clean) if clean.startswith(">") else None
        if nx_m is not None:
            confirmed_pkg = nx_m.group("pkg")
            confirmed_in_block = False
            pkg_by_line[idx] = confirmed_pkg if confirmed_in_block else None
            continue
        if npm_m is not None:
            if npm_m.group("pkg") == confirmed_pkg:
                confirmed_in_block = True
            else:
                confirmed_pkg = None
                confirmed_in_block = False
            pkg_by_line[idx] = confirmed_pkg if confirmed_in_block else None
            continue
        pkg_by_line[idx] = confirmed_pkg if confirmed_in_block else None

    hits: list[dict] = []
    for idx, raw in enumerate(all_lines):
        clean = _semantic_log_line(raw)
        cov_m = COVERAGE_ERROR_RE.match(clean)
        if cov_m is None:
            continue
        pct = float(cov_m.group("pct"))
        threshold = float(cov_m.group("thr"))
        if pct >= threshold:
            continue
        hits.append(
            {
                "line": idx + 1,
                "content": clean,
                "metric": cov_m.group("metric"),
                "pct": pct,
                "threshold": threshold,
                "kind": "global" if cov_m.group("thr_kind") else "per_file",
                "file": cov_m.group("file"),
                "package_name": pkg_by_line.get(idx),
            }
        )
    return hits


def count_japa_failure_blocks(all_lines: list[str]) -> int:
    """Count all structured Japa failure anchors without applying a display budget."""
    return sum(1 for line in all_lines if XFAIL_BLOCK_RE.match(_semantic_log_line(line)))


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

    # Fidget/Japa 失败定位：直接在清洗后的日志中收集 Japa `✖` 失败行。
    # 每行先剥 BuildKit `#N 时间戳` 前缀与 ANSI 色码，再用 Japa 失败正则命中 `✖ 标题`。
    # 该方式同时适用于本地 `node ./bin/test_runner.mjs` 与 Jenkins BuildKit 合并单流输出，
    # 不依赖任何特定 Stage/命令锚点。
    clean_lines = [_semantic_log_line(line) for line in all_lines]
    xfail_starts = [idx for idx, line in enumerate(clean_lines) if XFAIL_BLOCK_RE.match(line)]
    if xfail_starts:
        chunks = []
        for chunk_index, start in enumerate(xfail_starts[: max(1, max_chunks)]):
            detail_start = _find_japa_error_detail_start(clean_lines, start)
            if detail_start is not None:
                detail_end = _find_japa_error_detail_end(clean_lines, detail_start)
                detail_segment = [
                    all_lines[start],
                    "...[interleaved Japa output omitted]...",
                    *all_lines[detail_start:detail_end],
                ]
                chunk = _focused_chunk_from_segment(
                    detail_segment,
                    start_line=start + 1,
                    end_line=detail_end,
                    tail_lines=count,
                    max_output_chars=max_output_chars,
                    chunk_source="japa_failure_block",
                    stage_name="Unit Tests",
                    step_name="test_runner",
                    anchor_type="japa_failure_block",
                    prior_truncated=True,
                )
            else:
                block_end = start
                for idx in range(start + 1, len(clean_lines)):
                    if XFAIL_BLOCK_RE.match(clean_lines[idx]):
                        break
                    block_end = idx
                # 收敛失败块：到下一个失败行前或日志尾部。单个 Japa 失败在
                # Docker/BuildKit 合并日志里可能一直延伸到 Jenkins footer；截断时
                # 必须同时保留开头的 ``✖`` 身份和末尾的错误/路径，不能只留尾部。
                chunk = _focused_chunk(
                    all_lines,
                    start,
                    block_end,
                    count,
                    max_output_chars=max_output_chars,
                    chunk_source="japa_failure_block",
                    stage_name="Unit Tests",
                    step_name="test_runner",
                    anchor_type="japa_failure_block",
                )
            chunk["chunkIndex"] = chunk_index
            chunks.append(chunk)
        return {"chunks": chunks[:max_chunks]}

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
        "warning": "no Japa failure block found; returned console tail fallback",
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
        max_chunks=max_chunks,
        max_output_chars=max_output_chars,
    )
    return build_test_failure_summaries(focused, max_chunks=max_chunks)


def build_test_failure_summaries(focused: dict, *, max_chunks: int = 5) -> dict:
    focused_chunks = focused.get("chunks", [])
    if not focused_chunks:
        return {"chunks": [], "warning": "focused failure chunks unavailable"}
    chunks = []
    for focused_chunk in focused_chunks:
        if focused_chunk.get("chunkSource") == "local_console_tail_fallback":
            continue
        lines = [_semantic_log_line(line) for line in str(focused_chunk.get("content") or "").splitlines()]
        xfail_starts = [idx for idx, line in enumerate(lines) if XFAIL_BLOCK_RE.match(line) and "ERROR:" not in line]
        if not xfail_starts:
            continue
        built = _build_summary_chunks(
            lines=lines,
            starts=xfail_starts,
            max_chunks=max_chunks - len(chunks),
            focused_chunk=focused_chunk,
            chunk_source="local_test_failure_summary",
            anchor_type="japa_failure_block",
            signature_extractor=_extract_xfail_signature,
            score=0.9,
        )
        for chunk in built:
            chunk["chunkIndex"] = len(chunks)
            chunks.append(chunk)
            if len(chunks) >= max_chunks:
                break
        if len(chunks) >= max_chunks:
            break
    if chunks:
        return {"chunks": chunks}
    # Docker, BuildKit, Jenkins, shell, typecheck, and lint failures are not
    # stable enough for deterministic historical inheritance.  They remain
    # current-build evidence and must be handled by the Agent when needed.
    return {"chunks": [], "warning": "test failure summaries unavailable; no Japa failure block found; history similarity skipped"}


def build_coverage_failure_summaries(
    all_lines: list[str],
    *,
    max_chunks: int | None = 5,
) -> list[dict]:
    """把 c8 门槛失败聚合为可审计的 coverage chunks。

    per-file 以文件为粒度聚合：同一文件的 statements/branches/functions/lines
    只生成一个 chunk。global 没有文件，只能按 package + metric 保守建项。
    `max_chunks=None` 返回完整事实，供 provider 做统一预算和 omitted 记账。
    """
    errors = find_coverage_errors(all_lines)
    if not errors:
        return []

    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for error in errors:
        package = str(error.get("package_name") or "")
        if error.get("kind") == "global":
            key = ("global", package, str(error.get("metric") or "coverage").lower())
        else:
            key = ("per_file", package, str(error.get("file") or "").replace("\\", "/"))
        grouped.setdefault(key, []).append(error)

    chunks: list[dict] = []
    for chunk_index, ((kind, package, identity), group) in enumerate(grouped.items()):
        if max_chunks is not None and len(chunks) >= max(0, max_chunks):
            break
        raw_path = identity if kind == "per_file" else None
        metrics: dict[str, dict[str, float]] = {}
        for error in group:
            metric = str(error.get("metric") or "coverage").lower()
            value = {"pct": float(error["pct"]), "threshold": float(error["threshold"])}
            previous = metrics.get(metric)
            if previous is None or value["pct"] < previous["pct"]:
                metrics[metric] = value
        metric_names = sorted(metrics)
        if kind == "global":
            signature_key = f"coverage_threshold_failure|global|{package or 'unresolved'}|{identity}"
        else:
            file_scope = f"packages/{package}/{raw_path}" if package else f"unresolved|{raw_path}"
            signature_key = f"coverage_threshold_failure|{file_scope}"
        content = "\n".join(str(error.get("content") or "") for error in group)
        signature = {
            "failureKind": "coverage_threshold_failure",
            "testName": "coverage threshold failure",
            "testCase": "coverage threshold failure",
            "errorType": "CoverageError",
            "errorMessage": f"{','.join(metric_names)} below coverage threshold",
            "testFile": None,
            "packageName": package or None,
            "topStackFile": None,
            "rawCoveragePath": raw_path,
            "businessStackFiles": [],
            "metrics": metrics,
            "signatureKey": signature_key,
        }
        chunks.append(
            {
                "chunkIndex": chunk_index,
                "schemaVersion": 3,
                "chunkSource": "c8_coverage_threshold_failure",
                "stageName": "Unit Tests",
                "stepName": "test_runner",
                "anchorType": "coverage_failure_block",
                "startLine": min(int(error["line"]) for error in group),
                "endLine": max(int(error["line"]) for error in group),
                "score": 0.9,
                "content": content,
                "truncated": False,
                "signature": signature,
                # pct/threshold 属于详情，会随构建变化；hash 只锚定稳定 signatureKey。
                "signatureHash": hashlib.sha256(signature_key.encode("utf-8")).hexdigest(),
            }
        )
    return chunks


def _focused_chunk(all_lines: list[str], start_idx: int, end_idx: int, tail_lines: int, *, max_output_chars: int, chunk_source: str, stage_name: str | None, step_name: str | None, anchor_type: str) -> dict:
    segment = all_lines[start_idx : end_idx + 1]
    return _focused_chunk_from_segment(
        segment,
        start_line=start_idx + 1,
        end_line=end_idx + 1,
        tail_lines=tail_lines,
        max_output_chars=max_output_chars,
        chunk_source=chunk_source,
        stage_name=stage_name,
        step_name=step_name,
        anchor_type=anchor_type,
    )


def _focused_chunk_from_segment(segment: list[str], *, start_line: int, end_line: int, tail_lines: int, max_output_chars: int, chunk_source: str, stage_name: str | None, step_name: str | None, anchor_type: str, prior_truncated: bool = False) -> dict:
    selected, line_truncated = _keep_failure_block_edges(segment, tail_lines)
    content, text_truncated = _truncate_failure_block_text("\n".join(selected), max_output_chars)
    return {"chunkIndex": 0, "schemaVersion": 2, "chunkSource": chunk_source, "stageName": stage_name, "stepName": step_name, "anchorType": anchor_type, "startLine": start_line, "endLine": end_line, "score": 1.0, "content": content, "truncated": text_truncated or line_truncated or prior_truncated}


def _build_summary_chunks(*, lines: list[str], starts: list[int], max_chunks: int, focused_chunk: dict, chunk_source: str, anchor_type: str, signature_extractor: Callable[[str], dict], score: float) -> list[dict]:
    chunks = []
    for chunk_index, start in enumerate(starts[:max_chunks]):
        end = starts[chunk_index + 1] if chunk_index + 1 < len(starts) else len(lines)
        chunks.append(_summary_chunk(chunk_index=chunk_index, lines=lines, start=start, end=end, focused_chunk=focused_chunk, chunk_source=chunk_source, anchor_type=anchor_type, signature_extractor=signature_extractor, score=score))
    return chunks


def _summary_chunk(*, chunk_index: int, lines: list[str], start: int, end: int, focused_chunk: dict, chunk_source: str, anchor_type: str, signature_extractor: Callable[[str], dict], score: float) -> dict:
    end = _trim_failure_block_end(lines, start, end)
    block_lines, line_truncated = _keep_failure_block_edges(lines[start:end], 120)
    content, text_truncated = _truncate_failure_block_text("\n".join(block_lines), 12000)
    truncated = line_truncated or text_truncated
    signature = signature_extractor(content)
    start_line = (focused_chunk.get("startLine") or 1) + start
    end_line = start_line + max(0, len(block_lines) - 1)
    return {"chunkIndex": chunk_index, "schemaVersion": 3, "chunkSource": chunk_source, "stageName": focused_chunk.get("stageName"), "stepName": focused_chunk.get("stepName"), "anchorType": anchor_type, "startLine": start_line, "endLine": end_line, "score": score, "content": content, "truncated": truncated, "signature": signature, "signatureHash": _signature_hash(signature)}


def _strip_docker_log_prefix(line: str) -> str:
    match = re.match(r"^#\d+\s+(?:\d+(?:\.\d+)?\s+)?(.*)$", line)
    return match.group(1) if match else line


def _semantic_log_line(line: str) -> str:
    return ANSI_RE.sub("", _strip_docker_log_prefix(line)).strip()


def _find_japa_error_detail_start(clean_lines: list[str], xfail_start: int) -> int | None:
    xfail = XFAIL_BLOCK_RE.match(clean_lines[xfail_start])
    if not xfail:
        return None
    title = _normalize_japa_title(xfail.group(1))
    for idx in range(xfail_start + 1, len(clean_lines)):
        header = JAPA_ERROR_HEADER_RE.match(clean_lines[idx])
        if header and _normalize_japa_title(header.group("label").rsplit(" / ", 1)[-1]) == title:
            return idx
    return None


def _find_japa_error_detail_end(clean_lines: list[str], detail_start: int, *, max_lines: int = 120) -> int:
    end = min(len(clean_lines), detail_start + max(1, max_lines))
    for idx in range(detail_start + 1, end):
        line = clean_lines[idx]
        if (
            line == "FAILED"
            or line == "PASSED"
            or line.startswith("Tests  ")
            or line.startswith("Time  ")
            or line.startswith("===")
            or line.startswith("npm error")
            or line.startswith("> nx run ")
        ):
            return idx
    return end


def _normalize_japa_title(value: str) -> str:
    return JAPA_DURATION_SUFFIX_RE.sub("", value).strip().casefold()


def _keep_failure_block_edges(lines: list[str], max_lines: int) -> tuple[list[str], bool]:
    """Bound a failure block without dropping its ``✖`` anchor or final evidence."""
    if max_lines <= 0 or len(lines) <= max_lines:
        return lines, False
    head_count = max(1, max_lines // 2)
    tail_count = max(1, max_lines - head_count)
    return [*lines[:head_count], "...[truncated failure block middle]...", *lines[-tail_count:]], True


def _truncate_failure_block_text(text: str, max_output_chars: int) -> tuple[str, bool]:
    """Keep both stable Japa identity and trailing failure evidence when bounded."""
    if max_output_chars <= 0 or len(text) <= max_output_chars:
        return text, False
    marker = "\n...[truncated failure block middle]...\n"
    remaining = max(0, max_output_chars - len(marker))
    head_count = (remaining + 1) // 2
    tail_count = remaining - head_count
    return text[:head_count] + marker + text[-tail_count:], True


def _trim_failure_block_end(lines: list[str], start: int, end: int) -> int:
    for idx in range(start + 1, end):
        if any(lines[idx].strip().startswith(term) for term in FOOTER_TERMS):
            return idx
    return end


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
    # For Japa assertions, the framework's node_modules frame is not the
    # failing file.  Prefer the concrete test path whenever it is available.
    top_stack_file = test_file or next((path for path in files if "node_modules/" not in path), None)
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
