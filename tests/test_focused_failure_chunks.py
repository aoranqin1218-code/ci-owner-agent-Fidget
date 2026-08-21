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
