from __future__ import annotations

import re
from typing import Iterable

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.repository_path import normalize_repository_path


_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?(?:[\\/][^\s()'\"<>:]+|[^\s()'\"<>:\\/]+[\\/])+"
    r"[^\s()'\"<>:]+\.(?:ts|tsx|js|jsx))(?::\d+(?::\d+)?)?",
    re.IGNORECASE,
)
_TEST_SUFFIX_RE = re.compile(r"(?:\.test|\.spec)\.(?:ts|tsx|js|jsx)$|Test\.(?:ts|tsx|js|jsx)$")


def is_routable_repository_path(value: str | None) -> bool:
    path = normalize_repository_path(value)
    return bool(path and not path.startswith("node_modules/"))


def _routable_repository_path(value: str | None) -> str | None:
    path = normalize_repository_path(value)
    return path if path and is_routable_repository_path(path) else None


def is_test_file_path(value: str | None) -> bool:
    path = _routable_repository_path(value)
    if path is None:
        return False
    lower = path.lower()
    if any(segment in {"test", "tests", "__tests__"} for segment in lower.split("/")):
        return True
    return bool(_TEST_SUFFIX_RE.search(path))


def enrich_responsibility_item_paths(
    notice: CiResponsibilityNotice,
    failure_summaries: dict | None,
    failure_facts: dict | None,
    repo: str | None = None,
) -> CiResponsibilityNotice:
    notice.repo = repo or notice.repo
    summary_by_signature = _summary_signatures(failure_summaries)
    fact_by_signature = _failure_facts(failure_facts)
    evidence_by_id = {item.id: item for item in notice.evidence}

    for item in notice.responsibilityItems:
        item.testFilePath = _routable_repository_path(item.testFilePath)
        item.failureFilePath = _routable_repository_path(item.failureFilePath)
        signature = str(item.failureSignature or "")
        summary = summary_by_signature.get(signature)
        if summary:
            summary_test_path = _routable_repository_path(summary.get("testFile"))
            summary_failure_path = _routable_repository_path(summary.get("topStackFile"))
            if summary_test_path and not item.testFilePath:
                item.testFilePath = summary_test_path
            if summary_failure_path and not item.failureFilePath:
                item.failureFilePath = summary_failure_path

        fact = fact_by_signature.get(signature)
        if fact:
            fact_path = _routable_repository_path(fact.get("filePath"))
            if is_test_file_path(fact_path):
                if fact_path and not item.testFilePath:
                    item.testFilePath = fact_path
            elif fact_path and not item.failureFilePath:
                item.failureFilePath = fact_path

        if not item.testFilePath:
            texts = [item.failureTitle, item.failureSummary, item.reason]
            for evidence_id in item.evidenceIds:
                evidence = evidence_by_id.get(evidence_id)
                if evidence:
                    texts.extend([evidence.summary, evidence.detail])
            item.testFilePath = _first_test_path(texts)
    return notice


def _summary_signatures(failure_summaries: dict | None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    chunks = failure_summaries.get("chunks", []) if isinstance(failure_summaries, dict) else []
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        signature = chunk.get("signature") if isinstance(chunk.get("signature"), dict) else {}
        for key in (signature.get("signatureKey"), signature.get("signatureHash"), chunk.get("signatureHash")):
            if key:
                result[str(key)] = signature
    return result


def _failure_facts(failure_facts: dict | None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    facts = failure_facts.get("facts", []) if isinstance(failure_facts, dict) else []
    for fact in facts or []:
        if isinstance(fact, dict) and fact.get("signatureKey"):
            result[str(fact["signatureKey"])] = fact
    return result


def _first_test_path(texts: Iterable[str | None]) -> str | None:
    for text in texts:
        for match in _PATH_RE.finditer(str(text or "")):
            path = _routable_repository_path(match.group("path"))
            if is_test_file_path(path):
                return path
    return None
