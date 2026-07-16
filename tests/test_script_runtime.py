from __future__ import annotations

import subprocess

import pytest

from scripts import _runtime
import json


class FakeProcess:
    pid = 123

    def __init__(self, *, always_timeout: bool = False):
        self.calls = 0
        self.always_timeout = always_timeout
        self.killed = False

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1 or self.always_timeout:
            raise subprocess.TimeoutExpired(["fake"], timeout, output="partial", stderr="err")
        return "done", ""

    def kill(self):
        self.killed = True


class SequenceProcess(FakeProcess):
    def __init__(self, actions, kill_error=None):
        super().__init__()
        self.actions = iter(actions)
        self.kill_error = kill_error
    def communicate(self, timeout=None):
        action = next(self.actions)
        if isinstance(action, BaseException):
            raise action
        return action
    def kill(self):
        if self.kill_error:
            raise self.kill_error
        super().kill()


def invoke(monkeypatch, process):
    monkeypatch.setattr(_runtime.subprocess, "Popen", lambda *a, **k: process)
    with pytest.raises(_runtime.BoundedProcessTimeout) as caught:
        _runtime.run_process_bounded(["fake"], cwd=_runtime.REPO_ROOT, env={}, timeout_seconds=0.01, grace_seconds=0.01)
    return caught.value.termination


def success_marker_payload(notice, **updates):
    payload = {
        "schemaVersion": 1,
        "kind": "ci-owner-agent-resume-success",
        "workflow": "jenkins",
        "noticeFile": notice.name,
        "noticeSha256": _runtime.sha256_file(notice),
        "returnCode": 0,
        "repo": "r",
        "job": "j",
        "buildNumber": 13,
        "completedAt": "2026-07-16T00:00:00+00:00",
    }
    payload.update(updates)
    return payload


def test_posix_killpg_permission_error_is_warning(monkeypatch):
    monkeypatch.setattr(_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_runtime.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(_runtime.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(_runtime.os, "killpg", lambda *a: (_ for _ in ()).throw(PermissionError("denied")), raising=False)
    termination = invoke(monkeypatch, FakeProcess())
    assert termination.reaped is True
    assert "tree termination failed" in termination.warning


def test_windows_taskkill_nonzero_is_warning(monkeypatch):
    monkeypatch.setattr(_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(_runtime.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "Access is denied"))
    termination = invoke(monkeypatch, FakeProcess())
    assert termination.reaped is True
    assert "taskkill failed (1)" in termination.warning


def test_windows_taskkill_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(_runtime.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("taskkill", k["timeout"])))
    termination = invoke(monkeypatch, FakeProcess())
    assert termination.reaped is True
    assert "taskkill failed: TimeoutExpired" in termination.warning


def test_final_reap_timeout_returns_unreaped(monkeypatch):
    monkeypatch.setattr(_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_runtime.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(_runtime.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(_runtime.os, "killpg", lambda *a: None, raising=False)
    termination = invoke(monkeypatch, FakeProcess(always_timeout=True))
    assert termination.reaped is False
    assert "final reap timed out" in termination.warning


@pytest.mark.parametrize("grace_error", [OSError("pipe closed"), ValueError("bad pipe")])
def test_grace_reap_regular_error_preserves_timeout(monkeypatch, grace_error):
    monkeypatch.setattr(_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(_runtime.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
    process = SequenceProcess([subprocess.TimeoutExpired("fake", 1), grace_error, ("done", "")])
    termination = invoke(monkeypatch, process)
    assert termination.reaped is True and "grace reap failed" in termination.warning
    assert process.killed is True


def test_grace_and_direct_kill_and_final_reap_failures_are_warnings(monkeypatch):
    monkeypatch.setattr(_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(_runtime.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "denied"))
    process = SequenceProcess([subprocess.TimeoutExpired("fake", 1), OSError("pipe"), subprocess.TimeoutExpired("fake", 1)],
                              kill_error=PermissionError("kill denied"))
    termination = invoke(monkeypatch, process)
    assert termination.reaped is False
    assert all(part in termination.warning for part in ("grace reap failed", "direct kill failed", "final reap timed out"))


def test_final_reap_regular_error_preserves_timeout(monkeypatch):
    monkeypatch.setattr(_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setattr(_runtime.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
    process = SequenceProcess([subprocess.TimeoutExpired("fake", 1), subprocess.TimeoutExpired("fake", 1), OSError("closed")])
    termination = invoke(monkeypatch, process)
    assert termination.reaped is False and "final reap failed: OSError" in termination.warning


def test_mocked_windows_does_not_require_native_constant(monkeypatch):
    monkeypatch.setattr(_runtime.platform, "system", lambda: "Windows")
    monkeypatch.delattr(_runtime.subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)
    monkeypatch.setattr(_runtime.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
    termination = invoke(monkeypatch, FakeProcess())
    assert termination.reaped is True


def test_success_marker_is_atomic_and_bound_to_notice(tmp_path):
    notice = tmp_path / "notice.json"
    notice.write_text('{"value":1}', encoding="utf-8")
    marker = _runtime.success_marker_path(notice)
    payload = success_marker_payload(notice)
    _runtime.write_success_marker_atomic(marker, payload)
    assert json.loads(marker.read_text(encoding="utf-8"))["noticeSha256"] == payload["noticeSha256"]
    expected = {"workflow": "jenkins", "repo": "r", "job": "j", "buildNumber": 13}
    assert _runtime.validate_success_marker(marker, notice, expected) == (True, None)
    notice.write_text('{"value":2}', encoding="utf-8")
    valid, reason = _runtime.validate_success_marker(marker, notice, expected)
    assert valid is False and reason == "success marker notice digest mismatch"
    assert not list(tmp_path.glob(".*.tmp"))


def test_success_marker_replace_failure_cleans_temp(tmp_path, monkeypatch):
    notice, marker = tmp_path / "notice.json", tmp_path / "notice.json.success.json"
    notice.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(_runtime.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(OSError):
        _runtime.write_success_marker_atomic(marker, {"x": 1})
    assert not marker.exists() and not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (lambda marker, payload: marker.write_text("{invalid", encoding="utf-8"), "invalid success marker JSON: JSONDecodeError"),
        (lambda marker, payload: marker.write_text("[]", encoding="utf-8"), "invalid success marker schema: ValidationError"),
        (lambda marker, payload: marker.write_text(json.dumps({**payload, "schemaVersion": 2}), encoding="utf-8"), "success marker metadata mismatch: schemaVersion"),
        (lambda marker, payload: marker.write_text(json.dumps({**payload, "kind": "wrong"}), encoding="utf-8"), "success marker metadata mismatch: kind"),
        (lambda marker, payload: marker.write_text(json.dumps({**payload, "workflow": "wrong"}), encoding="utf-8"), "invalid success marker schema: ValidationError"),
        (lambda marker, payload: marker.write_text(json.dumps({**payload, "returnCode": 3}), encoding="utf-8"), "success marker metadata mismatch: returnCode"),
        (lambda marker, payload: marker.write_text(json.dumps({**payload, "noticeFile": "wrong.json"}), encoding="utf-8"), "success marker metadata mismatch: noticeFile"),
        (lambda marker, payload: marker.write_text(json.dumps({**payload, "noticeSha256": "0" * 64}), encoding="utf-8"), "success marker notice digest mismatch"),
    ],
)
def test_success_marker_invalid_matrix_is_fail_closed(tmp_path, mutation, expected_reason):
    notice = tmp_path / "notice.json"
    marker = _runtime.success_marker_path(notice)
    notice.write_text("{}", encoding="utf-8")
    payload = success_marker_payload(notice)
    mutation(marker, payload)
    valid, reason = _runtime.validate_success_marker(
        marker, notice, {"workflow": "jenkins", "repo": "r", "job": "j", "buildNumber": 13}
    )
    assert valid is False and reason is not None and expected_reason in reason


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("schemaVersion", True), ("schemaVersion", 1.0), ("schemaVersion", "1"),
        ("returnCode", False), ("returnCode", 0.0), ("returnCode", "0"),
        ("buildNumber", True), ("buildNumber", 13.0), ("buildNumber", "13"),
        ("repo", 123), ("job", []), ("noticeFile", None),
        ("noticeSha256", True), ("noticeSha256", "abc"),
        ("completedAt", True), ("completedAt", 123), ("completedAt", None),
    ],
)
def test_success_marker_rejects_non_strict_json_types(tmp_path, field, bad_value):
    notice = tmp_path / "notice.json"
    marker = _runtime.success_marker_path(notice)
    notice.write_text("{}", encoding="utf-8")
    marker.write_text(json.dumps(success_marker_payload(notice, **{field: bad_value})), encoding="utf-8")
    valid, reason = _runtime.validate_success_marker(
        marker, notice, {"workflow": "jenkins", "repo": "r", "job": "j", "buildNumber": 13}
    )
    assert valid is False and reason is not None and "invalid success marker schema: ValidationError" in reason


def test_success_marker_rejects_missing_completed_at_and_extra_fields(tmp_path):
    notice = tmp_path / "notice.json"
    marker = _runtime.success_marker_path(notice)
    notice.write_text("{}", encoding="utf-8")
    expected = {"workflow": "jenkins", "repo": "r", "job": "j", "buildNumber": 13}
    for payload in (
        {key: value for key, value in success_marker_payload(notice).items() if key != "completedAt"},
        success_marker_payload(notice, unexpectedField="value"),
    ):
        marker.write_text(json.dumps(payload), encoding="utf-8")
        valid, reason = _runtime.validate_success_marker(marker, notice, expected)
        assert valid is False and reason is not None and "invalid success marker schema: ValidationError" in reason


def test_success_marker_notice_hash_errors_are_fail_closed(tmp_path, monkeypatch):
    notice = tmp_path / "notice.json"
    marker = _runtime.success_marker_path(notice)
    notice.write_text("{}", encoding="utf-8")
    marker.write_text(json.dumps(success_marker_payload(notice)), encoding="utf-8")
    monkeypatch.setattr(_runtime, "sha256_file", lambda path: (_ for _ in ()).throw(PermissionError("locked")))
    assert _runtime.validate_success_marker(marker, notice, {"workflow": "jenkins", "repo": "r", "job": "j", "buildNumber": 13}) == (
        False, "failed to hash notice for success marker: PermissionError: locked"
    )


def test_success_marker_notice_disappearing_before_hash_is_fail_closed(tmp_path, monkeypatch):
    notice = tmp_path / "notice.json"
    marker = _runtime.success_marker_path(notice)
    notice.write_text("{}", encoding="utf-8")
    marker.write_text(json.dumps(success_marker_payload(notice)), encoding="utf-8")

    def disappear(path):
        path.unlink()
        return _runtime.hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(_runtime, "sha256_file", disappear)
    valid, reason = _runtime.validate_success_marker(
        marker, notice, {"workflow": "jenkins", "repo": "r", "job": "j", "buildNumber": 13}
    )
    assert valid is False and reason is not None
    assert "failed to hash notice for success marker: FileNotFoundError" in reason


def test_success_marker_exists_errors_are_fail_closed(tmp_path):
    class UnreadablePath:
        name = "artifact.json"

        def exists(self):
            raise PermissionError("locked")

    marker = tmp_path / "marker.json"
    notice = tmp_path / "notice.json"
    notice.write_text("{}", encoding="utf-8")
    assert _runtime.validate_success_marker(marker, UnreadablePath(), {}) == (
        False, "failed to inspect notice for success marker: PermissionError: locked"
    )
    assert _runtime.validate_success_marker(UnreadablePath(), notice, {}) == (
        False, "failed to inspect success marker: PermissionError: locked"
    )


def test_success_marker_files_disappearing_during_validation_are_fail_closed(tmp_path, monkeypatch):
    notice = tmp_path / "notice.json"
    marker = _runtime.success_marker_path(notice)
    notice.write_text("{}", encoding="utf-8")
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        _runtime, "load_success_marker", lambda path: (_ for _ in ()).throw(FileNotFoundError("gone"))
    )
    valid, reason = _runtime.validate_success_marker(marker, notice, {})
    assert valid is False and reason == "failed to read success marker: FileNotFoundError: gone"
