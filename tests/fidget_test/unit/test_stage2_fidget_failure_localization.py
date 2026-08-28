"""阶段二（Fidget）测试：Japa 失败定位与摘要提取。

覆盖 log_parsing.find_focused_failure_chunks / find_test_failure_summaries
对 Fidget Japa 失败日志的识别，使用 samples/fidget_log/ 下的真实失败样本：
- err1-source-tampered.log：72 个 Japa 失败（源码逻辑被改坏）
- err2-test-assert.log：1 个 Japa 失败（测试输入被改坏）

不依赖 tmp_path（避开 Windows 临时目录权限问题），直接从样例文件读取。
"""

from __future__ import annotations

from pathlib import Path

from ci_owner_agent.services.log_parsing import (
    find_focused_failure_chunks,
    find_test_failure_summaries,
)

_SAMPLES = Path(__file__).resolve().parents[3] / "samples" / "fidget_log"


def _load(name: str) -> list[str]:
    return (_SAMPLES / name).read_text(encoding="utf-8").splitlines()


def test_multi_failure_log_yields_japa_blocks():
    lines = _load("err1-source-tampered.log")
    result = find_focused_failure_chunks(lines, tail_lines=500, max_chunks=5)
    chunks = result["chunks"]
    assert chunks, "should locate at least one Japa failure block"
    assert chunks[0]["anchorType"] == "japa_failure_block"
    assert chunks[0]["chunkSource"] == "japa_failure_block"
    # 失败行应以 Japa 失败符号开头
    first = chunks[0]["content"].strip().splitlines()[0]
    assert "✖" in first


def test_multi_failure_log_summary_signatures():
    lines = _load("err1-source-tampered.log")
    result = find_test_failure_summaries(lines, tail_lines=500, max_chunks=5)
    chunks = result["chunks"]
    assert chunks, "should build summary chunks from Japa failures"
    sig = chunks[0].get("signature", {})
    assert sig.get("signatureKey")
    assert "japa" in chunks[0]["chunkSource"] or chunks[0]["anchorType"] == "japa_failure_block"


def test_single_failure_log_yields_japa_block():
    lines = _load("err2-test-assert.log")
    result = find_focused_failure_chunks(lines, tail_lines=500, max_chunks=5)
    chunks = result["chunks"]
    assert chunks, "should locate the single Japa failure block"
    assert chunks[0]["anchorType"] == "japa_failure_block"
    first = chunks[0]["content"].strip().splitlines()[0]
    assert "✖" in first


def test_success_log_has_no_failure_block():
    lines = _load("fidget-build-dev-313.log")  # SUCCESS，无 Japa 失败
    result = find_focused_failure_chunks(lines, tail_lines=500, max_chunks=5)
    assert result["chunks"] == [] or "warning" in result
    summaries = find_test_failure_summaries(lines, tail_lines=500, max_chunks=5)
    assert not summaries["chunks"], "successful build must not produce failure summaries"
