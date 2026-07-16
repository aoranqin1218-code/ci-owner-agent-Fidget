from __future__ import annotations

from ci_owner_agent.services.jenkins_client import JenkinsClient


def jenkins_get_build_info(client: JenkinsClient, job: str, buildNumber: int, logTailLines: int = 500) -> dict:
    return client.get_build_info(job, buildNumber, logTailLines)


def jenkins_get_latest_build_info(client: JenkinsClient, job: str, logTailLines: int = 500) -> dict:
    return client.get_latest_build_info(job, logTailLines)


def jenkins_get_last_successful_build_info(
    client: JenkinsClient,
    job: str,
    branch: str | None = None,
    beforeBuildNumber: int | None = None,
    scanLimit: int = 100,
) -> dict:
    return client.get_last_successful_build_info(job, branch, beforeBuildNumber, scanLimit)
