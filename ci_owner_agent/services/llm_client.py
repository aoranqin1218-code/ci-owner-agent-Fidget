from __future__ import annotations

from typing import Any

from ci_owner_agent.config import Settings


def build_chat_model(settings: Settings):
    try:
        from langchain_openai import ChatOpenAI
    except Exception as exc:
        raise RuntimeError(f"langchain-openai is not installed: {exc}") from exc
    kwargs: dict[str, Any] = {
        "model": settings.model_name,
        "api_key": settings.api_key,
        "temperature": 0,
        "timeout": settings.model_timeout_seconds,
        "max_retries": settings.model_max_retries,
    }
    if settings.model_base_url:
        kwargs["base_url"] = settings.model_base_url
    try:
        return ChatOpenAI(**kwargs)
    except TypeError:
        if "base_url" in kwargs:
            kwargs["openai_api_base"] = kwargs.pop("base_url")
        return ChatOpenAI(**kwargs)
