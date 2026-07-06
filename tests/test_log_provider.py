from __future__ import annotations

from ci_owner_agent.services.log_provider import LocalFileLogProvider


def provider(tmp_path, lines: list[str]) -> LocalFileLogProvider:
    path = tmp_path / "console.log"
    path.write_text("\n".join(["[Pipeline] { (Test)", "+ make docker-test", *lines, "[Pipeline] // stage"]), encoding="utf-8")
    return LocalFileLogProvider(path)


def test_mocha_failure_summary_still_extracts(tmp_path):
    result = provider(
        tmp_path,
        [
            "1) SomeSuite should do something",
            "AssertionError: expected 1 to equal 2",
            "    at test/server/foo.test.ts:10:5",
        ],
    ).find_test_failure_summaries()

    assert result["chunks"]
    assert result["chunks"][0]["anchorType"] == "mocha_failure_block"
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
    assert "no Mocha/Japa failure block found" in result["warning"]
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
    assert "no Mocha/Japa failure block found" in result["warning"]
    assert "fatal_error_block" not in str(result)
