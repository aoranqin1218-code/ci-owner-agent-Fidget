from __future__ import annotations

import json
import os
import re
from typing import Any

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.prompts import LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT
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
        return ChatOpenAI(**kwargs)

    def _invoke_agent(self) -> str:
        try:
            from langchain.agents import AgentExecutor, create_tool_calling_agent
            from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        except Exception as exc:
            raise RuntimeError(f"langchain tool-calling dependencies are not installed: {exc}") from exc

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT),
                ("human", "{input}"),
                MessagesPlaceholder("agent_scratchpad"),
            ]
        )
        agent = create_tool_calling_agent(self._model(), self.tools, prompt)
        executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            max_iterations=self.settings.max_tool_steps,
            handle_parsing_errors=True,
            verbose=False,
        )
        result = executor.invoke(
            {"input": self._initial_input()},
            config={"metadata": self._metadata()},
        )
        return str(result.get("output", result))

    def _repair_output(self, raw: str) -> str:
        try:
            model = self._model()
            prompt = (
                "把下面模型输出修复为严格合法的 CiResponsibilityNotice JSON。"
                "不要输出 Markdown，不要解释，只输出 JSON。"
                "如果证据不足，owner.type 必须为 no_high_confidence_owner。\n\n"
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
