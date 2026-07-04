from __future__ import annotations

from ci_owner_agent.services.log_provider import LocalFileLogProvider


def provider(tmp_path, lines: list[str]) -> LocalFileLogProvider:
    path = tmp_path / "console.log"
    path.write_text("\n".join(["[Pipeline] stage", "[Pipeline] { (Test)", *lines, "[Pipeline] // stage"]), encoding="utf-8")
    return LocalFileLogProvider(path)


def test_get_js_sdk_config_failure_summary_signature(tmp_path):
    lines = ["INFO benchmark noise"] * 80
    lines.extend(
        [
            "5425 passing",
            "142 pending",
            "1 failing",
            "",
            "1) getJsSdkConfig dingtalk ua",
            "     dingtalk ua dingtalk corpId:",
            "   Error: UNKNOWN",
            "    at Object.Corp (node_modules/@fx/corp-core/src/errors/Factory.ts:260:36)",
            "    at DingTalkService.getSuiteApiByIntegrateSuiteId (server/services/integrate/DingTalkService.ts:10:2)",
            "    at DingtalkApiService.getJsSdkConfig (server/services/integrate/dingtalk.integrate.ts:20:2)",
            "    at IntegrateService.getJsSdkConfig (server/services/integrate/integrate.ts:30:2)",
            "    at test/server/services/integrate/integrate.service.test.ts",
            "------",
            "Dockerfile:12",
            "ERROR: failed to solve:",
        ]
    )
    chunk = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"][0]
    assert chunk["chunkSource"] == "local_test_failure_summary"
    assert "getJsSdkConfig dingtalk ua" in chunk["content"]
    assert "Error: UNKNOWN" in chunk["content"]
    assert "test/server/services/integrate/integrate.service.test.ts" in chunk["content"]
    assert "INFO benchmark noise" not in chunk["content"]
    assert "Dockerfile" not in chunk["content"]
    signature = chunk["signature"]
    assert signature["testName"] == "getJsSdkConfig dingtalk ua"
    assert signature["errorType"] == "Error"
    assert "unknown" in signature["errorMessage"]
    assert signature["testFile"] == "test/server/services/integrate/integrate.service.test.ts"
    assert signature["signatureKey"]
    assert chunk["signatureHash"]


def test_view_data_query_service_failure_summary_signature(tmp_path):
    lines = [
        "5058 passing",
        "142 pending",
        "1 failing",
        "",
        "1) ViewDataQueryServiceTest 数据查询 - 流程表单:",
        "   AssertionError: expected { a: 1 } to deeply equal { a: 2 }",
        "   + expected",
        "   - actual",
        "   at Context.<anonymous> (test/server/services/form_view/data_query/ViewDataQueryServiceTest.ts:7:3628)",
    ]
    chunk = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"][0]
    signature = chunk["signature"]
    assert signature["testName"] == "ViewDataQueryServiceTest"
    assert "数据查询 - 流程表单" in signature["testCase"]
    assert signature["errorType"] == "AssertionError"
    assert signature["testFile"] == "test/server/services/form_view/data_query/ViewDataQueryServiceTest.ts"


def test_multiple_failure_blocks_return_multiple_chunks(tmp_path):
    lines = [
        "2 failing",
        "1) ATest first case:",
        "   Error: A",
        "   at test/a.test.ts:1:2",
        "2) BTest second case:",
        "   TypeError: B",
        "   at test/b.test.ts:3:4",
    ]
    chunks = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)["chunks"]
    assert len(chunks) == 2
    assert [chunk["chunkIndex"] for chunk in chunks] == [0, 1]
    assert chunks[0]["signature"]["testName"] == "ATest"
    assert chunks[1]["signature"]["testName"] == "BTest"
    assert chunks[0]["signature"]["signatureKey"] != chunks[1]["signature"]["signatureKey"]
    assert chunks[0]["startLine"] < chunks[1]["startLine"]


def test_no_failure_summary_returns_empty_chunks(tmp_path):
    result = provider(tmp_path, ["all good", "5425 passing"]).find_test_failure_summaries(tail_lines=500, max_chunks=5)
    assert result["chunks"] == []
    assert "warning" in result


def test_buildkit_prefixed_failure_summary_signature(tmp_path):
    lines = [
        "#28 973.7   1 failing",
        "#28 973.7   1) getJsSdkConfig dingtalk ua",
        "#28 973.7      dingtalk ua dingtalk corpId:",
        "#28 973.7    Error: UNKNOWN",
        "#28 973.7      at Object.Corp (node_modules/@fx/corp-core/src/errors/Factory.ts:260:36)",
        "#28 973.7      at test/server/services/integrate/integrate.service.test.ts",
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
    assert signature["testFile"] == "test/server/services/integrate/integrate.service.test.ts"
    assert "Error: UNKNOWN" in chunk["content"]
    assert "test/server/services/integrate/integrate.service.test.ts" in chunk["content"]
    assert "#28" not in chunk["content"]
    assert "ERROR: process" not in chunk["content"]
    assert "Dockerfile:" not in chunk["content"]
    assert "ERROR: failed to solve:" not in chunk["content"]


def test_ansi_and_buildkit_prefixed_failure_summary_signature(tmp_path):
    lines = [
        "#28 851.7 \x1b[31m  1 failing\x1b[0m",
        "#28 851.7 \x1b[0m  1) getJsSdkConfig dingtalk ua",
        "#28 851.7        dingtalk ua dingtalk corpId:",
        "#28 851.7 \x1b[0m\x1b[31m     Error: UNKNOWN\x1b[0m\x1b[90m",
        "#28 851.7       at Object.Corp (node_modules/@fx/corp-core/src/errors/Factory.ts:260:36)",
        "#28 851.7       at test/server/services/integrate/integrate.service.test.ts",
    ]
    result = provider(tmp_path, lines).find_test_failure_summaries(tail_lines=500, max_chunks=5)
    assert result["chunks"]
    chunk = result["chunks"][0]
    signature = chunk["signature"]
    assert signature["testName"] == "getJsSdkConfig dingtalk ua"
    assert signature["errorType"] == "Error"
    assert "unknown" in signature["errorMessage"]
    assert signature["testFile"] == "test/server/services/integrate/integrate.service.test.ts"
    assert "\x1b[" not in chunk["content"]
    assert "#28" not in chunk["content"]
    assert "Error: UNKNOWN" in chunk["content"]
