import argparse
import json
import subprocess
import pytest
from ci_owner_agent.schemas import CiResponsibilityNotice, Owner
from scripts import rerun_analyze_local
from scripts.rerun_analyze_local import build_command, read_notice, terminate_timed_out_process, validate_notice


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
    waits = []

    class FakeProcess:
        pid = 123
        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return -1
        def poll(self):
            return None
        def kill(self):
            return None

    monkeypatch.setattr(rerun_analyze_local.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(rerun_analyze_local, "kill_process_tree", lambda process: None)
    monkeypatch.setattr(
        "sys.argv",
        ["rerun", "--runs", "1", "--timeout-sec", "1", "--repo", "fx-code", "--job", "j", "--build", "1",
         "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)],
    )
    assert rerun_analyze_local.main() == 1
    assert waits == [1, 5]
    rows = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert rows[0]["status"] == "TIMEOUT" and rows[0]["errorKind"] == "timeout"
    assert rows[0]["terminationReaped"] is True
    assert rows[0]["noticeValid"] is False
    assert rows[0]["noticeValidationError"] == "analysis outputs are untrusted because execution timed out"
    assert rows[0]["noticeOwner"] is None
    assert rows[0]["metricsDurationMs"] is None


def test_timeout_termination_reports_warning_without_raising(monkeypatch):
    class BrokenProcess:
        def poll(self): return None
        def wait(self, timeout=None): raise subprocess.TimeoutExpired("fake", timeout)
        def kill(self): raise OSError("kill denied")
    monkeypatch.setattr(rerun_analyze_local, "kill_process_tree", lambda process: (_ for _ in ()).throw(OSError("tree denied")))
    result = terminate_timed_out_process(BrokenProcess(), grace_seconds=0.01)
    assert result.reaped is False
    assert "tree termination failed" in result.warning
    assert "final reap failed" in result.warning


def test_rerun_main_real_subprocess_success(tmp_path, monkeypatch):
    console = tmp_path / "console.log"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    monkeypatch.setenv("CI_AGENT_TEST_FAKE_ANALYZE_LOCAL_MODE", "success")
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "origin/dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)])
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
    monkeypatch.setenv("CI_AGENT_TEST_FAKE_ANALYZE_LOCAL_MODE", "missing_notice")
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)])
    assert rerun_analyze_local.main() == 1
    row = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["errorKind"] == "notice_missing" and row["noticeValid"] is False


@pytest.mark.parametrize(("mode", "error_kind"), [("invalid_json", "notice_schema"), ("invalid_schema", "notice_schema"), ("nonzero", "execution")])
def test_rerun_real_cli_failure_modes(tmp_path, monkeypatch, mode, error_kind):
    console = tmp_path / "console.log"
    console.write_text("Finished: FAILURE\n", encoding="utf-8")
    out_dir = tmp_path / "runs"
    monkeypatch.setenv("CI_AGENT_TEST_FAKE_ANALYZE_LOCAL_MODE", mode)
    monkeypatch.setattr("sys.argv", ["rerun", "--runs", "1", "--repo", "fx-code", "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "dev", "--base-commit", "base", "--head-commit", "head", "--console-file", str(console), "--out-dir", str(out_dir)])
    assert rerun_analyze_local.main() == 1
    row = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))[0]
    assert row["status"] == "FAILED" and row["errorKind"] == error_kind
