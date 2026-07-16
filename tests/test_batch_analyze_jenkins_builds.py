from __future__ import annotations

import sys
import os
import json
import csv
from pathlib import Path
import pytest

from scripts import batch_analyze_jenkins_builds as jenkins_batch
from scripts import _runtime
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
    expected = {
        "skipped": True, "executionSkipped": True, "resumeValidated": True, "resumable": True,
        "successMarkerValid": True, "noticeValid": True, "returnCode": 0,
        "skipReason": "resume: validated successful execution",
    }
    assert {key: record[key] for key in expected} == expected
    with (out / "summary.csv").open(encoding="utf-8-sig", newline="") as file:
        row = next(csv.DictReader(file))
    assert row["skipped"] == "True" and row["executionSkipped"] == "True"
    assert row["resumeValidated"] == "True" and row["noticeValid"] == "True" and row["returnCode"] == "0"


def test_jenkins_digest_read_error_reexecutes(tmp_path, monkeypatch):
    out = tmp_path / "out"
    argv = ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out),
            "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    monkeypatch.setattr(_runtime, "sha256_file", lambda path: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is False
    assert "failed to hash notice for success marker: PermissionError" in record["resumeInvalidReason"]
    assert record["executionSkipped"] is False and record["returnCode"] == 0


def test_jenkins_marker_write_failure_is_fail_closed_and_reexecutes(tmp_path, monkeypatch):
    out = tmp_path / "out"
    argv = ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out),
            "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    original = jenkins_batch.write_success_marker_atomic
    monkeypatch.setattr(jenkins_batch, "write_success_marker_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["returnCode"] == 0 and record["noticeValid"] is True and record["errorKind"] == "resume_marker"
    assert record["successMarkerValid"] is False and record["successMarkerError"] == "denied" and record["resumable"] is False
    assert not list((out / "notices").glob("*.success.json"))
    monkeypatch.setattr(jenkins_batch, "write_success_marker_atomic", original)
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 0
    resumed = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert resumed["resumeValidated"] is False and resumed["resumeInvalidReason"] == "success marker missing"
    assert resumed["executionSkipped"] is False and resumed["resumable"] is True
    old_marker = next((out / "notices").glob("*.success.json"))
    monkeypatch.setattr(jenkins_batch, "write_success_marker_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("denied again")))
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 1
    final = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert final["errorKind"] == "resume_marker" and final["resumable"] is False
    assert not old_marker.exists() and not list((out / "notices").glob("*.success.json"))


@pytest.mark.parametrize("resume", [False, True])
def test_jenkins_marker_cleanup_failure_is_fail_closed(tmp_path, monkeypatch, resume):
    out = tmp_path / "out"
    argv = ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out),
            "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    marker = next((out / "notices").glob("*.success.json"))
    if resume:
        payload = json.loads(marker.read_text(encoding="utf-8")); payload["workflow"] = "wrong"
        marker.write_text(json.dumps(payload), encoding="utf-8")

    def cleanup(paths):
        warnings = []
        for path in paths:
            if path == marker:
                warnings.append(f"failed to remove {path}: locked marker")
            else:
                path.unlink(missing_ok=True)
        return warnings

    monkeypatch.setattr(jenkins_batch, "cleanup_previous_outputs", cleanup)
    monkeypatch.setattr(jenkins_batch, "run_analyze", lambda **k: (_ for _ in ()).throw(AssertionError("child must not start")))
    monkeypatch.setattr("sys.argv", [*argv, "--resume"] if resume else argv)
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "cleanup" and record["noticeValid"] is False and record["resumable"] is False
    assert str(marker) in record["cleanupWarning"] and marker.exists()
    if resume:
        assert record["resumeValidated"] is False
        assert record["resumeInvalidReason"] == "success marker metadata mismatch: workflow"


def test_jenkins_timeout_residual_notice_is_not_resumed(tmp_path, monkeypatch):
    out = tmp_path / "out"
    argv = ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out),
            "--python", str(make_fake_python(tmp_path)), "--timeout-seconds", "1"]
    original_cleanup = jenkins_batch.cleanup_previous_outputs
    calls = 0

    def leave_timeout_notice(paths):
        nonlocal calls
        calls += 1
        return original_cleanup(paths) if calls == 1 else [f"failed to remove {paths[0]}: locked"]

    monkeypatch.setattr(jenkins_batch, "cleanup_previous_outputs", leave_timeout_notice)
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "write_then_timeout")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 1
    first = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    notice = Path(first["noticeFile"])
    assert first["errorKind"] == "timeout" and first["resumable"] is False and notice.exists()
    assert not Path(first["successMarkerFile"]).exists() and "failed to remove" in first["cleanupWarning"]
    monkeypatch.setattr(jenkins_batch, "cleanup_previous_outputs", original_cleanup)
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 0
    second = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert second["resumeValidated"] is False and second["resumeInvalidReason"] == "success marker missing"
    assert second["executionSkipped"] is False and second["returnCode"] == 0 and second["resumable"] is True


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("missing_marker", "success marker missing"),
        ("invalid_json", "invalid success marker JSON: JSONDecodeError"),
        ("not_object", "invalid success marker JSON: ValueError"),
        ("schemaVersion", "success marker metadata mismatch: schemaVersion"),
        ("kind", "success marker metadata mismatch: kind"),
        ("workflow", "success marker metadata mismatch: workflow"),
        ("returnCode", "success marker metadata mismatch: returnCode"),
        ("repo", "success marker metadata mismatch: repo"),
        ("job", "success marker metadata mismatch: job"),
        ("buildNumber", "success marker metadata mismatch: buildNumber"),
        ("noticeFile", "success marker metadata mismatch: noticeFile"),
        ("noticeSha256", "success marker notice digest mismatch"),
        ("notice_missing", "notice missing for success marker"),
    ],
)
def test_jenkins_invalid_marker_matrix_reexecutes(tmp_path, monkeypatch, case, expected_reason):
    out = tmp_path / "out"
    argv = ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out),
            "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    notice = next((out / "notices").glob("*.notice.json"))
    marker = next((out / "notices").glob("*.success.json"))
    if case == "missing_marker":
        marker.unlink()
    elif case == "invalid_json":
        marker.write_text("{invalid", encoding="utf-8")
    elif case == "not_object":
        marker.write_text("[]", encoding="utf-8")
    elif case == "notice_missing":
        notice.unlink()
    else:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload[case] = "0" * 64 if case == "noticeSha256" else (3 if case in {"schemaVersion", "returnCode", "buildNumber"} else "wrong")
        marker.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is False and expected_reason in record["resumeInvalidReason"]
    assert record["executionSkipped"] is False and record["returnCode"] == 0 and record["successMarkerValid"] is True


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


def test_jenkins_nonzero_valid_notice_is_not_resumable(tmp_path, monkeypatch):
    out = tmp_path / "out"
    argv = ["jenkins", "--job", "job", "--repo", "repo", "--builds", "13", "--out-dir", str(out), "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "nonzero")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 1
    assert not list((out / "notices").glob("*.success.json"))
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is False and "success marker missing" in record["resumeInvalidReason"]
    assert record["resumable"] is True and list((out / "notices").glob("*.success.json"))


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
