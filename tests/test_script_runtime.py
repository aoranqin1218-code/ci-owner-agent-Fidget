from __future__ import annotations

import subprocess

import pytest

from scripts import _runtime


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
