from __future__ import annotations

from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver


def _write_mapping(path, rules: str) -> None:
    path.write_text(f"version: 1\nrules:\n{rules}", encoding="utf-8")


def test_load_valid_yaml_and_deduplicate_maintainers(tmp_path):
    path = tmp_path / "maintainers.yml"
    _write_mapping(
        path,
        """
  - repo: fx-code
    job: services/fx-code-unittest
    paths: ["test/service/view/**"]
    maintainers:
      - {name: Charlie, wecomUserId: charlie}
      - {name: Charlie Duplicate, wecomUserId: charlie}
      - {name: Henry, wecomUserId: henry}
""",
    )

    resolver = TestMaintainerResolver.from_yaml(path)
    match = resolver.resolve(repo="fx-code", job="services/fx-code-unittest", test_file_path="test/service/view/FooTest.ts")

    assert resolver.warnings == ()
    assert [item.wecom_userid for item in match.maintainers] == ["charlie", "henry"]


def test_missing_and_invalid_yaml_return_warnings(tmp_path):
    missing = TestMaintainerResolver.from_yaml(tmp_path / "missing.yml")
    invalid_path = tmp_path / "invalid.yml"
    invalid_path.write_text("rules: [", encoding="utf-8")
    invalid = TestMaintainerResolver.from_yaml(invalid_path)

    assert "not found" in missing.warnings[0]
    assert "invalid" in invalid.warnings[0]


def test_empty_rules_and_invalid_rule_are_safe(tmp_path):
    empty_path = tmp_path / "empty.yml"
    empty_path.write_text("version: 1\nrules: []\n", encoding="utf-8")
    invalid_path = tmp_path / "invalid-rule.yml"
    invalid_path.write_text("version: 1\nrules:\n  - paths: []\n    maintainers: []\n", encoding="utf-8")

    assert TestMaintainerResolver.from_yaml(empty_path).rules == ()
    resolver = TestMaintainerResolver.from_yaml(invalid_path)
    assert resolver.rules == ()
    assert "ignored test maintainer rule" in resolver.warnings[0]


def test_glob_normalization_repo_job_and_first_rule(tmp_path):
    path = tmp_path / "maintainers.yml"
    _write_mapping(
        path,
        """
  - repo: other-repo
    paths: ["test/**"]
    maintainers: [{name: WrongRepo, wecomUserId: wrong.repo}]
  - repo: fx-code
    job: other-job
    paths: ["test/**"]
    maintainers: [{name: WrongJob, wecomUserId: wrong.job}]
  - repo: fx-code
    job: services/fx-code-unittest
    paths: ["test/*/View?ataQueryServiceTest.ts", "test/**/ViewDataQueryServiceTest.ts"]
    maintainers: [{name: First, wecomUserId: first}]
  - repo: fx-code
    paths: ["test/**"]
    maintainers: [{name: Later, wecomUserId: later}]
""",
    )
    resolver = TestMaintainerResolver.from_yaml(path)

    windows = resolver.resolve(
        repo="fx-code",
        job="services/fx-code-unittest",
        test_file_path=r".\test\service\view\ViewDataQueryServiceTest.ts",
    )
    exact = resolver.resolve(
        repo="fx-code",
        job="services/fx-code-unittest",
        test_file_path="test/x/ViewDataQueryServiceTest.ts",
    )

    assert windows.maintainers[0].wecom_userid == "first"
    assert exact.maintainers[0].wecom_userid == "first"
    assert windows.matched_pattern in {
        "test/*/View?ataQueryServiceTest.ts",
        "test/**/ViewDataQueryServiceTest.ts",
    }


def test_exact_path_rule_matches(tmp_path):
    path = tmp_path / "maintainers.yml"
    _write_mapping(
        path,
        """
  - paths: ["test/service/view/ExactTest.ts"]
    maintainers: [{name: Exact, wecomUserId: exact.userid}]
""",
    )
    match = TestMaintainerResolver.from_yaml(path).resolve(
        repo=None,
        job="any-job",
        test_file_path="test/service/view/ExactTest.ts",
    )
    assert match.maintainers[0].wecom_userid == "exact.userid"


def test_unmatched_or_missing_path_uses_fallback(tmp_path):
    resolver = TestMaintainerResolver()
    unmatched = resolver.resolve(repo="fx-code", job="job", test_file_path="test/unknown/FooTest.ts", fallback_userids=("fallback",))
    missing = resolver.resolve(repo="fx-code", job="job", test_file_path=None, fallback_userids=("fallback",))

    assert unmatched.used_fallback is True
    assert unmatched.maintainers[0].wecom_userid == "fallback"
    assert "未匹配" in unmatched.reason
    assert missing.test_file_path is None
    assert "未识别" in missing.reason


def test_unsafe_parent_path_does_not_match(tmp_path):
    resolver = TestMaintainerResolver()
    match = resolver.resolve(repo="fx-code", job="job", test_file_path="../test/FooTest.ts", fallback_userids=())
    assert match.test_file_path is None
    assert match.maintainers == ()
