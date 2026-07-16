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
