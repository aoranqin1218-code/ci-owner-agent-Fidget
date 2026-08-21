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
                "metric": cov_m.group("metric"),
                "pct": pct,
                "threshold": threshold,
                "kind": "global" if cov_m.group("thr_kind") else "per_file",
                "file": cov_m.group("file"),
                "package_name": pkg_by_line.get(idx),
            }
        )
    return hits


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
            block_end = start
            for idx in range(start + 1, len(clean_lines)):
                if XFAIL_BLOCK_RE.match(clean_lines[idx]):
                    break
                block_end = idx
            # 收敛失败块：到下一个失败行前或日志尾部。后续包装 footer
            # 会在摘要阶段裁掉，不能在这里硬截断，否则可能丢失失败块末尾
            # 的错误消息和 packages/<package>/test 路径。
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


def build_coverage_failure_summaries(all_lines: list[str], *, max_chunks: int = 5) -> list[dict]:
    """把 c8 覆盖率门槛失败转成 failure summary chunks，与 Japa 失败块平级。

    仅当存在覆盖率错误时返回非空列表；每个 per-file/global 门槛失败一条。
    用 c8 原始 ERROR 行作为块 content，签名基于 metric + 目标文件（稳定可去重）。
    """
    errors = find_coverage_errors(all_lines)
    if not errors:
        return []
    coverage_lines = []
    for e in errors:
        file_part = f" for {e['file']}" if e.get("file") else ""
        # 若已确认可信包名（B），拼上仓库相对前缀，使签名/后续 reconcile 能识别归属包。
        if e.get("file") and e.get("package_name"):
            file_part = f" for packages/{e['package_name']}/{e['file'].lstrip('/')}"
        coverage_lines.append(
            f"ERROR: Coverage for {e['metric']} ({e['pct']}%) "
            f"does not meet {'global ' if e['kind'] == 'global' else ''}threshold "
            f"({e['threshold']}%){file_part}"
        )
    chunks = _build_summary_chunks(
        lines=coverage_lines,
        starts=list(range(len(coverage_lines))),
        max_chunks=max_chunks,
        focused_chunk={"startLine": 1, "content": "\n".join(coverage_lines)},
        chunk_source="local_test_failure_summary",
        anchor_type="coverage_failure_block",
        signature_extractor=_extract_coverage_signature,
        score=0.9,
    )
    for idx, chunk in enumerate(chunks):
        chunk["chunkIndex"] = idx
    return chunks[:max_chunks]


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


def _extract_coverage_signature(content: str) -> dict:
    """从 c8 覆盖率错误行生成稳定指纹。

    指纹键基于 metric + 目标文件（不含变化的 pct/threshold），保证同一文件同一指标
    的覆盖率失败可去重、可寻历史。
    """
    from ci_owner_agent.services.failure_identity import build_responsibility_signature

    lines = content.splitlines()
    match = next((COVERAGE_ERROR_RE.match(line) for line in lines if COVERAGE_ERROR_RE.match(line)), None)
    metric = match.group("metric") if match else "coverage"
    raw_file = match.group("file") if match and match.group("file") else None
    threshold = match.group("thr") if match else "100"
    # 重建行可能带仓库前缀 `packages/<pkg>/...`（B 已确认包名时），拆出包名与包内相对路径
    package_name: str | None = None
    file_path = raw_file
    if raw_file and raw_file.startswith("packages/"):
        parts = raw_file.split("/", 2)
        if len(parts) >= 2:
            package_name = parts[1]
        if len(parts) >= 3:
            file_path = parts[2]
    title = f"coverage {metric} below {threshold}%"
    error_message = _stable_error_message(f"{metric} below {threshold}% threshold")
    test_file = None
    top_stack_file = file_path
    failure_title = f"{metric} coverage below {threshold}%"
    signature_key = build_responsibility_signature(
        failure_title=failure_title,
        failure_summary="\n".join((content, error_message)),
        existing_signature="|".join(["coverage", metric, file_path or ""]),
        failure_kind="coverage",
        error_type="CoverageError",
        test_file_path=None,
        failure_file_path=file_path,
    )
    return {
        "testName": failure_title,
        "testCase": failure_title,
        "errorType": "CoverageError",
        "errorMessage": error_message,
        "testFile": None,
        "packageName": package_name,
        "topStackFile": file_path,
        "businessStackFiles": [file_path] if file_path else [],
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
