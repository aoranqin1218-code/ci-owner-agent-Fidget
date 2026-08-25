"""WeCom-specific notification routing and delivery identity.

Maintainers here are notification recipients for unresolved failures. They do
not become causal owners and never change the responsibility notice itself.
"""

from __future__ import annotations

import hashlib
import json

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice, Owner
from ci_owner_agent.services.test_maintainer_mapping import (
    TestMaintainer,
    TestMaintainerMatch,
    TestMaintainerResolver,
)


def resolve_test_maintainer_matches(
    notice: CiResponsibilityNotice,
    *,
    maintainer_resolver: TestMaintainerResolver | None,
    repo: str | None,
    fallback_userids: tuple[str, ...],
) -> list[TestMaintainerMatch | None]:
    """Resolve only no-owner items to their current WeCom maintainer route."""
    resolver = maintainer_resolver or TestMaintainerResolver()
    if (
        str(notice.result or "").upper() in {"FAILURE", "UNSTABLE", "UNKNOWN"}
        and not notice.responsibilityItems
        and notice.owner.type == "no_high_confidence_owner"
    ):
        return [
            resolver.resolve(
                repo=repo or notice.repo,
                job=notice.job,
                test_file_path=None,
                fallback_userids=fallback_userids,
            )
        ]

    matches: list[TestMaintainerMatch | None] = []
    for item in notice.responsibilityItems:
        if item.responsibilityType != "no_high_confidence_owner":
            matches.append(None)
            continue
        # Japa 责任项通常使用 testFilePath；coverage 指向未覆盖的源文件，使用
        # failureFilePath。两者都只是维护人路由依据，不改变 causal owner。
        route_path = item.testFilePath or item.failureFilePath
        matches.append(
            resolver.resolve(
                repo=repo or notice.repo,
                job=notice.job,
                test_file_path=route_path,
                fallback_userids=fallback_userids,
            )
        )
    return matches


def collect_pending_maintainers(
    matches: list[TestMaintainerMatch | None],
) -> tuple[TestMaintainer, ...]:
    result: list[TestMaintainer] = []
    seen: set[str] = set()
    for match in matches:
        if match is None:
            continue
        for maintainer in match.maintainers:
            if maintainer.wecom_userid in seen:
                continue
            seen.add(maintainer.wecom_userid)
            result.append(maintainer)
    return tuple(result)


def collect_responsible_owners(notice: CiResponsibilityNotice) -> list[Owner]:
    """Return unique causal owners, excluding no-owner placeholder items."""
    owners: list[Owner] = []
    seen: set[str] = set()
    for item in notice.responsibilityItems:
        owner = item.owner
        if owner.type == "no_high_confidence_owner" or not owner.name or owner.name == NO_OWNER_NAME:
            continue
        key = owner.email.lower().strip() if owner.email else owner.name
        if key in seen:
            continue
        seen.add(key)
        owners.append(owner)
    return owners


def collect_responsible_display_names(notice: CiResponsibilityNotice) -> list[str]:
    return [owner.name for owner in collect_responsible_owners(notice)]


def notification_digest(
    notice: CiResponsibilityNotice,
    *,
    maintainer_resolver: TestMaintainerResolver | None = None,
    repo: str | None = None,
    fallback_userids: tuple[str, ...] = (),
    mention_mode: str = "userid",
) -> str:
    """Hash the notice and current WeCom routing into a delivery dedup identity."""
    matches = resolve_test_maintainer_matches(
        notice,
        maintainer_resolver=maintainer_resolver,
        repo=repo,
        fallback_userids=fallback_userids,
    )
    routes = []
    for index, match in enumerate(matches):
        if match is None:
            continue
        routes.append(
            {
                "itemIndex": index,
                "testFilePath": match.test_file_path,
                "matchedPattern": match.matched_pattern,
                "maintainerUserids": [item.wecom_userid for item in match.maintainers],
                "maintainerNames": [item.name for item in match.maintainers],
                "usedFallback": match.used_fallback,
                "reason": match.reason,
            }
        )
    payload = {
        "notice": notice.model_dump(mode="json"),
        "mentionMode": mention_mode,
        "testMaintainerRoutes": routes,
    }
    from ci_owner_agent.services.failure_identity import canonicalize_failure_message

    raw = canonicalize_failure_message(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
