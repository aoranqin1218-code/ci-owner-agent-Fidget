from __future__ import annotations

import json
import os
import re
from typing import Any

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.prompts import (
    CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT,
    LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT,
)
from ci_owner_agent.config import Settings, validate_model_settings
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.scorer import downgrade_to_no_high_confidence, validate_notice


class LangChainResponsibilityAgent:
    def __init__(self, settings: Settings, context: AgentRuntimeContext, tools: list[Any]) -> None:
        self.settings = settings
        self.context = context
        self.tools = tools

    def analyze(self) -> CiResponsibilityNotice:
        config_error = validate_model_settings(self.settings)
        if config_error:
            return self._failure_notice(f"LLM 配置错误：{config_error}")
        self._configure_langsmith()
        try:
            raw = self._invoke_agent()
            notice = self._parse_notice(raw)
            if notice is None:
                repaired = self._repair_output(raw)
                notice = self._parse_notice(repaired)
            if notice is None:
                return self._failure_notice("LLM 输出无法解析为 CiResponsibilityNotice，已降级为无高可信责任人。")
            return validate_notice(notice)
        except Exception as exc:
            return self._failure_notice(f"LLM 分析失败，已降级为无高可信责任人：{exc}")

    def _configure_langsmith(self) -> None:
        if self.settings.langsmith_tracing and self.settings.langsmith_api_key:
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGSMITH_API_KEY"] = self.settings.langsmith_api_key
            os.environ["LANGSMITH_PROJECT"] = self.settings.langsmith_project
            os.environ["LANGSMITH_ENDPOINT"] = self.settings.langsmith_endpoint

    def _model(self):
        try:
            from langchain_openai import ChatOpenAI
        except Exception as exc:
            raise RuntimeError(f"langchain-openai is not installed: {exc}") from exc
        kwargs: dict[str, Any] = {
            "model": self.settings.model_name,
            "api_key": self.settings.api_key,
            "temperature": 0,
        }
        if self.settings.model_base_url:
            kwargs["base_url"] = self.settings.model_base_url
        try:
            return ChatOpenAI(**kwargs)
        except TypeError:
            if "base_url" in kwargs:
                kwargs["openai_api_base"] = kwargs.pop("base_url")
            return ChatOpenAI(**kwargs)

    def _invoke_agent(self) -> str:
        agent = self._create_v1_agent(self._model())
        result = agent.invoke(
            {"messages": [{"role": "user", "content": self._initial_input()}]},
            config={
                "metadata": self._metadata(),
                "run_name": "ci-owner-agent-langchain-v1",
                "recursion_limit": max(self.settings.max_tool_steps * 2, 20),
            },
        )
        structured = result.get("structured_response") if isinstance(result, dict) else None
        if structured is not None:
            if isinstance(structured, CiResponsibilityNotice):
                return structured.model_dump_json()
            if isinstance(structured, dict):
                return json.dumps(structured, ensure_ascii=False, default=str)
            return str(structured)

        messages = result.get("messages") if isinstance(result, dict) else None
        if messages:
            last = messages[-1]
            content = getattr(last, "content", None)
            if content is not None:
                return str(content)

        return json.dumps(result, ensure_ascii=False, default=str)

    def _create_v1_agent(self, model):
        try:
            from langchain.agents import create_agent
            from langchain.agents.structured_output import ToolStrategy
        except Exception as exc:
            raise RuntimeError(
                "LangChain v1 create_agent dependencies are not installed or incompatible. "
                "Please install langchain>=1.3,<2 and langchain-openai compatible with LangChain v1. "
                f"Original error: {exc}"
            ) from exc
        return create_agent(
            model=model,
            tools=self.tools,
            system_prompt=LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT,
            response_format=ToolStrategy(
                schema=CiResponsibilityNotice,
                handle_errors=(
                    "请输出严格合法的 CiResponsibilityNotice。"
                    "不得新增 schema 之外的字段。"
                    "如果证据不足，必须输出 no_high_confidence_owner。"
                ),
            ),
        )

    def _repair_output(self, raw: str) -> str:
        try:
            model = self._model()
            prompt = (
                "请把下面模型输出修复为严格合法的 CiResponsibilityNotice JSON。\n"
                "不得新增 schema 之外的字段。\n"
                "不要输出 Markdown，不要解释，只输出 JSON object。\n"
                "如果原输出证据不足或无法判断，必须输出 no_high_confidence_owner。\n\n"
                f"{CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT}\n\n"
                f"原始输出:\n{raw}"
            )
            response = model.invoke(prompt)
            return str(getattr(response, "content", response))
        except Exception:
            return ""

    def _initial_input(self) -> str:
        payload = {
            "job": self.context.job,
            "buildNumber": self.context.build_number,
            "buildUrl": self.context.build_url,
            "result": self.context.result,
            "branch": self.context.branch,
            "baseCommit": self.context.base_commit,
            "headCommit": self.context.head_commit,
            "changedFiles": [item.model_dump() for item in self.context.changed_files[:80]],
            "changedFilesTruncated": len(self.context.changed_files) > 80,
            "commits": [item.model_dump() for item in self.context.commits[:50]],
            "commitsTruncated": len(self.context.commits) > 50,
            "logTail": self.context.build_info.logTail.model_dump() if self.context.build_info.logTail else None,
            "instruction": "必须基于工具证据。证据不足输出 no_high_confidence_owner。最终只输出 JSON。",
        }
        return json.dumps(payload, ensure_ascii=False)

    def _metadata(self) -> dict[str, Any]:
        return {
            "job": self.context.job,
            "buildNumber": self.context.build_number,
            "repo": self.context.repo,
            "branch": self.context.branch,
            "baseCommit": self.context.base_commit,
            "headCommit": self.context.head_commit,
            "result": self.context.result,
        }

    def _parse_notice(self, raw: str) -> CiResponsibilityNotice | None:
        text = self._strip_code_fence(raw)
        try:
            return CiResponsibilityNotice.model_validate_json(text)
        except Exception:
            try:
                return CiResponsibilityNotice.model_validate(json.loads(text))
            except Exception:
                return None

    def _strip_code_fence(self, raw: str) -> str:
        text = raw.strip()
        match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S)
        return match.group(1).strip() if match else text

    def _failure_notice(self, reason: str) -> CiResponsibilityNotice:
        base = CiResponsibilityNotice(
            job=self.context.job,
            buildNumber=self.context.build_number,
            buildUrl=self.context.build_url,
            result=self.context.result,
            branch=self.context.branch,
            headCommit=self.context.head_commit,
            baseCommit=self.context.base_commit,
            owner={
                "type": "no_high_confidence_owner",
                "name": "无高可信责任人",
                "email": None,
                "commit": None,
                "confidence": 0,
            },
            failureReason=reason,
            evidence=[],
            suggestions=[
                "人工查看 Jenkins 日志和本次 diff。",
                "检查 Agent prompt、模型输出格式和 LangSmith trace。",
            ],
            hasHighConfidenceOwner=False,
        )
        return downgrade_to_no_high_confidence(base, reason)
