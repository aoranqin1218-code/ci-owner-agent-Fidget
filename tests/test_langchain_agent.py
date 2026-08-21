from __future__ import annotations

import json
import re
import sys
import types
from dataclasses import replace

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.agents.prompts import (
    CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT,
    INITIAL_INPUT_INSTRUCTION,
    LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT,
)
from ci_owner_agent.config import load_settings
from ci_owner_agent.orchestrator import analyze_local
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CiResponsibilityNotice, CommitInfo, LogTail
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.investigation_scope import InvestigationScope
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
        response_format="tool",
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
        "repo": context.repo,
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


def test_responsibility_items_keep_inherited_owner_when_top_owner_is_no_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["owner"] = {
        "type": "no_high_confidence_owner",
        "name": "无高可信责任人",
        "email": None,
        "commit": None,
        "confidence": 0,
    }
    payload["hasHighConfidenceOwner"] = False
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "getJsSdkConfig dingtalk ua",
            "failureSignature": "sig-1",
            "failureSummary": "same mocha failure as 5104",
            "owner": {
                "type": "inherited_failure_owner",
                "name": "Tang.Tangerine-唐嘉伟",
                "email": "tang@example.com",
                "commit": "ab286e5",
                "confidence": 0.9,
            },
            "responsibilityType": "inherited_failure_owner",
            "sourceBuildNumber": 5104,
            "sourceBuildUrl": "local://services/fx-code-unittest/5104",
            "sourceCommit": "ab286e5",
            "matchType": "signature_exact",
            "relationship": "very_likely_same_failure",
            "confidence": 1.0,
            "reason": "历史持续失败，继承首次失败责任人。",
            "evidenceIds": ["E1"],
        }
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.hasHighConfidenceOwner is False
    assert notice.responsibilityItems[0].owner.type == "inherited_failure_owner"
    assert notice.responsibilityItems[0].owner.name == "Tang.Tangerine-唐嘉伟"


def test_single_current_build_responsibility_item_keeps_top_high_confidence(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "FILE_SIZE_EXCEEDED classify failure",
            "failureSignature": "sig-current",
            "failureSummary": "AssertionError FILE_SIZE_EXCEEDED",
            "owner": payload["owner"],
            "responsibilityType": "current_build_owner",
            "sourceCommit": context.head_commit,
            "confidence": 0.88,
            "reason": "日志和 diff 互相支撑。",
            "evidenceIds": ["E1", "E2"],
        }
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    assert notice.owner.type == "high_confidence"
    assert notice.hasHighConfidenceOwner is True
    assert notice.responsibilityItems[0].responsibilityType == "current_build_owner"


def test_multiple_responsibility_item_owners_keep_top_owner_no_high_confidence(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "historical failure",
            "owner": {
                "type": "inherited_failure_owner",
                "name": "Tang.Tangerine-唐嘉伟",
                "email": "tang@example.com",
                "commit": "ab286e5",
                "confidence": 0.9,
            },
            "responsibilityType": "inherited_failure_owner",
            "sourceBuildNumber": 5104,
            "confidence": 1.0,
            "reason": "历史持续失败。",
        },
        {
            "failureId": "F2",
            "failureTitle": "new independent failure",
            "owner": {
                "type": "high_confidence",
                "name": "Li Si",
                "email": "lisi@example.com",
                "commit": context.head_commit,
                "confidence": 0.88,
            },
            "responsibilityType": "current_build_owner",
            "sourceCommit": context.head_commit,
            "confidence": 0.88,
            "reason": "日志和 diff 支撑新失败。",
            "evidenceIds": ["E1", "E2"],
        },
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.hasHighConfidenceOwner is False
    assert {item.owner.name for item in notice.responsibilityItems} == {"Tang.Tangerine-唐嘉伟", "Li Si"}


def test_old_notice_without_responsibility_items_still_parses(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    notice = CiResponsibilityNotice.model_validate(payload)
    assert notice.responsibilityItems == []
    assert notice.owner.type == "high_confidence"


def test_failure_id_is_stable_from_failure_signature(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload_a = high_confidence_payload(context)
    payload_b = high_confidence_payload(context)
    item = {
        "failureId": "F2",
        "failureTitle": "same failure",
        "failureSignature": "sig-same",
        "failureSummary": "summary",
        "owner": payload_a["owner"],
        "responsibilityType": "current_build_owner",
        "confidence": 0.9,
        "reason": "same",
    }
    payload_a["responsibilityItems"] = [item]
    payload_b["responsibilityItems"] = [{**item, "failureId": "chunk-1"}]
    notice_a = CiResponsibilityNotice.model_validate(payload_a)
    notice_b = CiResponsibilityNotice.model_validate(payload_b)
    assert notice_a.responsibilityItems[0].failureId == notice_b.responsibilityItems[0].failureId
    assert re.fullmatch(r"failure-[0-9a-f]{12}", notice_a.responsibilityItems[0].failureId)


def test_failure_id_differs_for_different_failure_signatures(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "first",
            "failureSignature": "sig-one",
            "owner": payload["owner"],
            "responsibilityType": "current_build_owner",
            "confidence": 0.9,
            "reason": "first",
        },
        {
            "failureId": "F2",
            "failureTitle": "second",
            "failureSignature": "sig-two",
            "owner": payload["owner"],
            "responsibilityType": "current_build_owner",
            "confidence": 0.9,
            "reason": "second",
        },
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    assert notice.responsibilityItems[0].failureId != notice.responsibilityItems[1].failureId


def test_missing_failure_signature_gets_manual_fallback_and_stable_id(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "Webhook触发",
            "failureSignature": None,
            "failureSummary": "Run AwaitFunc Timeout",
            "owner": payload["owner"],
            "responsibilityType": "current_build_owner",
            "confidence": 0.9,
            "reason": "timeout",
        }
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    item = notice.responsibilityItems[0]
    assert item.failureSignature
    assert "run_awaitfunc_timeout" in item.failureSignature
    assert re.fullmatch(r"failure-[0-9a-f]{12}", item.failureId)


def test_invalid_inherited_item_downgrades_to_no_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["owner"] = {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0}
    payload["hasHighConfidenceOwner"] = False
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "historical failure",
            "failureSignature": "sig-historical",
            "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0},
            "responsibilityType": "inherited_failure_owner",
            "sourceBuildNumber": 5104,
            "confidence": 0.9,
            "reason": "bad inherited",
        }
    ]
    item = CiResponsibilityNotice.model_validate(payload).responsibilityItems[0]
    assert item.responsibilityType == "no_high_confidence_owner"
    assert item.owner.type == "no_high_confidence_owner"
    assert item.sourceBuildNumber is None
    assert item.confidence == 0
    assert "历史持续失败未找到可继承责任人" in item.reason


def test_current_build_owner_fills_source_build_and_commit(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "current failure",
            "failureSignature": "sig-current-build",
            "owner": payload["owner"],
            "responsibilityType": "current_build_owner",
            "sourceBuildNumber": None,
            "sourceCommit": None,
            "confidence": 1.5,
            "reason": "current",
        }
    ]
    item = CiResponsibilityNotice.model_validate(payload).responsibilityItems[0]
    assert item.sourceBuildNumber == context.build_number
    assert item.sourceCommit == context.head_commit
    assert item.confidence == 1


def test_no_high_confidence_item_clears_zero_source_build(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = high_confidence_payload(context)
    payload["owner"] = {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0}
    payload["hasHighConfidenceOwner"] = False
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "unknown failure",
            "failureSignature": "sig-unknown",
            "owner": payload["owner"],
            "responsibilityType": "no_high_confidence_owner",
            "sourceBuildNumber": 0,
            "sourceCommit": context.head_commit,
            "confidence": 0.5,
            "reason": "unknown",
        }
    ]
    item = CiResponsibilityNotice.model_validate(payload).responsibilityItems[0]
    assert item.owner.type == "no_high_confidence_owner"
    assert item.sourceBuildNumber is None
    assert item.sourceCommit is None
    assert item.confidence == 0


def test_langchain_agent_valid_json_high_confidence(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: json.dumps(high_confidence_payload(context), ensure_ascii=False))
    notice = agent.analyze()
    assert notice.hasHighConfidenceOwner is True
    assert notice.owner.type == "high_confidence"


def test_langchain_agent_parses_pure_json(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = json.dumps(high_confidence_payload(context), ensure_ascii=False)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: payload)
    notice = agent.analyze()
    assert notice.owner.type == "high_confidence"


def test_langchain_agent_parses_json_fenced_block(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = json.dumps(high_confidence_payload(context), ensure_ascii=False)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: f"```json\n{payload}\n```")
    notice = agent.analyze()
    assert notice.hasHighConfidenceOwner is True
    assert notice.owner.type == "high_confidence"


def test_langchain_agent_parses_explanation_plus_json_fenced_block(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = json.dumps(high_confidence_payload(context), ensure_ascii=False)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: f"下面是分析结果：\n```json\n{payload}\n```\n请查收。")
    notice = agent.analyze()
    assert notice.owner.type == "high_confidence"


def test_langchain_agent_parses_explanation_plus_json_object(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = json.dumps(high_confidence_payload(context), ensure_ascii=False)
    agent = LangChainResponsibilityAgent(context.settings, context, [])
    monkeypatch.setattr(agent, "_invoke_agent", lambda: f"分析如下：\n{payload}\n以上。")
    notice = agent.analyze()
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
    paths = tools["repo_find_paths"].invoke({"query": "classify", "paths": ["packages/fxp-ai"], "suffixes": [".ts"]})
    assert paths["ok"] is True
    assert "packages/fxp-ai/errors/classify.ts" in paths["matches"]
    diff = tools["repo_get_file_diff"].invoke({"path": "packages/fxp-ai/errors/classify.ts"})
    assert diff["ok"] is True
    assert "classifyError" in diff["diff"]
    keyword = tools["repo_keyword_search"].invoke({"keywords": ["FILE_SIZE_EXCEEDED"]})
    assert keyword["ok"] is True
    assert "ts_find_definitions" in tools
    assert "ts_find_callers" in tools
    assert "check_node_dependencies_for_analysis" in tools


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
        "responsibilityItems",
        "inherited_failure_owner",
        "hasHighConfidenceOwner",
        "no_high_confidence_owner",
        "禁止输出额外字段",
    ]:
        assert text in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT


def test_prompt_allows_explicit_log_file_paths_before_file_content():
    for text in [
        "日志堆栈",
        "失败测试行",
        "错误输出中明确出现的文件路径",
        "test/packages/fxp-ai/errors/classify.test.ts:1:23",
    ]:
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT


def test_prompt_requires_final_json_after_sufficient_evidence_or_budget_exhaustion():
    for text in [
        "日志失败证据 + 相关 diff 证据 + 测试断言证据 + 被测函数行为证据",
        "必须立即输出最终 CiResponsibilityNotice JSON",
        "不要为了补强证据而继续读取 base 版本文件",
        "tool call budget exhausted",
        "禁止继续调用任何工具",
    ]:
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT


def test_prompt_pre_existing_failure_keeps_schema_owner_type():
    for text in [
        "history_search_similar_failures",
        "pre-existing failure",
        "pre-existing failure 不代表没有责任人",
        "responsibilityType=inherited_failure_owner",
        "多个 failure item 可以有多个不同 owner",
        "不要输出 pre_existing_failure",
        "type 只能使用 reasoning 或 build_info",
    ]:
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT


def test_prompt_fast_paths_single_inherited_failure():
    for text in [
        "failureSummaries 只有 1 个",
        "inheritedOwner.found=true",
        "不要继续调用 repo/log/ts 工具",
        "顶层 owner 使用 no_high_confidence_owner",
        "inherited failure 的责任来自首次失败 build",
    ]:
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT


def test_prompt_uses_stable_inherited_reason_template():
    for text in [
        "稳定模板",
        "责任继承自首次失败责任人",
        "不是当前 build 新引入",
        "不要重新推断或改写首次失败的 diff 原因",
        "本 build 没有新的高可信责任人，责任项见 responsibilityItems",
    ]:
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT


def test_schema_prompt_distinguishes_top_owner_from_item_owner_type():
    assert "顶层 owner" in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
    assert "inherited_failure_owner 只能出现在 responsibilityItems" in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
    assert "顶层 owner 不要输出 inherited_failure_owner" in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
    assert '"type": "high_confidence | medium_confidence | no_high_confidence_owner",' in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
    top_owner_block = CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT.split('"failureReason"', 1)[0]
    assert "inherited_failure_owner" not in top_owner_block
    assert '"type": "high_confidence | medium_confidence | no_high_confidence_owner | inherited_failure_owner"' in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT


def test_schema_prompt_describes_source_build_number_and_auto_failure_id():
    assert '"sourceBuildNumber": 0' not in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
    assert '"sourceBuildNumber": "integer or null"' in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
    assert '"failureId": "auto"' in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
    assert "不要输出 0" in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
    assert "不要输出 F1/F2/chunk-0/failure-2" in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT


def test_prompt_describes_failure_signature_alignment_rule():
    for text in [
        "failureSignature",
        "signature.signatureKey",
        "signatureHash",
        "不要使用自然语言描述",
    ]:
        assert text in CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT


def test_prompt_and_tool_docs_describe_literal_log_search_and_direct_log_paths(repo_cache, sample_repo, logs):
    for text in [
        "log_search 是字面字符串搜索",
        "不支持正则表达式和 | OR",
        "不要传 \"FAILED|failed|Error\"",
        "可以直接调用 repo_get_file_content，不需要先 repo_find_paths",
    ]:
        assert text in LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT
    tools = {tool.name: tool for tool in build_langchain_tools(make_lc_context(repo_cache, sample_repo, logs))}
    assert "literal string" in tools["log_search"].description
    assert "FAILED|failed|Error" in tools["log_search"].description
    assert "call repo_get_file_content directly" in tools["repo_find_paths"].description


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
    json.loads(limited["contentJson"])


def test_tool_limit_content_json_is_valid_after_truncation():
    limited = _limit({"ok": True, "items": [{"x": "值", "long": "a" * 5000} for _ in range(100)]}, 300)
    assert limited["truncated"] is True
    json.loads(limited["contentJson"])


def test_tool_limit_truncates_large_list_structurally():
    limited = _limit({"ok": True, "matches": [{"file": f"src/{i}.ts"} for i in range(200)]}, 600)
    payload = json.loads(limited["contentJson"])
    assert payload["matches"]["_truncated_items"] is True
    assert payload["matches"]["_original_length"] == 200


def test_tool_limit_truncates_large_string_fields_structurally():
    limited = _limit({"ok": True, "content": "x" * 5000}, 800)
    payload = json.loads(limited["contentJson"])
    assert "content" in payload
    assert "content" in payload["_truncated_fields"]


def test_duplicate_tool_call_blocked(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    tools = {tool.name: tool for tool in build_langchain_tools(context)}
    assert tools["repo_keyword_search"].invoke({"keywords": ["FILE_SIZE_EXCEEDED"]})["ok"] is True
    blocked = tools["repo_keyword_search"].invoke({"keywords": ["FILE_SIZE_EXCEEDED"]})
    assert blocked["ok"] is False
    assert blocked["error"] == "duplicate tool call blocked"


def test_tool_budget_exhausted(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, max_tool_steps=1)
    context = replace(context, settings=settings)
    tools = {tool.name: tool for tool in build_langchain_tools(context)}
    assert tools["log_detect_final_status"].invoke({})["status"] == "FAILURE"
    blocked = tools["repo_get_diff_files"].invoke({})
    assert blocked["ok"] is False
    assert blocked["error"] == "tool call budget exhausted"


class ScopedGitClient:
    def __init__(self):
        self.calls = []

    def get_commits_between(self, repo, base_commit, head_commit):
        self.calls.append(("commits", base_commit, head_commit))
        return {"ok": True, "commits": []}

    def get_diff_files(self, repo, base_commit, head_commit):
        self.calls.append(("diff_files", base_commit, head_commit))
        return {"ok": True, "files": []}

    def get_file_diff(self, repo, base_commit, head_commit, path, context_lines):
        self.calls.append(("file_diff", base_commit, head_commit, path, context_lines))
        return {"ok": True, "path": path, "diff": ""}


def test_repo_tools_scope_focus_and_full(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    git_client = ScopedGitClient()
    context = replace(
        context,
        git_client=git_client,
        base_commit="full-base",
        head_commit="head",
        investigation_scope=InvestigationScope(
            mode="focus_then_full",
            full_base_commit="full-base",
            full_head_commit="head",
            focus_base_commit="focus-base",
            focus_head_commit="head",
            previous_build_number=5087,
        ),
    )
    tools = {tool.name: tool for tool in build_langchain_tools(context)}

    focus_result = tools["repo_get_diff_files"].invoke({"scope": "focus"})
    full_result = tools["repo_get_diff_files"].invoke({"scope": "full"})
    file_result = tools["repo_get_file_diff"].invoke({"path": "packages/fxp-ai/src/index.ts", "scope": "full"})
    invalid_result = tools["repo_get_commits_between"].invoke({"scope": "wide"})

    assert focus_result["scope"] == "focus"
    assert focus_result["baseCommit"] == "focus-base"
    assert full_result["scope"] == "full"
    assert full_result["baseCommit"] == "full-base"
    assert "expanded to fullRange" in full_result["warning"]
    assert file_result["baseCommit"] == "full-base"
    assert invalid_result["ok"] is False
    assert "unsupported scope" in invalid_result["error"]
    assert ("diff_files", "focus-base", "head") in git_client.calls
    assert ("diff_files", "full-base", "head") in git_client.calls


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


def test_initial_prompt_contains_investigation_scope(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    context = replace(
        context,
        investigation_scope=InvestigationScope(
            mode="focus_then_full",
            full_base_commit="full-base",
            full_head_commit="head",
            focus_base_commit="focus-base",
            focus_head_commit="head",
            previous_build_number=5087,
            previous_build_url="local://job/5087",
            previous_build_result="FAILURE",
            reason="previous build headCommit found in history store",
        ),
    )
    payload = json.loads(LangChainResponsibilityAgent(context.settings, context, [])._initial_input())

    assert payload["investigationScope"]["mode"] == "focus_then_full"
    assert payload["investigationScope"]["focusBaseCommit"] == "focus-base"
    assert payload["initialDiffScope"] == "focus"
    assert payload["changedFilesScope"] == "focus"
    assert payload["commitsScope"] == "focus"
    instruction = payload["instruction"]
    assert "focusRange" in instruction
    assert "fullRange" in instruction
    assert "repo_get_diff_files(scope=\"full\")" in instruction
    assert "不要一开始就全量分析 fullRange" in instruction
    assert instruction == INITIAL_INPUT_INSTRUCTION


def test_initial_input_omits_full_log_tail_and_includes_failure_summaries(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    noisy_tail = "NOISY_LOG_TOKEN_" + ("x" * 20000)
    build_info = context.build_info.model_copy(update={"logTail": LogTail(startLine=1, endLine=500, content=noisy_tail)})
    summaries = {
        "chunks": [
            {
                "chunkIndex": 0,
                "chunkSource": "local_test_failure_summary",
                "startLine": 100,
                "endLine": 110,
                "signature": {"testName": "getJsSdkConfig dingtalk ua", "errorType": "Error"},
                "content": "1) getJsSdkConfig dingtalk ua\nError: UNKNOWN\nat test/a.test.ts:1:1",
            }
        ]
    }
    context = replace(context, build_info=build_info, failure_summaries=summaries)
    raw = LangChainResponsibilityAgent(context.settings, context, [])._initial_input()
    payload = json.loads(raw)
    assert "logTail" not in payload
    assert payload["failureSummaries"]
    assert payload["failureSummaries"][0]["signature"]["testName"] == "getJsSdkConfig dingtalk ua"
    assert payload["logTailMeta"]["omitted"] is True
    assert payload["logTailMeta"]["contentChars"] == len(noisy_tail)
    assert noisy_tail not in raw
    assert "NOISY_LOG_TOKEN" not in raw


def test_initial_input_without_failure_summary_still_omits_full_log_tail(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    noisy_tail = "UNHELPFUL_TAIL_" + ("z" * 20000)
    build_info = context.build_info.model_copy(update={"logTail": LogTail(startLine=1, endLine=500, content=noisy_tail)})
    context = replace(
        context,
        build_info=build_info,
        failure_summaries={"chunks": [], "warning": "test failure summaries unavailable; no mocha failure blocks found"},
    )
    raw = LangChainResponsibilityAgent(context.settings, context, [])._initial_input()
    payload = json.loads(raw)
    assert payload["failureSummaries"] == []
    assert "no mocha failure blocks found" in payload["failureSummaryWarning"]
    assert "logTailPreview" not in payload
    assert noisy_tail not in raw
    assert "UNHELPFUL_TAIL" not in raw


def test_initial_input_includes_compact_history_precheck(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    long_text = "很长" * 5000
    context = replace(
        context,
        history_precheck={
            "ok": True,
            "historyEnabled": True,
            "currentBuild": 5077,
            "lastSuccessfulBuildNumber": 5068,
            "currentChunks": [{"chunkIndex": 0, "normalizedHash": "abc", "signature": {"testName": "t"}}],
            "candidates": [
                {
                    "buildNumber": 5076,
                    "similarity": 1.0,
                    "relationship": "very_likely_same_failure",
                    "matchType": "signature_exact",
                    "matchedHistoricalChunk": long_text,
                    "matchedCurrentChunk": long_text,
                    "notice": {"large": long_text},
                    "ownerType": "no_high_confidence_owner",
                    "ownerName": "无高可信责任人",
                    "hasHighConfidenceOwner": False,
                    "failureReason": "历史持续失败",
                    "inheritedOwner": {
                        "found": True,
                        "sourceBuildNumber": 5104,
                        "sourceBuildUrl": "local://services/fx-code-unittest/5104",
                        "ownerType": "high_confidence",
                        "ownerName": "Tang.Tangerine-唐嘉伟",
                        "ownerEmail": "tang@example.com",
                        "ownerCommit": "ab286e5",
                        "confidence": 0.9,
                        "matchType": "signature_exact",
                        "relationship": "very_likely_same_failure",
                    },
                }
            ],
        },
    )
    raw = LangChainResponsibilityAgent(context.settings, context, [])._initial_input()
    payload = json.loads(raw)
    candidate = payload["historyPrecheck"]["candidates"][0]
    assert candidate["buildNumber"] == 5076
    assert candidate["matchType"] == "signature_exact"
    assert candidate["relationship"] == "very_likely_same_failure"
    assert candidate["inheritedOwner"]["found"] is True
    assert candidate["inheritedOwner"]["sourceBuildNumber"] == 5104
    assert candidate["inheritedOwner"]["ownerName"] == "Tang.Tangerine-唐嘉伟"
    instruction = payload["historyPrecheck"]["instruction"]
    assert "必须输出 no_high_confidence_owner" not in instruction
    assert "responsibilityType=inherited_failure_owner" in instruction
    assert "owner 使用 inheritedOwner" in instruction
    assert "顶层 owner" in instruction
    assert "failureSummaries 只有 1 个" in instruction
    assert "inheritedOwner.found=true" in instruction
    assert "不要继续调用 repo/log/ts 工具" in instruction
    assert "顶层 owner 使用 no_high_confidence_owner" in instruction
    assert "matchedHistoricalChunk" not in candidate
    assert "matchedCurrentChunk" not in candidate
    assert "notice" not in candidate
    assert long_text not in raw


def test_initial_input_includes_failure_facts(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    context = replace(
        context,
        failure_summaries={"chunks": [], "warning": "no Mocha/Japa failure block found"},
        failure_facts={
            "ok": True,
            "warning": None,
            "facts": [
                {
                    "factId": "fact-123",
                    "signatureKey": "typescript_compile_error|TS2305|packages/fxp-ai/src/index.ts|classifyErrorMessage",
                    "historyEligible": True,
                    "isGenericWrapper": False,
                    "failureKind": "typescript_compile_error",
                    "phase": "nx:build",
                    "command": "npm run nx:build",
                    "errorCode": "TS2305",
                    "errorType": "TypeScriptCompileError",
                    "packageName": "@fx/ai",
                    "filePath": "packages/fxp-ai/src/index.ts",
                    "symbol": "classifyErrorMessage",
                    "message": "Module './errors' has no exported member 'classifyErrorMessage'",
                    "rootCauseSummary": "missing export",
                    "confidence": 0.92,
                    "evidenceLines": ["large evidence omitted from compact input"],
                }
            ],
        },
    )

    raw = LangChainResponsibilityAgent(context.settings, context, [])._initial_input()
    payload = json.loads(raw)

    assert payload["failureFacts"]["ok"] is True
    fact = payload["failureFacts"]["facts"][0]
    assert fact["factId"] == "fact-123"
    assert fact["errorCode"] == "TS2305"
    assert "evidenceLines" not in fact
    assert "failureFacts" in payload["instruction"]
    assert "wrapper" in payload["instruction"]
    assert "inherited_failure_owner" in payload["instruction"]


def _ai_history_precheck_payload(count: int = 1) -> dict:
    return {
        "ok": True,
        "mode": "ai_failure_facts",
        "threshold": 0.9,
        "warning": None,
        "diagnostics": {
            "eligibleCurrentFactsCount": count,
            "historicalBuildsCount": 2,
            "historicalFactsCount": 2,
            "rankedPairsCount": count,
            "comparedPairsCount": count,
            "acceptedCandidatesCount": 1,
            "skipped": {"compareNotSameFailure": 0, "compareError": 0},
            "queryStage": "ok",
            "historicalBuildNumbers": [7, 6],
            "historicalFactBuildNumbers": [7, 6],
            "notice": {"large": "should be omitted"},
            "evidence": ["should be omitted"],
            "compareResults": [
                {
                    "currentFactId": f"fact-{idx}",
                    "currentSignatureKey": f"typescript_compile_error|TS2305|src/{idx}.ts|symbol",
                    "historicalFactId": f"hist-{idx}",
                    "historicalSignatureKey": f"typescript_compile_error|TS2305|src/{idx}.ts|symbol",
                    "historicalBuildNumber": 7,
                    "sameFailure": True,
                    "confidence": 0.95,
                    "relationship": "same_root_cause",
                    "accepted": True,
                    "skipReason": None,
                    "reason": "same root cause",
                    "notice": {"large": "should be omitted"},
                    "evidence": ["should be omitted"],
                }
                for idx in range(count)
            ],
        },
        "currentFacts": [
            {
                "factId": f"fact-{idx}",
                "signatureKey": f"typescript_compile_error|TS2305|src/{idx}.ts|symbol",
                "failureKind": "typescript_compile_error",
                "errorCode": "TS2305",
                "packageName": "@fx/ai",
                "filePath": f"src/{idx}.ts",
                "symbol": "classifyErrorMessage",
                "confidence": 0.95,
                "blockedReason": None,
                "notice": {"large": "should be omitted"},
                "evidence": ["should be omitted"],
                "inheritedOwner": {
                    "found": True,
                    "sourceBuildNumber": 7,
                    "sourceBuildUrl": "local://services/fx-code-unittest/7",
                    "ownerType": "high_confidence",
                    "ownerName": "test",
                    "ownerEmail": "test@test.com",
                    "ownerCommit": "commit-7",
                    "confidence": 0.95,
                    "matchType": "ai_fact_semantic",
                    "relationship": "same_root_cause",
                    "feedbackVerified": False,
                    "feedbackCorrected": False,
                },
            }
            for idx in range(count)
        ],
        "candidates": [
            {
                "currentFactId": f"fact-{idx}",
                "historicalFactId": f"hist-{idx}",
                "buildNumber": 7,
                "buildUrl": "local://services/fx-code-unittest/7",
                "sameFailure": True,
                "confidence": 0.95,
                "relationship": "same_root_cause",
                "matchType": "ai_fact_semantic",
                "reason": "same root cause",
                "ownerName": "test",
                "ownerEmail": "test@test.com",
                "ownerCommit": "commit-7",
                "notice": {"large": "should be omitted"},
                "evidence": ["should be omitted"],
                "feedbackOverride": {"action": "confirm_owner", "reviewer": "qa", "note": "verified"},
            }
            for idx in range(count)
        ],
    }


def test_initial_input_includes_ai_history_precheck(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    context = replace(context, ai_history_precheck=_ai_history_precheck_payload())

    payload = json.loads(LangChainResponsibilityAgent(context.settings, context, [])._initial_input())

    precheck = payload["aiHistoryPrecheck"]
    assert precheck["mode"] == "ai_failure_facts"
    assert precheck["currentFacts"][0]["inheritedOwner"]["found"] is True
    assert precheck["currentFacts"][0]["inheritedOwner"]["matchType"] == "ai_fact_semantic"
    assert precheck["candidates"][0]["matchType"] == "ai_fact_semantic"
    assert precheck["diagnostics"]["historicalFactsCount"] == 2
    assert precheck["diagnostics"]["compareResults"][0]["accepted"] is True


def test_initial_input_ai_history_precheck_is_compacted(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    context = replace(context, ai_history_precheck=_ai_history_precheck_payload(8))

    raw = LangChainResponsibilityAgent(context.settings, context, [])._initial_input()
    payload = json.loads(raw)

    precheck = payload["aiHistoryPrecheck"]
    assert len(precheck["currentFacts"]) == 5
    assert len(precheck["candidates"]) == 5
    assert "notice" not in precheck["currentFacts"][0]
    assert "evidence" not in precheck["currentFacts"][0]
    assert "notice" not in precheck["candidates"][0]
    assert "evidence" not in precheck["candidates"][0]
    assert "notice" not in precheck["diagnostics"]
    assert "evidence" not in precheck["diagnostics"]
    assert "notice" not in precheck["diagnostics"]["compareResults"][0]
    assert "evidence" not in precheck["diagnostics"]["compareResults"][0]
    assert "should be omitted" not in raw


def test_instruction_mentions_ai_fact_semantic_inherited_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = json.loads(LangChainResponsibilityAgent(context.settings, context, [])._initial_input())
    instruction = payload["instruction"]
    for text in [
        "aiHistoryPrecheck",
        "ai_fact_semantic",
        "inherited_failure_owner",
        "same_root_cause",
        "blockedReason",
    ]:
        assert text in instruction


def test_instruction_tells_failure_facts_should_be_log_evidence(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = json.loads(LangChainResponsibilityAgent(context.settings, context, [])._initial_input())
    instruction = payload["instruction"]
    for text in [
        "failureFacts",
        "evidence.type",
        "log",
        "build_info 只用于 metadata",
    ]:
        assert text in instruction


def test_initial_input_instruction_does_not_default_to_log_tail(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    payload = json.loads(LangChainResponsibilityAgent(context.settings, context, [])._initial_input())
    instruction = payload["instruction"]

    assert "failureSummaries 是当前构建最重要的失败摘要" in instruction
    assert "不要默认读取 log tail" in instruction
    assert "log_find_error_chunks" in instruction
    assert "SUCCESS / ABORTED 已由 orchestrator 处理" in instruction
    assert "先阅读构建信息和日志尾部" not in instruction


def test_system_prompt_prioritizes_summaries_over_log_tail():
    prompt = LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT

    assert "当前 Agent 正常只接收 FAILURE / UNSTABLE / UNKNOWN 构建" in prompt
    assert "SUCCESS / ABORTED 已由 orchestrator 直接处理" in prompt
    assert "failureSummaries 是当前构建最重要的失败摘要" in prompt
    assert "不要默认调用 log_read_tail" in prompt
    assert "log_find_error_chunks / log_search / log_read_range / log_read_tail" in prompt
    assert "先阅读构建信息和日志尾部" not in prompt


def test_load_settings_reads_model_timeout_and_retries(monkeypatch):
    monkeypatch.setenv("CI_AGENT_MODEL_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("CI_AGENT_MODEL_MAX_RETRIES", "2")
    settings = load_settings()
    assert settings.model_timeout_seconds == 180
    assert settings.model_max_retries == 2


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
