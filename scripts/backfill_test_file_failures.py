from __future__ import annotations

import argparse
import json

from ci_owner_agent.config import load_settings
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice
from ci_owner_agent.services.history_store import get_history_store
from ci_owner_agent.services.responsibility_path_enricher import enrich_responsibility_item_paths
from ci_owner_agent.services.responsibility_path_enricher import is_test_file_path


def backfill(store, *, repo: str | None = None, job: str | None = None, branch: str | None = None,
             build_from: int | None = None, build_to: int | None = None, dry_run: bool = False,
             overwrite: bool = False) -> dict:
    query = {}
    if job is not None:
        query["job"] = job
    if branch is not None:
        query["branch"] = branch
    notices = list(store.notices.find(query))
    result = {"scannedBuildCount": 0, "createdEventCount": 0, "updatedEventCount": 0,
              "skippedEventCount": 0, "unidentifiedPathCount": 0, "missingBuildTimestampCount": 0}
    for notice_doc in notices:
        raw_notice = notice_doc.get("notice") or {}
        notice = CiResponsibilityNotice.model_validate(raw_notice)
        effective_repo = notice.repo or notice_doc.get("repo") or ""
        if repo is not None and effective_repo != repo:
            continue
        number = int(notice_doc.get("buildNumber") or 0)
        if build_from is not None and number < build_from or build_to is not None and number > build_to:
            continue
        result["scannedBuildCount"] += 1
        scope = {"repo": effective_repo, "job": notice_doc.get("job"),
                 "branch": notice_doc.get("branch"), "buildNumber": number}
        existing = list(store.test_file_failures.find(scope))
        if existing and not overwrite:
            result["skippedEventCount"] += len(existing)
            continue
        chunks = list(store.failure_chunks.find({"job": notice.job, "branch": notice.branch, "buildNumber": number}))
        enrich_responsibility_item_paths(notice, {"chunks": chunks}, None, repo=notice.repo or scope["repo"])
        build_doc = store.builds.find_one({"job": notice.job, "branch": notice.branch, "buildNumber": number}) or {}
        timestamp = build_doc.get("buildTimestamp") or notice_doc.get("buildTimestamp")
        if timestamp is None:
            result["missingBuildTimestampCount"] += 1
        build_info = BuildInfo(job=notice.job, buildNumber=number, result=notice.result, buildUrl=notice.buildUrl,
                               branch=notice.branch, commit=notice.headCommit,
                               timestamp=timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp)
        identifiable = sum(1 for item in notice.responsibilityItems if is_test_file_path(item.testFilePath))
        result["unidentifiedPathCount"] += len(notice.responsibilityItems) - identifiable
        if dry_run:
            if existing:
                result["updatedEventCount"] += identifiable
            else:
                result["createdEventCount"] += identifiable
            continue
        if build_doc and build_doc.get("repo") != scope["repo"]:
            store.builds.update_one(
                {"job": notice.job, "branch": notice.branch, "buildNumber": number},
                {"$set": {"repo": scope["repo"]}},
            )
        saved = store.replace_test_file_failures(build_info=build_info, notice=notice)["testFileFailuresSaved"]
        if existing:
            result["updatedEventCount"] += saved
        else:
            result["createdEventCount"] += saved
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill ci_test_file_failures without invoking an LLM")
    parser.add_argument("--repo")
    parser.add_argument("--job")
    parser.add_argument("--branch")
    parser.add_argument("--build-from", type=int)
    parser.add_argument("--build-to", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    store = get_history_store(load_settings())
    if store is None:
        parser.error("history store disabled or unavailable")
    print(json.dumps(backfill(store, repo=args.repo, job=args.job, branch=args.branch,
                              build_from=args.build_from, build_to=args.build_to, dry_run=args.dry_run,
                              overwrite=args.overwrite), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
