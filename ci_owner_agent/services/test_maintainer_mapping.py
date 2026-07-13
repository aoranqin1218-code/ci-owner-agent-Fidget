from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from ci_owner_agent.services.responsibility_path_enricher import normalize_repository_path


@dataclass(frozen=True)
class TestMaintainer:
    name: str
    wecom_userid: str


@dataclass(frozen=True)
class TestMaintainerRule:
    repo: str | None
    job: str | None
    paths: tuple[str, ...]
    maintainers: tuple[TestMaintainer, ...]


@dataclass(frozen=True)
class TestMaintainerMatch:
    test_file_path: str | None
    matched_pattern: str | None
    maintainers: tuple[TestMaintainer, ...]
    used_fallback: bool
    reason: str


class TestMaintainerResolver:
    __test__ = False

    def __init__(self, rules: tuple[TestMaintainerRule, ...] = (), warnings: tuple[str, ...] = ()) -> None:
        self.rules = rules
        self.warnings = warnings

    @classmethod
    def from_yaml(cls, path: str | Path | None) -> "TestMaintainerResolver":
        if path is None:
            return cls()
        source = Path(path)
        if not source.exists():
            return cls(warnings=(f"test maintainer mapping file not found: {source}",))
        try:
            import yaml

            data = yaml.safe_load(source.read_text(encoding="utf-8"))
        except Exception as exc:
            return cls(warnings=(f"invalid test maintainer mapping yaml: {exc}",))
        if not isinstance(data, dict):
            return cls(warnings=("invalid test maintainer mapping: root must be an object",))
        raw_rules = data.get("rules")
        if raw_rules is None:
            raw_rules = []
        if not isinstance(raw_rules, list):
            return cls(warnings=("invalid test maintainer mapping: rules must be a list",))

        rules: list[TestMaintainerRule] = []
        warnings: list[str] = []
        for index, raw_rule in enumerate(raw_rules):
            try:
                rules.append(_parse_rule(raw_rule))
            except ValueError as exc:
                warnings.append(f"ignored test maintainer rule {index}: {exc}")
        return cls(tuple(rules), tuple(warnings))

    def resolve(
        self,
        *,
        repo: str | None,
        job: str,
        test_file_path: str | None,
        fallback_userids: tuple[str, ...] = (),
    ) -> TestMaintainerMatch:
        normalized = normalize_repository_path(test_file_path)
        if not normalized:
            return _fallback_match(None, fallback_userids, "未识别失败测试文件，使用默认兜底人")
        for rule in self.rules:
            if rule.repo is not None and rule.repo != repo:
                continue
            if rule.job is not None and rule.job != job:
                continue
            for pattern in rule.paths:
                if _glob_match(normalized, pattern):
                    return TestMaintainerMatch(
                        test_file_path=normalized,
                        matched_pattern=pattern,
                        maintainers=rule.maintainers,
                        used_fallback=False,
                        reason=f"匹配测试文件维护规则：{pattern}",
                    )
        return _fallback_match(normalized, fallback_userids, "未匹配测试文件维护规则，使用默认兜底人")


def _parse_rule(raw_rule: Any) -> TestMaintainerRule:
    if not isinstance(raw_rule, dict):
        raise ValueError("rule must be an object")
    raw_paths = raw_rule.get("paths")
    raw_maintainers = raw_rule.get("maintainers")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("paths must contain at least one pattern")
    if not isinstance(raw_maintainers, list) or not raw_maintainers:
        raise ValueError("maintainers must contain at least one entry")
    paths: list[str] = []
    for value in raw_paths:
        pattern = normalize_repository_path(str(value or ""))
        if not pattern:
            raise ValueError("path pattern is empty or unsafe")
        paths.append(pattern)
    maintainers: list[TestMaintainer] = []
    seen: set[str] = set()
    for raw in raw_maintainers:
        if not isinstance(raw, dict):
            raise ValueError("maintainer must be an object")
        userid = str(raw.get("wecomUserId") or "").strip()
        if not userid:
            raise ValueError("maintainer.wecomUserId is required")
        if userid in seen:
            continue
        seen.add(userid)
        maintainers.append(TestMaintainer(name=str(raw.get("name") or "").strip(), wecom_userid=userid))
    if not maintainers:
        raise ValueError("maintainers contain no usable userid")
    repo = str(raw_rule.get("repo") or "").strip() or None
    job = str(raw_rule.get("job") or "").strip() or None
    return TestMaintainerRule(repo=repo, job=job, paths=tuple(paths), maintainers=tuple(maintainers))


def _fallback_match(path: str | None, userids: tuple[str, ...], reason: str) -> TestMaintainerMatch:
    maintainers: list[TestMaintainer] = []
    seen: set[str] = set()
    for raw in userids:
        userid = str(raw or "").strip()
        if not userid or userid in seen:
            continue
        seen.add(userid)
        maintainers.append(TestMaintainer(name="", wecom_userid=userid))
    return TestMaintainerMatch(
        test_file_path=path,
        matched_pattern=None,
        maintainers=tuple(maintainers),
        used_fallback=True,
        reason=reason,
    )


def _glob_match(path: str, pattern: str) -> bool:
    regex: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    regex.append("(?:.*/)?")
                    index += 1
                else:
                    regex.append(".*")
                continue
            regex.append("[^/]*")
        elif char == "?":
            regex.append("[^/]")
        else:
            regex.append(re.escape(char))
        index += 1
    regex.append("$")
    return re.match("".join(regex), path) is not None
