"""阶段三（Fidget 集成失败分析）测试：协议 index、marker 解析、逐块分类。

覆盖 log_parsing 新增的：
- parse_integration_marker / build_integration_protocol_index（严格校验 + fail-closed）
- classify_japa_block（逐块 assertion / connection / unknown）

使用 samples/fidget_log/integration/ 下的真实脱敏样本。
通用性：不写死具体 suite 名或失败用例，只按 marker/错误类型判定。
"""

from __future__ import annotations

from pathlib import Path

from ci_owner_agent.services.log_parsing import (
    build_integration_protocol_index,
    classify_japa_block,
    parse_integration_marker,
)

_SAMPLES = Path(__file__).resolve().parents[3] / "samples" / "fidget_log" / "integration"


def _lines(name: str) -> list[str]:
    return (_SAMPLES / name).read_text(encoding="utf-8").splitlines()


def test_parse_integration_marker_fields():
    line = "FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=failed exit=1 run_id=abc-123"
    fields = parse_integration_marker(line)
    assert fields["phase"] == "preflight"
    assert fields["step"] == "image_pull"
    assert fields["status"] == "failed"
    assert fields["exit"] == "1"


def test_parse_integration_marker_rejects_non_marker():
    assert parse_integration_marker("plain log line") == {}
    assert parse_integration_marker("FIDGET_INTEGRATION_V2 phase=x") == {}


def test_protocol_index_detects_integration():
    assert build_integration_protocol_index(_lines("failure-assertion.log"))["is_integration"] is True


def test_protocol_index_detects_preflight_failure():
    index = build_integration_protocol_index(_lines("failure-pipeline-image-pull.log"))
    assert [f["step"] for f in index["preflight_failures"]] == ["image_pull"]


def test_protocol_index_suite_ranges():
    index = build_integration_protocol_index(_lines("failure-assertion.log"))
    suite_names = [r["name"] for r in index["suite_ranges"]]
    assert len(suite_names) == 8
    assert "select-integration" in suite_names


def test_protocol_index_flags_run_id_conflict():
    lines = [
        "FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=start run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=end exit=0 run_id=r2",
    ]
    index = build_integration_protocol_index(lines)
    assert any("run_id 不一致" in c for c in index["conflicts"])


def test_protocol_index_flags_missing_step_on_failure():
    lines = [
        "FIDGET_INTEGRATION_V1 phase=preflight status=failed run_id=r1",
    ]
    index = build_integration_protocol_index(lines)
    assert any("缺 step" in c for c in index["conflicts"])


def test_protocol_index_flags_summary_total_mismatch():
    lines = [
        "FIDGET_INTEGRATION_V1 phase=summary status=failed total=7 passed=7 failed=0 not_started=0 run_id=r1",
    ]
    index = build_integration_protocol_index(lines)
    assert any("total=7" in c for c in index["conflicts"])


def test_classify_japa_block_assertion():
    assert classify_japa_block(["AssertionError: expected true to equal false"]) == "assertion"


def test_classify_japa_block_connection():
    assert classify_japa_block(["Error: connect ECONNREFUSED 127.0.0.1:5432"]) == "connection"


def test_classify_japa_block_unknown():
    assert classify_japa_block(["some vague error"]) == "unknown"


def test_classify_japa_block_assertion_wins_over_connection():
    """含 AssertionError 时优先判 assertion（已进入测试 body，是代码问题）。"""
    assert (
        classify_japa_block(["Error: connect ECONNREFUSED", "AssertionError: expected X"]) == "assertion"
    )
