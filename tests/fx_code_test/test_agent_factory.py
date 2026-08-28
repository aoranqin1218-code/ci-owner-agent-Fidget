from __future__ import annotations

from dataclasses import replace

import pytest

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.factory import AgentConfigurationError, create_responsibility_agent
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.agents.responsibility_agent import FakeResponsibilityAgent
from ci_owner_agent.config import load_settings
from ci_owner_agent.schemas import BuildInfo
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.log_provider import LocalFileLogProvider


def make_context(repo_cache, sample_repo, logs, provider="fake"):
    settings = replace(load_settings(), model_provider=provider, repo_cache_dir=repo_cache)
    provider_obj = LocalFileLogProvider(logs["auth_failed"])
    build_info = BuildInfo(
        job="job",
        buildNumber=1,
        result="FAILURE",
        buildUrl="local://job/1",
        branch="dev",
        commit=sample_repo["head"],
        logTail=provider_obj.read_tail(20),
    )
    return AgentRuntimeContext(
        repo=sample_repo["repo"],
        job="job",
        build_number=1,
        build_url="local://job/1",
        result="FAILURE",
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        build_info=build_info,
        commits=[],
        changed_files=[],
        log_provider=provider_obj,
        git_client=GitClient(repo_cache),
        settings=settings,
    )


def test_factory_fake_returns_fixed_no_owner_agent(repo_cache, sample_repo, logs):
    context = make_context(repo_cache, sample_repo, logs, provider="fake")
    agent = create_responsibility_agent(context.settings, context)
    assert isinstance(agent, FakeResponsibilityAgent)
    notice = agent.analyze(context)
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.hasHighConfidenceOwner is False


def test_factory_openai_missing_key_is_clear_error(repo_cache, sample_repo, logs):
    context = make_context(repo_cache, sample_repo, logs, provider="openai")
    context = replace(context, settings=replace(context.settings, api_key=None, model_name="test-model"))
    with pytest.raises(AgentConfigurationError) as exc:
        create_responsibility_agent(context.settings, context)
    assert "CI_AGENT_API_KEY" in str(exc.value)


def test_factory_real_provider_returns_langchain_agent(repo_cache, sample_repo, logs):
    context = make_context(repo_cache, sample_repo, logs, provider="doubao")
    settings = replace(
        context.settings,
        model_provider="doubao",
        api_key="test-key",
        model_name="test-model",
        model_base_url="https://example.test/v1",
    )
    context = replace(context, settings=settings)
    agent = create_responsibility_agent(settings, context)
    assert isinstance(agent, LangChainResponsibilityAgent)


def test_openai_compatible_requires_base_url(repo_cache, sample_repo, logs):
    context = make_context(repo_cache, sample_repo, logs, provider="openai-compatible")
    settings = replace(
        context.settings,
        model_provider="openai-compatible",
        api_key="test-key",
        model_name="test-model",
        model_base_url=None,
    )
    context = replace(context, settings=settings)
    with pytest.raises(AgentConfigurationError) as exc:
        create_responsibility_agent(settings, context)
    assert "CI_AGENT_MODEL_BASE_URL" in str(exc.value)


def test_openai_compatible_valid(repo_cache, sample_repo, logs):
    context = make_context(repo_cache, sample_repo, logs, provider="openai-compatible")
    settings = replace(
        context.settings,
        model_provider="openai-compatible",
        api_key="test-key",
        model_name="test-model",
        model_base_url="https://compatible.example/v1",
    )
    context = replace(context, settings=settings)
    agent = create_responsibility_agent(settings, context)
    assert isinstance(agent, LangChainResponsibilityAgent)
