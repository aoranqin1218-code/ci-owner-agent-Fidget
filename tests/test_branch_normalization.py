import pytest

from ci_owner_agent.services.branch_normalization import normalize_branch_name


@pytest.mark.parametrize(("raw", "expected"), [
    ("refs/remotes/origin/dev", "dev"), ("refs/remotes/upstream/dev", "dev"),
    ("remotes/origin/dev", "dev"), ("refs/heads/dev", "dev"), ("origin/dev", "dev"),
    ("*/dev", "dev"), ("refs/heads/feature/a", "feature/a"),
    ("refs/remotes/origin/feature/a", "feature/a"), ("dev", "dev"), ("feature/a", "feature/a"),
    (None, None), ("", None), ("   ", None), ("HEAD", None), ("origin/HEAD", None),
    ("refs/remotes/origin/HEAD", None), ("refs/tags/v1.0.0", None), ("refs/pull/123/merge", None),
])
def test_normalize_branch_name(raw, expected):
    assert normalize_branch_name(raw) == expected
    assert normalize_branch_name(normalize_branch_name(raw)) == expected


def test_feature_branches_remain_distinct():
    assert normalize_branch_name("feature/a") != normalize_branch_name("feature/b")


@pytest.mark.parametrize(("raw", "expected"), [
    ("refs/heads/origin/dev", "dev"),
    ("refs/heads/upstream/feature/a", "feature/a"),
    ("refs/remotes/origin/refs/heads/dev", "dev"),
    ("refs/heads/refs/tags/v1", None),
    ("refs/remotes/origin/refs/pull/123/merge", None),
])
def test_normalize_branch_name_is_idempotent_for_nested_prefixes(raw, expected):
    normalized = normalize_branch_name(raw)
    assert normalized == expected
    assert normalize_branch_name(normalized) == normalized
