from __future__ import annotations

import json
from pathlib import Path

from ci_owner_agent.main import main
from ci_owner_agent.services.metrics import AnalysisMetricsRecorder, TokenUsageCallbackHandler


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
