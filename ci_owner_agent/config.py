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
    jenkins_successful_build_scan_limit: int
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
    history_inherit_no_owner_enabled: bool
    failure_chunk_tail_lines: int
    ai_failure_facts_enabled: bool
    ai_failure_fact_min_confidence: float
    ai_failure_fact_max_log_chars: int
    ai_history_compare_enabled: bool
    ai_history_compare_threshold: float
    ai_history_max_fact_candidates: int
    ai_history_max_compare_calls: int
    wecom_notify_enabled: bool
    wecom_notify_dry_run: bool
    wecom_notify_on_success: bool
    wecom_notify_on_no_owner: bool
    wecom_user_mapping_file: Path | None
    test_maintainer_mapping_file: Path | None
    wecom_mention_mode: str
    wecom_fallback_userids: tuple[str, ...]
    wecom_bot_enabled: bool
    wecom_bot_id: str | None
    wecom_bot_secret: str | None
    wecom_bot_discover_chat_id: bool
    wecom_bot_notify_chat_id: str | None
    wecom_bot_notify_poll_seconds: int
    wecom_bot_notify_lease_seconds: int
    wecom_bot_notify_max_attempts: int
    wecom_bot_confirm_ttl_seconds: int
    wecom_feedback_code_ttl_days: int
    wecom_bot_event_ttl_days: int
    wecom_bot_llm_enabled: bool
    wecom_bot_llm_max_input_chars: int
    feedback_base_url: str | None
    feedback_server_host: str
    feedback_server_port: int
    feedback_shared_token: str | None
    notification_dedup_enabled: bool
    metrics_enabled: bool
    metrics_file: Path
    weekly_test_report_config_file: Path


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


def _int_env_min_or_default(name: str, default: int, minimum: int) -> int:
    value = _int_env_or_default(name, default)
    return value if value >= minimum else default


def _float_env_or_default(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _parse_fallback_userids(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",")]
    seen: set[str] = set()
    result: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return tuple(result)


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
        jenkins_successful_build_scan_limit=_int_env("CI_AGENT_JENKINS_SUCCESSFUL_BUILD_SCAN_LIMIT", 100),
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
        history_inherit_no_owner_enabled=_bool_env("CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED", True),
        failure_chunk_tail_lines=_int_env_min_or_default("CI_AGENT_FAILURE_CHUNK_TAIL_LINES", 500, 50),
        ai_failure_facts_enabled=_bool_env("CI_AGENT_AI_FAILURE_FACTS_ENABLED", False),
        ai_failure_fact_min_confidence=max(0, min(_float_env_or_default("CI_AGENT_AI_FAILURE_FACT_MIN_CONFIDENCE", 0.70), 1)),
        ai_failure_fact_max_log_chars=_int_env_or_default("CI_AGENT_AI_FAILURE_FACT_MAX_LOG_CHARS", 12000),
        ai_history_compare_enabled=_bool_env("CI_AGENT_AI_HISTORY_COMPARE_ENABLED", False),
        ai_history_compare_threshold=max(0, min(_float_env_or_default("CI_AGENT_AI_HISTORY_COMPARE_THRESHOLD", 0.90), 1)),
        ai_history_max_fact_candidates=_int_env_or_default("CI_AGENT_AI_HISTORY_MAX_FACT_CANDIDATES", 20),
        ai_history_max_compare_calls=_int_env_or_default("CI_AGENT_AI_HISTORY_MAX_COMPARE_CALLS", 20),
        wecom_notify_enabled=_bool_env("CI_AGENT_WECOM_NOTIFY_ENABLED", False),
        wecom_notify_dry_run=_bool_env("CI_AGENT_WECOM_NOTIFY_DRY_RUN", True),
        wecom_notify_on_success=_bool_env("CI_AGENT_WECOM_NOTIFY_ON_SUCCESS", False),
        wecom_notify_on_no_owner=_bool_env("CI_AGENT_WECOM_NOTIFY_ON_NO_OWNER", True),
        wecom_user_mapping_file=Path(os.getenv("CI_AGENT_WECOM_USER_MAPPING_FILE")).expanduser() if os.getenv("CI_AGENT_WECOM_USER_MAPPING_FILE") else None,
        test_maintainer_mapping_file=Path(os.getenv("CI_AGENT_TEST_MAINTAINER_MAPPING_FILE")).expanduser()
        if os.getenv("CI_AGENT_TEST_MAINTAINER_MAPPING_FILE")
        else None,
        wecom_mention_mode=os.getenv("CI_AGENT_WECOM_MENTION_MODE", "userid").lower()
        if os.getenv("CI_AGENT_WECOM_MENTION_MODE", "userid").lower() in {"userid", "name"}
        else "userid",
        wecom_fallback_userids=_parse_fallback_userids(os.getenv("CI_AGENT_WECOM_FALLBACK_USERIDS")),
        wecom_bot_enabled=_bool_env("CI_AGENT_WECOM_BOT_ENABLED", False),
        wecom_bot_id=os.getenv("CI_AGENT_WECOM_BOT_ID") or None,
        wecom_bot_secret=os.getenv("CI_AGENT_WECOM_BOT_SECRET") or None,
        wecom_bot_discover_chat_id=_bool_env("CI_AGENT_WECOM_BOT_DISCOVER_CHAT_ID", False),
        wecom_bot_notify_chat_id=(os.getenv("CI_AGENT_WECOM_BOT_NOTIFY_CHAT_ID") or "").strip() or None,
        wecom_bot_notify_poll_seconds=_int_env("CI_AGENT_WECOM_BOT_NOTIFY_POLL_SECONDS", 2),
        wecom_bot_notify_lease_seconds=_int_env("CI_AGENT_WECOM_BOT_NOTIFY_LEASE_SECONDS", 30),
        wecom_bot_notify_max_attempts=_int_env("CI_AGENT_WECOM_BOT_NOTIFY_MAX_ATTEMPTS", 5),
        wecom_bot_confirm_ttl_seconds=_int_env("CI_AGENT_WECOM_BOT_CONFIRM_TTL_SECONDS", 300),
        wecom_feedback_code_ttl_days=_int_env("CI_AGENT_WECOM_FEEDBACK_CODE_TTL_DAYS", 30),
        wecom_bot_event_ttl_days=_int_env("CI_AGENT_WECOM_BOT_EVENT_TTL_DAYS", 7),
        wecom_bot_llm_enabled=_bool_env("CI_AGENT_WECOM_BOT_LLM_ENABLED", False),
        wecom_bot_llm_max_input_chars=_int_env("CI_AGENT_WECOM_BOT_LLM_MAX_INPUT_CHARS", 2000),
        feedback_base_url=os.getenv("CI_AGENT_FEEDBACK_BASE_URL") or None,
        feedback_server_host=os.getenv("CI_AGENT_FEEDBACK_SERVER_HOST", "127.0.0.1"),
        feedback_server_port=_int_env_or_default("CI_AGENT_FEEDBACK_SERVER_PORT", 8765),
        feedback_shared_token=os.getenv("CI_AGENT_FEEDBACK_SHARED_TOKEN") or None,
        notification_dedup_enabled=_bool_env("CI_AGENT_NOTIFICATION_DEDUP_ENABLED", True),
        metrics_enabled=_bool_env("CI_AGENT_METRICS_ENABLED", False),
        metrics_file=Path(os.getenv("CI_AGENT_METRICS_FILE", "./runs/metrics/ci_analysis_metrics.jsonl")).expanduser(),
        weekly_test_report_config_file=Path(
            os.getenv("CI_AGENT_WEEKLY_TEST_REPORT_CONFIG_FILE", "config/weekly-test-report.yml")
        ).expanduser(),
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
    data["feedback_shared_token"] = "***" if settings.feedback_shared_token else None
    data["wecom_bot_secret"] = "***" if settings.wecom_bot_secret else None
    data["wecom_bot_notify_chat_id"] = "***" if settings.wecom_bot_notify_chat_id else None
    data["wecom_bot_llm_max_input_chars"] = settings.wecom_bot_llm_max_input_chars
    data["repo_cache_dir"] = str(settings.repo_cache_dir)
    data["ts_analyzer_dir"] = str(settings.ts_analyzer_dir)
    data["wecom_user_mapping_file"] = str(settings.wecom_user_mapping_file) if settings.wecom_user_mapping_file else None
    data["test_maintainer_mapping_file"] = str(settings.test_maintainer_mapping_file) if settings.test_maintainer_mapping_file else None
    data["wecom_fallback_userids"] = list(settings.wecom_fallback_userids)
    data["weekly_test_report_config_file"] = str(settings.weekly_test_report_config_file)
    return data
