from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

from ci_owner_agent.config import load_settings
from ci_owner_agent.services.command_runner import (
    run_command,
    validate_commit_ref,
    validate_git_path,
    validate_repo_name,
)
from ci_owner_agent.services.git_client import GitClient

DEPENDENCY_FILES = ["package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"]


def _settings_paths(repo_cache_dir: str | Path | None = None, analyzer_dir: str | Path | None = None) -> tuple[Path, Path]:
    settings = load_settings()
    return (
        (Path(repo_cache_dir).expanduser() if repo_cache_dir is not None else settings.repo_cache_dir).resolve(),
        (Path(analyzer_dir).expanduser() if analyzer_dir is not None else settings.ts_analyzer_dir).resolve(),
    )


def _repo_path(repo: str, repo_cache_dir: Path) -> tuple[Path | None, str | None]:
    repo_error = validate_repo_name(repo)
    if repo_error:
        return None, repo_error
    client = GitClient(repo_cache_dir)
    path, bare, error = client.resolve_repo(repo)
    if error or path is None:
        return None, error
    if bare:
        return None, "TypeScript analyzer requires a normal working-tree repo, not a bare mirror"
    return path, None


def _checkout_for_program(
    repo: str,
    commit: str,
    repo_cache_dir: Path,
    allow_checkout: bool,
    force_checkout: bool,
    empty_key: str,
) -> dict | None:
    if not allow_checkout:
        return None
    client = GitClient(repo_cache_dir)
    checkout = client.checkout_commit_for_analysis(repo, commit, force=force_checkout)
    if not checkout.get("ok"):
        return {
            "ok": False,
            "error": f"failed to checkout commit for TypeScript analysis: {checkout.get('error')}",
            "checkout": checkout,
            empty_key: [],
        }
    return None


def _validate_paths(paths: list[str]) -> str | None:
    for path in paths:
        error = validate_git_path(path)
        if error:
            return error
    return None


def _call_node(
    script_name: str,
    payload: dict,
    empty_key: str,
    analyzer_dir: Path,
    max_output_chars: int = 20000,
) -> dict:
    analyzer_dir = analyzer_dir.expanduser().resolve()
    script_path = (analyzer_dir / "src" / script_name).resolve()
    if not script_path.exists():
        return {
            "ok": False,
            "error": (
                f"TypeScript analyzer script not found: {script_path}. "
                "Set TS_ANALYZER_DIR to the directory containing src/find_definitions.js, "
                "for example E:/workspace/lanchain/ci-owner-agent/ts-analyzer"
            ),
            empty_key: [],
        }
    result = run_command(
        ["node", str(script_path), json.dumps(payload, ensure_ascii=False)],
        cwd=analyzer_dir,
        timeout=60,
        max_output_chars=max_output_chars,
    )
    if not result.ok:
        return {
            "ok": False,
            "error": result.error or "node analyzer command failed",
            "command": result.to_dict(),
            empty_key: [],
        }
    try:
        parsed = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"node analyzer returned invalid JSON: {exc}",
            "stdout": result.stdout,
            "stderr": result.stderr,
            empty_key: [],
        }
    if empty_key not in parsed:
        parsed[empty_key] = []
    if "ok" not in parsed:
        parsed["ok"] = False
        parsed["error"] = "node analyzer response missing ok field"
    return parsed


def _hash_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_hashes(repo_path: Path) -> dict[str, str | None]:
    return {name: _hash_file(repo_path / name) for name in DEPENDENCY_FILES}


def _strip_json_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(^|\s)//.*?$", r"\1", text, flags=re.M)
    return text


def _read_tsconfig_extends(tsconfig_path: Path) -> tuple[str | None, str | None]:
    try:
        raw = tsconfig_path.read_text(encoding="utf-8")
        data = json.loads(_strip_json_comments(raw))
    except Exception as exc:
        return None, f"failed to parse {tsconfig_path}: {exc}"
    value = data.get("extends")
    return (str(value), None) if value else (None, None)


def _npm_package_root(extends_value: str) -> str:
    normalized = extends_value.replace("\\", "/")
    if normalized.startswith("@"):
        parts = normalized.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else normalized
    return normalized.split("/", 1)[0]


def _package_manager(repo_path: Path) -> list[str]:
    if (repo_path / "package-lock.json").exists():
        return ["npm", "install"]
    if (repo_path / "pnpm-lock.yaml").exists():
        return ["pnpm", "install"]
    if (repo_path / "yarn.lock").exists():
        return ["yarn", "install"]
    return ["npm", "install"]


def _marker_path(repo_path: Path) -> Path:
    return repo_path / ".ci-owner-agent" / "deps.json"


def _load_marker(repo_path: Path) -> dict | None:
    path = _marker_path(repo_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_marker(repo_path: Path, hashes: dict[str, str | None]) -> None:
    path = _marker_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dependencyFileHashes": hashes}, ensure_ascii=False, indent=2), encoding="utf-8")


def check_node_dependencies_for_analysis(
    repo: str,
    tsconfig: str = "tsconfig.json",
    repo_cache_dir: str | Path | None = None,
    install: bool = False,
    max_output_chars: int = 20000,
) -> dict:
    repo_cache, _ts_dir = _settings_paths(repo_cache_dir, None)
    repo_path, repo_error = _repo_path(repo, repo_cache)
    if repo_error or repo_path is None:
        return {"ok": False, "error": repo_error, "warnings": []}
    path_error = _validate_paths([tsconfig])
    if path_error:
        return {"ok": False, "error": path_error, "warnings": []}

    warnings: list[str] = []
    tsconfig_path = repo_path / tsconfig
    if not tsconfig_path.exists():
        return {"ok": False, "error": f"tsconfig not found: {tsconfig_path}", "warnings": warnings}

    node_modules = repo_path / "node_modules"
    missing: list[str] = []
    if not node_modules.exists():
        missing.append("node_modules")

    extends_value, extends_error = _read_tsconfig_extends(tsconfig_path)
    if extends_error:
        return {"ok": False, "error": extends_error, "warnings": warnings}
    if extends_value:
        if extends_value.startswith(".") or extends_value.startswith("/") or re.match(r"^[A-Za-z]:", extends_value):
            extended_path = (tsconfig_path.parent / extends_value).resolve()
            if not extended_path.exists():
                missing.append(str(extended_path))
        else:
            package_root = _npm_package_root(extends_value)
            package_path = node_modules / package_root
            if not package_path.exists():
                missing.append(f"node_modules/{package_root}")

    if missing and not install:
        return {
            "ok": False,
            "error": "Node dependencies are missing for TypeScript analysis; run npm install in the agent-owned repo",
            "missing": missing,
            "suggestion": f"cd {repo_path} && npm install",
            "warnings": warnings,
        }

    install_result = None
    if missing and install:
        command = _package_manager(repo_path)
        result = run_command(command, cwd=repo_path, timeout=300, max_output_chars=max_output_chars)
        install_result = result.to_dict()
        if not result.ok:
            return {
                "ok": False,
                "error": f"dependency install failed using {' '.join(command)}",
                "missing": missing,
                "install": install_result,
                "warnings": warnings,
            }

    hashes = _dependency_hashes(repo_path)
    marker = _load_marker(repo_path)
    if marker is None:
        warnings.append("dependency marker created; run npm install manually if node_modules is incomplete")
        dependencies_current = True
    else:
        old_hashes = marker.get("dependencyFileHashes") or {}
        dependencies_current = old_hashes == hashes
        if not dependencies_current:
            warnings.append("dependency definition files changed; run npm install in the agent-owned repo if analysis fails")
    _write_marker(repo_path, hashes)

    return {
        "ok": True,
        "repoPath": str(repo_path),
        "tsconfig": str(tsconfig_path),
        "nodeModules": str(node_modules),
        "extends": extends_value,
        "dependencyFileHashes": hashes,
        "dependenciesCurrent": dependencies_current,
        "install": install_result,
        "warnings": warnings,
    }


def ts_analyze_changed_functions(
    repo: str | None = None,
    baseCommit: str | None = None,
    headCommit: str | None = None,
    files: list[str] | None = None,
    tsconfig: str = "tsconfig.json",
    repo_cache_dir: str | Path | None = None,
    analyzer_dir: str | Path | None = None,
    max_output_chars: int = 20000,
) -> dict:
    if not repo or not baseCommit or not headCommit:
        return {"ok": False, "error": "repo, baseCommit and headCommit are required", "changedFunctions": []}
    commit_error = validate_commit_ref(baseCommit) or validate_commit_ref(headCommit)
    if commit_error:
        return {"ok": False, "error": commit_error, "changedFunctions": []}
    ts_files = [path for path in (files or []) if path.endswith((".ts", ".tsx"))]
    path_error = _validate_paths(ts_files + [tsconfig])
    if path_error:
        return {"ok": False, "error": path_error, "changedFunctions": []}
    if not ts_files:
        return {"ok": True, "changedFunctions": []}
    repo_cache, ts_dir = _settings_paths(repo_cache_dir, analyzer_dir)
    repo_path, repo_error = _repo_path(repo, repo_cache)
    if repo_error or repo_path is None:
        return {"ok": False, "error": repo_error, "changedFunctions": []}
    return _call_node(
        "analyze_changed_functions.js",
        {
            "repoPath": str(repo_path),
            "baseCommit": baseCommit,
            "headCommit": headCommit,
            "files": ts_files,
            "tsconfig": tsconfig,
        },
        "changedFunctions",
        ts_dir,
        max_output_chars=max_output_chars,
    )


def ts_find_definitions(
    repo: str | None = None,
    commit: str | None = None,
    symbols: list[str] | None = None,
    tsconfig: str = "tsconfig.json",
    repo_cache_dir: str | Path | None = None,
    analyzer_dir: str | Path | None = None,
    allow_checkout: bool = True,
    force_checkout: bool = False,
    max_output_chars: int = 20000,
) -> dict:
    if not repo or not commit:
        return {"ok": False, "error": "repo and commit are required", "definitions": []}
    commit_error = validate_commit_ref(commit)
    if commit_error:
        return {"ok": False, "error": commit_error, "definitions": []}
    path_error = _validate_paths([tsconfig])
    if path_error:
        return {"ok": False, "error": path_error, "definitions": []}
    if not symbols:
        return {"ok": True, "definitions": []}
    repo_cache, ts_dir = _settings_paths(repo_cache_dir, analyzer_dir)
    repo_path, repo_error = _repo_path(repo, repo_cache)
    if repo_error or repo_path is None:
        return {"ok": False, "error": repo_error, "definitions": []}
    checkout_error = _checkout_for_program(repo, commit, repo_cache, allow_checkout, force_checkout, "definitions")
    if checkout_error is not None:
        return checkout_error
    return _call_node(
        "find_definitions.js",
        {
            "repoPath": str(repo_path),
            "commit": commit,
            "symbols": symbols,
            "tsconfig": tsconfig,
        },
        "definitions",
        ts_dir,
        max_output_chars=max_output_chars,
    )


def ts_find_callers(
    repo: str | None = None,
    commit: str | None = None,
    symbol: str | None = None,
    definitionFile: str | None = None,
    tsconfig: str = "tsconfig.json",
    maxResults: int = 50,
    repo_cache_dir: str | Path | None = None,
    analyzer_dir: str | Path | None = None,
    allow_checkout: bool = True,
    force_checkout: bool = False,
    max_output_chars: int = 20000,
) -> dict:
    if not repo or not commit or not symbol or not definitionFile:
        return {"ok": False, "error": "repo, commit, symbol and definitionFile are required", "callers": []}
    commit_error = validate_commit_ref(commit)
    if commit_error:
        return {"ok": False, "error": commit_error, "callers": []}
    path_error = _validate_paths([definitionFile, tsconfig])
    if path_error:
        return {"ok": False, "error": path_error, "callers": []}
    repo_cache, ts_dir = _settings_paths(repo_cache_dir, analyzer_dir)
    repo_path, repo_error = _repo_path(repo, repo_cache)
    if repo_error or repo_path is None:
        return {"ok": False, "error": repo_error, "callers": []}
    checkout_error = _checkout_for_program(repo, commit, repo_cache, allow_checkout, force_checkout, "callers")
    if checkout_error is not None:
        return checkout_error
    return _call_node(
        "find_callers.js",
        {
            "repoPath": str(repo_path),
            "commit": commit,
            "symbol": symbol,
            "definitionFile": definitionFile,
            "tsconfig": tsconfig,
            "maxResults": maxResults,
        },
        "callers",
        ts_dir,
        max_output_chars=max_output_chars,
    )
