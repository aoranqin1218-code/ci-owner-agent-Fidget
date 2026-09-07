"""Fidget 集成测试日志样本基线测试。

校验 samples/fidget_log/integration/ 下的脱敏样本、manifest 与 PROTOCOL.md：
- 精确锁定 3 个样本 ID 与 protocol 路径，防空数组空循环通过；
- 样本按 LF 规范化后的 sha256 与 manifest 完全一致；
- checkout SHA 与 manifest 的 checkoutCommit 一致；
- 恰好一个 Finished，与 finalStatus 一致；
- 恰好一个 summary，数字与 suiteTotals 一致；
- suite marker 成对、无重复、顺序正确、run_id 一致；
- 三份样本各自的专属失败特征（断言基线仅 F-1003/O-0503、image-pull 失败、Protonbase ECONNREFUSED）；
- summary → cleanup → Finished 完整顺序；
- secret scan 覆盖所有 .log / manifest.json / PROTOCOL.md；
- rejected evidence 与 deferred 场景与 manifest 一致。

不依赖 tmp_path（避开 Windows 临时目录权限问题），直接从样例文件读取。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_SAMPLES = Path(__file__).resolve().parents[3] / "samples" / "fidget_log" / "integration"
_MANIFEST = _SAMPLES / "manifest.json"

# 精确锁定的样本 ID，防止 manifest 被改空数组后测试空循环通过。
_EXPECTED_SAMPLE_IDS = {
    "failure-assertion",
    "failure-pipeline-image-pull",
    "failure-protonbase-unavailable",
}

# 已知 secret pattern：凭据 id、内部域名、真实数据库地址、常见密码字段、真实用户路径。
_SECRET_PATTERNS = [
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I),
    re.compile(r"code\.fineres\.com|jenkins\.jdydevelop\.com|npm\.jdydevelop\.com"),
    re.compile(r"ep-bp1[a-z0-9-]*\.aliyuncs\.com"),
    re.compile(r"172\.24\.65\.\d+"),
    re.compile(r"(password|passwd|token|secret|webhook)\s*[:=]\s*[^\s'\"<>]+", re.I),
    # 真实个人路径（~<name>）不应出现在 <internal-git> URL 中
    re.compile(r"<internal-git>/scm/~[^<]"),
]

_MARKER = "FIDGET_INTEGRATION_V1"
_SUITE_ENUM = {
    "select-integration",
    "stream-select-integration",
    "delete-integration",
    "insert-integration",
    "update-integration",
    "upsert-integration",
    "bulk-integration",
    "shadow-integration",
}


def _load_manifest() -> dict:
    assert _MANIFEST.exists(), "manifest.json missing"
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _protocol_path() -> Path:
    data = _load_manifest()
    proto = data.get("protocol")
    assert proto == "PROTOCOL.md", f"manifest.protocol must be 'PROTOCOL.md', got {proto!r}"
    # 按 manifest 所在目录解析
    return _MANIFEST.parent / proto


def _read(path: Path) -> str:
    assert path.exists(), f"missing: {path}"
    text = path.read_text(encoding="utf-8")
    assert "�" not in text, f"invalid UTF-8 in {path}"
    return text


def _sha256(path: Path) -> str:
    # Git 在 Windows 和 Linux 上可能分别检出 CRLF 与 LF。清单锁定的是
    # 规范化后的日志内容，避免同一 Git blob 因工作区换行符不同而误报。
    normalized = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def _marker_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith(_MARKER)]


def _suite_name(marker: str) -> str:
    return marker.split("suite=")[1].split(" ")[0]


def _samples_by_id() -> dict[str, dict]:
    data = _load_manifest()
    samples = data.get("samples", [])
    ids = {s.get("id") for s in samples}
    assert ids == _EXPECTED_SAMPLE_IDS, f"manifest must declare exactly {_EXPECTED_SAMPLE_IDS}, got {ids}"
    return {s["id"]: s for s in samples}


def test_manifest_declares_exact_three_sample_ids():
    data = _load_manifest()
    samples = data.get("samples", [])
    assert len(samples) == len(_EXPECTED_SAMPLE_IDS), f"expected {len(_EXPECTED_SAMPLE_IDS)} samples, got {len(samples)}"
    ids = [s.get("id") for s in samples]
    assert len(ids) == len(set(ids)), f"sample ids must be unique, got duplicates: {ids}"
    assert set(ids) == _EXPECTED_SAMPLE_IDS, f"exactly 3 sample ids expected, got {set(ids)}"


def test_protocol_path_resolves_and_readable():
    path = _protocol_path()
    text = _read(path)
    assert "FIDGET_INTEGRATION_V1" in text
    assert "image_pull" in text
    assert "fail-closed" in text or "fail closed" in text


def test_samples_exist_and_sha256_match():
    for sid, sample in _samples_by_id().items():
        path = _SAMPLES / sample["file"]
        assert path.exists(), f"sample missing: {sample['file']}"
        assert _sha256(path) == sample["sha256"], f"sha256 mismatch: {sample['file']}"


def test_checkout_commit_matches_manifest():
    for sid, sample in _samples_by_id().items():
        text = _read(_SAMPLES / sample["file"])
        expected = sample["checkoutCommit"]
        src = re.findall(r"Checking out Revision ([0-9a-f]{40}) \(refs/remotes/origin/main\)", text)
        assert src, f"no source checkout in {sample['file']}"
        assert src[-1] == expected, f"checkout SHA mismatch in {sample['file']}: {src[-1]} != {expected}"


def test_exactly_one_finished_matches_final_status():
    for sid, sample in _samples_by_id().items():
        text = _read(_SAMPLES / sample["file"])
        finished = re.findall(r"^Finished: (\w+)", text, re.M)
        assert len(finished) == 1, f"expected exactly one Finished in {sample['file']}: {finished}"
        assert finished[0] == sample["finalStatus"], f"Finished mismatch in {sample['file']}"


def test_summary_matches_suite_totals():
    for sid, sample in _samples_by_id().items():
        text = _read(_SAMPLES / sample["file"])
        markers = _marker_lines(text)
        summaries = [ln for ln in markers if "phase=summary" in ln]
        assert len(summaries) == 1, f"expected exactly one summary in {sample['file']}"
        m = re.search(r"total=(\d+) passed=(\d+) failed=(\d+) not_started=(\d+)", summaries[0])
        assert m, f"summary missing totals in {sample['file']}"
        total, passed, failed, not_started = map(int, m.groups())
        expected = sample["suiteTotals"]
        assert (total, passed, failed, not_started) == (
            expected["total"], expected["passed"], expected["failed"], expected["notStarted"]
        ), f"suiteTotals mismatch in {sample['file']}"


def test_suite_markers_complete_and_ordered():
    for sid, sample in _samples_by_id().items():
        text = _read(_SAMPLES / sample["file"])
        markers = _marker_lines(text)
        not_started = sample["suiteTotals"]["notStarted"]

        suite_markers = [ln for ln in markers if "phase=suite" in ln]
        if not_started == 8:
            assert not suite_markers, f"no suite markers expected when notStarted=8 in {sample['file']}"
            continue

        starts = [ln for ln in suite_markers if "status=start" in ln]
        ends = [ln for ln in suite_markers if "status=end" in ln]
        assert len(starts) == 8, f"expected 8 suite starts in {sample['file']}: {len(starts)}"
        assert len(ends) == 8, f"expected 8 suite ends in {sample['file']}: {len(ends)}"

        start_names = [_suite_name(ln) for ln in starts]
        end_names = [_suite_name(ln) for ln in ends]
        assert set(start_names) == _SUITE_ENUM, f"suite start names must equal full enum in {sample['file']}"
        assert set(end_names) == _SUITE_ENUM, f"suite end names must equal full enum in {sample['file']}"

        for name in _SUITE_ENUM:
            assert start_names.count(name) == 1, f"suite {name} must start exactly once in {sample['file']}"
            assert end_names.count(name) == 1, f"suite {name} must end exactly once in {sample['file']}"
            si = markers.index(next(ln for ln in starts if _suite_name(ln) == name))
            ei = markers.index(next(ln for ln in ends if _suite_name(ln) == name))
            assert si < ei, f"suite {name} start must precede end in {sample['file']}"


def test_run_id_consistent_across_markers():
    for sid, sample in _samples_by_id().items():
        text = _read(_SAMPLES / sample["file"])
        markers = _marker_lines(text)
        run_ids = {re.search(r"run_id=(\S+)", ln).group(1) for ln in markers if "run_id=" in ln}
        assert len(run_ids) == 1, f"run_id must be consistent in {sample['file']}: {run_ids}"


def test_full_marker_order_summary_cleanup_finished():
    """summary → cleanup 都在 Finished 之前，且 summary 先于 cleanup。"""
    for sid, sample in _samples_by_id().items():
        text = _read(_SAMPLES / sample["file"])
        markers = _marker_lines(text)
        summary_idx = [i for i, ln in enumerate(markers) if "phase=summary" in ln]
        cleanup_idx = [i for i, ln in enumerate(markers) if "phase=cleanup" in ln]
        assert summary_idx and cleanup_idx, f"summary and cleanup required in {sample['file']}"
        assert summary_idx[0] < cleanup_idx[0], f"summary must precede cleanup in {sample['file']}"

        # Finished 必须出现在最后一个 marker 之后（即整份日志末尾）
        lines = text.splitlines()
        finished_idx = [i for i, ln in enumerate(lines) if re.match(r"^Finished: \w+", ln)]
        assert len(finished_idx) == 1, f"exactly one Finished line in {sample['file']}"
        last_marker_idx = lines.index(markers[-1])
        assert last_marker_idx < finished_idx[0], f"Finished must come after last marker in {sample['file']}"


def test_cleanup_matches_manifest():
    for sid, sample in _samples_by_id().items():
        text = _read(_SAMPLES / sample["file"])
        markers = _marker_lines(text)
        cleanups = [ln for ln in markers if "phase=cleanup" in ln]
        assert len(cleanups) == 1, f"exactly one cleanup in {sample['file']}"
        expected = sample.get("cleanupExpected", "success")
        assert f"status={expected}" in cleanups[0], f"cleanup status mismatch in {sample['file']}"


def test_stage_boundary_uses_exact_jenkins_stage_line():
    for sid, sample in _samples_by_id().items():
        text = _read(_SAMPLES / sample["file"])
        assert "[Pipeline] { (Integration Tests)" in text, (
            f"exact Jenkins Integration Tests stage line missing in {sample['file']}"
        )
        assert "+ bash ci/run-integration-tests.v2.sh" in text, (
            f"runner invocation line missing in {sample['file']}"
        )


def test_assertion_sample_only_known_failures():
    sample = _samples_by_id()["failure-assertion"]
    text = _read(_SAMPLES / sample["file"])
    # 只有 select 非零（suiteTotals failed=1）
    assert sample["suiteTotals"]["failed"] == 1

    # 逐条解析 8 个 suite 的 exit，断言只有 select 非零、其余全为 0
    markers = _marker_lines(text)
    exits: dict[str, int] = {}
    for ln in markers:
        if "phase=suite" in ln and "status=end" in ln:
            name = _suite_name(ln)
            m = re.search(r"exit=(\d+)", ln)
            assert m, f"suite end missing exit in {ln}"
            exits[name] = int(m.group(1))
    assert set(exits) == _SUITE_ENUM, f"expected all 8 suite exits, got {set(exits)}"
    assert exits["select-integration"] != 0, "select-integration must be non-zero"
    for name in _SUITE_ENUM - {"select-integration"}:
        assert exits[name] == 0, f"{name} must exit 0, got {exits[name]}"

    # 两个已知断言失败锚点，无其它 ✖
    fails = [ln for ln in text.splitlines() if "✖" in ln]
    assert len(fails) == 2, f"expected exactly 2 assertion failures, got {len(fails)}"
    assert any("F-1003" in ln for ln in fails), "F-1003 must be present"
    assert any("O-0503" in ln for ln in fails), "O-0503 must be present"
    # 无环境错误特征
    assert "ECONNREFUSED" not in text, "assertion sample must not contain ECONNREFUSED"
    assert "image_pull status=failed" not in text, "assertion sample must not contain image_pull failure"


def test_image_pull_sample_is_pipeline_failure_only():
    sample = _samples_by_id()["failure-pipeline-image-pull"]
    text = _read(_SAMPLES / sample["file"])
    markers = _marker_lines(text)
    assert "step=image_pull status=failed" in text, "image_pull must have failed"
    # 没有 Mongo readiness / suite marker
    assert not any("step=mongo_ready" in ln for ln in markers), "must not reach mongo_ready"
    assert not any("phase=suite" in ln for ln in markers), "must not run any suite"
    assert sample["suiteTotals"]["notStarted"] == 8


def test_protonbase_sample_has_econnrefused():
    sample = _samples_by_id()["failure-protonbase-unavailable"]
    text = _read(_SAMPLES / sample["file"])
    assert "connect ECONNREFUSED" in text, "Protonbase sample must contain ECONNREFUSED"
    # 7 个 suite 失败，shadow 通过
    assert sample["suiteTotals"]["failed"] == 7
    assert sample["suiteTotals"]["passed"] == 1


def test_secret_free_logs_manifest_protocol():
    files = [_protocol_path(), _MANIFEST] + [_SAMPLES / s["file"] for s in _samples_by_id().values()]
    for path in files:
        text = _read(path)
        for pattern in _SECRET_PATTERNS:
            assert not pattern.search(text), f"secret pattern {pattern.pattern!r} in {path.name}"


def test_rejected_and_deferred_consistent():
    data = _load_manifest()
    deferred_ids = {d.get("id") for d in data.get("deferred", [])}
    assert "mongo-readiness-unavailable" in deferred_ids, "mongo readiness must be deferred"
    assert not any("success" in d.get("id", "") for d in data.get("deferred", [])), "no success sample should be deferred"
    assert "missing" not in data, "manifest should use 'deferred', not 'missing'"

    rejected = {"integration_test-37.log", "fidget-build-master-13.log"}
    assert set(data.get("rejectedEvidence", [])) == rejected, "rejectedEvidence must be exactly the two legacy logs"
    present = {p.name for p in _SAMPLES.iterdir() if p.is_file()}
    assert not (rejected & present), "rejected evidence must not be copied into protocol dir"
