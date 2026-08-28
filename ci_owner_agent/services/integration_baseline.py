"""Trusted Git baseline resolution for Fidget integration assertion failures.

The resolver intentionally limits *where* an investigation starts.  It never
selects a causal owner, and every caller must retain the normal diff and
``base -> owner -> head`` guards.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ci_owner_agent.schemas import BuildInfo
from ci_owner_agent.services.git_client import GitClient


FIDGET_SDK_PACKAGE_PATH = "packages/fidget-sdk/package.json"
# The current Fidget Integration runner has one approved, non-secret test
# profile.  This is an identity label only; it is deliberately not a URI,
# hostname, user name, or feature switch.
FIDGET_INTEGRATION_ENVIRONMENT_PROFILE = "fidget-mongo42-protonbase-test"
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$"
)


@dataclass(frozen=True)
class IntegrationBaselineResolution:
    """Result of selecting an integration investigation baseline."""

    ok: bool
    baseline_commit: str | None
    baseline_type: str | None
    current_version: str | None = None
    target_version: str | None = None
    suites: tuple[str, ...] = ()
    candidate_sources: tuple[str, ...] = ()
    source_build_number: int | None = None
    environment_profile: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "baselineCommit": self.baseline_commit,
            "baselineType": self.baseline_type,
            "currentVersion": self.current_version,
            "targetVersion": self.target_version,
            "suites": list(self.suites),
            "candidateSources": list(self.candidate_sources),
            "sourceBuildNumber": self.source_build_number,
            "environmentProfile": self.environment_profile,
            "reason": self.reason,
        }


def derive_previous_stable_version(current_version: str) -> str | None:
    """Return the previous stable minor release for a valid SDK version.

    Both stable and prerelease heads use the same release-line rule.  There is
    deliberately no time-window or commit-count fallback when a prior minor
    does not exist.
    """
    match = _SEMVER_RE.fullmatch(current_version.strip())
    if match is None:
        return None
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    if minor == 0:
        return None
    return f"{major}.{minor - 1}.0"


def trusted_integration_assertion_suites(failure_summaries: dict[str, Any] | None) -> tuple[str, ...]:
    """Return suites eligible for version baseline selection, or ``()``.

    Only a complete V1 protocol with assertion failures exclusively in the
    Integration Tests stage is eligible.  Unit/coverage/mixed runs preserve
    their existing overall-success baseline behavior instead of widening
    their investigation range incidentally.
    """
    if not isinstance(failure_summaries, dict):
        return ()
    if failure_summaries.get("integrationConflicts"):
        return ()
    if failure_summaries.get("coverageFiles") or any(
        isinstance(item, dict) and item.get("anchorType") == "coverage_failure_block"
        for item in (failure_summaries.get("chunks") or [])
    ):
        return ()
    classifications = failure_summaries.get("integrationClassifications") or []
    assertion_suites = {
        str(item.get("suite"))
        for item in classifications
        if isinstance(item, dict)
        and item.get("kind") == "assertion"
        and item.get("stage") == "Integration Tests"
        and isinstance(item.get("suite"), str)
        and item["suite"]
    }
    if not assertion_suites:
        return ()
    chunks = failure_summaries.get("chunks") or []
    japa_chunks = [
        item
        for item in chunks
        if isinstance(item, dict) and item.get("anchorType") == "japa_failure_block"
    ]
    if not japa_chunks or any(item.get("stageName") != "Integration Tests" for item in japa_chunks):
        return ()
    return tuple(sorted(assertion_suites))


def build_trusted_integration_run_facts(
    protocol_index: dict[str, Any] | None,
    build_info: BuildInfo,
) -> dict[str, Any] | None:
    """Build a persistable V1 suite fact only when the full protocol is trusted.

    The returned document intentionally contains only controlled marker values
    and suite exit codes.  It never carries the runner's ``mongo_target`` value
    or any connection details.
    """
    if not isinstance(protocol_index, dict):
        return None
    if str(build_info.result or "").upper() == "ABORTED":
        return None
    head = (build_info.commit or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        return None
    if not protocol_index.get("has_marker") or not protocol_index.get("is_integration"):
        return None
    if protocol_index.get("conflicts") or protocol_index.get("preflight_failures"):
        return None
    if protocol_index.get("cleanup_status") != "success":
        return None
    if not isinstance(protocol_index.get("summary"), dict):
        return None

    suites: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in protocol_index.get("suite_ranges") or []:
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        exit_value = raw.get("exit")
        if not isinstance(name, str) or not name or name in seen:
            return None
        if not re.fullmatch(r"-?\d+", str(exit_value)):
            return None
        seen.add(name)
        exit_code = int(str(exit_value))
        suites.append(
            {
                "name": name,
                "status": "passed" if exit_code == 0 else "failed",
                "exitCode": exit_code,
            }
        )
    if not suites:
        return None
    return {
        "protocol": "FIDGET_INTEGRATION_V1",
        "protocolValid": True,
        "environmentProfile": FIDGET_INTEGRATION_ENVIRONMENT_PROFILE,
        "cleanupStatus": "success",
        "suites": suites,
    }


def select_latest_trusted_suite_checkpoint(
    *,
    git_client: GitClient,
    repo: str,
    head_commit: str,
    suite: str,
    checkpoint_candidates: list[dict[str, Any]],
    current_build_number: int,
    environment_profile: str = FIDGET_INTEGRATION_ENVIRONMENT_PROFILE,
) -> IntegrationBaselineResolution | None:
    """Choose the newest persisted passing suite whose commit is a head ancestor.

    Candidate order is build-number descending, but the order is only an
    optimisation: each candidate still has to satisfy the immutable protocol
    facts and the Git ancestry guard in the Agent-only cache.
    """
    for candidate in checkpoint_candidates:
        if not _is_trusted_suite_checkpoint(
            candidate,
            suite=suite,
            current_build_number=current_build_number,
            environment_profile=environment_profile,
        ):
            continue
        commit = str(candidate.get("headCommit") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit) or commit == head_commit:
            continue
        ancestry = git_client.check_ancestor(repo, commit, head_commit)
        if not ancestry.get("ok"):
            return _rejected(
                reason=f"cannot verify suite checkpoint ancestry {commit[:8]}: {ancestry.get('error')}",
                suites=(suite,),
            )
        if not ancestry.get("isAncestor"):
            continue
        return IntegrationBaselineResolution(
            ok=True,
            baseline_commit=commit,
            baseline_type="suite_checkpoint",
            suites=(suite,),
            candidate_sources=("ci_builds.integrationRun",),
            source_build_number=int(candidate["buildNumber"]),
            environment_profile=environment_profile,
            reason=(
                f"suite {suite} uses trusted passing checkpoint from build "
                f"#{candidate['buildNumber']} at {commit[:8]}"
            ),
        )
    return None


def resolve_integration_baseline(
    *,
    git_client: GitClient,
    repo: str,
    head_commit: str,
    suite: str,
    checkpoint_candidates: list[dict[str, Any]],
    current_build_number: int,
) -> IntegrationBaselineResolution:
    """Prefer a suite checkpoint, then conservatively fall back to version.

    Missing MongoDB history is intentionally non-fatal: version resolution
    remains available.  A malformed or non-ancestor persisted record merely
    cannot serve as a checkpoint.
    """
    checkpoint = select_latest_trusted_suite_checkpoint(
        git_client=git_client,
        repo=repo,
        head_commit=head_commit,
        suite=suite,
        checkpoint_candidates=checkpoint_candidates,
        current_build_number=current_build_number,
    )
    if checkpoint is not None:
        return checkpoint
    return resolve_previous_version_checkpoint(
        git_client=git_client,
        repo=repo,
        head_commit=head_commit,
        suites=(suite,),
    )


def resolve_previous_version_checkpoint(
    *,
    git_client: GitClient,
    repo: str,
    head_commit: str,
    package_path: str = FIDGET_SDK_PACKAGE_PATH,
    suites: tuple[str, ...] = (),
) -> IntegrationBaselineResolution:
    """Resolve a trusted previous-minor checkpoint from the Agent Git cache.

    Candidates may come from a release tag or the SDK package history.  Every
    candidate must be a proper ancestor of ``head_commit`` and declare exactly
    the intended SDK version.  Multiple incomparable candidates are rejected.
    """
    current_version, read_error = _read_package_version(git_client, repo, head_commit, package_path)
    if read_error:
        return _rejected(reason=f"current SDK version unavailable: {read_error}", suites=suites)
    target_version = derive_previous_stable_version(current_version)
    if target_version is None:
        return _rejected(
            current_version=current_version,
            reason=f"cannot derive a previous stable minor from SDK version {current_version!r}",
            suites=suites,
        )

    candidates: dict[str, set[str]] = {}
    history = git_client.get_file_commit_history(repo, head_commit, package_path)
    if not history.get("ok"):
        return _rejected(
            current_version=current_version,
            target_version=target_version,
            reason=f"SDK package history unavailable: {history.get('error')}",
            suites=suites,
        )
    for commit in history.get("commits") or []:
        version, error = _read_package_version(git_client, repo, commit, package_path)
        if error:
            return _rejected(
                current_version=current_version,
                target_version=target_version,
                reason=f"SDK version unreadable at package-history candidate {commit[:8]}: {error}",
                suites=suites,
            )
        if version == target_version:
            candidates.setdefault(commit, set()).add("package_history")

    tags = git_client.list_tags(repo)
    if not tags.get("ok"):
        return _rejected(
            current_version=current_version,
            target_version=target_version,
            reason=f"release tag listing unavailable: {tags.get('error')}",
            suites=suites,
        )
    for tag in tags.get("tags") or []:
        if _tag_version(tag) != target_version:
            continue
        peeled = git_client.peel_tag_to_commit(repo, tag)
        if not peeled.get("ok"):
            return _rejected(
                current_version=current_version,
                target_version=target_version,
                reason=f"release tag {tag!r} could not be peeled: {peeled.get('error')}",
                suites=suites,
            )
        tag_commit = str(peeled["commit"])
        direct = git_client.check_ancestor(repo, tag_commit, head_commit)
        if not direct.get("ok"):
            return _rejected(
                current_version=current_version,
                target_version=target_version,
                reason=f"cannot verify ancestry for tag {tag!r}: {direct.get('error')}",
                suites=suites,
            )
        if direct.get("isAncestor"):
            candidates.setdefault(tag_commit, set()).add(f"tag:{tag}")
            continue
        merge_base = git_client.get_merge_base(repo, tag_commit, head_commit)
        if not merge_base.get("ok"):
            return _rejected(
                current_version=current_version,
                target_version=target_version,
                reason=f"cannot resolve merge-base for tag {tag!r}: {merge_base.get('error')}",
                suites=suites,
            )
        candidates.setdefault(str(merge_base["commit"]), set()).add(f"tag_merge_base:{tag}")

    valid: dict[str, set[str]] = {}
    for commit, sources in candidates.items():
        if commit == head_commit:
            continue
        ancestry = git_client.check_ancestor(repo, commit, head_commit)
        if not ancestry.get("ok"):
            return _rejected(
                current_version=current_version,
                target_version=target_version,
                reason=f"cannot verify candidate ancestry {commit[:8]}: {ancestry.get('error')}",
                suites=suites,
            )
        if not ancestry.get("isAncestor"):
            continue
        version, error = _read_package_version(git_client, repo, commit, package_path)
        if error:
            return _rejected(
                current_version=current_version,
                target_version=target_version,
                reason=f"SDK version unreadable at candidate {commit[:8]}: {error}",
                suites=suites,
            )
        if version == target_version:
            valid[commit] = sources

    if not valid:
        return _rejected(
            current_version=current_version,
            target_version=target_version,
            reason=f"no trusted ancestor declares SDK version {target_version}",
            suites=suites,
        )

    latest = _select_unique_latest_ancestor(git_client, repo, valid)
    if latest is None:
        return _rejected(
            current_version=current_version,
            target_version=target_version,
            reason="multiple incomparable previous-version checkpoint candidates",
            suites=suites,
        )
    return IntegrationBaselineResolution(
        ok=True,
        baseline_commit=latest,
        baseline_type="previous_stable_version",
        current_version=current_version,
        target_version=target_version,
        suites=suites,
        candidate_sources=tuple(sorted(valid[latest])),
        reason=(
            f"SDK {current_version} resolved previous stable minor {target_version}; "
            f"selected trusted ancestor {latest[:8]} from {', '.join(sorted(valid[latest]))}"
        ),
    )


def _read_package_version(
    git_client: GitClient,
    repo: str,
    commit: str,
    package_path: str,
) -> tuple[str | None, str | None]:
    result = git_client.get_file_content(repo, commit, package_path)
    if not result.get("ok"):
        return None, str(result.get("error") or "git file read failed")
    if result.get("truncated"):
        return None, "package.json output was truncated"
    try:
        data = json.loads(str(result.get("content") or ""))
    except json.JSONDecodeError:
        return None, "package.json is not valid JSON"
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version.strip()):
        return None, "package.json has no valid semantic version"
    return version.strip(), None


def _tag_version(tag: str) -> str | None:
    value = tag[1:] if tag.startswith("v") else tag
    return value if _SEMVER_RE.fullmatch(value) else None


def _select_unique_latest_ancestor(
    git_client: GitClient,
    repo: str,
    candidates: dict[str, set[str]],
) -> str | None:
    """Choose the one candidate descending from every other candidate."""
    latest: list[str] = []
    for candidate in candidates:
        is_latest = True
        for other in candidates:
            if candidate == other:
                continue
            ancestry = git_client.check_ancestor(repo, other, candidate)
            if not ancestry.get("ok"):
                return None
            if not ancestry.get("isAncestor"):
                is_latest = False
                break
        if is_latest:
            latest.append(candidate)
    return latest[0] if len(latest) == 1 else None


def _is_trusted_suite_checkpoint(
    candidate: dict[str, Any],
    *,
    suite: str,
    current_build_number: int,
    environment_profile: str,
) -> bool:
    """Validate persisted facts again before they can narrow a Git window."""
    if candidate.get("result") == "ABORTED":
        return False
    build_number = candidate.get("buildNumber")
    if not isinstance(build_number, int) or build_number >= current_build_number:
        return False
    run = candidate.get("integrationRun")
    if not isinstance(run, dict):
        return False
    if run.get("protocol") != "FIDGET_INTEGRATION_V1" or run.get("protocolValid") is not True:
        return False
    if run.get("environmentProfile") != environment_profile or run.get("cleanupStatus") != "success":
        return False
    suites = run.get("suites")
    if not isinstance(suites, list):
        return False
    matches = [item for item in suites if isinstance(item, dict) and item.get("name") == suite]
    return len(matches) == 1 and matches[0].get("status") == "passed" and matches[0].get("exitCode") == 0


def _rejected(
    *,
    reason: str,
    current_version: str | None = None,
    target_version: str | None = None,
    suites: tuple[str, ...] = (),
) -> IntegrationBaselineResolution:
    return IntegrationBaselineResolution(
        ok=False,
        baseline_commit=None,
        baseline_type=None,
        current_version=current_version,
        target_version=target_version,
        suites=suites,
        reason=reason,
    )
