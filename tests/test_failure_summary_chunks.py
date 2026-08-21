from __future__ import annotations

from ci_owner_agent.services.log_provider import LocalFileLogProvider


def provider(tmp_path, lines: list[str]) -> LocalFileLogProvider:
    path = tmp_path / "console.log"
    path.write_text("\n".join(["[Pipeline] stage", "[Pipeline] { (Test)", *lines, "[Pipeline] // stage"]), encoding="utf-8")
    return LocalFileLogProvider(path)


def test_japa_failure_summary_signature(tmp_path):
    lines = ["INFO benchmark noise"] * 80
    lines.extend(
        [
            "✖ getJsSdkConfig dingtalk ua",
            "Error: UNKNOWN",
            "at packages/fidget-core/test/integrate/IntegrateTest.ts:20:2",
            "------",
            "Dockerfile:12",
            "ERROR: failed to solve:",
        ]
    )
    chunk = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"][0]
    assert chunk["chunkSource"] == "local_test_failure_summary"
    assert "getJsSdkConfig dingtalk ua" in chunk["content"]
    assert "Error: UNKNOWN" in chunk["content"]
    assert "packages/fidget-core/test/integrate/IntegrateTest.ts" in chunk["content"]
    assert "INFO benchmark noise" not in chunk["content"]
    assert "Dockerfile" not in chunk["content"]
    signature = chunk["signature"]
    assert signature["testName"] == "getJsSdkConfig dingtalk ua"
    assert signature["errorType"] == "Error"
    assert "unknown" in signature["errorMessage"]
    assert signature["testFile"] == "packages/fidget-core/test/integrate/IntegrateTest.ts"
    assert signature["signatureKey"]
    assert chunk["signatureHash"]


def test_japa_package_failure_summary_signature(tmp_path):
    lines = [
        "✖ ViewDataQueryServiceTest 数据查询 - 流程表单",
        "AssertionError: expected { a: 1 } to deeply equal { a: 2 }",
        "at packages/fidget-lake/test/data/ViewDataQueryTest.ts:7:3628",
    ]
    chunk = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"][0]
    signature = chunk["signature"]
    assert signature["testName"] == "ViewDataQueryServiceTest 数据查询 - 流程表单"
    assert signature["testCase"] == "ViewDataQueryServiceTest 数据查询 - 流程表单"
    assert signature["errorType"] == "AssertionError"
    assert signature["testFile"] == "packages/fidget-lake/test/data/ViewDataQueryTest.ts"


def test_multiple_failure_blocks_return_multiple_chunks(tmp_path):
    lines = [
        "✖ ATest first case",
        "Error: A",
        "at packages/fidget-core/test/aTest.ts:1:2",
        "✖ BTest second case",
        "TypeError: B",
        "at packages/fidget-core/test/bTest.ts:3:4",
    ]
    chunks = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"]
    assert len(chunks) == 2
    assert [chunk["chunkIndex"] for chunk in chunks] == [0, 1]
    assert chunks[0]["signature"]["testName"] == "ATest first case"
    assert chunks[1]["signature"]["testName"] == "BTest second case"
    assert chunks[0]["signature"]["signatureKey"] != chunks[1]["signature"]["signatureKey"]
    assert chunks[0]["startLine"] < chunks[1]["startLine"]


def test_no_failure_summary_returns_empty_chunks(tmp_path):
    result = provider(tmp_path, ["all good", "5425 passing"]).find_test_failure_summaries(tail_lines=500, max_chunks=5)
    assert result["chunks"] == []
    assert "warning" in result


def test_xfail_timeout_summary_signature_webhook(tmp_path):
    lines = [
        "✖ Webhook触发",
        "",
        "Run",
        "AwaitFunc",
        "Timeout!",
        "",
        "at Timeout.<anonymous> modules/automation/tests/venv.ts:834",
    ]
    chunk = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"][0]
    signature = chunk["signature"]
    assert chunk["anchorType"] == "japa_failure_block"
    assert signature["testName"] == "Webhook触发"
    assert signature["errorType"] == "Timeout"
    assert "run awaitfunc timeout" in signature["errorMessage"]
    assert signature["testFile"] == "modules/automation/tests/venv.ts"
    assert signature["signatureKey"]
    assert chunk["signatureHash"]


def test_xfail_multiple_timeout_blocks(tmp_path):
    lines = [
        "✖ 大模型-自定义提示词-接收回调-执行成功",
        "Run",
        "AwaitFunc",
        "Timeout!",
        "✖ 多模态大模型-自定义提示词-接收回调-解析失败",
        "Run",
        "AwaitFunc",
        "Timeout!",
    ]
    chunks = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"]
    assert len(chunks) == 2
    assert [chunk["chunkIndex"] for chunk in chunks] == [0, 1]
    assert chunks[0]["signature"]["testName"].startswith("大模型")
    assert chunks[1]["signature"]["testName"].startswith("多模态大模型")
    assert chunks[0]["signature"]["signatureKey"] != chunks[1]["signature"]["signatureKey"]


def test_fatal_error_unsupported_dir_import_no_longer_generates_summary(tmp_path):
    lines = [
        "✖ ERROR: Error: Directory import '/var/app/server/components' is not supported resolving ES modules imported from /var/app/packages/fidget-core/test/initTest.ts",
        "code: 'ERR_UNSUPPORTED_DIR_IMPORT'",
        "url: 'file:///var/app/server/components'",
    ]
    result = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)
    assert result["chunks"] == []
    assert "no Japa failure block found" in result["warning"]


def test_buildkit_prefixed_failure_summary_signature(tmp_path):
    lines = [
        "#28 973.7   ✖ getJsSdkConfig dingtalk ua",
        "#28 973.7    Error: UNKNOWN",
        "#28 973.7      at packages/fidget-core/test/integrate/IntegrateTest.ts",
        "#28 ERROR: process \"/bin/sh -c make docker-test\" did not complete successfully",
        "------",
        "Dockerfile:",
        "ERROR: failed to solve:",
        "make: *** [docker-test] Error 1",
    ]
    chunk = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"][0]
    signature = chunk["signature"]
    assert chunk["chunkSource"] == "local_test_failure_summary"
    assert signature["testName"] == "getJsSdkConfig dingtalk ua"
    assert signature["errorType"] == "Error"
    assert "unknown" in signature["errorMessage"]
    assert signature["testFile"] == "packages/fidget-core/test/integrate/IntegrateTest.ts"
    assert "Error: UNKNOWN" in chunk["content"]
    assert "packages/fidget-core/test/integrate/IntegrateTest.ts" in chunk["content"]
    assert "#28" not in chunk["content"]
    assert "ERROR: process" not in chunk["content"]
    assert "Dockerfile:" not in chunk["content"]
    assert "ERROR: failed to solve:" not in chunk["content"]


def test_ansi_and_buildkit_prefixed_failure_summary_signature(tmp_path):
    lines = [
        "#28 851.7 \x1b[31m  ✖ getJsSdkConfig dingtalk ua\x1b[0m",
        "#28 851.7 \x1b[0m\x1b[31m     Error: UNKNOWN\x1b[0m\x1b[90m",
        "#28 851.7       at packages/fidget-core/test/integrate/IntegrateTest.ts",
    ]
    result = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)
    assert result["chunks"]
    chunk = result["chunks"][0]
    signature = chunk["signature"]
    assert signature["testName"] == "getJsSdkConfig dingtalk ua"
    assert signature["errorType"] == "Error"
    assert "unknown" in signature["errorMessage"]
    assert signature["testFile"] == "packages/fidget-core/test/integrate/IntegrateTest.ts"
    assert "\x1b[" not in chunk["content"]
    assert "#28" not in chunk["content"]
    assert "Error: UNKNOWN" in chunk["content"]


def test_ts2305_docker_wrapper_does_not_generate_summary_chunk(tmp_path):
    lines = [
        "src/index.ts(10,27): error TS2305: Module '\"./errors\"' has no exported member 'classifyErrorMessage'.",
        'ERROR: process "/bin/sh -c npm run nx:build && npm run test" did not complete successfully: exit code: 130',
    ]
    result = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)
    assert result["chunks"] == []
    assert "no Japa failure block found" in result["warning"]
    assert "fatal_error_block" not in str(result)
    assert "fatal|fatal error|fatal error||" not in str(result)


def test_npm_etarget_docker_wrapper_does_not_generate_summary_chunk(tmp_path):
    lines = [
        "npm error code ETARGET",
        "npm error notarget No matching version found for @ai-sdk/provider@99.0.0-nonexistent.",
        'ERROR: process "/bin/sh -c npm install --production" did not complete successfully: exit code: 1',
    ]
    result = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)
    assert result["chunks"] == []
    assert "no Japa failure block found" in result["warning"]
    assert "fatal_error_block" not in str(result)
