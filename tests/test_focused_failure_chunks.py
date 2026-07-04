from __future__ import annotations

from ci_owner_agent.services.log_provider import LocalFileLogProvider


def write_log(tmp_path, text: str):
    path = tmp_path / "console.log"
    path.write_text(text, encoding="utf-8")
    return LocalFileLogProvider(path)


def test_find_focused_failure_chunks_extracts_test_stage_tail(tmp_path):
    provider = write_log(
        tmp_path,
        "\n".join(
            [
                "Checking out Revision abcdef123456",
                "docker build noise",
                "[Pipeline] stage",
                "[Pipeline] { (Test)",
                "+ make docker-test",
                "1 failing",
                "1) ViewDataQueryServiceTest",
                "   数据查询 - 流程表单:",
                "AssertionError: expected true to equal false",
                "at Context.<anonymous> (test/server/services/form_view/data_query/ViewDataQueryServiceTest.ts:7:3628)",
                "[Pipeline] }",
                "[Pipeline] // stage",
                "post actions",
            ]
        ),
    )
    result = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)
    chunk = result["chunks"][0]
    assert chunk["chunkSource"] == "local_test_stage_tail"
    assert chunk["stageName"] == "Test"
    assert "ViewDataQueryServiceTest" in chunk["content"]
    assert "AssertionError" in chunk["content"]
    assert "test/server/services/form_view/data_query/ViewDataQueryServiceTest.ts" in chunk["content"]
    assert "Checking out Revision" not in chunk["content"]
    assert "docker build noise" not in chunk["content"]


def test_find_focused_failure_chunks_falls_back_to_make_docker_test(tmp_path):
    provider = write_log(
        tmp_path,
        "checkout noise\n+ make docker-test\nAssertionError: expected\nFinished: FAILURE\n",
    )
    chunk = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)["chunks"][0]
    assert chunk["chunkSource"] == "local_make_docker_test_tail"
    assert chunk["anchorType"] == "make_docker_test_tail"
    assert "AssertionError" in chunk["content"]


def test_find_focused_failure_chunks_console_tail_fallback(tmp_path):
    provider = write_log(tmp_path, "checkout noise\nrandom error\nFinished: FAILURE\n")
    result = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)
    chunk = result["chunks"][0]
    assert chunk["chunkSource"] == "local_console_tail_fallback"
    assert "warning" in result


def test_find_focused_failure_chunks_tail_lines_limit(tmp_path):
    lines = ["[Pipeline] stage", "[Pipeline] { (Test)"]
    lines.extend(f"test line {idx}" for idx in range(1000))
    lines.extend(["AssertionError: final failure", "[Pipeline] // stage"])
    provider = write_log(tmp_path, "\n".join(lines))
    chunk = provider.find_focused_failure_chunks(tail_lines=500, max_chunks=3)["chunks"][0]
    assert len(chunk["content"].splitlines()) <= 500
    assert chunk["startLine"] > 1
    assert chunk["endLine"] == len(lines)
    assert "AssertionError: final failure" in chunk["content"]
