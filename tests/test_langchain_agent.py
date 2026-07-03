from __future__ import annotations

import json
import sys
import types
from dataclasses import replace

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.agents.prompts import (
    CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT,
    LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT,
)
from ci_owner_agent.config import load_settings
from ci_owner_agent.orchestrator import analyze_local
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CommitInfo
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.log_provider import LocalFileLogProvider
from ci_owner_agent.tools.langchain_tools import _limit, build_langchain_tools


def make_lc_context(repo_cache, sample_repo, logs):
    settings = replace(
        load_settings(),
        model_provider="doubao",
        api_key="test-key",
        model_name="test-model",
        model_base_url="https://example.test/v1",
        repo_cache_dir=repo_cache,
        langsmith_tracing=False,
        langsmith_api_key=None,
    )
    log_provider = LocalFileLogProvider(logs["auth_failed"], max_output_chars=500)
    build_info = BuildInfo(
        job="services/fx-code-unittest",
        buildNumber=5061,
        result="FAILURE",
        buildUrl="local://services/fx-code-unittest/5061",
        branch="dev",
        commit=sample_repo["head"],
        logTail=log_provider.read_tail(20),
    )
    changed = ChangedFile(
        path="packages/fxp-ai/errors/classify.ts",
        status="M",
        additions=2,
        deletions=1,
        authors=[],
    )
    commit = CommitInfo(
        hash=sample_repo["head"],
        authorName="Zhang San",
        authorEmail="zhangsan@example.com",
        subject="change classify limit",
        timestamp=None,
    )
    return AgentRuntimeContext(
        repo=sample_repo["repo"],
        job=build_info.job,
        build_number=build_info.buildNumber,
        build_url=build_info.buildUrl,
        result=build_info.result,
        branch=build_info.branch,
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        build_info=build_info,
        commits=[commit],
        changed_files=[changed],
        log_provider=log_provider,
        git_client=GitClient(repo_cache),
        settings=settings,
    )


def high_confidence_payload(context):
    return {
        "job": context.job,
        "buildNumber": context.build_number,
        "buildUrl": context.build_url,
        "result": context.result,
        "branch": context.branch,
        "headCommit": context.head_commit,
        "baseCommit": context.base_commit,
        "owner": {
            "type": "high_confidence",
            "name": "Zhang San",
            "email": "zhangsan@example.com",
            "commit": context.head_commit,
            "confidence": 0.88,
        },
        "failureReason": "日志和 diff 互相支撑。",
        "evidence": [
            {"id": "E1", "type": "log", "summary": "log", "detail": "FILE_SIZE_EXCEEDED", "source": "log:1-3"},
            {
                "id": "E2",
                "type": "diff",
                "summary": "diff",
                "detail": "packages/fxp-ai/errors/classify.ts changed",
                "source": "git diff",
            },
        ],
        "suggestions": [],
        "hasHighConfidenceOwner": True,
    }


def test_langchain_agent_valid_json_high_confidence(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: json.dumps(high_confidence_payload(context), ensure_ascii=False))
    notice = agent.analyze()
    assert notice.hasHighConfidenceOwner is True
    assert notice.owner.type == "high_confidence"


def test_langchain_v1_create_agent_is_used(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    calls = {}

    class FakeAgent:
        def invoke(self, payload, config=None):
            calls["invoke_payload"] = payload
            calls["invoke_config"] = config
            return {"structured_response": high_confidence_payload(context)}

    def fake_create_agent(**kwargs):
        calls["create_agent"] = kwargs
        return FakeAgent()

    class FakeToolStrategy:
        def __init__(self, schema, handle_errors=None):
            self.schema = schema
            self.handle_errors = handle_errors

    fake_agents = types.ModuleType("langchain.agents")
    fake_agents.create_agent = fake_create_agent
    fake_structured = types.ModuleType("langchain.agents.structured_output")
    fake_structured.ToolStrategy = FakeToolStrategy
    monkeypatch.setitem(sys.modules, "langchain.agents", fake_agents)
    monkeypatch.setitem(sys.modules, "langchain.agents.structured_output", fake_structured)

    agent = LangChainResponsibilityAgent(context.settings, context, ["tool"])
    monkeypatch.setattr(agent, "_model", lambda: object())
    notice = agent.analyze()
    assert notice.owner.type == "high_confidence"
    assert calls["create_agent"]["tools"] == ["tool"]
    assert "system_prompt" in calls["create_agent"]
    assert "response_format" in calls["create_agent"]
    assert "messages" in calls["invoke_payload"]


def test_model_uses_timeout_and_max_retries(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, model_timeout_seconds=77, model_max_retries=3)
    context = replace(context, settings=settings)
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.ModuleType("langchain_openai")
    fake_module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)
    LangChainResponsibilityAgent(settings, context, [])._model()
    assert captured["timeout"] == 77
    assert captured["max_retries"] == 3


def test_response_format_tool_uses_tool_strategy(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    calls = {}

    def fake_create_agent(**kwargs):
        calls.update(kwargs)
        return object()

    class FakeToolStrategy:
        def __init__(self, schema, handle_errors=None):
            self.schema = schema

    fake_agents = types.ModuleType("langchain.agents")
    fake_agents.create_agent = fake_create_agent
    fake_structured = types.ModuleType("langchain.agents.structured_output")
    fake_structured.ToolStrategy = FakeToolStrategy
    monkeypatch.setitem(sys.modules, "langchain.agents", fake_agents)
    monkeypatch.setitem(sys.modules, "langchain.agents.structured_output", fake_structured)
    LangChainResponsibilityAgent(context.settings, context, [])._create_v1_agent(object())
    assert "response_format" in calls


def test_response_format_json_text_omits_response_format(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, response_format="json_text")
    context = replace(context, settings=settings)
    calls = {}

    def fake_create_agent(**kwargs):
        calls.update(kwargs)
        return object()

    class FakeToolStrategy:
        def __init__(self, schema, handle_errors=None):
            self.schema = schema

    fake_agents = types.ModuleType("langchain.agents")
    fake_agents.create_agent = fake_create_agent
    fake_structured = types.ModuleType("langchain.agents.structured_output")
    fake_structured.ToolStrategy = FakeToolStrategy
    monkeypatch.setitem(sys.modules, "langchain.agents", fake_agents)
    monkeypatch.setitem(sys.modules, "langchain.agents.structured_output", fake_structured)
    LangChainResponsibilityAgent(settings, context, [])._create_v1_agent(object())
    assert "response_format" not in calls


def test_langchain_agent_structured_response_parsed(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)

    class FakeAgent:
        def invoke(self, payload, config=None):
            return {"structured_response": high_confidence_payload(context)}

    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_model", lambda: object())
    monkeypatch.setattr(agent, "_create_v1_agent", lambda model: FakeAgent())
    notice = agent.analyze()
    assert notice.owner.type == "high_confidence"


def test_agent_timeout_downgrades(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)

    class FakeAgent:
        def invoke(self, payload, config=None):
            raise TimeoutError("model timed out")

    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_model", lambda: object())
    monkeypatch.setattr(agent, "_create_v1_agent", lambda model: FakeAgent())
    notice = agent.analyze()
    assert notice.owner.type == "no_high_confidence_owner"
    assert "超时" in notice.failureReason or "LLM 分析失败" in notice.failureReason


def test_langchain_agent_messages_fallback_parsed(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    output_payload = json.dumps(high_confidence_payload(context), ensure_ascii=False)

    class FakeAgent:
        def invoke(self, payload, config=None):
            return {"messages": [type("Msg", (), {"content": output_payload})()]}

    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_model", lambda: object())
    monkeypatch.setattr(agent, "_create_v1_agent", lambda model: FakeAgent())
    notice = agent.analyze()
    assert notice.owner.type == "high_confidence"


def test_langchain_v1_import_error_is_clear(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_model", lambda: object())
    monkeypatch.setattr(
        agent,
        "_create_v1_agent",
        lambda model: (_ for _ in ()).throw(
            RuntimeError(
                "LangChain v1 create_agent dependencies are not installed or incompatible. "
                "Please install langchain>=1.3,<2 and langchain-openai compatible with LangChain v1."
            )
        ),
    )
    notice = agent.analyze()
    assert notice.owner.type == "no_high_confidence_owner"
    assert "LangChain v1 create_agent" in notice.failureReason
    assert "langchain>=1.3,<2" in notice.failureReason


def test_langchain_agent_insufficient_evidence_downgrades(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["evidence"] = [{"id": "E1", "type": "commit", "summary": "commit", "detail": "only commit"}]
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: json.dumps(payload, ensure_ascii=False))
    notice = agent.analyze()
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.hasHighConfidenceOwner is False


def test_langchain_agent_invalid_json_repair_failure(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: "not json")
    monkeypatch.setattr(agent, "_repair_output", lambda raw: "still not json")
    notice = agent.analyze()
    assert notice.owner.type == "no_high_confidence_owner"
    assert "LLM 输出无法解析" in notice.failureReason


def test_node_modules_evidence_downgrades(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["evidence"] = [
        {"id": "E1", "type": "log", "summary": "log", "detail": "node_modules/pkg/index.d.ts", "source": "node_modules/pkg/index.d.ts"},
        {"id": "E2", "type": "ts_symbol", "summary": "ts", "detail": "node_modules/pkg/index.d.ts", "source": "node_modules/pkg/index.d.ts"},
    ]
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: json.dumps(payload, ensure_ascii=False))
    notice = agent.analyze()
    assert notice.owner.type == "no_high_confidence_owner"


def test_langsmith_disabled_does_not_fail(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, langsmith_tracing=True, langsmith_api_key=None)
    context = replace(context, settings=settings)
    agent = LangChainResponsibilityAgent(settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: json.dumps(high_confidence_payload(context), ensure_ascii=False))
    notice = agent.analyze()
    assert notice.owner.type == "high_confidence"


def test_langchain_tools_context_defaults(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    tools = {tool.name: tool for tool in build_langchain_tools(context)}
    assert tools["log_read_tail"].invoke({"lines": 2})["endLine"] >= 1
    diff = tools["repo_get_file_diff"].invoke({"path": "packages/fxp-ai/errors/classify.ts"})
    assert diff["ok"] is True
    assert "classifyError" in diff["diff"]
    keyword = tools["repo_keyword_search"].invoke({"keywords": ["FILE_SIZE_EXCEEDED"]})
    assert keyword["ok"] is True
    ts_result = tools["ts_analyze_changed_functions"].invoke({})
    assert "changedFunctions" in ts_result


def test_langchain_repo_keyword_search_repo_scope_with_paths(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    tools = {tool.name: tool for tool in build_langchain_tools(context)}
    result = tools["repo_keyword_search"].invoke(
        {
            "scope": "repo",
            "paths": ["packages/fxp-ai"],
            "keywords": ["FILE_SIZE_EXCEEDED"],
        }
    )
    assert result["ok"] is True
    assert "unsupported scope: repo" not in str(result)
    assert result["matches"]
    assert all(match["file"].startswith("packages/fxp-ai/") for match in result["matches"])


def test_langchain_tools_force_checkout_for_ts_definitions_and_callers(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    calls = {}

    def fake_find_definitions(**kwargs):
        calls["definitions"] = kwargs
        return {"ok": True, "definitions": []}

    def fake_find_callers(**kwargs):
        calls["callers"] = kwargs
        return {"ok": True, "callers": []}

    monkeypatch.setattr("ci_owner_agent.tools.langchain_tools.find_definitions", fake_find_definitions)
    monkeypatch.setattr("ci_owner_agent.tools.langchain_tools.find_callers", fake_find_callers)
    tools = {tool.name: tool for tool in build_langchain_tools(context)}
    tools["ts_find_definitions"].invoke({"symbols": ["classifyError"]})
    tools["ts_find_callers"].invoke({"symbol": "classifyError", "definitionFile": "packages/fxp-ai/errors/classify.ts"})
    assert calls["definitions"]["force_checkout"] is True
    assert calls["callers"]["force_checkout"] is True


def test_prompt_contains_schema_fields():
    for text in [
        "buildNumber",
        "owner",
        "evidence",
        "hasHighConfidenceOwner",
        "no_high_confidence_owner",
        "禁止输出额外字段",
    ]:
        assert text in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT


def test_repair_prompt_uses_schema(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    captured = {}

    class FakeModel:
        def invoke(self, prompt):
            captured["prompt"] = prompt
            return type("Msg", (), {"content": "{}"})()

    monkeypatch.setattr(agent, "_model", lambda: FakeModel())
    agent._repair_output("bad")
    assert "buildNumber" in captured["prompt"]
    assert "hasHighConfidenceOwner" in captured["prompt"]
    assert "禁止输出额外字段" in captured["prompt"]


def test_tool_limit_returns_json_text_not_python_repr():
    limited = _limit({"ok": True, "items": [{"x": "值"}] * 100}, 100)
    assert limited["truncated"] is True
    assert "contentJson" in limited
    assert "originalKeys" in limited
    assert "'ok': True" not in limited["contentJson"]


def test_initial_input_is_truncated(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    changed_files = [
        ChangedFile(path=f"src/file{i}.ts", status="M", additions=1, deletions=0, authors=[])
        for i in range(45)
    ]
    commits = [
        CommitInfo(hash=f"{i:040x}", authorName="A", authorEmail="a@example.com", subject="s", timestamp=None)
        for i in range(35)
    ]
    context = replace(context, changed_files=changed_files, commits=commits)
    payload = json.loads(LangChainResponsibilityAgent(context.settings, context, [])._initial_input())
    assert len(payload["changedFiles"]) == 30
    assert payload["changedFilesTotal"] == 45
    assert payload["changedFilesTruncated"] is True
    assert len(payload["commits"]) == 20
    assert payload["commitsTotal"] == 35
    assert payload["commitsTruncated"] is True


def test_success_short_circuits_before_agent_factory(monkeypatch, repo_cache, sample_repo, logs):
    def fail_factory(*_args, **_kwargs):
        raise AssertionError("agent factory should not be called")

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", fail_factory)
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="job",
        build=1,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(logs["success"]),
        build_url="local://job/1",
        git_client=GitClient(repo_cache),
    )
    assert notice.result == "SUCCESS"


def test_aborted_short_circuits_before_agent_factory(monkeypatch, repo_cache, sample_repo, logs):
    def fail_factory(*_args, **_kwargs):
        raise AssertionError("agent factory should not be called")

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", fail_factory)
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="job",
        build=1,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(logs["aborted"]),
        build_url="local://job/1",
        git_client=GitClient(repo_cache),
    )
    assert notice.result == "ABORTED"
