from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def repo_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "repos"
    cache.mkdir()
    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(cache))
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    return cache


@pytest.fixture()
def sample_repo(repo_cache: Path) -> dict[str, str]:
    repo = repo_cache / "sample-ts-repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Zhang San")
    git(repo, "config", "user.email", "zhangsan@example.com")

    target = repo / "packages" / "fxp-ai" / "errors"
    target.mkdir(parents=True)
    classify = target / "classify.ts"
    classify.write_text(
        "export function classifyError(size: number) {\n"
        "  return size > 5 ? 'FILE_SIZE_EXCEEDED' : 'OK';\n"
        "}\n",
        encoding="utf-8",
    )
    readme = repo / "README.md"
    readme.write_text("# sample\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")

    classify.write_text(
        "export function classifyError(size: number) {\n"
        "  const limit = 10;\n"
        "  return size > limit ? 'FILE_SIZE_EXCEEDED' : 'OK';\n"
        "}\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change classify limit")
    head = git(repo, "rev-parse", "HEAD")
    return {"repo": "sample-ts-repo", "path": str(repo), "base": base, "head": head}


@pytest.fixture()
def readme_only_repo(repo_cache: Path) -> dict[str, str]:
    repo = repo_cache / "readme-only-repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Li Si")
    git(repo, "config", "user.email", "lisi@example.com")
    (repo / "packages" / "fxp-ai" / "errors").mkdir(parents=True)
    (repo / "packages" / "fxp-ai" / "errors" / "classify.ts").write_text(
        "export function classifyError() { return 'OK'; }\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("# sample\n\nDocs only.\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "docs")
    head = git(repo, "rev-parse", "HEAD")
    return {"repo": "readme-only-repo", "path": str(repo), "base": base, "head": head}


@pytest.fixture()
def logs(tmp_path: Path) -> dict[str, Path]:
    files = {
        "auth_failed": (
            "Running tests\n"
            "AssertionError: FILE_SIZE_EXCEEDED expected limit 10MB\n"
            "at packages/fxp-ai/errors/classify.ts:2:10\n"
            "Finished: FAILURE\n"
        ),
        "unknown_failed": (
            "Running tests\n"
            "AssertionError: unrelated behavior failed\n"
            "at tests/unrelated.test.ts:9:1\n"
            "Finished: FAILURE\n"
        ),
        "success": (
            "Running tests\n"
            "temporary ERROR appeared but recovered\n"
            "some test name says FAILED historically\n"
            "Finished: SUCCESS\n"
        ),
        "aborted": "Running pipeline\nMissing context hudson.FilePath\nFinished: ABORTED\n",
    }
    result = {}
    for name, content in files.items():
        path = tmp_path / f"{name}.log"
        path.write_text(content, encoding="utf-8")
        result[name] = path
    return result
