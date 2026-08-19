import argparse
import json
import pytest
import os
import sys
from pathlib import Path
from ci_owner_agent.schemas import CiResponsibilityNotice, Owner
from scripts import rerun_analyze_local
from scripts._runtime import BoundedProcessTimeout, ProcessTerminationResult
from scripts.rerun_analyze_local import build_command, read_notice, validate_notice, write_summaries

FIXTURE = Path(__file__).parent / "fixtures" / "fake_analyze_launcher.py"


def make_fake_python(tmp_path: Path) -> Path:
    if os.name == "nt":
        launcher = tmp_path / "fake-python.cmd"
        launcher.write_text(f'@"{sys.executable}" "{FIXTURE}" %*\r\n', encoding="utf-8")
    else:
        launcher = tmp_path / "fake-python"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def make_notice() -> CiResponsibilityNotice:
    return CiResponsibilityNotice(
        repo="fx-code", job="services/fx-code-unittest", buildNumber=5088,
        buildUrl="local://job/5088", result="FAILURE", branch="dev",
        baseCommit="base", headCommit="head",
        owner=Owner(type="no_high_confidence_owner", name="无高可信责任人", confidence=0),
        failureReason="test", hasHighConfidenceOwner=False,
    )


def test_read_notice_distinguishes_missing_invalid_and_valid(tmp_path):
    missing, error = read_notice(tmp_path / "missing.json")
    assert missing is None and error == "notice file missing"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{invalid", encoding="utf-8")
    parsed, error = read_notice(invalid)
    assert parsed is None and error and error.startswith("invalid notice:")
    valid = tmp_path / "notice.json"
    valid.write_text(make_notice().model_dump_json(), encoding="utf-8")
    parsed, error = read_notice(valid)
    assert error is None and parsed and parsed["branch"] == "dev"


def test_validate_notice_accepts_normalized_branch():
    args = argparse.Namespace(repo="fx-code", job="services/fx-code-unittest", build=5088, branch="dev", result="FAILURE", base_commit="base", head_commit="head")
    assert validate_notice(make_notice().model_dump(mode="json"), args) is None


def test_rerun_analyze_local_passes_previous_build_and_commit():
    args = argparse.Namespace(
        python="python",
        repo="fx-code",
        job="services/fx-code-unittest",
        build=5088,
        branch="dev",
        base_commit="last-success",
        head_commit="current",
        console_file="log.txt",
        build_url=None,
        last_success_build=5068,
        previous_build=5087,
        previous_commit="previous",
        result=None,
        ignore_checkout_commit_mismatch=False,
        notify=False,
        force_notify=False,
    )
    command = build_command(args)
    assert "--previous-build" in command
    assert "5087" in command
    assert "--previous-commit" in command
    assert "previous" in command


def test_rerun_analyze_local_omits_previous_when_not_provided():
    args = argparse.Namespace(
        python="python",
        repo="fx-code",
        job="services/fx-code-unittest",
        build=5088,
        branch="dev",
        base_commit="last-success",
        head_commit="current",
        console_file="log.txt",
        build_url=None,
        last_success_build=5068,
        previous_build=None,
        previous_commit=None,
        result=None,
        ignore_checkout_commit_mismatch=False,
        notify=False,
        force_notify=False,
    )
    command = build_command(args)
    assert "--previous-build" not in command
    assert "--previous-commit" not in command
    assert "--last-success-build" in command
    assert "5068" in command


def test_rerun_main_records_popen_failure_and_writes_summary(tmp_path, monkeypatch):
    console = tmp_path / "console.log"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    monkeypatch.setattr(
        "sys.argv",
        ["rerun", "--runs", "1", "--python", str(tmp_path / "missing-python"), "--repo", "fx-code", "--job", "services/fx-code-unittest",
         "--build", "5088", "--branch", "dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)],
    )
    assert rerun_analyze_local.main() == 1
    rows = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["errorKind"] == "execution"


def test_rerun_unsupported_status_writes_structured_summary(tmp_path, monkeypatch):
    console = tmp_path / "console.log"
    console.write_text("Finished: CANCELLED\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    monkeypatch.setattr(
        "sys.argv",
        ["rerun", "--repo", "fx-code", "--job", "j", "--build", "1", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)],
    )
    assert rerun_analyze_local.main() == 1
    rows = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert rows[0]["errorKind"] == "status_validation"


def test_rerun_explicit_not_built_uses_structured_failure(tmp_path, monkeypatch):
    console = tmp_path / "console.log"
    console.write_text("Finished: NOT_BUILT\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    monkeypatch.setattr(
        "sys.argv",
        ["rerun", "--repo", "fx-code", "--job", "j", "--build", "1", "--base-commit", "base", "--head-commit", "head",
         "--console-file", str(console), "--out-dir", str(out_dir), "--result", "NOT_BUILT"],
    )
    assert rerun_analyze_local.main() == 1
    rows = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert rows[0]["errorKind"] == "status_validation"


def test_rerun_timeout_reaps_process_and_returns_failure(tmp_path, monkeypatch):
    console = tmp_path / "console.log"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    out_dir = tmp_path / "runs"

    def timed_out(*_args, **_kwargs):
        raise BoundedProcessTimeout(
            ["fake"],
            1,
            output="partial stdout",
            stderr="partial stderr",
            termination=ProcessTerminationResult(True),
        )

    monkeypatch.setattr(rerun_analyze_local, "run_process_bounded", timed_out)
    monkeypatch.setattr(
        "sys.argv",
        ["rerun", "--runs", "1", "--timeout-sec", "1", "--repo", "fx-code", "--job", "j", "--build", "1",
         "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)],
    )
    assert rerun_analyze_local.main() == 1
    rows = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert rows[0]["status"] == "TIMEOUT" and rows[0]["errorKind"] == "timeout"
    assert rows[0]["terminationReaped"] is True
    assert rows[0]["noticeValid"] is False
    assert rows[0]["noticeValidationError"] == "analysis outputs are untrusted because execution timed out"
    assert rows[0]["noticeOwner"] is None
    assert rows[0]["metricsDurationMs"] is None
    assert (out_dir / "run-01" / "stdout.log").read_text(encoding="utf-8") == "partial stdout"
    assert (out_dir / "run-01" / "stderr.log").read_text(encoding="utf-8") == "partial stderr"


def test_rerun_main_real_subprocess_success(tmp_path, monkeypatch):
    console = tmp_path / "console.log"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--python", str(make_fake_python(tmp_path)), "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "origin/dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)])
    assert rerun_analyze_local.main() == 0
    row = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["status"] == "OK" and row["noticeValid"] is True
    command = (out_dir / "run-01" / "command.txt").read_text(encoding="utf-8")
    assert "-m ci_owner_agent analyze-local" in command
    assert "--output-file" in command and "--branch dev" in command


def test_rerun_main_real_subprocess_missing_notice(tmp_path, monkeypatch):
    console = tmp_path / "console.log"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "missing_notice")
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--python", str(make_fake_python(tmp_path)), "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)])
    assert rerun_analyze_local.main() == 1
    row = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["errorKind"] == "notice_missing" and row["noticeValid"] is False


@pytest.mark.parametrize(("mode", "error_kind"), [("invalid_json", "notice_schema"), ("invalid_schema", "notice_schema"), ("nonzero", "execution")])
def test_rerun_real_cli_failure_modes(tmp_path, monkeypatch, mode, error_kind):
    console = tmp_path / "console.log"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    monkeypatch.setenv("FAKE_ANALYZE_MODE", mode)
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--python", str(make_fake_python(tmp_path)), "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)])
    assert rerun_analyze_local.main() == 1
    row = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["status"] == "FAILED" and row["errorKind"] == error_kind


def test_write_summaries_csv_failure_returns_false(tmp_path, monkeypatch, capsys):
    original_open = Path.open
    def failing_open(path, *args, **kwargs):
        if path.name == "summary.csv":
            raise OSError("csv locked")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", failing_open)
    assert write_summaries(tmp_path, [{"run": 1, "status": "OK"}]) is False
    assert "failed to write rerun summaries" in capsys.readouterr().err


def test_write_summaries_json_failure_returns_false(tmp_path, monkeypatch, capsys):
    original_write = Path.write_text
    def failing_write(path, *args, **kwargs):
        if path.name == "summary.json":
            raise OSError("disk full")
        return original_write(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", failing_write)
    assert write_summaries(tmp_path, [{"run": 1, "status": "OK"}]) is False
    assert (tmp_path / "summary.csv").exists()
    assert "failed to write rerun summaries" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["repo", "job", "buildNumber", "branch", "result", "baseCommit", "headCommit"])
def test_rerun_metadata_mismatch_matrix_real_child(tmp_path, monkeypatch, field):
    console = tmp_path / "console.log"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "metadata_mismatch")
    monkeypatch.setenv("FAKE_ANALYZE_MISMATCH_FIELD", field)
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--python", str(make_fake_python(tmp_path)), "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "origin/dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out)])
    assert rerun_analyze_local.main() == 1
    row = json.loads((out / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["errorKind"] == "notice_metadata" and field in row["noticeValidationError"]


def test_rerun_stale_outputs_are_removed(tmp_path, monkeypatch):
    console, out = tmp_path / "console.log", tmp_path / "out"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    run = out / "run-01"
    run.mkdir(parents=True)
    for name, content in {"notice.json": make_notice().model_dump_json(), "metrics.jsonl": '{"durationMs":999999,"totalTokens":999999}', "trace.json": "{}", "trace-summary.json": "{}", "stdout.log": "STALE_OWNER", "stderr.log": "old", "command.txt": "old"}.items():
        (run / name).write_text(content, encoding="utf-8")
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "missing_notice")
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--python", str(make_fake_python(tmp_path)), "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out)])
    assert rerun_analyze_local.main() == 1
    row = json.loads((out / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["errorKind"] == "notice_missing" and row["noticeOwner"] is None and row["totalTokens"] is None
    assert not (run / "notice.json").exists() and not (run / "metrics.jsonl").exists()


def test_rerun_cleanup_failure_does_not_stop_next_run(tmp_path, monkeypatch):
    console, out = tmp_path / "console.log", tmp_path / "out"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    calls = 0
    original = rerun_analyze_local.cleanup_previous_outputs
    def cleanup(paths):
        nonlocal calls
        calls += 1
        return ["locked"] if calls == 1 else original(paths)
    monkeypatch.setattr(rerun_analyze_local, "cleanup_previous_outputs", cleanup)
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "2", "--python", str(make_fake_python(tmp_path)), "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out)])
    assert rerun_analyze_local.main() == 1
    rows = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert len(rows) == 2 and rows[0]["errorKind"] == "cleanup" and rows[1]["status"] == "OK"


def test_rerun_main_preserves_unreaped_timeout(tmp_path, monkeypatch):
    console, out = tmp_path / "console.log", tmp_path / "out"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")

    def timed_out(*_args, **_kwargs):
        raise BoundedProcessTimeout(
            ["fake"],
            1,
            output="",
            stderr="",
            termination=ProcessTerminationResult(False, "final reap failed"),
        )

    monkeypatch.setattr(rerun_analyze_local, "run_process_bounded", timed_out)
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--timeout-sec", "1", "--repo", "fx-code", "--job", "j", "--build", "1", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out)])
    assert rerun_analyze_local.main() == 1
    row = json.loads((out / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["status"] == "TIMEOUT" and row["terminationReaped"] is False and row["noticeValid"] is False
    assert "final reap failed" in row["terminationWarning"] and row["metricsDurationMs"] is None


def test_rerun_real_child_write_before_timeout_is_untrusted(tmp_path, monkeypatch):
    console, out = tmp_path / "console.log", tmp_path / "out"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "write_then_timeout")
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--timeout-sec", "1", "--python", str(make_fake_python(tmp_path)), "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out)])
    assert rerun_analyze_local.main() == 1
    row = json.loads((out / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["status"] == "TIMEOUT" and row["errorKind"] == "timeout"
    assert row["noticeValid"] is False and row["noticeOwner"] is None and row["metricsDurationMs"] is None
