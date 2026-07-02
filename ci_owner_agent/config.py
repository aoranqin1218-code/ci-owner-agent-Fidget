from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared, fallback helps bootstrapping.
    load_dotenv = None


@dataclass(frozen=True)
class Settings:
    jenkins_url: str | None
    jenkins_user: str | None
    jenkins_token: str | None
    repo_cache_dir: Path
    default_log_tail_lines: int
    max_tool_steps: int
    max_tool_output_chars: int
    model_provider: str
    model_base_url: str | None
    model_name: str | None
    api_key: str | None
    ts_analyzer_dir: Path


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def load_settings(env_file: str | Path | None = None) -> Settings:
    if load_dotenv is not None:
        load_dotenv(dotenv_path=env_file, override=False)
    repo_cache_dir = Path(os.getenv("CI_AGENT_REPO_CACHE_DIR", "repos")).expanduser()
    ts_analyzer_dir = Path(os.getenv("TS_ANALYZER_DIR", "./ts-analyzer")).expanduser()
    return Settings(
        jenkins_url=os.getenv("JENKINS_URL") or None,
        jenkins_user=os.getenv("JENKINS_USER") or None,
        jenkins_token=os.getenv("JENKINS_TOKEN") or None,
        repo_cache_dir=repo_cache_dir,
        default_log_tail_lines=_int_env("CI_AGENT_DEFAULT_LOG_TAIL_LINES", 500),
        max_tool_steps=_int_env("CI_AGENT_MAX_TOOL_STEPS", 12),
        max_tool_output_chars=_int_env("CI_AGENT_MAX_TOOL_OUTPUT_CHARS", 20000),
        model_provider=os.getenv("CI_AGENT_MODEL_PROVIDER", "fake"),
        model_base_url=os.getenv("CI_AGENT_MODEL_BASE_URL") or None,
        model_name=os.getenv("CI_AGENT_MODEL_NAME") or None,
        api_key=os.getenv("CI_AGENT_API_KEY") or None,
        ts_analyzer_dir=ts_analyzer_dir,
    )


def public_settings(settings: Settings) -> dict[str, object]:
    data = settings.__dict__.copy()
    data["jenkins_token"] = "***" if settings.jenkins_token else None
    data["api_key"] = "***" if settings.api_key else None
    data["repo_cache_dir"] = str(settings.repo_cache_dir)
    data["ts_analyzer_dir"] = str(settings.ts_analyzer_dir)
    return data
