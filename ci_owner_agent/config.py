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
    model_timeout_seconds: int
    model_max_retries: int
    response_format: str
    agent_recursion_limit: int
    ts_analyzer_dir: Path
    langsmith_tracing: bool
    langsmith_api_key: str | None
    langsmith_project: str
    langsmith_endpoint: str
    history_enabled: bool
    history_mongo_uri: str
    history_mongo_db: str
    history_max_candidates: int


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


def _int_env_or_default(name: str, default: int) -> int:
    try:
        return _int_env(name, default)
    except ValueError:
        return default


def _response_format_env() -> str:
    value = os.getenv("CI_AGENT_RESPONSE_FORMAT", "tool").lower()
    return value if value in {"tool", "json_text"} else "tool"


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def load_settings(env_file: str | Path | None = None) -> Settings:
    if load_dotenv is not None:
        load_dotenv(dotenv_path=env_file, override=False)
    repo_cache_dir = Path(os.getenv("CI_AGENT_REPO_CACHE_DIR", "repos")).expanduser()
    ts_analyzer_dir = Path(os.getenv("TS_ANALYZER_DIR", "./ts-analyzer")).expanduser()
    max_tool_steps = _int_env("CI_AGENT_MAX_TOOL_STEPS", 12)
    return Settings(
        jenkins_url=os.getenv("JENKINS_URL") or None,
        jenkins_user=os.getenv("JENKINS_USER") or None,
        jenkins_token=os.getenv("JENKINS_TOKEN") or None,
        repo_cache_dir=repo_cache_dir,
        default_log_tail_lines=_int_env("CI_AGENT_DEFAULT_LOG_TAIL_LINES", 500),
        max_tool_steps=max_tool_steps,
        max_tool_output_chars=_int_env("CI_AGENT_MAX_TOOL_OUTPUT_CHARS", 20000),
        model_provider=os.getenv("CI_AGENT_MODEL_PROVIDER", "fake"),
        model_base_url=os.getenv("CI_AGENT_MODEL_BASE_URL") or None,
        model_name=os.getenv("CI_AGENT_MODEL_NAME") or None,
        api_key=os.getenv("CI_AGENT_API_KEY") or None,
        model_timeout_seconds=_int_env_or_default("CI_AGENT_MODEL_TIMEOUT_SECONDS", 90),
        model_max_retries=_int_env_or_default("CI_AGENT_MODEL_MAX_RETRIES", 1),
        response_format=_response_format_env(),
        agent_recursion_limit=_int_env_or_default("CI_AGENT_RECURSION_LIMIT", max(max_tool_steps * 4, 40)),
        ts_analyzer_dir=ts_analyzer_dir,
        langsmith_tracing=os.getenv("LANGSMITH_TRACING", "false").lower() in {"1", "true", "yes", "on"},
        langsmith_api_key=os.getenv("LANGSMITH_API_KEY") or None,
        langsmith_project=os.getenv("LANGSMITH_PROJECT", "ci-owner-agent-dev"),
        langsmith_endpoint=os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
        history_enabled=_bool_env("CI_AGENT_HISTORY_ENABLED", False),
        history_mongo_uri=os.getenv("CI_AGENT_HISTORY_MONGO_URI", "mongodb://localhost:27017"),
        history_mongo_db=os.getenv("CI_AGENT_HISTORY_MONGO_DB", "ci_owner_agent"),
        history_max_candidates=_int_env_or_default("CI_AGENT_HISTORY_MAX_CANDIDATES", 5),
    )


def validate_model_settings(settings: Settings) -> str | None:
    provider = settings.model_provider.lower()
    if provider == "fake":
        return None
    if provider not in {"openai", "deepseek", "doubao", "openai-compatible"}:
        return f"unsupported CI_AGENT_MODEL_PROVIDER: {settings.model_provider}"
    if not settings.api_key:
        return "CI_AGENT_API_KEY is required when CI_AGENT_MODEL_PROVIDER is not fake"
    if not settings.model_name:
        return "CI_AGENT_MODEL_NAME is required when CI_AGENT_MODEL_PROVIDER is not fake"
    if provider in {"deepseek", "doubao", "openai-compatible"} and not settings.model_base_url:
        return "CI_AGENT_MODEL_BASE_URL is required for deepseek/doubao/openai-compatible providers"
    return None


def public_settings(settings: Settings) -> dict[str, object]:
    data = settings.__dict__.copy()
    data["jenkins_token"] = "***" if settings.jenkins_token else None
    data["api_key"] = "***" if settings.api_key else None
    data["langsmith_api_key"] = "***" if settings.langsmith_api_key else None
    data["repo_cache_dir"] = str(settings.repo_cache_dir)
    data["ts_analyzer_dir"] = str(settings.ts_analyzer_dir)
    return data
