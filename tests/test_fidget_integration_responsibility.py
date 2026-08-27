"""阶段三（Fidget 集成失败分析）测试：环境-only 短路判定与责任保护。

覆盖：
- orchestrator._is_integration_env_only_failures（短路判定纯函数）；
- 真实 provider 端到端：环境失败不进 history-eligible chunks，代码失败正常保留；
- 混合失败保留两类，环境项不污染代码责任。

不触发 Jenkins/Agent/Mongo。
"""

from __future__ import annotations

from pathlib import Path

from ci_owner_agent.orchestrator import (
    _is_coverage_only_failures,
    _is_integration_env_only_failures,
)
from ci_owner_agent.services.log_provider import LocalFileLogProvider

_SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "fidget_log" / "integration"


def _summaries(totals: dict, chunks: list[dict] | None = None) -> dict:
    return {"totals": totals, "chunks": chunks or []}


def test_env_only_short_circuits():
    summaries = _summaries({"integrationEnv": 3, "japa": 0, "coverage": 0}, [])
    assert _is_integration_env_only_failures(summaries) is True


def test_code_failure_does_not_short_circuit():
    summaries = _summaries(
        {"integrationEnv": 0, "japa": 2, "coverage": 0},
        [{"anchorType": "japa_failure_block", "chunkSource": "japa_failure_block"}],
    )
    assert _is_integration_env_only_failures(summaries) is False


def test_mixed_failure_does_not_short_circuit():
    """环境失败 + 代码失败并存时，不能短路（代码责任不能被环境清空）。"""
    summaries = _summaries(
        {"integrationEnv": 1, "japa": 1, "coverage": 0},
        [{"anchorType": "japa_failure_block", "chunkSource": "japa_failure_block"}],
    )
    assert _is_integration_env_only_failures(summaries) is False


def test_no_env_total_does_not_short_circuit():
    summaries = _summaries({"japa": 0, "coverage": 0}, [])
    assert _is_integration_env_only_failures(summaries) is False


def test_non_dict_or_missing_totals_safe():
    assert _is_integration_env_only_failures(None) is False
    assert _is_integration_env_only_failures({"chunks": []}) is False


def test_coverage_only_unchanged():
    summaries = _summaries(
        {"integrationEnv": 0, "japa": 0, "coverage": 2},
        [{"anchorType": "coverage_failure_block"}],
    )
    assert _is_coverage_only_failures(summaries) is True
    assert _is_integration_env_only_failures(summaries) is False


# ---- 真实 provider 端到端 ----


def test_protonbase_provider_produces_no_history_eligible_chunks():
    """Protonbase 环境故障：769 个连接失败不得产出任何 history-eligible chunk。"""
    result = LocalFileLogProvider(_SAMPLES / "failure-protonbase-unavailable.log").find_test_failure_summaries(max_chunks=5)
    chunks = result.get("chunks", [])
    # 连接失败全部隔离到 integrationEnvChunks，displayed chunks 应为空
    assert chunks == [], f"expected no displayed chunks, got {len(chunks)}"
    assert result["totals"]["integrationEnv"] > 0


def test_assertion_provider_keeps_code_chunks():
    """断言失败（代码问题）应保留为可定责的 chunks。"""
    result = LocalFileLogProvider(_SAMPLES / "failure-assertion.log").find_test_failure_summaries(max_chunks=5)
    chunks = result.get("chunks", [])
    assert len(chunks) == 2, f"expected 2 code chunks, got {len(chunks)}"
    assert result["totals"]["integrationEnv"] == 0


def test_image_pull_provider_short_circuits_env_only():
    result = LocalFileLogProvider(_SAMPLES / "failure-pipeline-image-pull.log").find_test_failure_summaries(max_chunks=5)
    assert result.get("chunks", []) == []
    assert result["totals"]["integrationEnv"] == 1
    assert _is_integration_env_only_failures(result) is True


def test_assertion_provider_does_not_short_circuit():
    result = LocalFileLogProvider(_SAMPLES / "failure-assertion.log").find_test_failure_summaries(max_chunks=5)
    assert _is_integration_env_only_failures(result) is False


# ---- Codex 复审场景（合成日志，避免 tmp_path 权限问题用真实样本 + 临时文件） ----


def _provider_from_lines(lines: list[str]) -> dict:
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".log", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return LocalFileLogProvider(Path(path)).find_test_failure_summaries(max_chunks=5)
    finally:
        os.unlink(path)


def _complete_preflight(run_id: str = "r1") -> list[str]:
    """构造协议完整的 preflight + config 前缀（与真实 runner marker 格式一致）。"""
    return [
        f"FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=start image=mongo:4.2 run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=end exit=0 run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=preflight step=network_create status=start run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=preflight step=network_create status=end run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=preflight step=mongo_start status=start run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=preflight step=mongo_start status=end run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=preflight step=mongo_ready status=start run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=preflight step=mongo_ready status=end run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=config mongo_target=fidget-mongo-{run_id} run_id={run_id}",
    ]


_ALL_SUITES = [
    "select-integration", "stream-select-integration", "delete-integration", "insert-integration",
    "update-integration", "upsert-integration", "bulk-integration", "shadow-integration",
]


def _complete_suite_markers(run_id: str = "r1", failed_suites: tuple[str, ...] = ()) -> list[str]:
    """构造协议完整的 8 个 suite marker（start/end 各 8，failed 指定非零 exit）。"""
    lines: list[str] = []
    for suite in _ALL_SUITES:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id={run_id}")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit={'1' if suite in failed_suites else '0'} run_id={run_id}")
    return lines


def _summary_cleanup(run_id: str = "r1", failed: int = 0) -> list[str]:
    """构造 summary + cleanup marker（total=8，failed 与 suite 一致）。"""
    return [
        f"FIDGET_INTEGRATION_V1 phase=summary status={'failed' if failed else 'success'} total=8 passed={8 - failed} failed={failed} not_started=0 run_id={run_id}",
        f"FIDGET_INTEGRATION_V1 phase=cleanup status=success run_id={run_id}",
    ]


def test_budget_external_assertion_not_lost():
    """前 5 个连接失败 + 第 6 个断言失败：断言块不得被预算丢弃，不得错误短路 env-only。"""
    lines = _complete_preflight()
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1"]
    for i in range(5):
        lines += [f"✖ conn test {i} (1ms)", f"❯ group / conn test {i}", "Error: connect ECONNREFUSED 127.0.0.1:5432"]
    lines += ["✖ assert test (1ms)", "❯ group / assert test", "AssertionError: expected true"]
    # 8 个 suite 全部 end（select 非零），summary failed=1
    lines.append("FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1")
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += _summary_cleanup(run_id="r1", failed=1)
    result = _provider_from_lines(lines)
    # 断言块必须保留（预算外代码失败不丢），且不能短路 env-only
    assert len(result["chunks"]) == 1, f"expected 1 assertion chunk, got {len(result['chunks'])}"
    assert _is_integration_env_only_failures(result) is False


def test_unit_only_econnrefused_not_integration_env():
    """Unit-only 的 ECONNREFUSED 不触发集成分类，不计入 integrationEnv。"""
    lines = ["[Pipeline] { (Unit Tests)", "✖ unit conn test", "Error: connect ECONNREFUSED 127.0.0.1:5432"]
    result = _provider_from_lines(lines)
    assert result["totals"]["integrationEnv"] == 0


def test_unknown_japa_block_is_no_owner_not_code():
    """unknown 分类（无断言无连接错误）不得进入 code chunks，应计为环境 no-owner。"""
    lines = [
        "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1",
        "✖ vague failure (1ms)",
        "❯ group / vague failure",
        "some vague error without assertion or connection",
        "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=summary status=failed total=8 passed=7 failed=1 not_started=0 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=cleanup status=success run_id=r1",
    ]
    result = _provider_from_lines(lines)
    assert result["chunks"] == [], f"unknown must not be a code chunk, got {len(result['chunks'])}"
    assert result["totals"]["integrationEnv"] > 0


def test_illegal_phase_and_run_id_fail_closed():
    """非法 phase + 非法 run_id 应产生 conflicts，不静默通过。"""
    lines = [
        "FIDGET_INTEGRATION_V1 phase=bogus run_id=BAD$",
        "FIDGET_INTEGRATION_V1 phase=summary status=failed total=8 passed=7 failed=1 not_started=0 run_id=BAD$",
    ]
    result = _provider_from_lines(lines)
    conflicts = result.get("integrationConflicts", [])
    assert any("phase" in c for c in conflicts), f"expected unknown phase conflict, got {conflicts}"
    assert any("run_id" in c for c in conflicts), f"expected run_id format conflict, got {conflicts}"


def test_unit_only_assertion_has_no_integration_conflict():
    """Unit-only 断言日志不得伪造出集成协议冲突责任项（P1-1）。"""
    lines = ["[Pipeline] { (Unit Tests)", "✖ unit test", "❯ group / unit test", "AssertionError: expected"]
    result = _provider_from_lines(lines)
    assert result.get("integrationConflicts") == []
    assert result["totals"]["integrationEnv"] == 0
    assert len(result["chunks"]) == 1


def test_mixed_stage_unit_block_not_integration_env():
    """Unit + Integration 混合：Unit 块保持 Unit 语义，不计 integrationEnv（P1-3）。"""
    lines = [
        "[Pipeline] { (Unit Tests)",
        "✖ unit conn", "❯ group / unit conn", "Error: connect ECONNREFUSED 127.0.0.1:5432",
        "[Pipeline] // stage",
        "[Pipeline] { (Integration Tests)",
    ]
    lines += _complete_preflight()
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1"]
    lines += ["✖ int assert", "❯ group / int assert", "AssertionError: expected"]
    lines.append("FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1")
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += _summary_cleanup(run_id="r1", failed=1)
    result = _provider_from_lines(lines)
    kinds = {c["kind"] for c in result["integrationClassifications"]}
    assert "unit" in kinds, "Unit 块应标为 unit"
    assert result["totals"]["integrationEnv"] == 0, "Unit 的 ECONNREFUSED 不得计环境"
    # Unit 块与 Integration assertion 都是代码失败，均保留为 chunk（Unit 保持回归语义）。
    assert len(result["chunks"]) == 2, "Unit 块与 Integration assertion 都应保留为代码块"
    stage_names = {c["stageName"] for c in result["chunks"]}
    assert "Unit Tests" in stage_names and "Integration Tests" in stage_names


def test_integration_stage_without_marker_is_conflict():
    """Integration stage 存在但无 V1 marker → 协议不完整，标记 conflict（P1-4）。"""
    lines = ["[Pipeline] { (Integration Tests)", "✖ some failure", "❯ group / some failure", "AssertionError: x"]
    result = _provider_from_lines(lines)
    assert any("marker" in c for c in result.get("integrationConflicts", []))


def test_protocol_conflict_does_not_emit_code_chunk_and_short_circuits():
    """协议冲突时 Integration 断言不得产出 code chunk，且应确定性 no-owner。"""
    lines = ["[Pipeline] { (Integration Tests)", "✖ some failure", "❯ group / some failure", "AssertionError: x"]
    result = _provider_from_lines(lines)
    assert result["chunks"] == [], "协议冲突时 Integration 断言不得产出 code chunk"
    assert _is_integration_env_only_failures(result) is True, "协议冲突应确定性 no-owner"


def test_mixed_unit_assertion_with_integration_connection_keeps_unit_chunk():
    """Unit assertion + Integration connection：Unit 断言保留为 Unit chunk，不 env-only 短路。"""
    lines = [
        "[Pipeline] { (Unit Tests)",
        "✖ unit assert", "❯ group / unit assert", "AssertionError: expected true",
        "[Pipeline] // stage",
        "[Pipeline] { (Integration Tests)",
        "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1",
        "✖ int conn", "❯ group / int conn", "Error: connect ECONNREFUSED 127.0.0.1:5432",
        "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=summary status=failed total=8 passed=7 failed=1 not_started=0 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=cleanup status=success run_id=r1",
    ]
    result = _provider_from_lines(lines)
    assert len(result["chunks"]) == 1, "Unit 断言应保留为 code chunk"
    assert result["chunks"][0]["stageName"] == "Unit Tests"
    assert result["totals"]["integrationEnv"] > 0, "Integration 连接失败应计环境"
    assert _is_integration_env_only_failures(result) is False, "有 Unit 代码块，不得短路"


def test_mixed_unit_assertion_with_integration_conflict_keeps_unit_chunk():
    """Unit assertion + Integration 协议冲突：Unit 块保留，Integration 部分降级 no-owner。"""
    lines = [
        "[Pipeline] { (Unit Tests)",
        "✖ unit assert", "❯ group / unit assert", "AssertionError: expected true",
        "[Pipeline] // stage",
        "[Pipeline] { (Integration Tests)",
        "✖ int assert", "❯ group / int assert", "AssertionError: x",
    ]
    result = _provider_from_lines(lines)
    # 只有 Unit 断言保留为 code chunk；Integration 断言因协议冲突降级
    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["stageName"] == "Unit Tests"
    assert _is_integration_env_only_failures(result) is False, "有 Unit 代码块，不得短路"


def test_missing_preflight_and_config_is_conflict():
    """缺全部 preflight 和 config 的 Integration 日志 → conflict，断言不得进 code chunk。"""
    lines = [
        "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1",
        "✖ int assert", "❯ group / int assert", "AssertionError: x",
        "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=summary status=failed total=8 passed=7 failed=1 not_started=0 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=cleanup status=success run_id=r1",
    ]
    result = _provider_from_lines(lines)
    assert result.get("integrationConflicts"), "缺 preflight/config 应产生 conflict"
    assert result["chunks"] == [], "协议冲突时断言不得产出 code chunk"
    assert _is_integration_env_only_failures(result) is True


def test_only_mongo_ready_without_earlier_preflight_is_conflict():
    """仅执行 mongo_ready（缺 image_pull/network_create/mongo_start）→ conflict，无 Integration chunk。"""
    lines = [
        "FIDGET_INTEGRATION_V1 phase=preflight step=mongo_ready status=start run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=preflight step=mongo_ready status=end run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=config mongo_target=m run_id=r1",
    ]
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1",
              "✖ a", "❯ g / a", "AssertionError: x",
              "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1"]
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += _summary_cleanup(run_id="r1", failed=1)
    result = _provider_from_lines(lines)
    assert result.get("integrationConflicts"), "缺 preflight 前三步应 conflict"
    assert result["chunks"] == []


def test_preflight_failed_without_start_is_conflict():
    """image_pull failed 无 start → conflict。"""
    lines = [
        "FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=failed exit=1 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=summary status=failed total=8 passed=0 failed=0 not_started=8 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=cleanup status=success run_id=r1",
    ]
    result = _provider_from_lines(lines)
    assert any("failed 无对应 start" in c for c in result.get("integrationConflicts", []))


def test_preflight_repeated_complete_step_is_conflict():
    """某 step 完整结束后再次 start → conflict。"""
    lines = _complete_preflight()
    # mongo_ready 之后重复 image_pull start（顺序回退 + 已开始）
    lines.append("FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=start run_id=r1")
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1",
              "✖ a", "❯ g / a", "AssertionError: x",
              "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1"]
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += _summary_cleanup(run_id="r1", failed=1)
    result = _provider_from_lines(lines)
    assert any("image_pull" in c for c in result.get("integrationConflicts", []))


def test_preflight_failed_then_continue_is_conflict():
    """image_pull failed 后继续 preflight → conflict。"""
    lines = [
        "FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=start run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=preflight step=image_pull status=failed exit=1 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=preflight step=network_create status=start run_id=r1",
    ]
    result = _provider_from_lines(lines)
    assert any("failed" in c for c in result.get("integrationConflicts", []))


def test_summary_bogus_cleanup_end_status_is_conflict():
    """summary status=bogus、cleanup status=end → conflict。"""
    lines = _complete_preflight()
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1",
              "✖ a", "❯ g / a", "AssertionError: x",
              "FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1"]
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += [
        "FIDGET_INTEGRATION_V1 phase=summary status=bogus total=8 passed=7 failed=1 not_started=0 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=cleanup status=end run_id=r1",
    ]
    result = _provider_from_lines(lines)
    conflicts = result.get("integrationConflicts", [])
    assert any("summary" in c and "status" in c for c in conflicts)
    assert any("cleanup" in c and "status" in c for c in conflicts)


def test_free_assertion_without_suite_and_config_is_conflict():
    """完整 preflight、无 config、无 suite marker、游离 assertion → conflict、chunks=[]。"""
    lines = _complete_preflight()[:-1]  # 去掉 config 行
    lines += [
        "✖ free assert", "❯ g / free assert", "AssertionError: x",
        "FIDGET_INTEGRATION_V1 phase=summary status=failed total=8 passed=0 failed=0 not_started=8 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=cleanup status=success run_id=r1",
    ]
    result = _provider_from_lines(lines)
    assert result.get("integrationConflicts"), "无 config 应 conflict"
    assert result["chunks"] == [], "游离断言不得产出 code chunk"


def test_free_assertion_without_suite_is_unknown_no_owner():
    """完整 preflight + config、游离 assertion（无 suite 归属）→ unknown/no-owner、chunks=[]。"""
    lines = _complete_preflight()
    lines += [
        "✖ free assert", "❯ g / free assert", "AssertionError: x",
        "FIDGET_INTEGRATION_V1 phase=summary status=failed total=8 passed=0 failed=0 not_started=8 run_id=r1",
        "FIDGET_INTEGRATION_V1 phase=cleanup status=success run_id=r1",
    ]
    result = _provider_from_lines(lines)
    kinds = {c["kind"] for c in result["integrationClassifications"]}
    assert "unknown" in kinds, "游离断言应降级 unknown"
    assert result["chunks"] == [], "游离断言不得产出 code chunk"


def test_suite_exit_non_integer_is_conflict():
    """suite end exit=abc（非整数）→ conflict，断言不得进 code chunk。"""
    lines = _complete_preflight()
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1"]
    lines += ["✖ int assert", "❯ group / int assert", "AssertionError: x"]
    lines.append("FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=abc run_id=r1")
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += _summary_cleanup(run_id="r1", failed=1)
    result = _provider_from_lines(lines)
    assert any("exit" in c for c in result.get("integrationConflicts", [])), "exit=abc 应产生 conflict"
    assert result["chunks"] == [], "exit 非整数时断言不得产出 code chunk"
    assert _is_integration_env_only_failures(result) is True


def test_reconcile_integration_writes_canonical_no_owner():
    """guard：混合构建里环境事实应写 canonical no-owner，移除 Agent 误生成的环境 owner。"""
    from ci_owner_agent.schemas import CiResponsibilityNotice, Owner, ResponsibilityItem
    from ci_owner_agent.services.integration_responsibility import reconcile_integration_responsibilities

    notice = CiResponsibilityNotice(
        repo="repo",
        job="job",
        buildNumber=1,
        buildUrl="local://x",
        result="FAILURE",
        branch="main",
        headCommit="abc",
        baseCommit="abc",
        owner=Owner(type="no_high_confidence_owner", name="NO_OWNER", email=None, commit=None, confidence=0),
        failureReason="failed",
        evidence=[],
        suggestions=[],
        hasHighConfidenceOwner=False,
        responsibilityItems=[
            # Agent 意外生成的环境 owner，应被 guard 移除
            ResponsibilityItem(
                failureId="bad",
                failureTitle="integration env wrong owner",
                failureSignature="integration_connection|select-integration@L10",
                failureSummary="x",
                owner=Owner(type="high_confidence", name="someone", email="e", commit="c", confidence=0.9),
                responsibilityType="current_build_owner",
                confidence=0.9,
                reason="wrong",
                evidenceIds=[],
            )
        ],
    )
    summaries = {
        "integrationClassifications": [
            {"line": 10, "kind": "connection", "stage": "Integration Tests", "suite": "select-integration"},
        ],
        "integrationConflicts": [],
    }
    reconciled = reconcile_integration_responsibilities(notice, failure_summaries=summaries)
    # 环境 owner 被移除，写 canonical no-owner
    env_items = [i for i in reconciled.responsibilityItems if i.failureSignature.startswith("integration_")]
    assert len(env_items) == 1
    assert env_items[0].owner.type == "no_high_confidence_owner"
    assert reconciled.hasHighConfidenceOwner is False


def test_last_suite_failure_keeps_assertion_after_failed_marker():
    """最后一个失败块的 AssertionError 在 FAILED 之后，不得被 FAILED 截断误判 unknown。

    真实 Japa 行序：❯ 头 -> diff(Expected/Received) -> FAILED -> ℹ AssertionError -> 堆栈 -> Tests 汇总。
    """
    from ci_owner_agent.services.log_parsing import classify_all_japa_failures

    lines = _complete_preflight()
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1"]
    lines += [
        "  ✖ O-0503: CaseWhenProjection OID(select record and object OID branches when state <= 2), return string or null values (29.59ms)",
        "❯ O-查询结果映射专题集成测试 / O-0503: CaseWhenProjection OID(select record and object OID branches when state <= 2), return string or null values",
        "- Expected  - 2",
        "+ Received  + 2",
        "FAILED",
        "ℹ AssertionError: SharedPgObject(app:6793:form:6793): expected [ …(3) ] to deeply equal [ …(3) ]",
        "  ⁃ at Assert.deepEqual",
        "Tests  1659 passed, 2 failed (1661)",
    ]
    lines.append("FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1")
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += _summary_cleanup(run_id="r1", failed=1)

    classifications = classify_all_japa_failures(lines)
    kinds = [c["kind"] for c in classifications]
    assert kinds == ["assertion"], f"最后一个失败块应判 assertion，实际 {kinds}"

    result = _provider_from_lines(lines)
    assert len(result["chunks"]) == 1, f"应产出 1 个 code chunk，实际 {len(result['chunks'])}"
    assert result["totals"]["integrationEnv"] == 0


def test_two_failures_both_assertion_when_last_has_trailing_assertion():
    """同 suite 两个失败，最后一个的 AssertionError 在 FAILED 之后，两个都应判 assertion。"""
    from ci_owner_agent.services.log_parsing import classify_all_japa_failures

    lines = _complete_preflight()
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1"]
    lines += [
        "  ✖ F-1003: group by main field (36.18ms)",
        "❯ F-聚合汇总主题测试 / F-1003: group by main field",
        "FAILED",
        "ℹ AssertionError: SharedPgObject expected [ …(5) ] to deeply equal [ …(5) ]",
        "Tests  1659 passed, 2 failed (1661)",
        "  ✖ O-0503: CaseWhenProjection OID (29.59ms)",
        "❯ O-查询结果映射专题集成测试 / O-0503: CaseWhenProjection OID",
        "- Expected",
        "+ Received",
        "FAILED",
        "ℹ AssertionError: SharedPgObject expected [ …(3) ] to deeply equal [ …(3) ]",
        "Tests  1659 passed, 2 failed (1661)",
    ]
    lines.append("FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1")
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += _summary_cleanup(run_id="r1", failed=1)

    classifications = classify_all_japa_failures(lines)
    kinds = [c["kind"] for c in classifications]
    assert kinds == ["assertion", "assertion"], f"两个失败都应判 assertion，实际 {kinds}"


def test_two_assertions_have_independent_signatures_not_cross_contaminated():
    """F-1003/O-0503 两个断言失败，signature 必须独立，不能串入对方的 AssertionError。"""
    from ci_owner_agent.services.log_parsing import build_integration_failure_summaries

    lines = _complete_preflight()
    lines += ["FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=start run_id=r1"]
    lines += [
        "  ✖ F-1003: group by main field (36.18ms)",
        "  ✖ O-0503: CaseWhenProjection OID (29.59ms)",
        "❯ F-聚合汇总主题测试 / F-1003: group by main field",
        "ℹ AssertionError: F-specific expected [ { name: 'alice' } ]",
        "❯ O-查询结果映射专题集成测试 / O-0503: CaseWhenProjection OID",
        "ℹ AssertionError: O-specific case_record_oid",
        "Tests  1659 passed, 2 failed (1661)",
    ]
    lines.append("FIDGET_INTEGRATION_V1 phase=suite suite=select-integration status=end exit=1 run_id=r1")
    for suite in _ALL_SUITES[1:]:
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=start run_id=r1")
        lines.append(f"FIDGET_INTEGRATION_V1 phase=suite suite={suite} status=end exit=0 run_id=r1")
    lines += _summary_cleanup(run_id="r1", failed=1)

    chunks = build_integration_failure_summaries(lines)
    assert len(chunks) == 2, f"应产出 2 个独立 chunk，实际 {len(chunks)}"
    f_chunk = next(c for c in chunks if "F-1003" in c["content"])
    o_chunk = next(c for c in chunks if "O-0503" in c["content"])
    # F 不包含 O 的 AssertionError，O 不包含 F 的
    assert "F-specific" in f_chunk["content"]
    assert "O-specific" not in f_chunk["content"]
    assert "O-specific" in o_chunk["content"]
    assert "F-specific" not in o_chunk["content"]
    # signatureKey 必须不同
    assert f_chunk["signatureHash"] != o_chunk["signatureHash"]


def test_connection_items_aggregate_per_suite():
    """同 suite 数百次 connection 只生成一条 canonical no-owner，signature 不含行号。"""
    from ci_owner_agent.schemas import CiResponsibilityNotice, Owner
    from ci_owner_agent.services.integration_responsibility import reconcile_integration_responsibilities

    classifications = []
    for i in range(100):
        classifications.append({"kind": "connection", "suite": "select-integration", "line": 1000 + i})
    for i in range(50):
        classifications.append({"kind": "connection", "suite": "delete-integration", "line": 2000 + i})
    summaries = {"integrationClassifications": classifications, "integrationConflicts": []}

    notice = CiResponsibilityNotice(
        repo="fidget-xiaoqin",
        job="j",
        buildNumber=1,
        buildUrl="u",
        result="FAILURE",
        branch="main",
        headCommit="h",
        baseCommit="b",
        owner=Owner(type="no_high_confidence_owner", name="无高可信责任人", confidence=0.0),
        failureReason="env",
        responsibilityItems=[],
        hasHighConfidenceOwner=False,
    )
    result = reconcile_integration_responsibilities(notice, failure_summaries=summaries)
    sigs = [item.failureSignature for item in result.responsibilityItems]
    assert len(sigs) == 2, f"应聚合为 2 条（每 suite 一条），实际 {len(sigs)}"
    assert "integration_connection|select-integration" in sigs
    assert "integration_connection|delete-integration" in sigs
    # signature 不含 @L 行号
    assert all("@L" not in sig for sig in sigs)


def test_connection_and_unknown_not_merged():
    """connection 与 unknown 不合并，各自独立签名。"""
    from ci_owner_agent.schemas import CiResponsibilityNotice, Owner
    from ci_owner_agent.services.integration_responsibility import reconcile_integration_responsibilities

    summaries = {
        "integrationClassifications": [
            {"kind": "connection", "suite": "select-integration", "line": 10},
            {"kind": "unknown", "suite": "select-integration", "line": 20},
        ],
        "integrationConflicts": [],
    }
    notice = CiResponsibilityNotice(
        repo="r", job="j", buildNumber=1, buildUrl="u", result="FAILURE", branch="main",
        headCommit="h", baseCommit="b",
        owner=Owner(type="no_high_confidence_owner", name="无高可信责任人", confidence=0.0),
        failureReason="env", responsibilityItems=[], hasHighConfidenceOwner=False,
    )
    result = reconcile_integration_responsibilities(notice, failure_summaries=summaries)
    sigs = sorted(item.failureSignature for item in result.responsibilityItems)
    assert "integration_connection|select-integration" in sigs
    assert "integration_unknown|select-integration" in sigs
