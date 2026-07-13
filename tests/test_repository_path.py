from __future__ import annotations

import pytest

from ci_owner_agent.services.repository_path import normalize_repository_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("server/a.ts", "server/a.ts"),
        ("./server/a.ts", "server/a.ts"),
        ("server/a.ts:12", "server/a.ts"),
        ("server/a.ts:12:3", "server/a.ts"),
        ("modules/workflow/a.ts", "modules/workflow/a.ts"),
        ("packages/core/src/index.ts", "packages/core/src/index.ts"),
        ("index.ts", "index.ts"),
        ("/var/app/server/a.ts", "server/a.ts"),
        ("/var/app/test/init.ts", "test/init.ts"),
        ("file:///var/app/server/a.ts", "server/a.ts"),
        ("/var/app/node_modules/@fx/file-sdk/src/http.ts", "node_modules/@fx/file-sdk/src/http.ts"),
        ("file:///var/app/node_modules/@fx/file-sdk/src/http.ts", "node_modules/@fx/file-sdk/src/http.ts"),
    ],
)
def test_normalizes_supported_log_paths(path, expected):
    assert normalize_repository_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/var/lib/jenkins/workspace/services/fx-code-unittest/server/a.ts",
        "/home/jenkins/workspace/fx-code/server/a.ts",
        "/home/runner/work/fx-code/fx-code/server/a.ts",
        "/workspace/fx-code/server/a.ts",
        "/workspaces/fx-code/server/a.ts",
        "/tmp/build-123/server/a.ts",
        "/var/tmp/task/server/a.ts",
        "/opt/custom/server/a.ts",
        "/bin/server/a.ts",
        "/usr/bin/server/a.ts",
        r"C:\agent\_work\fx-code\server\a.ts",
        r"D:\custom\location\server\a.ts",
        r"\\server\share\repo\server\a.ts",
        "file:///tmp/server/a.ts",
        "file:///home/user/repo/server/a.ts",
        "https://example.com/server/a.ts",
    ],
)
def test_rejects_unsupported_absolute_paths(path):
    assert normalize_repository_path(path) is None


@pytest.mark.parametrize(
    "path",
    [
        "../server/a.ts",
        "server/../a.ts",
        "./packages/../../server/a.ts",
        "/var/app/server/../a.ts",
        "file:///var/app/server/../a.ts",
    ],
)
def test_rejects_path_traversal(path):
    assert normalize_repository_path(path) is None
