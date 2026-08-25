from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from ci_owner_agent.schemas import BuildInfo, LogTail, SuccessfulBuildInfo
from ci_owner_agent.services.branch_normalization import normalize_branch_name
from ci_owner_agent.services.command_runner import truncate_tail_text, truncate_text
from ci_owner_agent.services.log_parsing import (
    CheckoutCommitResolution,
    log_detect_final_status,
    resolve_checkout_revision_from_console_log,
)


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

    def _get_text(self, url: str, *, retain_full_content: bool = False) -> dict[str, Any]:
        try:
            response = self.session.get(url, auth=self.auth, timeout=self.timeout)
            response.raise_for_status()
            full_text = response.text
            resolution = resolve_checkout_revision_from_console_log(full_text)
            text, truncated = truncate_tail_text(full_text, self.max_output_chars * 5)
            result = {
                "ok": True,
                "content": text,
                "truncated": truncated,
                "checkoutCommit": resolution.commit,
                "checkoutCommitAmbiguous": resolution.ambiguous,
                "checkoutRefs": list(resolution.refs),
                "checkoutCommitFromExplicitStage": resolution.from_explicit_checkout_stage,
            }
            # ``content`` remains bounded for regular callers and Agent tool output.
            # JenkinsLogProvider asks explicitly for the full text so it can scan the
            # complete console for structured Japa/c8 facts.  Keeping only the tail
            # here can discard the actual test failure while retaining its Docker
            # footer, which makes historical failure matching unsafe.
            if retain_full_content:
                result["analysisContent"] = full_text
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc), "content": ""}

    def get_console_text(self, job: str, build_number: int) -> dict:
        return self._get_text(self._url(job, f"{build_number}/consoleText"))

    def get_console_text_for_analysis(self, job: str, build_number: int) -> dict:
        """Fetch a console with full text for bounded LogProvider operations.

        The HTTP response is already read in full to obtain checkout evidence.
        This method keeps that text only inside the log-provider boundary; every
        public Agent-facing operation still applies its own output limit.
        """
        return self._get_text(
            self._url(job, f"{build_number}/consoleText"),
            retain_full_content=True,
        )

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
        metadata = self._resolve_checkout_commit_from_metadata(data)
        console_resolution = CheckoutCommitResolution(
            console.get("checkoutCommit"),
            bool(console.get("checkoutCommitAmbiguous")),
            tuple(str(ref) for ref in console.get("checkoutRefs") or ()),
            bool(console.get("checkoutCommitFromExplicitStage")),
        )
        commit, commit_error = self._merge_checkout_commit_resolutions(metadata, console_resolution)
        if commit_error:
            warnings.append(commit_error)
        branch = self._extract_branch(data)
        if console_resolution.from_explicit_checkout_stage and console_resolution.commit:
            console_branches = {
                normalized
                for ref in console_resolution.refs
                if (normalized := normalize_branch_name(ref)) is not None
            }
            if len(console_branches) == 1:
                branch = console_branches.pop()
            elif len(console_branches) > 1:
                warnings.append("could not determine a unique logical branch from trusted checkout refs")
        elif branch is None and not self._has_branch_metadata(data) and console_resolution.commit:
            console_branches = {
                normalized
                for ref in console_resolution.refs
                if (normalized := normalize_branch_name(ref)) is not None
            }
            if len(console_branches) == 1:
                branch = console_branches.pop()
            elif len(console_branches) > 1:
                warnings.append("could not determine a unique logical branch from trusted checkout refs")
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
            reasons: list[str] = []
            if before_build_number is not None and number >= before_build_number:
                reasons.append("not before current build")
            result = (data.get("result") or "UNKNOWN").upper()
            if result != "SUCCESS":
                reasons.append(f"result={result}")
            if reasons:
                rejected.append(f"{source}: build {number} rejected: {', '.join(reasons)}")
                return None

            branch_name = self._extract_branch(data)
            metadata = self._resolve_checkout_commit_from_metadata(data)
            commit = metadata.commit
            if branch_name is None or metadata.ambiguous or not commit:
                console = self.get_console_text(job, number)
                if not console.get("ok"):
                    rejected.append(f"{source}: build {number} console unreadable: {console.get('error')}")
                    return None
                console_resolution = CheckoutCommitResolution(
                    console.get("checkoutCommit"),
                    bool(console.get("checkoutCommitAmbiguous")),
                    tuple(str(ref) for ref in console.get("checkoutRefs") or ()),
                    bool(console.get("checkoutCommitFromExplicitStage")),
                )
                commit, commit_error = self._merge_checkout_commit_resolutions(metadata, console_resolution)
                if commit_error:
                    rejected.append(f"{source}: build {number} rejected: {commit_error}")
                    return None
                if console_resolution.from_explicit_checkout_stage or (
                    branch_name is None and not self._has_branch_metadata(data)
                ):
                    console_branches = {
                        normalized
                        for ref in console_resolution.refs
                        if (normalized := normalize_branch_name(ref)) is not None
                    }
                    if len(console_branches) == 1:
                        branch_name = console_branches.pop()
                    elif len(console_branches) > 1:
                        rejected.append(f"{source}: build {number} rejected: ambiguous trusted checkout branch")
                        return None
            if branch_name != current_branch:
                rejected.append(f"{source}: build {number} rejected: branch={branch_name!r} does not match {current_branch!r}")
                return None
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
        candidates: set[str] = set()
        for action in data.get("actions") or []:
            builds_by_branch = action.get("buildsByBranchName")
            if isinstance(builds_by_branch, dict):
                candidates.update(branch for key in builds_by_branch if (branch := normalize_branch_name(str(key))))
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

    def _resolve_checkout_commit_from_metadata(self, data: dict[str, Any]) -> CheckoutCommitResolution:
        candidates: set[str] = set()
        for action in data.get("actions") or []:
            last_built = action.get("lastBuiltRevision") or {}
            sha1 = last_built.get("SHA1")
            if isinstance(sha1, str) and len(sha1) == 40 and all(char in "0123456789abcdefABCDEF" for char in sha1):
                candidates.add(sha1.lower())
            for param in action.get("parameters") or []:
                if str(param.get("name", "")).lower() == "git_commit":
                    value = str(param.get("value") or "")
                    if len(value) == 40 and all(char in "0123456789abcdefABCDEF" for char in value):
                        candidates.add(value.lower())
        if len(candidates) == 1:
            return CheckoutCommitResolution(candidates.pop())
        return CheckoutCommitResolution(None, ambiguous=bool(candidates))

    def _merge_checkout_commit_resolutions(self, metadata: CheckoutCommitResolution, console: CheckoutCommitResolution) -> tuple[str | None, str | None]:
        if console.from_explicit_checkout_stage and console.commit and not console.ambiguous:
            return console.commit, None
        if metadata.ambiguous or console.ambiguous or (metadata.commit and console.commit and metadata.commit != console.commit):
            return None, "trusted checkout commit metadata is ambiguous or conflicting"
        commit = metadata.commit or console.commit
        return (commit, None) if commit else (None, "trusted checkout commit not found")
