from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_jenkins_branch_extraction_normalizes_and_rejects_ambiguous_candidates():
    job = "services/fx-code-unittest"
    payload = build_payload(1, "FAILURE", "abc1234", branch="refs/heads/feature/a")
    assert client_for({jenkins_url(job, "1/api/json"): FakeResponse(payload), jenkins_url(job, "1/consoleText"): FakeResponse()}).get_build_info(job, 1)["buildInfo"]["branch"] == "feature/a"
    payload["actions"] = [{"buildsByBranchName": {"refs/remotes/origin/dev": {}, "refs/remotes/origin/release": {}}}]
    result = client_for({jenkins_url(job, "1/api/json"): FakeResponse(payload), jenkins_url(job, "1/consoleText"): FakeResponse()}).get_build_info(job, 1)
    assert result["buildInfo"]["branch"] is None
    assert any("unique logical branch" in warning for warning in result["buildInfo"]["warnings"])


@pytest.mark.parametrize("parameters", [
    [{"name": "BRANCH", "value": "dev"}, {"name": "GIT_BRANCH", "value": "release"}],
    [{"name": "GIT_BRANCH", "value": "release"}, {"name": "BRANCH", "value": "dev"}],
])
def test_jenkins_conflicting_branch_parameters_fail_closed(parameters):
    job = "services/fx-code-unittest"
    payload = build_payload(1, "FAILURE", "abc1234")
    payload["actions"] = [{"parameters": parameters}, {"buildsByBranchName": {"refs/remotes/origin/dev": {}}}]
    result = client_for({jenkins_url(job, "1/api/json"): FakeResponse(payload), jenkins_url(job, "1/consoleText"): FakeResponse()}).get_build_info(job, 1)
    assert result["buildInfo"]["branch"] is None
    assert any("unique logical branch" in warning for warning in result["buildInfo"]["warnings"])


def test_jenkins_branch_parameters_normalize_and_deduplicate_across_actions():
    job = "services/fx-code-unittest"
    payload = build_payload(1, "FAILURE", "abc1234")
    payload["actions"] = [
        {"parameters": [{"name": "BRANCH", "value": "dev"}]},
        {"parameters": [{"name": "GIT_BRANCH", "value": "refs/remotes/origin/dev"}, {"name": "SOURCE_BRANCH", "value": "refs/heads/dev"}]},
    ]
    result = client_for({jenkins_url(job, "1/api/json"): FakeResponse(payload), jenkins_url(job, "1/consoleText"): FakeResponse()}).get_build_info(job, 1)
    assert result["buildInfo"]["branch"] == "dev"


def test_last_successful_build_scans_past_wrong_branch(sample_repo):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(build_payload(10, "SUCCESS", sample_repo["base"], "release")),
        jenkins_url(job, "10/api/json"): FakeResponse(build_payload(10, "SUCCESS", sample_repo["base"], "release")),
        jenkins_url(job, "10/consoleText"): FakeResponse(),
        jenkins_url(job, "9/api/json"): FakeResponse(build_payload(9, "SUCCESS", sample_repo["base"], "refs/remotes/origin/dev")),
        jenkins_url(job, "9/consoleText"): FakeResponse(),
    }
    result = client_for(routes).get_last_successful_build_info(job, branch="dev", before_build_number=11)
    assert result["ok"] is True
    assert result["successfulBuildInfo"]["buildNumber"] == 9
    assert result["successfulBuildInfo"]["branch"] == "dev"


def test_last_successful_requires_current_branch_without_requests():
    client = client_for({})
    result = client.get_last_successful_build_info("job", branch=None, before_build_number=10)
    assert result["ok"] is False
    assert result["scannedBuildCount"] == 0
    assert client.session.requested == []


@pytest.mark.parametrize("before_build_number", [None, 0, -1])
def test_last_successful_requires_positive_current_build_number_before_requests(before_build_number, sample_repo):
    job = "services/fx-code-unittest"
    client = client_for({jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(build_payload(10, "SUCCESS", sample_repo["base"], "dev"))})
    result = client.get_last_successful_build_info(job, "dev", before_build_number)
    assert result["ok"] is False
    assert result["scannedBuildCount"] == 0
    assert result["candidateRejectedReasons"] == []
    assert client.session.requested == []


def test_last_successful_candidate_console_is_loaded_only_when_needed(sample_repo):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(build_payload(10, "SUCCESS", sample_repo["base"], "release")),
        jenkins_url(job, "9/api/json"): FakeResponse(build_payload(9, "SUCCESS", sample_repo["base"], "dev")),
    }
    client = client_for(routes)
    result = client.get_last_successful_build_info(job, "dev", 11)
    assert result["ok"] is True
    assert all("consoleText" not in url for url in client.session.requested)
    assert jenkins_url(job, "10/api/json") not in client.session.requested


def test_last_successful_uses_console_only_for_matching_missing_commit(sample_repo):
    job = "services/fx-code-unittest"
    routes = {
        jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(build_payload(9, "SUCCESS", None, "dev")),
        jenkins_url(job, "9/consoleText"): FakeResponse(text=f"Checked out revision {sample_repo['base']}"),
    }
    client = client_for(routes)
    result = client.get_last_successful_build_info(job, "dev", 10)
    assert result["successfulBuildInfo"]["commit"] == sample_repo["base"]
    assert client.session.requested == [jenkins_url(job, "lastSuccessfulBuild/api/json"), jenkins_url(job, "9/consoleText")]


def test_last_successful_rejects_unknown_candidate_branch_and_scans(sample_repo):
    job = "services/fx-code-unittest"
    unknown = build_payload(9, "SUCCESS", sample_repo["base"], "dev")
    unknown["actions"] = []
    routes = {
        jenkins_url(job, "lastSuccessfulBuild/api/json"): FakeResponse(unknown),
        jenkins_url(job, "8/api/json"): FakeResponse(build_payload(8, "SUCCESS", sample_repo["base"], "refs/remotes/origin/dev")),
    }
    result = client_for(routes).get_last_successful_build_info(job, "dev", 10)
    assert result["ok"] is True
    assert result["successfulBuildInfo"]["buildNumber"] == 8


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


def test_analyze_jenkins_stops_before_baseline_when_current_branch_is_ambiguous(sample_repo):
    job = "services/fx-code-unittest"
    payload = build_payload(11, "FAILURE", sample_repo["head"])
    payload["actions"] = [{"buildsByBranchName": {"refs/remotes/origin/dev": {}, "refs/remotes/origin/release": {}}}, {"lastBuiltRevision": {"SHA1": sample_repo["head"]}}]
    client = client_for({jenkins_url(job, "11/api/json"): FakeResponse(payload), jenkins_url(job, "11/consoleText"): FakeResponse(text="Finished: FAILURE")})

    class NoGit:
        def __getattr__(self, name):
            raise AssertionError(f"Git must not be called: {name}")

    notice = analyze_jenkins(sample_repo["repo"], job, 11, client, NoGit())
    assert notice.owner.type == "no_high_confidence_owner"
    assert "分支无法确认" in notice.failureReason
    assert all("lastSuccessfulBuild" not in url for url in client.session.requested)


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
