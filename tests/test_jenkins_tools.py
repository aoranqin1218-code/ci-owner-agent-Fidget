from __future__ import annotations

import json
from pathlib import Path

from ci_owner_agent.main import main
from ci_owner_agent.orchestrator import analyze_jenkins
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.jenkins_client import JenkinsClient
from ci_owner_agent.services.log_provider import JenkinsLogProvider
from ci_owner_agent.tools.jenkins_tools import (
    jenkins_get_build_info,
    jenkins_get_last_successful_build_info,
    jenkins_get_latest_build_info,
)


class FakeResponse:
    def __init__(self, payload=None, text: str = "", status_code: int = 200) -> None:
        self.payload = payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, routes: dict[str, FakeResponse]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def get(self, url, auth=None, timeout=None):
        self.requested.append(url)
        response = self.routes.get(url)
        if response is None:
            raise RuntimeError(f"unexpected URL: {url}")
        return response


def jenkins_url(job: str, suffix: str) -> str:
    return f"http://jenkins.test/job/{'/job/'.join(job.split('/'))}/{suffix}"


def build_payload(number: int, result: str, commit: str | None, branch: str = "dev") -> dict:
    actions = [{"parameters": [{"name": "BRANCH", "value": branch}]}]
    if commit:
        actions.append({"lastBuiltRevision": {"SHA1": commit}})
    return {
        "number": number,
        "result": result,
        "url": f"http://jenkins.test/build/{number}/",
        "timestamp": 1700000000000,
        "duration": 1234,
        "actions": actions,
        "changeSet": {"items": []},
    }


def client_for(routes: dict[str, FakeResponse]) -> JenkinsClient:
    return JenkinsClient("http://jenkins.test", user="u", token="t", session=FakeSession(routes))


def test_jenkins_client_and_tools_extract_build_info(sample_repo):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "5061/api/json"): FakeResponse(build_payload(5061, "FAILURE", sample_repo["head"])),
        jenkins_url(job, "5061/consoleText"): FakeResponse(
            text="running\nAssertionError FILE_SIZE_EXCEEDED\nFinished: FAILURE\n"
        ),
        jenkins_url(job, "lastBuild/api/json"): FakeResponse(build_payload(5061, "FAILURE", sample_repo["head"])),
        jenkins_url(job, "lastBuild/consoleText"): FakeResponse(text="Finished: FAILURE\n"),
        jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(
            build_payload(5060, "SUCCESS", sample_repo["base"])
        ),
    }
    client = client_for(routes)
    build = jenkins_get_build_info(client, job, 5061, 2)
    assert build["ok"] is True
    assert build["buildInfo"]["commit"] == sample_repo["head"]
    assert build["buildInfo"]["logTail"]["startLine"] == 2

    latest = jenkins_get_latest_build_info(client, job, 1)
    assert latest["ok"] is True
    assert latest["buildInfo"]["buildNumber"] == 5061

    last_success = jenkins_get_last_successful_build_info(client, job, "dev", 5061)
    assert last_success["ok"] is True
    assert last_success["successfulBuildInfo"]["commit"] == sample_repo["base"]


def test_jenkins_client_warns_when_commit_missing():
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "5061/api/json"): FakeResponse(build_payload(5061, "FAILURE", None)),
        jenkins_url(job, "5061/consoleText"): FakeResponse(text="Finished: FAILURE\n"),
    }
    result = client_for(routes).get_build_info(job, 5061)
    assert result["ok"] is True
    assert result["buildInfo"]["commit"] is None
    assert any("commit not found" in item for item in result["buildInfo"]["warnings"])


def test_jenkins_log_provider_methods(sample_repo):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "5061/consoleText"): FakeResponse(
            text="line1\nError: bad\nat packages/fxp-ai/errors/classify.ts:1\nFinished: FAILURE\n"
        ),
    }
    provider = JenkinsLogProvider(client_for(routes), job, 5061, max_output_chars=1000)
    assert provider.read_tail(2).content.endswith("Finished: FAILURE")
    assert provider.search("classify", 1, 1)["matches"][0]["line"] == 3
    assert "Error" in provider.read_range(2, 2)["content"]
    assert provider.find_error_chunks(2, 1)["chunks"]
    assert provider.detect_final_status() == "FAILURE"


def test_long_jenkins_console_keeps_tail_for_status_and_tail():
    job = "services/fx-code-unittest"
    long_head = "\n".join(f"noise line {idx}" for idx in range(2000))
    console = f"{long_head}\nAssertionError near end\nFinished: FAILURE\n"
    routes = {
        jenkins_url(job, "5061/api/json"): FakeResponse(build_payload(5061, "FAILURE", None)),
        jenkins_url(job, "5061/consoleText"): FakeResponse(text=console),
    }
    client = JenkinsClient(
        "http://jenkins.test",
        session=FakeSession(routes),
        max_output_chars=200,
    )
    result = client.get_build_info(job, 5061, log_tail_lines=3)
    assert result["ok"] is True
    assert result["buildInfo"]["result"] == "FAILURE"
    assert result["buildInfo"]["logTail"]["content"].endswith("Finished: FAILURE")

    provider = JenkinsLogProvider(client, job, 5061, max_output_chars=200)
    assert provider.detect_final_status() == "FAILURE"
    assert provider.search("AssertionError", 1, 1)["matches"]
    assert provider.find_error_chunks(5, 1)["chunks"]


def test_last_successful_build_extracts_commit_from_console(sample_repo):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(build_payload(5060, "SUCCESS", None)),
        jenkins_url(job, "5060/consoleText"): FakeResponse(text=f"Checked out revision {sample_repo['base']}\n"),
    }
    result = client_for(routes).get_last_successful_build_info(job, branch="dev", before_build_number=5061)
    assert result["ok"] is True
    assert result["successfulBuildInfo"]["commit"] == sample_repo["base"]


def test_analyze_jenkins_success_short_circuits(repo_cache: Path, sample_repo, monkeypatch):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "5060/api/json"): FakeResponse(build_payload(5060, "SUCCESS", sample_repo["head"])),
        jenkins_url(job, "5060/consoleText"): FakeResponse(text="ERROR recovered\nFinished: SUCCESS\n"),
    }
    client = client_for(routes)
    notice = analyze_jenkins(sample_repo["repo"], job, 5060, client, GitClient(repo_cache))
    assert notice.result == "SUCCESS"
    assert notice.owner.type == "no_high_confidence_owner"


def test_analyze_jenkins_aborted_short_circuits(repo_cache: Path, sample_repo):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "5090/api/json"): FakeResponse(build_payload(5090, "ABORTED", sample_repo["head"])),
        jenkins_url(job, "5090/consoleText"): FakeResponse(text="Missing context\nFinished: ABORTED\n"),
    }
    notice = analyze_jenkins(sample_repo["repo"], job, 5090, client_for(routes), GitClient(repo_cache))
    assert notice.result == "ABORTED"
    assert "不进入普通业务代码定责流程" in notice.failureReason


def test_analyze_jenkins_missing_commit_returns_no_owner(repo_cache: Path, sample_repo):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "5061/api/json"): FakeResponse(build_payload(5061, "FAILURE", None)),
        jenkins_url(job, "5061/consoleText"): FakeResponse(text="AssertionError\nFinished: FAILURE\n"),
        jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(
            build_payload(5060, "SUCCESS", sample_repo["base"])
        ),
    }
    notice = analyze_jenkins(sample_repo["repo"], job, 5061, client_for(routes), GitClient(repo_cache))
    assert notice.owner.type == "no_high_confidence_owner"
    assert "缺少 headCommit 或 baseCommit" in notice.failureReason


def test_analyze_jenkins_failure_runs_fake_no_owner_agent(repo_cache: Path, sample_repo, monkeypatch):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "5061/api/json"): FakeResponse(build_payload(5061, "FAILURE", sample_repo["head"])),
        jenkins_url(job, "5061/consoleText"): FakeResponse(
            text=(
                "Running tests\n"
                "AssertionError: FILE_SIZE_EXCEEDED expected limit 10MB\n"
                "at packages/fxp-ai/errors/classify.ts:2:10\n"
                "Finished: FAILURE\n"
            )
        ),
        jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(
            build_payload(5060, "SUCCESS", sample_repo["base"])
        ),
    }
    git_client = GitClient(repo_cache)
    monkeypatch.setattr(git_client, "sync", lambda repo: {"ok": True})
    notice = analyze_jenkins(sample_repo["repo"], job, 5061, client_for(routes), git_client)
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.hasHighConfidenceOwner is False
    assert "fake provider" in notice.failureReason


def test_cli_analyze_uses_jenkins_mode(repo_cache: Path, sample_repo, monkeypatch, capsys):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "5060/api/json"): FakeResponse(build_payload(5060, "SUCCESS", sample_repo["head"])),
        jenkins_url(job, "5060/consoleText"): FakeResponse(text="Finished: SUCCESS\n"),
    }
    fake_client = client_for(routes)

    class FakeJenkinsClient:
        def __new__(cls, *args, **kwargs):
            return fake_client

    monkeypatch.setenv("JENKINS_URL", "http://jenkins.test")
    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(repo_cache))
    monkeypatch.setattr("ci_owner_agent.main.JenkinsClient", FakeJenkinsClient)
    code = main(["analyze", "--job", job, "--build", "5060", "--repo", sample_repo["repo"]])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "SUCCESS"


def test_cli_analyze_without_jenkins_url_returns_json(repo_cache: Path, sample_repo, monkeypatch, capsys):
    monkeypatch.setenv("JENKINS_URL", "")
    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(repo_cache))
    code = main(["analyze", "--job", "services/fx-code-unittest", "--build", "5061", "--repo", sample_repo["repo"]])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["owner"]["type"] == "no_high_confidence_owner"
    assert "JENKINS_URL" in payload["failureReason"]
