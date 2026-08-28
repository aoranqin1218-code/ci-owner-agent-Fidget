from __future__ import annotations

import pytest
from pathlib import Path
import json
import os
import sys
import time
import csv
from scripts import batch_analyze_company_logs as company_batch
from scripts import _runtime

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


def read_summary_row(out_dir: Path, build: int | None = None) -> dict[str, str]:
    with (out_dir / "summary.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    return next(row for row in rows if build is None or row["build"] == str(build))

from scripts.batch_analyze_company_logs import (
    build_analyze_command,
    cleanup_previous_outputs,
    extract_history_stats_from_notice,
    extract_responsibility_stats_from_notice,
    filter_logs_by_build_range,
    load_logs,
    main,
    parse_build_log,
    read_last_jsonl,
)


def test_real_company_logs_use_trusted_checkout_evidence_and_normalize_branch():
    root = Path(__file__).resolve().parents[2]
    for name in ("company-unittest-5116.log", "company-unittest-5136.log", "company-unittest-5137.log"):
        item = parse_build_log(root / "samples" / "company_log" / name)
        assert item is not None
        assert item.head_commit is not None
        assert item.branch == "dev"
        assert item.checkout_ambiguous is False


@pytest.mark.parametrize("ref", ["refs/tags/v1", "refs/pull/1/head", "HEAD", "refs/unknown/foo"])
def test_invalid_checkout_ref_is_not_treated_as_missing_ref(tmp_path, ref):
    sha = "a" * 40
    path = tmp_path / "company-unittest-1.log"
    path.write_text(f"Checking out Revision {sha} ({ref})\nFinished: FAILURE\n", encoding="utf-8")
    item = parse_build_log(path)
    assert item is not None
    assert item.checkout_refs == (ref,)
    assert item.invalid_checkout_refs == (ref,)
    assert item.branch_error == "invalid checkout ref"
    analyzed = load_logs(tmp_path, "*.log", initial_base_commit="b" * 40, branch="dev")[0]
    assert analyzed.validation_failed is True
    assert analyzed.error_kind == "branch_validation"


def write_log(log_dir, build: int, commit: str, status: str) -> None:
    log_dir.joinpath(f"company-unittest-{build}.log").write_text(
        f"Checking out Revision {commit}\nAssertionError\nFinished: {status}\n",
        encoding="utf-8",
    )


def test_cleanup_previous_outputs_removes_existing_files(tmp_path):
    paths = [
        tmp_path / "notice.json",
        tmp_path / "stdout.txt",
        tmp_path / "stderr.txt",
        tmp_path / "trace.json",
    ]
    for path in paths:
        path.write_text("old", encoding="utf-8")
    warnings = cleanup_previous_outputs(paths)
    assert warnings == []
    assert all(not path.exists() for path in paths)


def test_cleanup_previous_outputs_ignores_missing_files(tmp_path):
    paths = [tmp_path / "missing.notice.json", tmp_path / "missing.trace.json"]
    warnings = cleanup_previous_outputs(paths)
    assert warnings == []
    assert all(not path.exists() for path in paths)


def test_batch_analyze_passes_last_success_build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    success_commit = "1" * 40
    failure_commit = "2" * 40
    (log_dir / "company-unittest-5075.log").write_text(
        f"Checking out Revision {success_commit}\nFinished: SUCCESS\n",
        encoding="utf-8",
    )
    (log_dir / "company-unittest-5076.log").write_text(
        f"Checking out Revision {failure_commit}\nAssertionError\nFinished: FAILURE\n",
        encoding="utf-8",
    )
    logs = load_logs(log_dir, "*.log")
    failure = next(item for item in logs if item.build == 5076)
    assert failure.base_commit == success_commit
    assert failure.last_success_build_number == 5075

    command = build_analyze_command(
        item=failure,
        repo="fx-code",
        job="services/fx-code-unittest",
        branch="dev",
        build_url_prefix="local://services/fx-code-unittest",
    )
    assert "--last-success-build" in command
    assert command[command.index("--last-success-build") + 1] == "5075"


def test_build_range_does_not_break_base_commit_calculation(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    success_commit = "1" * 40
    failure_commit = "2" * 40
    write_log(log_dir, 5068, success_commit, "SUCCESS")
    write_log(log_dir, 5072, failure_commit, "FAILURE")

    logs_all = load_logs(log_dir, "*.log")
    selected = filter_logs_by_build_range(logs_all, 5072, None)
    failure = selected[0]
    assert failure.build == 5072
    assert failure.base_commit == success_commit
    assert failure.last_success_build_number == 5068


def test_build_from_to_filters_selected_logs(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for build in [5072, 5073, 5076, 5080]:
        write_log(log_dir, build, str(build % 10) * 40, "FAILURE")

    selected = filter_logs_by_build_range(load_logs(log_dir, "*.log", initial_base_commit="a" * 40), 5072, 5076)
    assert [item.build for item in selected] == [5072, 5073, 5076]


def test_limit_applies_after_build_range_filter(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for build in [5072, 5073, 5076, 5080]:
        write_log(log_dir, build, str(build % 10) * 40, "FAILURE")

    selected = filter_logs_by_build_range(load_logs(log_dir, "*.log", initial_base_commit="a" * 40), 5072, 5080)
    runnable = [item for item in selected if item.status == "FAILURE" and not item.skip_reason]
    limited = runnable[:2]
    assert [item.build for item in limited] == [5072, 5073]


def test_build_from_greater_than_build_to_errors(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        [
            "batch_analyze_company_logs.py",
            "--log-dir",
            str(log_dir),
            "--build-from",
            "5076",
            "--build-to",
            "5072",
            "--dry-run",
        ],
    )
    with pytest.raises(SystemExit):
        main()


def test_main_conflicting_checkout_sha_returns_validation_failure(tmp_path, monkeypatch):
    log_dir, out_dir = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-1.log").write_text(
        f"Checking out Revision {'a' * 40} (refs/remotes/origin/dev)\n> git checkout -f {'b' * 40}\nFinished: FAILURE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out_dir), "--initial-base-commit", "c" * 40, "--dry-run"])
    assert main() == 1
    record = __import__("json").loads((out_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["validationFailed"] is True
    assert record["errorKind"] == "checkout_validation"
    assert record["executionSkipped"] is True
    assert read_summary_row(out_dir)["executionSkipped"] == "True"


def test_main_manifest_only_failure_is_validation_failure(tmp_path, monkeypatch):
    log_dir, out_dir = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"build": 2, "result": "FAILURE", "branch": "dev", "headCommit": "a" * 40}]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out_dir), "--manifest-file", str(manifest), "--initial-base-commit", "b" * 40, "--dry-run"])
    assert main() == 1
    record = json.loads((out_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["consoleFile"] is None and record["errorKind"] == "manifest_validation"
    assert record["executionSkipped"] is True
    assert read_summary_row(out_dir)["executionSkipped"] == "True"
    assert not list((out_dir / "notices").glob("*"))


def test_manifest_success_supplies_baseline_to_real_failure_log(tmp_path, monkeypatch):
    log_dir, out_dir = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    base, head = "a" * 40, "b" * 40
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {head} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"build": 1, "result": "SUCCESS", "branch": "dev", "headCommit": base}]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out_dir), "--manifest-file", str(manifest), "--dry-run"])
    assert main() == 0
    records = [json.loads(line) for line in (out_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    failure = next(item for item in records if item["build"] == 2)
    assert failure["baseCommit"] == base and failure["dryRun"] is True
    success = next(item for item in records if item["build"] == 1)
    assert success["executionSkipped"] is True and failure["executionSkipped"] is True
    assert all(read_summary_row(out_dir, build)["executionSkipped"] == "True" for build in (1, 2))


def test_company_cleanup_failure_is_fail_closed(tmp_path, monkeypatch):
    log_dir, out_dir = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    head = "b" * 40
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {head} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    called = False
    def forbidden_run(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not start")
    monkeypatch.setattr(company_batch, "cleanup_previous_outputs", lambda paths: ["failed to remove stale notice"])
    monkeypatch.setattr(company_batch, "run_analyze_local", forbidden_run)
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out_dir), "--initial-base-commit", "a" * 40])
    assert main() == 1 and called is False
    record = json.loads((out_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "cleanup" and record["noticeValid"] is False and record["skipped"] is False
    assert record["executionSkipped"] is True and read_summary_row(out_dir)["executionSkipped"] == "True"


@pytest.mark.parametrize(("mode", "notice_valid"), [("nonzero", True), ("nonzero_missing", False)])
def test_company_real_child_nonzero_keeps_execution_error(tmp_path, monkeypatch, mode, notice_valid):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_ANALYZE_MODE", mode)
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
                                      "--python", str(make_fake_python(tmp_path))])
    code = main()
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert code == 1 and record["returnCode"] == 3 and record["errorKind"] == "execution" and record["noticeValid"] is notice_valid
    assert record["executionSkipped"] is False and read_summary_row(out)["executionSkipped"] == "False"


def test_company_nonzero_valid_notice_is_not_resumable(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40, "--python", str(make_fake_python(tmp_path))]
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


def test_company_success_execution_state_matches_summary(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(
        f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8"
    )
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out),
                                      "--initial-base-commit", "a" * 40, "--python", str(make_fake_python(tmp_path))])
    assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["executionSkipped"] is False and read_summary_row(out)["executionSkipped"] == "False"


def test_company_marker_write_failure_is_fail_closed(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr(company_batch, "write_success_marker_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40, "--python", str(make_fake_python(tmp_path))])
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["noticeValid"] is True and record["errorKind"] == "resume_marker" and record["resumable"] is False
    assert record["executionSkipped"] is False and read_summary_row(out)["executionSkipped"] == "False"


def test_company_marker_hash_failure_is_resume_marker_and_reexecutes(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(
        f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8"
    )
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
            "--python", str(make_fake_python(tmp_path))]
    original_hash = company_batch.sha256_file
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr(company_batch, "sha256_file", lambda path: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["returnCode"] == 0 and record["noticeValid"] is True
    assert record["errorKind"] == "resume_marker" and "PermissionError: locked" in record["error"]
    assert record["successMarkerValid"] is False and "PermissionError: locked" in record["successMarkerError"]
    assert record["resumable"] is False and record["executionSkipped"] is False
    assert read_summary_row(out)["executionSkipped"] == "False"
    assert not list((out / "notices").glob("*.success.json"))
    monkeypatch.setattr(company_batch, "sha256_file", original_hash)
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 0
    resumed = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert resumed["resumeValidated"] is False and resumed["resumeInvalidReason"] == "success marker missing"
    assert resumed["executionSkipped"] is False and resumed["successMarkerValid"] is True


def test_company_marker_write_failure_does_not_leave_old_marker(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(
        f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8"
    )
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
            "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    old_marker = next((out / "notices").glob("*.success.json"))
    monkeypatch.setattr(company_batch, "write_success_marker_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "resume_marker" and record["resumable"] is False
    assert not old_marker.exists() and not list((out / "notices").glob("*.success.json"))


def test_company_partial_marker_is_removed_without_overwriting_primary_error(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(
        f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8"
    )

    def partial_write(marker_path, payload):
        marker_path.write_text(json.dumps(payload), encoding="utf-8")
        raise OSError("after write")

    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr(company_batch, "write_success_marker_atomic", partial_write)
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out),
                                      "--initial-base-commit", "a" * 40, "--python", str(make_fake_python(tmp_path))])
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "resume_marker" and "OSError: after write" in record["successMarkerError"]
    assert record["noticeValid"] is True and record["executionSkipped"] is False
    assert read_summary_row(out)["executionSkipped"] == "False"
    assert not list((out / "notices").glob("*.success.json"))


@pytest.mark.parametrize("resume", [False, True])
def test_company_marker_cleanup_failure_is_fail_closed(tmp_path, monkeypatch, resume):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(
        f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8"
    )
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
            "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    marker = next((out / "notices").glob("*.success.json"))
    if resume:
        payload = json.loads(marker.read_text(encoding="utf-8")); payload["workflow"] = "jenkins"
        marker.write_text(json.dumps(payload), encoding="utf-8")

    def cleanup(paths):
        warnings = []
        for path in paths:
            if path == marker:
                warnings.append(f"failed to remove {path}: locked marker")
            else:
                path.unlink(missing_ok=True)
        return warnings

    monkeypatch.setattr(company_batch, "cleanup_previous_outputs", cleanup)
    monkeypatch.setattr(company_batch, "run_analyze_local", lambda **k: (_ for _ in ()).throw(AssertionError("child must not start")))
    monkeypatch.setattr("sys.argv", [*argv, "--resume"] if resume else argv)
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "cleanup" and record["noticeValid"] is False and record["resumable"] is False
    assert record["executionSkipped"] is True and read_summary_row(out)["executionSkipped"] == "True"
    assert str(marker) in record["cleanupWarning"] and marker.exists()
    if resume:
        assert record["resumeValidated"] is False
        assert record["resumeInvalidReason"] == "success marker metadata mismatch: workflow"


def test_company_valid_resume_row_is_complete(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40, "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv); assert main() == 0
    monkeypatch.setattr(company_batch, "run_analyze_local", lambda **k: (_ for _ in ()).throw(AssertionError("must skip")))
    monkeypatch.setattr("sys.argv", [*argv, "--resume"]); assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    expected = {
        "skipped": True, "executionSkipped": True, "resumeValidated": True, "resumable": True,
        "successMarkerValid": True, "noticeValid": True, "returnCode": 0,
        "skipReason": "resume: validated successful execution",
    }
    assert {key: record[key] for key in expected} == expected
    assert record["skipReason"] == "resume: validated successful execution"
    with (out / "summary.csv").open(encoding="utf-8-sig", newline="") as file:
        row = next(csv.DictReader(file))
    assert row["skipped"] == "True" and row["executionSkipped"] == "True"
    assert row["resumeValidated"] == "True" and row["noticeValid"] == "True" and row["returnCode"] == "0"


def test_company_digest_read_error_reexecutes(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40, "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success"); monkeypatch.setattr("sys.argv", argv); assert main() == 0
    monkeypatch.setattr(_runtime, "sha256_file", lambda path: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr("sys.argv", [*argv, "--resume"]); assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is False
    assert "failed to hash notice for success marker: PermissionError" in record["resumeInvalidReason"]


def test_company_digest_error_cleanup_failure_continues_next_build(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    for build, commit in ((2, "b" * 40), (3, "c" * 40)):
        (log_dir / f"company-unittest-{build}.log").write_text(
            f"Checking out Revision {commit} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8"
        )
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
            "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    monkeypatch.setattr(_runtime, "sha256_file", lambda path: (_ for _ in ()).throw(PermissionError("locked")))

    def cleanup(paths):
        warnings = []
        for path in paths:
            if "_2_" in path.name and path.name.endswith(".success.json"):
                warnings.append(f"failed to remove {path}: locked marker")
            else:
                path.unlink(missing_ok=True)
        return warnings

    monkeypatch.setattr(company_batch, "cleanup_previous_outputs", cleanup)
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 1
    records = [json.loads(line) for line in (out / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[0]["resumeValidated"] is False and records[0]["errorKind"] == "cleanup"
    assert "PermissionError" in records[0]["resumeInvalidReason"] and "locked marker" in records[0]["cleanupWarning"]
    assert records[1]["returnCode"] == 0 and records[1]["noticeValid"] is True and records[1]["resumable"] is True


def test_company_timeout_residual_notice_is_not_resumed(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(
        f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8"
    )
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
            "--python", str(make_fake_python(tmp_path)), "--timeout-seconds", "1"]
    original_cleanup = company_batch.cleanup_previous_outputs
    calls = 0

    def leave_timeout_notice(paths):
        nonlocal calls
        calls += 1
        return original_cleanup(paths) if calls == 1 else [f"failed to remove {paths[0]}: locked"]

    monkeypatch.setattr(company_batch, "cleanup_previous_outputs", leave_timeout_notice)
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "write_then_timeout")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 1
    first = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    notice = Path(first["noticeFile"])
    assert first["errorKind"] == "timeout" and first["resumable"] is False and notice.exists()
    assert not Path(first["successMarkerFile"]).exists() and "failed to remove" in first["cleanupWarning"]

    monkeypatch.setattr(company_batch, "cleanup_previous_outputs", original_cleanup)
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
        ("not_object", "invalid success marker schema: ValidationError"),
        ("schemaVersion_bool", "invalid success marker schema: ValidationError"),
        ("returnCode_bool", "invalid success marker schema: ValidationError"),
        ("buildNumber_float", "invalid success marker schema: ValidationError"),
        ("unexpectedField", "invalid success marker schema: ValidationError"),
        ("schemaVersion", "success marker metadata mismatch: schemaVersion"),
        ("kind", "success marker metadata mismatch: kind"),
        ("workflow", "success marker metadata mismatch: workflow"),
        ("returnCode", "success marker metadata mismatch: returnCode"),
        ("repo", "success marker metadata mismatch: repo"),
        ("job", "success marker metadata mismatch: job"),
        ("buildNumber", "success marker metadata mismatch: buildNumber"),
        ("branch", "success marker metadata mismatch: branch"),
        ("result", "success marker metadata mismatch: result"),
        ("baseCommit", "success marker metadata mismatch: baseCommit"),
        ("headCommit", "success marker metadata mismatch: headCommit"),
        ("noticeFile", "success marker metadata mismatch: noticeFile"),
        ("noticeSha256", "success marker notice digest mismatch"),
        ("notice_missing", "notice missing for success marker"),
    ],
)
def test_company_invalid_marker_matrix_reexecutes(tmp_path, monkeypatch, case, expected_reason):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(
        f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8"
    )
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
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
        if case == "schemaVersion_bool":
            payload["schemaVersion"] = True
        elif case == "returnCode_bool":
            payload["returnCode"] = False
        elif case == "buildNumber_float":
            payload["buildNumber"] = 2.0
        elif case == "unexpectedField":
            payload["unexpectedField"] = "value"
        elif case == "workflow":
            payload["workflow"] = "jenkins"
        else:
            payload[case] = "0" * 64 if case == "noticeSha256" else (3 if case in {"schemaVersion", "returnCode", "buildNumber"} else "wrong")
        marker.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 0
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is False and expected_reason in record["resumeInvalidReason"]
    assert record["executionSkipped"] is False and record["returnCode"] == 0 and record["successMarkerValid"] is True


def test_company_descendant_stops_after_timeout(tmp_path, monkeypatch):
    log_dir, out, marker = tmp_path / "logs", tmp_path / "out", tmp_path / "marker.txt"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "spawn_descendant_then_timeout")
    monkeypatch.setenv("FAKE_ANALYZE_MARKER_FILE", str(marker))
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
                                      "--python", str(make_fake_python(tmp_path)), "--timeout-seconds", "1"])
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["errorKind"] == "timeout" and record["noticeValid"] is False and record["terminationReaped"] is True
    assert record["executionSkipped"] is False and read_summary_row(out)["executionSkipped"] == "False"
    size = marker.stat().st_size
    time.sleep(0.3)
    assert marker.stat().st_size == size


def test_company_cleanup_failure_continues_next_build(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    for build, sha in ((2, "b" * 40), (3, "c" * 40)):
        (log_dir / f"company-unittest-{build}.log").write_text(f"Checking out Revision {sha} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    calls = 0
    original = company_batch.cleanup_previous_outputs
    def cleanup(paths):
        nonlocal calls
        calls += 1
        return ["locked"] if calls == 1 else original(paths)
    monkeypatch.setattr(company_batch, "cleanup_previous_outputs", cleanup)
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40,
                                      "--python", str(make_fake_python(tmp_path))])
    assert main() == 1
    records = [json.loads(line) for line in (out / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[0]["errorKind"] == "cleanup" and records[1]["noticeValid"] is True and records[1]["returnCode"] == 0


def test_company_invalid_resume_cleanup_failure_is_fail_closed(tmp_path, monkeypatch):
    log_dir, out = tmp_path / "logs", tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "company-unittest-2.log").write_text(f"Checking out Revision {'b' * 40} (refs/remotes/origin/dev)\nFinished: FAILURE\n", encoding="utf-8")
    argv = ["batch", "--log-dir", str(log_dir), "--out-dir", str(out), "--initial-base-commit", "a" * 40, "--python", str(make_fake_python(tmp_path))]
    monkeypatch.setenv("FAKE_ANALYZE_MODE", "success")
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    notice = next((out / "notices").glob("*.notice.json"))
    payload = json.loads(notice.read_text(encoding="utf-8")); payload["buildNumber"] = 999
    notice.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(company_batch, "cleanup_previous_outputs", lambda paths: ["cannot remove invalid resume notice"])
    monkeypatch.setattr(company_batch, "run_analyze_local", lambda **kwargs: (_ for _ in ()).throw(AssertionError("child must not start")))
    monkeypatch.setattr("sys.argv", [*argv, "--resume"])
    assert main() == 1
    record = json.loads((out / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["resumeValidated"] is False and record["resumeInvalidReason"] and record["errorKind"] == "cleanup"
    assert record["executionSkipped"] is True and read_summary_row(out)["executionSkipped"] == "True"


def test_extract_history_stats_from_structured_notice():
    stats = extract_history_stats_from_notice(
        {
            "history_search_similar_failures": {
                "ok": True,
                "candidates": [
                    {
                        "buildNumber": 5111,
                        "similarity": 1.0,
                        "matchType": "signature_exact",
                        "relationship": "very_likely_same_failure",
                    },
                    {"buildNumber": 5110, "similarity": 1.0},
                ],
            }
        }
    )
    assert stats["historicalMatchCount"] == 2
    assert stats["topHistoricalMatchBuild"] == 5111
    assert stats["topHistoricalMatchSimilarity"] == 1.0
    assert stats["topHistoricalMatchType"] == "signature_exact"
    assert stats["topHistoricalRelationship"] == "very_likely_same_failure"


def test_extract_history_stats_no_candidates_and_warning():
    no_candidates = extract_history_stats_from_notice({"history_search_similar_failures": {"ok": True, "candidates": []}})
    assert no_candidates["historicalMatchCount"] == 0
    assert no_candidates["topHistoricalMatchBuild"] is None

    warning = extract_history_stats_from_notice(
        {"history_search_similar_failures": {"ok": True, "warning": "test failure summaries unavailable; history similarity skipped", "candidates": []}}
    )
    assert warning["historicalMatchCount"] == 0
    assert warning["topHistoricalMatchBuild"] is None


def test_extract_history_stats_from_notice_evidence_text():
    stats = extract_history_stats_from_notice(
        {
            "evidence": [
                {
                    "source": "history_search_similar_failures",
                    "summary": "历史相似失败检查命中",
                    "detail": (
                        "history_search_similar_failures 返回 5 个候选，"
                        "top build=5111，similarity=1.0，matchType=signature_exact，"
                        "relationship=very_likely_same_failure"
                    ),
                }
            ]
        }
    )
    assert stats["historicalMatchCount"] == 5
    assert stats["topHistoricalMatchBuild"] == 5111
    assert stats["topHistoricalMatchSimilarity"] == 1.0
    assert stats["topHistoricalMatchType"] == "signature_exact"
    assert stats["topHistoricalRelationship"] == "very_likely_same_failure"


def test_extract_responsibility_stats_from_notice_items():
    stats = extract_responsibility_stats_from_notice(
        {
            "responsibilityItems": [
                {
                    "failureId": "F1",
                    "failureTitle": "historical failure",
                    "owner": {
                        "type": "inherited_failure_owner",
                        "name": "Tang.Tangerine-唐嘉伟",
                        "email": "tang@example.com",
                        "commit": "ab286e5",
                        "confidence": 0.9,
                    },
                    "responsibilityType": "inherited_failure_owner",
                    "sourceBuildNumber": 5104,
                    "confidence": 1.0,
                    "reason": "历史持续失败。",
                },
                {
                    "failureId": "F2",
                    "failureTitle": "new failure",
                    "owner": {
                        "type": "high_confidence",
                        "name": "Li Si",
                        "email": "lisi@example.com",
                        "commit": "f" * 40,
                        "confidence": 0.88,
                    },
                    "responsibilityType": "current_build_owner",
                    "confidence": 0.88,
                    "reason": "新失败可定责。",
                },
                {
                    "failureId": "F3",
                    "failureTitle": "unknown failure",
                    "owner": {
                        "type": "no_high_confidence_owner",
                        "name": "无高可信责任人",
                        "email": None,
                        "commit": None,
                        "confidence": 0,
                    },
                    "responsibilityType": "no_high_confidence_owner",
                    "confidence": 0,
                    "reason": "证据不足。",
                },
            ]
        }
    )
    assert stats["responsibilityItemCount"] == 3
    assert stats["responsibleOwners"] == "Tang.Tangerine-唐嘉伟(inherited from #5104); Li Si(high_confidence)"
    assert stats["inheritedOwners"] == "Tang.Tangerine-唐嘉伟"
    assert stats["currentBuildOwners"] == "Li Si"
    assert stats["unresolvedFailureCount"] == 1


def test_extract_responsibility_stats_backward_compatible_without_items():
    stats = extract_responsibility_stats_from_notice({"owner": {"type": "high_confidence", "name": "Zhang San"}})
    assert stats["responsibilityItemCount"] == 0
    assert stats["responsibleOwners"] == ""
    assert stats["unresolvedFailureCount"] == 0

def test_load_logs_sets_previous_commit_for_consecutive_failures(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    success_commit = "a" * 40
    fail1_commit = "b" * 40
    fail2_commit = "c" * 40

    (log_dir / "company-unittest-5075.log").write_text(
        f"Checking out Revision {success_commit}\nFinished: SUCCESS\n",
        encoding="utf-8",
    )
    (log_dir / "company-unittest-5076.log").write_text(
        f"Checking out Revision {fail1_commit}\nAssertionError\nFinished: FAILURE\n",
        encoding="utf-8",
    )
    (log_dir / "company-unittest-5077.log").write_text(
        f"Checking out Revision {fail2_commit}\nAssertionError\nFinished: FAILURE\n",
        encoding="utf-8",
    )

    logs = load_logs(log_dir, "*.log")

    fail1 = next(item for item in logs if item.build == 5076)
    assert fail1.base_commit == success_commit
    assert fail1.last_success_build_number == 5075
    # Previous build is the last SUCCESS (#5075)
    assert fail1.previous_build_number == 5075
    assert fail1.previous_commit == success_commit

    fail2 = next(item for item in logs if item.build == 5077)
    assert fail2.base_commit == success_commit
    assert fail2.last_success_build_number == 5075
    # Previous build is the FAILURE #5076
    assert fail2.previous_build_number == 5076
    assert fail2.previous_commit == fail1_commit


def test_load_logs_previous_commit_none_when_no_prior_build_with_head_commit(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    fail_commit = "a" * 40

    (log_dir / "company-unittest-5076.log").write_text(
        f"Checking out Revision {fail_commit}\nAssertionError\nFinished: FAILURE\n",
        encoding="utf-8",
    )

    # No initial_base_commit and no prior SUCCESS -> skip_reason
    logs = load_logs(log_dir, "*.log")
    fail = next(item for item in logs if item.build == 5076)
    assert fail.skip_reason is not None  # missing previous successful commit
    assert fail.previous_build_number is None
    assert fail.previous_commit is None


def test_load_logs_previous_commit_with_initial_base_commit(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    fail_commit = "a" * 40
    initial = "i" * 40

    (log_dir / "company-unittest-5076.log").write_text(
        f"Checking out Revision {fail_commit}\nAssertionError\nFinished: FAILURE\n",
        encoding="utf-8",
    )

    logs = load_logs(log_dir, "*.log", initial_base_commit=initial)
    fail = next(item for item in logs if item.build == 5076)
    assert fail.base_commit == initial
    # No prior build with headCommit, so previous fields are None
    assert fail.previous_build_number is None
    assert fail.previous_commit is None


def test_build_analyze_command_includes_previous_args(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    success_commit = "a" * 40
    fail_commit = "b" * 40

    (log_dir / "company-unittest-5075.log").write_text(
        f"Checking out Revision {success_commit}\nFinished: SUCCESS\n",
        encoding="utf-8",
    )
    (log_dir / "company-unittest-5076.log").write_text(
        f"Checking out Revision {fail_commit}\nAssertionError\nFinished: FAILURE\n",
        encoding="utf-8",
    )

    logs = load_logs(log_dir, "*.log")
    failure = next(item for item in logs if item.build == 5076)

    command = build_analyze_command(
        item=failure,
        repo="fx-code",
        job="services/fx-code-unittest",
        branch="dev",
        build_url_prefix="local://services/fx-code-unittest",
    )

    assert "--last-success-build" in command
    assert command[command.index("--last-success-build") + 1] == "5075"
    assert "--previous-build" in command
    assert command[command.index("--previous-build") + 1] == "5075"
    assert "--previous-commit" in command
    assert command[command.index("--previous-commit") + 1] == success_commit


def test_build_analyze_command_omits_previous_when_none(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    initial = "i" * 40
    fail_commit = "a" * 40

    (log_dir / "company-unittest-5076.log").write_text(
        f"Checking out Revision {fail_commit}\nAssertionError\nFinished: FAILURE\n",
        encoding="utf-8",
    )

    logs = load_logs(log_dir, "*.log", initial_base_commit=initial)
    failure = next(item for item in logs if item.build == 5076)

    command = build_analyze_command(
        item=failure,
        repo="fx-code",
        job="services/fx-code-unittest",
        branch="dev",
        build_url_prefix="local://services/fx-code-unittest",
    )

    assert "--previous-build" not in command
    assert "--previous-commit" not in command


def test_read_last_jsonl_reads_last_line(tmp_path):
    metrics_file = tmp_path / "test.metrics.jsonl"
    metrics_file.write_text(
        '{"durationMs": 100}\n{"durationMs": 200, "llmCalls": 3}\n',
        encoding="utf-8",
    )
    result = read_last_jsonl(metrics_file)
    assert result is not None
    assert result["durationMs"] == 200
    assert result["llmCalls"] == 3


def test_read_last_jsonl_returns_none_for_missing_file(tmp_path):
    result = read_last_jsonl(tmp_path / "nonexistent.jsonl")
    assert result is None


def test_read_last_jsonl_returns_none_for_empty_file(tmp_path):
    metrics_file = tmp_path / "empty.jsonl"
    metrics_file.write_text("", encoding="utf-8")
    result = read_last_jsonl(metrics_file)
    assert result is None
