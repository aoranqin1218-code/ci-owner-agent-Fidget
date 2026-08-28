from __future__ import annotations

import pytest

from ci_owner_agent.services.log_parsing import resolve_final_status_from_console_log
from ci_owner_agent.services.log_provider import LocalFileLogProvider


@pytest.mark.parametrize(
    ("text", "status", "detected", "raw", "unsupported"),
    [
        ("no final line", "UNKNOWN", False, None, None),
        ("Finished: FAILURE", "FAILURE", True, "FAILURE", None),
        ("Finished: UNKNOWN", "UNKNOWN", True, "UNKNOWN", None),
        ("Finished: NOT_BUILT", "NOT_BUILT", True, "NOT_BUILT", None),
        ("Finished: CANCELLED", "UNKNOWN", True, "CANCELLED", "CANCELLED"),
        ("2026-07-16T18:00:00.123+08:00 Finished: UNSTABLE", "UNSTABLE", True, "UNSTABLE", None),
        ("[2026-07-16T18:00:00Z] Finished: SUCCESS", "SUCCESS", True, "SUCCESS", None),
        ("\x1b[32mFinished: FAILURE\x1b[0m", "FAILURE", True, "FAILURE", None),
        ("[INFO] Finished: SUCCESS", "UNKNOWN", False, None, None),
        ("message: Finished: FAILURE", "UNKNOWN", False, None, None),
        ("Finished: SUCCESS\nFinished: failure", "FAILURE", True, "FAILURE", None),
    ],
)
def test_resolve_final_status(text, status, detected, raw, unsupported):
    result = resolve_final_status_from_console_log(text)
    assert (result.status, result.detected, result.raw_status, result.unsupported_status) == (status, detected, raw, unsupported)
    assert bool(result.error) is bool(unsupported)


def provider(tmp_path, lines: list[str]) -> LocalFileLogProvider:
    path = tmp_path / "console.log"
    path.write_text("\n".join(lines), encoding="utf-8")
    return LocalFileLogProvider(path)


def test_japa_failure_summary_extracts(tmp_path):
    result = provider(
        tmp_path,
        [
            "✖ Some test title",
            "AssertionError: expected 1 to equal 2",
            "at packages/fidget-sql/test/fooTest.ts:10:5",
        ],
    ).find_test_failure_summaries()

    assert result["chunks"]
    assert result["chunks"][0]["anchorType"] == "japa_failure_block"
    assert result["chunks"][0]["signature"]["signatureKey"]
    assert result["chunks"][0]["chunkSource"] == "local_test_failure_summary"


def test_japa_xfail_failure_summary_still_extracts(tmp_path):
    result = provider(
        tmp_path,
        [
            "✖ Some test title",
            "Error: something failed",
            "    at test/foo.test.ts:1:1",
        ],
    ).find_test_failure_summaries()

    assert result["chunks"]
    assert result["chunks"][0]["anchorType"] == "japa_failure_block"
    assert result["chunks"][0]["signature"]["signatureKey"]


def test_ts2305_docker_wrapper_no_summary_chunk(tmp_path):
    result = provider(
        tmp_path,
        [
            "src/index.ts(10,27): error TS2305: Module '\"./errors\"' has no exported member 'classifyErrorMessage'.",
            'ERROR: process "/bin/sh -c npm run nx:build && npm run test" did not complete successfully: exit code: 130',
        ],
    ).find_test_failure_summaries()

    assert result["chunks"] == []
    assert "no Japa failure block found" in result["warning"]
    assert "fatal_error_block" not in str(result)
    assert "fatal|fatal error|fatal error||" not in str(result)


def test_npm_etarget_docker_wrapper_no_summary_chunk(tmp_path):
    result = provider(
        tmp_path,
        [
            "npm error code ETARGET",
            "npm error notarget No matching version found for @ai-sdk/provider@99.0.0-nonexistent.",
            'ERROR: process "/bin/sh -c npm install --production" did not complete successfully: exit code: 1',
        ],
    ).find_test_failure_summaries()

    assert result["chunks"] == []
    assert "no Japa failure block found" in result["warning"]
    assert "fatal_error_block" not in str(result)
