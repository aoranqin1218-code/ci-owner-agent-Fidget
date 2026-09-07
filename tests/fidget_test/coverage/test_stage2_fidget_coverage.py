"""阶段二（Fidget）测试：c8 覆盖率门槛失败识别、包名归属与摘要生成。

覆盖 log_parsing 的 coverage 能力：
- find_coverage_errors：识别 c8 门槛失败行（per-file / global，仅 pct<threshold）
- Nx task 块归属（受约束 B）：nx 包头 + npm 行包名一致才写 package_name
- find_test_failure_summaries：无 Japa 失败时产出 coverage_failure_block chunk

resolve（owner / C 消歧）依赖真实 git 仓库，放在实现侧验证；本文件聚焦纯日志解析逻辑。
"""

from __future__ import annotations

from pathlib import Path

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.coverage_responsibility import (
    reconcile_coverage_responsibilities,
    resolve_coverage_owner_candidate,
    resolve_coverage_package,
)
from ci_owner_agent.services.history_search import CURRENT_ALLOWED_CHUNK_SOURCES
from ci_owner_agent.services.history_store import ALLOWED_HISTORY_CHUNK_SOURCES
from ci_owner_agent.services.investigation_scope import InvestigationScope
from ci_owner_agent.services.log_parsing import build_coverage_failure_summaries, find_coverage_errors
from ci_owner_agent.services.log_provider import LocalFileLogProvider, TextLogProvider
from ci_owner_agent.services.scorer import no_owner, validate_notice

_SAMPLES = Path(__file__).resolve().parents[3] / "samples" / "fidget_log"
_HEAD = "a" * 40
_BASE = "b" * 40


class FakeGitClient:
    """最小 fake：仅覆盖 coverage resolve 用到的 list_paths / get_file_diff。"""

    def __init__(self, existing_paths=None, authors_by_path=None):
        self.existing_paths = set(existing_paths or [])
        self.authors_by_path = authors_by_path or {}

    def list_paths(self, repo, commit, paths=None):
        if paths is None:
            hits = [p for p in self.existing_paths]
        else:
            hits = [p for p in paths if p in self.existing_paths]
        return {"ok": True, "paths": hits}

    def get_file_diff(self, repo, base, head, path):
        authors = self.authors_by_path.get(path, [])
        return {"ok": True, "diff": "diff --git a b", "authors": authors}


class MemoryLogProvider(TextLogProvider):
    def __init__(self, lines):
        super().__init__()
        self.lines = list(lines)

    def _lines(self):
        return self.lines

    def _content(self):
        return "\n".join(self.lines)


def _scope():
    return InvestigationScope(mode="default", full_base_commit=_BASE, full_head_commit=_HEAD)


def test_coverage_error_per_file_recognized():
    lines = [
        "ERROR: Coverage for statements (87%) does not meet threshold (100%) for src/foo/Bar.ts",
        "PASSED",
        "Tests  240 passed (240)",
    ]
    hits = find_coverage_errors(lines)
    assert len(hits) == 1
    assert hits[0]["metric"] == "statements"
    assert hits[0]["pct"] == 87.0
    assert hits[0]["threshold"] == 100.0
    assert hits[0]["kind"] == "per_file"
    assert hits[0]["file"] == "src/foo/Bar.ts"


def test_coverage_error_global_recognized():
    lines = ["ERROR: Coverage for branches (55.88%) does not meet global threshold (100%)"]
    hits = find_coverage_errors(lines)
    assert len(hits) == 1
    assert hits[0]["kind"] == "global"
    assert hits[0]["file"] is None


def test_coverage_error_at_threshold_not_flagged():
    # pct == threshold 不算失败（对齐 c8 `pct < threshold`）
    lines = ["ERROR: Coverage for functions (100%) does not meet threshold (100%) for a/b.ts"]
    assert find_coverage_errors(lines) == []


def test_non_coverage_error_not_matched():
    lines = [
        "AssertionError: expected true to equal false",
        "some random text mentioning coverage but no ERROR prefix",
    ]
    assert find_coverage_errors(lines) == []


def test_package_attribution_with_trusted_block():
    # nx 包头 + npm 行包名一致 → 可信归属 fidget-sql
    lines = [
        "> nx run @fx/fidget-sql:\"test:coverage\"",
        "> @fx/fidget-sql@1.8.0-dev.0 test:coverage",
        "ERROR: Coverage for statements (80%) does not meet threshold (100%) for src/parser/Ts.ts",
    ]
    hits = find_coverage_errors(lines)
    assert len(hits) == 1
    assert hits[0]["package_name"] == "fidget-sql"


def test_package_attribution_unknown_without_npm_line():
    # 有 nx 包头但无 npm 行 → 归属存疑，不写 package_name
    lines = [
        "> nx run @fx/fidget-sql:\"test:coverage\"",
        "ERROR: Coverage for statements (80%) does not meet threshold (100%) for src/parser/Ts.ts",
    ]
    hits = find_coverage_errors(lines)
    assert len(hits) == 1
    assert hits[0]["package_name"] is None


def test_package_attribution_multi_package_blocks():
    # sql 块有 npm 确认归属 sql；core 块无 npm 行 → core 归属存疑
    lines = [
        "> nx run @fx/fidget-sql:\"test:coverage\"",
        "> @fx/fidget-sql@1.8.0-dev.0 test:coverage",
        "ERROR: Coverage for statements (80%) does not meet threshold (100%) for src/parser/Sql.ts",
        "> nx run @fx/fidget-core:\"test:coverage\"",
        "ERROR: Coverage for lines (70%) does not meet threshold (100%) for src/common/Core.ts",
    ]
    hits = find_coverage_errors(lines)
    assert len(hits) == 2
    assert hits[0]["package_name"] == "fidget-sql"
    assert hits[1]["package_name"] is None


def test_summary_builds_coverage_chunk_with_package():
    # 真实 cov-gap 样本：无 Japa 失败、纯 coverage → 产出 coverage_failure_block chunk，含 packageName
    p = LocalFileLogProvider(_SAMPLES / "cov-gap-single-file.log")
    result = p.find_test_failure_summaries(max_chunks=5)
    chunks = result["chunks"]
    assert chunks, "coverage-only 日志应产出 coverage chunk"
    for chunk in chunks:
        assert chunk["anchorType"] == "coverage_failure_block"
        sig = chunk.get("signature") or {}
        assert sig.get("signatureKey", "").startswith("coverage_threshold_failure|")
        assert sig.get("topStackFile") is None
        assert sig.get("rawCoveragePath") == "src/parser/TokenScanner.ts"
        assert chunk["chunkSource"] == "c8_coverage_threshold_failure"


def test_summary_success_log_no_coverage_chunk():
    # SUCCESS 日志（无 coverage 错误）不应产出 coverage chunk
    p = LocalFileLogProvider(_SAMPLES / "fidget-build-dev-313.log")
    result = p.find_test_failure_summaries(max_chunks=5)
    chunks = result.get("chunks") or []
    coverage = [c for c in chunks if c.get("anchorType") == "coverage_failure_block"]
    assert coverage == []


def test_same_file_multiple_metrics_becomes_one_chunk_and_one_signature():
    lines = [
        "ERROR: Coverage for statements (80%) does not meet threshold (100%) for src/X.ts",
        "ERROR: Coverage for lines (90%) does not meet threshold (100%) for src/X.ts",
    ]
    chunks = build_coverage_failure_summaries(lines, max_chunks=None)

    assert len(chunks) == 1
    signature = chunks[0]["signature"]
    assert set(signature["metrics"]) == {"statements", "lines"}
    assert signature["signatureKey"] == "coverage_threshold_failure|unresolved|src/X.ts"
    changed_percentages = build_coverage_failure_summaries(
        [
            "ERROR: Coverage for statements (70%) does not meet threshold (100%) for src/X.ts",
            "ERROR: Coverage for lines (85%) does not meet threshold (100%) for src/X.ts",
        ],
        max_chunks=None,
    )
    assert changed_percentages[0]["signatureHash"] == chunks[0]["signatureHash"]


def test_mixed_budget_keeps_coverage_and_records_omitted_counts():
    lines = []
    for index in range(5):
        lines.extend([f"✖ test {index}", "AssertionError: expected true to be false"])
    lines.append("ERROR: Coverage for lines (90%) does not meet threshold (100%) for src/X.ts")

    result = MemoryLogProvider(lines).find_test_failure_summaries(max_chunks=5)

    assert len(result["chunks"]) == 5
    assert sum(chunk["anchorType"] == "coverage_failure_block" for chunk in result["chunks"]) == 1
    assert result["totals"] == {
        "japa": 5,
        "coverage": 1,
        "integrationEnv": 0,
        "omittedJapa": 1,
        "omittedCoverage": 0,
    }
    assert result["coverageFiles"][0]["rawCoveragePath"] == "src/X.ts"


def test_coverage_chunk_source_is_excluded_from_deterministic_history():
    chunk = build_coverage_failure_summaries(
        ["ERROR: Coverage for lines (90%) does not meet threshold (100%) for src/X.ts"]
    )[0]

    assert chunk["chunkSource"] not in CURRENT_ALLOWED_CHUNK_SOURCES
    assert chunk["chunkSource"] not in ALLOWED_HISTORY_CHUNK_SOURCES


def test_resolve_single_author_medium():
    git = FakeGitClient(
        existing_paths=["packages/fidget-sql/src/generator/SqlUtils.ts"],
        authors_by_path={
            "packages/fidget-sql/src/generator/SqlUtils.ts": [
                {"name": "Zhang San", "email": "zs@x.com", "commits": [_HEAD]}
            ]
        },
    )
    r = resolve_coverage_owner_candidate(
        repo="fxp-fidget", package_name="fidget-sql",
        raw_coverage_path="src/generator/SqlUtils.ts",
        git_client=git, investigation_scope=_scope(), trusted_head_commit=_HEAD,
    )
    assert r.owner_type == "medium_confidence"
    assert r.owner_candidate["name"] == "Zhang San"
    assert r.diff_evidence is True


def test_resolve_multi_author_no_owner():
    git = FakeGitClient(
        existing_paths=["packages/fidget-sql/src/generator/SqlUtils.ts"],
        authors_by_path={
            "packages/fidget-sql/src/generator/SqlUtils.ts": [
                {"name": "A", "email": "a@x.com", "commits": ["c1"]},
                {"name": "B", "email": "b@x.com", "commits": ["c2"]},
            ]
        },
    )
    r = resolve_coverage_owner_candidate(
        repo="fxp-fidget", package_name="fidget-sql",
        raw_coverage_path="src/generator/SqlUtils.ts",
        git_client=git, investigation_scope=_scope(), trusted_head_commit=_HEAD,
    )
    assert r.owner_type == "no_high_confidence_owner"
    assert r.owner_candidate is None


def test_resolve_file_not_present_no_owner():
    git = FakeGitClient(existing_paths=[])
    r = resolve_coverage_owner_candidate(
        repo="fxp-fidget", package_name="fidget-sql",
        raw_coverage_path="src/does/NotExist.ts",
        git_client=git, investigation_scope=_scope(), trusted_head_commit=_HEAD,
    )
    assert r.owner_type == "no_high_confidence_owner"
    assert r.resolved_path is None


def test_resolve_package_c_unique_hit():
    git = FakeGitClient(existing_paths=["packages/fidget-sql/src/generator/SqlUtils.ts"])
    pkg = resolve_coverage_package(
        repo="fxp-fidget", raw_coverage_path="src/generator/SqlUtils.ts",
        git_client=git, trusted_head_commit=_HEAD,
    )
    assert pkg == "fidget-sql"


def test_resolve_package_c_ambiguous_no_owner():
    # 两个包都有同名文件 → 歧义，不归属
    git = FakeGitClient(existing_paths=[
        "packages/fidget-sql/src/common/X.ts",
        "packages/fidget-core/src/common/X.ts",
    ])
    pkg = resolve_coverage_package(
        repo="fxp-fidget", raw_coverage_path="src/common/X.ts",
        git_client=git, trusted_head_commit=_HEAD,
    )
    assert pkg is None


def _notice() -> CiResponsibilityNotice:
    return CiResponsibilityNotice(
        repo="fxp-fidget",
        job="npm/fxp-fidget/fidget-build",
        buildNumber=1,
        buildUrl="https://jenkins.example/build/1",
        result="FAILURE",
        branch="dev",
        headCommit=_HEAD,
        baseCommit=_BASE,
        owner=no_owner(),
        failureReason="coverage failed",
        hasHighConfidenceOwner=False,
    )


def test_reconciler_uses_complete_coverage_files_and_promotes_single_medium_owner():
    path = "packages/fidget-sql/src/X.ts"
    git = FakeGitClient(
        existing_paths=[path],
        authors_by_path={
            path: [{"name": "Zhang San", "email": "zs@x.com", "commits": [_HEAD]}]
        },
    )
    summaries = {
        "chunks": [],
        "coverageFiles": [
            {
                "packageName": "fidget-sql",
                "rawCoveragePath": "src/X.ts",
                "metrics": {
                    "statements": {"pct": 80.0, "threshold": 100.0},
                    "lines": {"pct": 90.0, "threshold": 100.0},
                },
                "content": "ERROR: Coverage for statements ... for src/X.ts",
            }
        ],
    }

    input_notice = _notice()
    input_notice.failureReason = (
        "单元测试断言失败。"
        "日志中的 c8 Coverage threshold 错误按规则不生成责任项。"
    )
    input_notice.suggestions = [
        "修复必然失败的单元测试断言。",
        "c8 覆盖率阈值失败由确定性 reconciler 单独处理，本通知不为其分配责任人。",
    ]
    notice = reconcile_coverage_responsibilities(
        input_notice,
        failure_summaries=summaries,
        repo="fxp-fidget",
        head_commit=_HEAD,
        git_client=git,
        investigation_scope=_scope(),
    )
    notice = validate_notice(notice)

    assert len(notice.responsibilityItems) == 1
    item = notice.responsibilityItems[0]
    assert item.failureSignature == f"coverage_threshold_failure|{path.lower()}"
    assert item.owner.type == "medium_confidence"
    assert item.failureFilePath == path
    assert notice.owner.type == "medium_confidence"
    assert notice.owner.name == "Zhang San"
    assert notice.hasHighConfidenceOwner is False
    assert notice.failureReason == "单元测试断言失败。"
    assert notice.suggestions == ["修复必然失败的单元测试断言。"]
    assert {evidence.type for evidence in notice.evidence if evidence.id in item.evidenceIds} == {"log", "diff"}


def test_global_coverage_reconciles_to_no_owner_without_fake_path():
    summaries = {
        "coverageFiles": [
            {
                "packageName": "fidget-sql",
                "rawCoveragePath": None,
                "metrics": {"branches": {"pct": 80.0, "threshold": 100.0}},
                "content": "ERROR: Coverage for branches ... global threshold",
            }
        ]
    }

    notice = reconcile_coverage_responsibilities(
        _notice(),
        failure_summaries=summaries,
        repo="fxp-fidget",
        head_commit=_HEAD,
        git_client=FakeGitClient(),
        investigation_scope=_scope(),
    )

    item = notice.responsibilityItems[0]
    assert item.failureFilePath is None
    assert item.owner.type == "no_high_confidence_owner"
    assert item.failureSignature == "coverage_threshold_failure|global|fidget-sql|branches"
    assert item.evidenceIds and all(value.startswith("coverage-log-") for value in item.evidenceIds)


def test_reconciler_does_not_choose_one_build_owner_when_files_have_different_owners():
    first = "packages/fidget-sql/src/A.ts"
    second = "packages/fidget-core/src/B.ts"
    git = FakeGitClient(
        existing_paths=[first, second],
        authors_by_path={
            first: [{"name": "Author A", "email": "a@x.com", "commits": ["commit-a"]}],
            second: [{"name": "Author B", "email": "b@x.com", "commits": ["commit-b"]}],
        },
    )
    summaries = {
        "coverageFiles": [
            {
                "packageName": "fidget-sql",
                "rawCoveragePath": "src/A.ts",
                "metrics": {"lines": {"pct": 90.0, "threshold": 100.0}},
                "content": "coverage A",
            },
            {
                "packageName": "fidget-core",
                "rawCoveragePath": "src/B.ts",
                "metrics": {"lines": {"pct": 90.0, "threshold": 100.0}},
                "content": "coverage B",
            },
        ]
    }

    notice = reconcile_coverage_responsibilities(
        _notice(),
        failure_summaries=summaries,
        repo="fxp-fidget",
        head_commit=_HEAD,
        git_client=git,
        investigation_scope=_scope(),
    )

    assert [item.owner.name for item in notice.responsibilityItems] == ["Author A", "Author B"]
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.hasHighConfidenceOwner is False
