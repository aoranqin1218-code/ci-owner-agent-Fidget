from __future__ import annotations

import pytest

from ci_owner_agent.services.repository_path import normalize_repository_path


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/build-123/server/workflow/service.ts",
        "/var/tmp/task/modules/workflow/a.ts",
        "/bin/server/workflow/service.ts",
        "/usr/bin/packages/core/index.ts",
        r"C:\Users\me\AppData\Local\Temp\packages\core\index.ts",
        r"C:\Windows\Temp\server\workflow\service.ts",
        r"c:\users\ME\appdata\local\temp\server\workflow\service.ts",
    ],
)
def test_rejects_unsafe_absolute_paths(path):
    assert normalize_repository_path(path) is None


@pytest.mark.parametrize(
    "path",
    [
        r"C:\agent\_work\fx-code\index.ts",
        r"C:\actions-runner\_work\fx-code\fx-code\index.ts",
        "/home/jenkins/workspace/fx-code/index.ts",
        "/workspace/fx-code/index.ts",
        "/workspaces/fx-code/index.ts",
        "/var/app/index.ts",
        "./index.ts",
        "index.ts",
    ],
)
def test_normalizes_root_level_workspace_file(path):
    assert normalize_repository_path(path) == "index.ts"


@pytest.mark.parametrize(
    "path",
    [
        r"C:\agent\_work\fx-code\server\workflow\service.ts",
        r"C:\actions-runner\_work\fx-code\fx-code\server\workflow\service.ts",
        "/home/jenkins/workspace/fx-code/server/workflow/service.ts",
        "/workspace/fx-code/server/workflow/service.ts",
        "/var/app/server/workflow/service.ts",
        "./server/workflow/service.ts",
    ],
)
def test_normalizes_nested_workspace_file(path):
    assert normalize_repository_path(path) == "server/workflow/service.ts"


@pytest.mark.parametrize(
    "path",
    [
        "/home/app/workspace/fx-code/server/a.ts",
        "/home/lib/workspace/fx-code/server/a.ts",
        "/home/server/workspace/fx-code/server/a.ts",
        "/home/test/workspace/fx-code/server/a.ts",
    ],
)
def test_workspace_marker_wins_over_parent_directory_name(path):
    assert normalize_repository_path(path) == "server/a.ts"


@pytest.mark.parametrize(
    "path",
    [
        "/opt/custom/location/fx-code/index.ts",
        "/data/random/project/server/a.ts",
        r"D:\custom\location\fx-code\index.ts",
    ],
)
def test_unknown_absolute_workspace_is_not_guessed(path):
    assert normalize_repository_path(path) is None
