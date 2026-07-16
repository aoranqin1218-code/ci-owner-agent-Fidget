from __future__ import annotations

import sys
import os
import json
from pathlib import Path
import pytest

from scripts import batch_analyze_jenkins_builds as jenkins_batch
from scripts._runtime import BoundedProcessTimeout, ProcessTerminationResult

from scripts.batch_analyze_jenkins_builds import (
    build_analyze_command,
    extract_ai_history_stats_from_trace_or_notice,
    extract_first_json_object,
    extract_responsibility_stats_from_notice,
    metadata_matches_jenkins,
    parse_builds,
    main,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_analyze_launcher.py"


def make_fake_python(tmp_path: Path) -> Path:
    if os.name == "nt":
        path = tmp_path / "fake-python.cmd"
        path.write_text(f'@"{sys.executable}" "{FIXTURE}" %*\r\n', encoding="utf-8")
    else:
        path = tmp_path / "fake-python"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    return path


def run_main(tmp_path, monkeypatch, mode: str, mismatch_field: str | None = None) -> tuple[int, dict]:
    out = tmp_path / "out"
    monkeypatch.setenv("FAKE_ANALYZE_MODE", mode)
    if mismatch_field:
        monkeypatch.setenv("FAKE_ANALYZE_MISMATCH_FIELD", mismatch_field)
    monkeypatch.setattr("sys.argv", ["jenkins-batch", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out), "--python", str(make_fake_python(tmp_path))])
    code = main()
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    return code, record


def test_jenkins_main_real_child_success(tmp_path, monkeypatch):
    code, record = run_main(tmp_path, monkeypatch, "success")
    assert code == 0 and record["returnCode"] == 0 and record["noticeValid"] is True


def test_jenkins_main_real_child_nonzero(tmp_path, monkeypatch):
    code, record = run_main(tmp_path, monkeypatch, "nonzero")
    assert code == 1 and record["returnCode"] == 3 and record["errorKind"] == "execution"


def test_jenkins_main_missing_notice(tmp_path, monkeypatch):
    code, record = run_main(tmp_path, monkeypatch, "missing_notice")
    assert code == 1 and record["noticeValid"] is False and record["errorKind"] == "notice_missing"


@pytest.mark.parametrize("mode", ["invalid_json", "invalid_schema"])
def test_jenkins_main_invalid_notice_schema(tmp_path, monkeypatch, mode):
    code, record = run_main(tmp_path, monkeypatch, mode)
    assert code == 1 and record["noticeValid"] is False and record["errorKind"] == "notice_schema"


@pytest.mark.parametrize("field", ["repo", "job", "buildNumber"])
def test_jenkins_main_metadata_mismatch(tmp_path, monkeypatch, field):
    code, record = run_main(tmp_path, monkeypatch, "metadata_mismatch", field)
    assert code == 1 and record["errorKind"] == "notice_metadata" and field in record["error"]


def test_jenkins_cleanup_failure_is_fail_closed(tmp_path, monkeypatch):
    out = tmp_path / "out"
    called = False
    def forbidden(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("child must not start")
    monkeypatch.setattr(jenkins_batch, "cleanup_previous_outputs", lambda paths: ["locked stale notice"])
    monkeypatch.setattr(jenkins_batch, "run_analyze", forbidden)
    monkeypatch.setattr("sys.argv", ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out)])
    assert main() == 1 and called is False
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "cleanup" and record["noticeValid"] is False


def test_jenkins_dry_run_does_not_start_child(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(jenkins_batch, "run_analyze", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not run")))
    monkeypatch.setattr("sys.argv", ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out), "--dry-run"])
    assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["dryRun"] is True


def test_jenkins_main_real_child_timeout(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "write_then_timeout")
    monkeypatch.setattr("sys.argv", ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out),
                                      "--python", str(make_fake_python(tmp_path)), "--timeout-seconds", "1"])
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "timeout" and record["noticeValid"] is not True


def test_jenkins_valid_resume_skips_child(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    argv = ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out), "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    monkeypatch.setattr(jenkins_batch, "run_analyze", lambda **kwargs: (_ for _ in ()).throw(AssertionError("resume must not run child")))
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is True and record["skipped"] is True


@pytest.mark.parametrize("invalid_content", ["{invalid", '{"repo":"repo"}'])
def test_jenkins_invalid_resume_reexecutes(tmp_path, monkeypatch, invalid_content):
    out = tmp_path / "out"
    notice_dir = out / "notices"
    notice_dir.mkdir(parents=True)
    (notice_dir / "job_13.notice.json").write_text(invalid_content, encoding="utf-8")
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out),
                                      "--python", str(make_fake_python(tmp_path)), "--resume"])
    assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is False and record["resumeInvalidReason"]
    assert record["returnCode"] == 0 and record["noticeValid"] is True


def test_jenkins_invalid_resume_cleanup_failure_is_fail_closed(tmp_path, monkeypatch):
    out = tmp_path / "out"
    argv = ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out), "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    notice = next((out / "notices").glob("*.notice.json"))
    payload = json.loads(notice.read_text(encoding="utf-8")); payload["repo"] = "wrong"
    notice.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(jenkins_batch, "cleanup_previous_outputs", lambda paths: ["cannot remove invalid resume notice"])
    monkeypatch.setattr(jenkins_batch, "run_analyze", lambda **kwargs: (_ for _ in ()).throw(AssertionError("child must not start")))
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is False and record["resumeInvalidReason"] and record["errorKind"] == "cleanup"


@pytest.mark.parametrize(("mode", "notice_valid"), [
    ("success", True), ("missing_notice", False), ("invalid_json", False),
    ("invalid_schema", False), ("metadata_mismatch", False),
])
def test_jenkins_nonzero_execution_precedes_notice_failures(tmp_path, monkeypatch, mode, notice_valid):
    monkeypatch.setenv("FAKE_ANALYZE_EXIT_CODE", "3")
    code, record = run_main(tmp_path, monkeypatch, mode, "repo" if mode == "metadata_mismatch" else None)
    assert code == 1 and record["returnCode"] == 3
    assert record["errorKind"] == "execution" and "exited with 3" in record["error"]
    assert record["noticeValid"] is notice_valid
    if not notice_valid:
        assert record["noticeValidationError"]


def test_jenkins_timeout_preserves_cleanup_and_termination_warnings(tmp_path, monkeypatch):
    out = tmp_path / "out"
    calls = 0
    def cleanup(paths):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else ["failed to remove notice"]
    timeout = BoundedProcessTimeout(["fake"], 1, output="partial", stderr="err",
                                    termination=ProcessTerminationResult(False, "final reap timed out"))
    monkeypatch.setattr(jenkins_batch, "cleanup_previous_outputs", cleanup)
    monkeypatch.setattr(jenkins_batch, "run_analyze", lambda **kwargs: (_ for _ in ()).throw(timeout))
    monkeypatch.setattr("sys.argv", ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out)])
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "timeout" and record["noticeValid"] is False
    assert record["terminationReaped"] is False and "final reap timed out" in record["terminationWarning"]
    assert "failed to remove notice" in record["cleanupWarning"]


def test_parse_builds_list_range_and_union():
    assert parse_builds("7,13", None, None) == [7, 13]
    assert parse_builds(None, 7, 9) == [7, 8, 9]
    assert parse_builds("7,13", 8, 10) == [7, 8, 9, 10, 13]


def test_build_analyze_command_flags():
    command = build_analyze_command(
        build=13,
        repo="fx-code",
        job="CI-test/unintest-MatureLeek",
        log_tail_lines=200,
        notify=False,
        notify_dry_run=False,
        force_notify=False,
    )
    assert command[:4] == [sys.executable, "-m", "ci_owner_agent", "analyze"]
    assert "--notify" not in command
    assert "--branch" not in command
    assert "--base-commit" not in command
    assert "--head-commit" not in command
    assert "--console-file" not in command
    assert "--build-url" not in command
    assert "--last-success-build" not in command

    command = build_analyze_command(
        build=13,
        repo="fx-code",
        job="CI-test/unintest-MatureLeek",
        log_tail_lines=200,
        notify=True,
        notify_dry_run=True,
        force_notify=True,
    )
    assert "--notify" in command
    assert "--notify-dry-run" in command
    assert "--force-notify" in command


def test_metadata_matches_jenkins_uses_job_repo_build_only():
    assert metadata_matches_jenkins(
        {
            "job": "CI-test/unintest-MatureLeek",
            "repo": "fx-code",
            "buildNumber": 13,
            "baseCommit": "different",
            "headCommit": "different",
        },
        build=13,
        repo="fx-code",
        job="CI-test/unintest-MatureLeek",
    )
    assert not metadata_matches_jenkins(
        {"job": "CI-test/unintest-MatureLeek", "repo": "fx-code", "buildNumber": 12},
        build=13,
        repo="fx-code",
        job="CI-test/unintest-MatureLeek",
    )


def test_extract_first_json_object_from_noisy_stdout():
    parsed = extract_first_json_object('noise before\n{"job":"j","buildNumber":13}\nnoise after')
    assert parsed == {"job": "j", "buildNumber": 13}


def test_extract_ai_history_stats_from_nested_trace():
    trace = {
        "child_runs": [
            {
                "inputs": {
                    "payload": {
                        "aiHistoryPrecheck": {
                            "diagnostics": {
                                "eligibleCurrentFactsCount": 1,
                                "historicalBuildsCount": 1,
                                "historicalFactsCount": 1,
                                "rankedPairsCount": 1,
                                "comparedPairsCount": 1,
                                "acceptedCandidatesCount": 1,
                                "queryStage": "ok",
                                "skipped": {"compareNotSameFailure": 0},
                            }
                        }
                    }
                }
            }
        ]
    }
    stats = extract_ai_history_stats_from_trace_or_notice({}, trace)
    assert stats["aiHistoryEligibleCurrentFactsCount"] == 1
    assert stats["aiHistoryHistoricalBuildsCount"] == 1
    assert stats["aiHistoryHistoricalFactsCount"] == 1
    assert stats["aiHistoryRankedPairsCount"] == 1
    assert stats["aiHistoryComparedPairsCount"] == 1
    assert stats["aiHistoryAcceptedCandidatesCount"] == 1
    assert stats["aiHistoryQueryStage"] == "ok"
    assert "compareNotSameFailure" in stats["aiHistorySkipped"]


def test_summary_responsibility_stats_for_mixed_items():
    stats = extract_responsibility_stats_from_notice(
        {
            "responsibilityItems": [
                {
                    "owner": {"type": "inherited_failure_owner", "name": "Tang"},
                    "responsibilityType": "inherited_failure_owner",
                    "sourceBuildNumber": 7,
                },
                {
                    "owner": {"type": "high_confidence", "name": "Li"},
                    "responsibilityType": "current_build_owner",
                },
                {
                    "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人"},
                    "responsibilityType": "no_high_confidence_owner",
                },
            ]
        }
    )
    assert stats["responsibilityItemCount"] == 3
    assert stats["responsibleOwners"] == "Tang(inherited from #7); Li(high_confidence)"
    assert stats["inheritedOwners"] == "Tang"
    assert stats["currentBuildOwners"] == "Li"
    assert stats["unresolvedFailureCount"] == 1
