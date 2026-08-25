from __future__ import annotations

from ci_owner_agent.services.log_provider import JenkinsLogProvider, LocalFileLogProvider


def write_log(tmp_path, text: str):
    path = tmp_path / "console.log"
    path.write_text(text, encoding="utf-8")
    return LocalFileLogProvider(path)


def test_find_focused_failure_chunks_extracts_japa_failure_block(tmp_path):
    provider = write_log(
        tmp_path,
        "\n".join(
            [
                "Checking out Revision abcdef123456",
                "docker build noise",
                "[Pipeline] { (Unit Tests)",
                "✔ passing test",
                "✖ ViewDataQueryServiceTest 数据查询",
                "AssertionError: expected true to equal false",
                "at Context.<anonymous> (packages/fidget-core/test/data/ViewDataQueryTest.ts:7:36)",
                "[Pipeline] // stage",
                "post actions",
            ]
        ),
    )
    result = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)
    chunk = result["chunks"][0]
    assert chunk["chunkSource"] == "japa_failure_block"
    assert chunk["stageName"] == "Unit Tests"
    assert "ViewDataQueryServiceTest" in chunk["content"]
    assert "AssertionError" in chunk["content"]
    assert "packages/fidget-core/test/data/ViewDataQueryTest.ts" in chunk["content"]
    assert "Checking out Revision" not in chunk["content"]
    assert "docker build noise" not in chunk["content"]


def test_find_focused_failure_chunks_extracts_multiple_japa_blocks(tmp_path):
    provider = write_log(
        tmp_path,
        "\n".join(
            [
                "✖ first failure",
                "Error: first",
                "at packages/fidget-core/test/firstTest.ts:1:1",
                "✖ second failure",
                "Error: second",
                "at packages/fidget-lake/test/secondTest.ts:2:2",
            ]
        ),
    )
    result = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)
    assert len(result["chunks"]) == 2
    assert [chunk["chunkIndex"] for chunk in result["chunks"]] == [0, 1]
    assert result["chunks"][0]["startLine"] < result["chunks"][1]["startLine"]


def test_find_focused_failure_chunks_console_tail_fallback(tmp_path):
    provider = write_log(tmp_path, "checkout noise\nrandom error\nFinished: FAILURE\n")
    result = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)
    chunk = result["chunks"][0]
    assert chunk["chunkSource"] == "local_console_tail_fallback"
    assert "warning" in result


def test_find_focused_failure_chunks_tail_lines_limit(tmp_path):
    lines = ["noise"] * 1000
    lines.extend(
        [
            "✖ final failure",
            "Error: final failure",
            "at packages/fidget-sql/test/finalTest.ts:1:1",
        ]
    )
    provider = write_log(tmp_path, "\n".join(lines))
    chunk = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)["chunks"][0]
    assert len(chunk["content"].splitlines()) <= 500
    assert chunk["startLine"] > 1
    assert chunk["endLine"] == len(lines)
    assert "Error: final failure" in chunk["content"]


def test_focused_failure_chunk_truncates_from_head_and_keeps_final_failure(tmp_path):
    lines = ["✖ final failure"]
    lines.extend("noise " + ("x" * 80) for _ in range(200))
    lines.extend(
        [
            "Error: final expected value",
            "at packages/fidget-core/test/data/ViewDataQueryTest.ts:7:3628",
        ]
    )
    path = tmp_path / "console.log"
    path.write_text("\n".join(lines), encoding="utf-8")
    provider = LocalFileLogProvider(path, max_output_chars=260)
    chunk = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)["chunks"][0]
    assert chunk["truncated"] is True
    assert "Error: final expected value" in chunk["content"]
    assert "ViewDataQueryTest.ts" in chunk["content"]


def test_long_single_japa_block_keeps_anchor_for_structured_summary(tmp_path):
    lines = ["✖ deliberately long Japa failure"]
    lines.extend(f"wrapper noise {index}" for index in range(700))
    lines.extend(["AssertionError: final expected value", "at packages/fidget-sql/test/SqlLexerTest.ts:12:3"])
    path = tmp_path / "console.log"
    path.write_text("\n".join(lines), encoding="utf-8")

    summaries = LocalFileLogProvider(path, max_output_chars=260).find_test_failure_summaries(
        tail_lines=100,
        max_chunks=5,
    )

    assert summaries["totals"]["japa"] == 1
    assert len(summaries["chunks"]) == 1
    chunk = summaries["chunks"][0]
    assert "deliberately long Japa failure" in chunk["content"]
    assert "AssertionError: final expected value" in chunk["content"]


def test_interleaved_japa_detail_uses_its_matching_assertion_and_test_file(tmp_path):
    lines = [
        "✖ controlled failure (1.16ms)",
        "OtherTest (test/other/OtherTest.ts)",
        *[f"✔ unrelated test {index}" for index in range(180)],
        "❯ SqlLexer / controlled failure",
        "ℹ AssertionError: expected actual to equal expected",
        "⁃ at Assert.equal (/var/opt/build/node_modules/@japa/assert/build/index.js:69:19)",
        "⁃ at Object.executor (test/lexer/SqlLexerTest.ts:179:16)",
        "FAILED",
    ]
    path = tmp_path / "console.log"
    path.write_text("\n".join(lines), encoding="utf-8")

    chunk = LocalFileLogProvider(path).find_test_failure_summaries(max_chunks=5)["chunks"][0]

    signature = chunk["signature"]
    assert signature["testName"] == "controlled failure (1.16ms)"
    assert signature["errorType"] == "AssertionError"
    assert signature["testFile"] == "test/lexer/SqlLexerTest.ts"
    assert signature["topStackFile"] == "test/lexer/SqlLexerTest.ts"
    assert "OtherTest.ts" not in chunk["content"]


def test_jenkins_focused_failure_chunks_keeps_japa_source():
    class FakeJenkinsClient:
        def get_console_text(self, job, build_number):
            return {
                "ok": True,
                "content": "\n".join(
                    [
                        "✖ remote failure",
                        "Error: failed",
                        "at packages/fidget-mongo/test/remoteTest.ts:1:1",
                    ]
                ),
            }

    provider = JenkinsLogProvider(FakeJenkinsClient(), "job", 1)
    chunk = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)["chunks"][0]
    assert chunk["chunkSource"] == "japa_failure_block"
    assert chunk["stageName"] == "Unit Tests"
