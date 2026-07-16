from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from ci_owner_agent.schemas import BuildInfo, LogTail, SuccessfulBuildInfo
from ci_owner_agent.services.branch_normalization import normalize_branch_name
from ci_owner_agent.services.command_runner import truncate_tail_text, truncate_text
from ci_owner_agent.services.log_provider import log_detect_final_status


COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)


class JenkinsClient:
    def __init__(
        self,
        base_url: str,
        user: str | None = None,
        token: str | None = None,
        timeout: int = 20,
        max_output_chars: int = 20000,
        session: requests.Session | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("JENKINS_URL is required for analyze mode")
        self.base_url = base_url.rstrip("/")
        self.auth = (user, token) if user and token else None
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self.session = session or requests.Session()

    def _job_path(self, job: str) -> str:
        parts = [quote(part, safe="") for part in job.strip("/").split("/") if part]
        return "/job/".join(parts)

    def _url(self, job: str, suffix: str) -> str:
        return f"{self.base_url}/job/{self._job_path(job)}/{suffix.lstrip('/')}"

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self.session.get(url, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            return {"ok": True, "data": response.json()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _get_text(self, url: str) -> dict[str, Any]:
        try:
            response = self.session.get(url, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            text, truncated = truncate_tail_text(response.text, self.max_output_chars * 5)
            return {"ok": True, "content": text, "truncated": truncated}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "content": ""}

    def get_console_text(self, job: str, build_number: int) -> dict:
        return self._get_text(self._url(job, f"{build_number}/consoleText"))

    def get_build_json(self, job: str, build_number: int | str) -> dict:
        return self._get_json(self._url(job, f"{build_number}/api/json"))

    def get_build_info(self, job: str, build_number: int, log_tail_lines: int = 500) -> dict:
        json_result = self.get_build_json(job, build_number)
        if not json_result.get("ok"):
            return {"ok": False, "error": json_result.get("error")}
        data = json_result["data"]
        console = self.get_console_text(job, build_number)
        console_text = console.get("content", "")
        result = (data.get("result") or log_detect_final_status(console_text) or "UNKNOWN").upper()
        warnings: list[str] = []
        if console.get("truncated"):
            warnings.append("console text was truncated while reading from Jenkins")
        log_tail = self._tail_from_text(console_text, log_tail_lines)
        commit = self._extract_commit(data, console_text)
        if not commit:
            warnings.append("commit not found from Jenkins build metadata or console log")
        branch = self._extract_branch(data)
        if branch is None and self._has_branch_metadata(data):
            warnings.append("could not determine a unique logical branch from Jenkins build metadata")
        build_info = BuildInfo(
            job=job,
            buildNumber=int(data.get("number") or build_number),
            result=result,
            buildUrl=data.get("url") or self._url(job, f"{build_number}/"),
            branch=branch,
            commit=commit,
            timestamp=self._timestamp(data.get("timestamp")),
            durationMs=data.get("duration"),
            logTail=log_tail,
            warnings=warnings,
        )
        return {"ok": True, "buildInfo": build_info.model_dump()}

    def get_latest_build_info(self, job: str, log_tail_lines: int = 500) -> dict:
        result = self.get_build_json(job, "lastBuild")
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error")}
        number = result["data"].get("number")
        if number is None:
            return {"ok": False, "error": "latest build number not found"}
        return self.get_build_info(job, int(number), log_tail_lines)

    def get_last_successful_build_info(
        self,
        job: str,
        branch: str | None = None,
        before_build_number: int | None = None,
        scan_limit: int = 100,
    ) -> dict:
        current_branch = normalize_branch_name(branch)
        if current_branch is None:
            return {
                "ok": False,
                "error": "current build branch could not be determined; refusing to select a Git diff baseline",
                "scannedBuildCount": 0,
                "candidateRejectedReasons": [],
            }
        if before_build_number is None:
            return {
                "ok": False,
                "error": "current build number is required; refusing to select a Git diff baseline",
                "scannedBuildCount": 0,
                "candidateRejectedReasons": [],
            }
        if not isinstance(before_build_number, int) or isinstance(before_build_number, bool) or before_build_number <= 0:
            return {
                "ok": False,
                "error": "current build number must be a positive integer",
                "scannedBuildCount": 0,
                "candidateRejectedReasons": [],
            }
        rejected: list[str] = []
        scanned = 0
        seen_numbers: set[int] = set()

        def load_candidate(data: dict[str, Any], source: str) -> SuccessfulBuildInfo | None:
            nonlocal scanned
            number = data.get("number")
            try:
                number = int(number)
            except (TypeError, ValueError):
                rejected.append(f"{source}: missing build number")
                return None
            if number in seen_numbers:
                return None
            seen_numbers.add(number)
            scanned += 1
            branch_name = self._extract_branch(data)
            reasons: list[str] = []
            if before_build_number is not None and number >= before_build_number:
                reasons.append("not before current build")
            result = (data.get("result") or "UNKNOWN").upper()
            if result != "SUCCESS":
                reasons.append(f"result={result}")
            if branch_name != current_branch:
                reasons.append(f"branch={branch_name!r} does not match {current_branch!r}")
            if reasons:
                rejected.append(f"{source}: build {number} rejected: {', '.join(reasons)}")
                return None
            commit = self._extract_commit(data, "")
            if not commit:
                console = self.get_console_text(job, number)
                if not console.get("ok"):
                    rejected.append(f"{source}: build {number} console unreadable: {console.get('error')}")
                    return None
                commit = self._extract_commit(data, console.get("content", ""))
            if not commit:
                rejected.append(f"{source}: build {number} rejected: missing commit")
                return None
            return SuccessfulBuildInfo(
                buildNumber=number,
                result=result,
                commit=commit,
                buildUrl=data.get("url") or self._url(job, f"{number}/"),
                branch=branch_name,
            )

        fast = self.get_build_json(job, "lastSuccessfulBuild")
        if fast.get("ok") and fast["data"].get("number") is not None:
            found = load_candidate(fast["data"], "lastSuccessfulBuild")
            if found:
                return {"ok": True, "successfulBuildInfo": found.model_dump(), "scannedBuildCount": scanned, "candidateRejectedReasons": rejected}
        elif not fast.get("ok"):
            rejected.append(f"lastSuccessfulBuild unreadable: {fast.get('error')}")
        for number in range(before_build_number - 1, max(0, before_build_number - max(1, scan_limit)) - 1, -1):
            if number in seen_numbers:
                continue
            result = self.get_build_json(job, number)
            if not result.get("ok"):
                rejected.append(f"history: build {number} unreadable: {result.get('error')}")
                continue
            found = load_candidate(result["data"], "history")
            if found:
                return {"ok": True, "successfulBuildInfo": found.model_dump(), "scannedBuildCount": scanned, "candidateRejectedReasons": rejected}
        return {"ok": False, "error": "no earlier successful build with a valid commit on the current branch", "scannedBuildCount": scanned, "candidateRejectedReasons": rejected}

    def _tail_from_text(self, text: str, lines: int) -> LogTail:
        all_lines = text.splitlines()
        count = max(0, lines)
        start = max(1, len(all_lines) - count + 1) if all_lines else 1
        selected = all_lines[-count:] if count else []
        content, _ = truncate_text("\n".join(selected), self.max_output_chars)
        return LogTail(startLine=start, endLine=len(all_lines), content=content)

    def _timestamp(self, value: Any) -> str | None:
        if value is None:
            return None
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return str(value)

    def _extract_branch(self, data: dict[str, Any]) -> str | None:
        parameter_candidates: set[str] = set()
        for action in data.get("actions") or []:
            for param in action.get("parameters") or []:
                name = str(param.get("name", "")).lower()
                if name in {"branch", "git_branch", "source_branch"}:
                    value = param.get("value")
                    normalized = normalize_branch_name(str(value) if value else None)
                    if normalized:
                        parameter_candidates.add(normalized)
        if len(parameter_candidates) == 1:
            return parameter_candidates.pop()
        if len(parameter_candidates) > 1:
            return None
        for action in data.get("actions") or []:
            builds_by_branch = action.get("buildsByBranchName")
            if isinstance(builds_by_branch, dict) and builds_by_branch:
                candidates = {normalize_branch_name(str(key)) for key in builds_by_branch}
                candidates.discard(None)
                if len(candidates) == 1:
                    return candidates.pop()
        return None

    def _has_branch_metadata(self, data: dict[str, Any]) -> bool:
        for action in data.get("actions") or []:
            if isinstance(action.get("buildsByBranchName"), dict):
                return True
            for param in action.get("parameters") or []:
                if str(param.get("name", "")).lower() in {"branch", "git_branch", "source_branch"}:
                    return True
        return False

    def _extract_commit(self, data: dict[str, Any], console_text: str) -> str | None:
        candidates: list[str] = []
        for action in data.get("actions") or []:
            last_built = action.get("lastBuiltRevision") or {}
            sha1 = last_built.get("SHA1")
            if sha1:
                candidates.append(str(sha1))
            for branch in last_built.get("branch") or []:
                sha1 = branch.get("SHA1")
                if sha1:
                    candidates.append(str(sha1))
            for param in action.get("parameters") or []:
                name = str(param.get("name", "")).lower()
                if "commit" in name or name in {"git_revision", "git_commit", "sha", "sha1"}:
                    value = param.get("value")
                    if value:
                        candidates.append(str(value))
        change_set = data.get("changeSet") or {}
        for item in change_set.get("items") or []:
            for key in ("commitId", "id"):
                value = item.get(key)
                if value:
                    candidates.append(str(value))
        candidates.extend(match.group(0) for match in COMMIT_RE.finditer(console_text))
        for candidate in candidates:
            match = COMMIT_RE.search(candidate)
            if match:
                return match.group(0)
        return None
