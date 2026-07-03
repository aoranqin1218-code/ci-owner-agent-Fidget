from __future__ import annotations

import json
from dataclasses import replace

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.config import load_settings
from ci_owner_agent.orchestrator import analyze_local
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CommitInfo
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.log_provider import LocalFileLogProvider
from ci_owner_agent.tools.langchain_tools import build_langchain_tools


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
