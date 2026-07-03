from __future__ import annotations

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.agents.responsibility_agent import RuleBasedResponsibilityAgent
from ci_owner_agent.config import Settings, validate_model_settings
from ci_owner_agent.tools.langchain_tools import build_langchain_tools


class AgentConfigurationError(RuntimeError):
    pass


def create_responsibility_agent(settings: Settings, context: AgentRuntimeContext):
    provider = settings.model_provider.lower()
    if provider == "fake":
        return RuleBasedResponsibilityAgent(context.git_client)
    config_error = validate_model_settings(settings)
    if config_error:
        raise AgentConfigurationError(config_error)
    return LangChainResponsibilityAgent(settings, context, build_langchain_tools(context))
