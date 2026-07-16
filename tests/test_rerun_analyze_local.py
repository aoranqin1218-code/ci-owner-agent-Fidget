import argparse
import json
from ci_owner_agent.schemas import CiResponsibilityNotice, Owner
from scripts import rerun_analyze_local
from scripts.rerun_analyze_local import build_command, read_notice, validate_notice


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
