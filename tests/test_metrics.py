from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.config import load_settings
from ci_owner_agent.main import main
from ci_owner_agent.schemas import FailureFact
from ci_owner_agent.services.failure_fact_ai import extract_failure_facts_with_ai
from ci_owner_agent.services.failure_fact_compare_ai import compare_failure_facts_with_ai
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.metrics import AnalysisMetricsRecorder, TokenUsageCallbackHandler, llm_invoke_with_metrics, use_metrics_recorder


def _analyze_local_args(sample_repo, logs):
    return [
        "analyze-local",
        "--repo",
        sample_repo["repo"],
        "--job",
        "services/fx-code-unittest",
        "--build",
        "5061",
        "--branch",
        "dev",
        "--base-commit",
        sample_repo["base"],
        "--head-commit",
        sample_repo["head"],
        "--console-file",
        str(logs["auth_failed"]),
        "--build-url",
        "local://services/fx-code-unittest/5061",
    ]


def test_metrics_disabled_does_not_generate_file(monkeypatch, tmp_path: Path, repo_cache: Path, sample_repo, logs):
    metrics_file = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(repo_cache))
    monkeypatch.setenv("CI_AGENT_METRICS_ENABLED", "false")
    monkeypatch.setenv("CI_AGENT_METRICS_FILE", str(metrics_file))

    code = main(_analyze_local_args(sample_repo, logs))

    assert code == 0
    assert not metrics_file.exists()


def test_metrics_enabled_analyze_local_writes_jsonl(monkeypatch, tmp_path: Path, repo_cache: Path, sample_repo, logs):
    metrics_file = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(repo_cache))
    monkeypatch.setenv("CI_AGENT_METRICS_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_METRICS_FILE", str(metrics_file))

    code = main(_analyze_local_args(sample_repo, logs))

    assert code == 0
    rows = [json.loads(line) for line in metrics_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    record = rows[0]
    assert record["job"] == "services/fx-code-unittest"
    assert record["buildNumber"] == 5061
    assert record["repo"] == sample_repo["repo"]
    assert record["command"] == "analyze-local"
    assert record["status"] == "ok"
    assert record["durationMs"] >= 0
    assert record["responsibilityItemCount"] >= 0
    assert {stage["name"] for stage in record["stages"]} >= {"gitSync", "gitDiff", "failureSummary", "historyPrecheck", "agentAnalyze", "saveHistory"}


def test_token_callback_extracts_response_metadata_usage():
    recorder = AnalysisMetricsRecorder.start(
        enabled=True,
        job="job",
        buildNumber=1,
        repo="repo",
        command="analyze-local",
        model_provider="openai",
        model_name="test-model",
    )
    handler = TokenUsageCallbackHandler(recorder)

    handler.on_llm_end({"generations": [[{"message": {"response_metadata": {"token_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}}}]]})

    assert recorder.llmCalls == 1
    assert recorder.inputTokens == 7
    assert recorder.outputTokens == 3
    assert recorder.totalTokens == 10


def test_llm_invoke_with_metrics_records_usage():
    recorder = AnalysisMetricsRecorder.start(enabled=True, job="job", buildNumber=1, repo="repo", command="analyze")

    class FakeModel:
        def invoke(self, prompt, **kwargs):
            return SimpleNamespace(content="ok", usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    with use_metrics_recorder(recorder):
        result = llm_invoke_with_metrics(FakeModel(), "prompt")

    assert result.content == "ok"
    assert recorder.llmCalls == 1
    assert recorder.inputTokens == 5
    assert recorder.outputTokens == 2
    assert recorder.totalTokens == 7


def test_token_callback_extracts_llmresult_generations_usage():
    recorder = AnalysisMetricsRecorder.start(enabled=True, job="job", buildNumber=1, repo="repo", command="analyze")
    handler = TokenUsageCallbackHandler(recorder)
    message = SimpleNamespace(usage_metadata={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})
    generation = SimpleNamespace(message=message)
    result = SimpleNamespace(generations=[[generation]])

    handler.on_llm_end(result)

    assert recorder.llmCalls == 1
    assert recorder.inputTokens == 11
    assert recorder.outputTokens == 4
    assert recorder.totalTokens == 15


def test_failure_fact_ai_uses_metrics_callback(monkeypatch, repo_cache: Path, sample_repo, logs):
    settings = replace(
        load_settings(),
        model_provider="openai",
        model_name="test-model",
        api_key="test-key",
        ai_failure_facts_enabled=True,
    )
    recorder = AnalysisMetricsRecorder.start(enabled=True, job="job", buildNumber=1, repo="repo", command="analyze")

    class FakeModel:
        def invoke(self, prompt, **kwargs):
            assert kwargs.get("config", {}).get("callbacks")
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "ok": True,
                        "facts": [
                            {
                                "schemaVersion": 1,
                                "signatureKey": "typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
                                "historyEligible": True,
                                "isGenericWrapper": False,
                                "failureKind": "typescript_compile_error",
                                "errorCode": "TS2305",
                                "message": "missing export",
                                "rootCauseSummary": "missing export",
                                "confidence": 0.9,
                            }
                        ],
                    }
                ),
                response_metadata={"token_usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14}},
            )

    monkeypatch.setattr("ci_owner_agent.services.failure_fact_ai.build_chat_model", lambda settings: FakeModel())

    with use_metrics_recorder(recorder):
        result = extract_failure_facts_with_ai(
            settings=settings,
            job="job",
            build_number=1,
            build_url="local://job/1",
            branch="dev",
            log_excerpt="src/index.ts(1,1): error TS2305",
            changed_files=[],
            commits=[],
        )

    assert result.ok is True
    assert recorder.llmCalls == 1
    assert recorder.totalTokens == 14


def test_failure_fact_compare_uses_metrics_callback(monkeypatch):
    settings = replace(
        load_settings(),
        model_provider="openai",
        model_name="test-model",
        api_key="test-key",
        ai_history_compare_enabled=True,
    )
    fact = FailureFact(
        signatureKey="typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
        historyEligible=True,
        failureKind="typescript_compile_error",
        errorCode="TS2305",
        message="missing export",
        rootCauseSummary="missing export",
        confidence=0.95,
    )
    recorder = AnalysisMetricsRecorder.start(enabled=True, job="job", buildNumber=1, repo="repo", command="analyze")

    class FakeModel:
        def invoke(self, prompt, **kwargs):
            assert kwargs.get("config", {}).get("callbacks")
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "sameFailure": True,
                        "confidence": 0.95,
                        "relationship": "same_root_cause",
                        "samePoints": ["same symbol"],
                        "differentPoints": [],
                        "reason": "same",
                    }
                ),
                response_metadata={"token_usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}},
            )

    monkeypatch.setattr("ci_owner_agent.services.failure_fact_compare_ai.build_chat_model", lambda settings: FakeModel())

    with use_metrics_recorder(recorder):
        result = compare_failure_facts_with_ai(settings=settings, current_fact=fact, historical_fact=fact)

    assert result.sameFailure is True
    assert recorder.llmCalls == 1
    assert recorder.totalTokens == 14


def test_repair_output_uses_metrics_callback(monkeypatch, repo_cache: Path, sample_repo, logs):
    settings = replace(load_settings(), model_provider="openai", model_name="test-model", api_key="test-key")
    context = AgentRuntimeContext(
        repo=sample_repo["repo"],
        job="job",
        build_number=1,
        build_url="local://job/1",
        result="FAILURE",
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        build_info=SimpleNamespace(logTail="", job="job", buildNumber=1),
        commits=[],
        changed_files=[],
        log_provider=SimpleNamespace(),
        git_client=GitClient(repo_cache),
        settings=settings,
    )
    agent = LangChainResponsibilityAgent(settings, context, tools=[])
    recorder = AnalysisMetricsRecorder.start(enabled=True, job="job", buildNumber=1, repo="repo", command="analyze")

    class FakeModel:
        def invoke(self, prompt, **kwargs):
            assert kwargs.get("config", {}).get("callbacks")
            return SimpleNamespace(content='{"ok": true}', usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})

    monkeypatch.setattr(agent, "_model", lambda: FakeModel())

    with use_metrics_recorder(recorder):
        repaired = agent._repair_output("bad json")

    assert repaired == '{"ok": true}'
    assert recorder.llmCalls == 1
    assert recorder.totalTokens == 5


def test_metrics_settings_have_jsonl_only(monkeypatch, tmp_path: Path):
    metrics_file = tmp_path / "metrics.jsonl"
    monkeypatch.setenv("CI_AGENT_METRICS_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_METRICS_FILE", str(metrics_file))

    settings = load_settings()

    assert settings.metrics_enabled is True
    assert settings.metrics_file == metrics_file
    assert not hasattr(settings, "metrics_mongo_enabled")


def test_metrics_write_failure_does_not_fail_analyze(monkeypatch, tmp_path: Path, repo_cache: Path, sample_repo, logs, capsys):
    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(repo_cache))
    monkeypatch.setenv("CI_AGENT_METRICS_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_METRICS_FILE", str(tmp_path / "metrics.jsonl"))

    def fail_append(self, path):
        raise OSError("disk full")

    monkeypatch.setattr("ci_owner_agent.services.metrics.AnalysisMetricsRecorder.append_jsonl", fail_append)

    code = main(_analyze_local_args(sample_repo, logs))

    captured = capsys.readouterr()
    assert code == 0
    assert "metrics write failed" in captured.err
