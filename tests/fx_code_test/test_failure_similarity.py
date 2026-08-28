from __future__ import annotations

from ci_owner_agent.services.failure_similarity import chunk_similarity, normalize_error_chunk


def test_normalize_error_chunk_ignores_line_numbers_and_hashes():
    a = "Error at test/a.ts:12:3 commit 1234567 duration 123ms"
    b = "Error at test/a.ts:99:10 commit abcdef1234567890 duration 1.23s"
    assert normalize_error_chunk(a) == normalize_error_chunk(b)


def test_normalize_error_chunk_preserves_chinese_and_expected_actual():
    text = "测试 getJsSdkConfig 钉钉 UA\nExpected: dingtalk\nActual: unknown"
    normalized = normalize_error_chunk(text)
    assert "测试" in normalized
    assert "expected" in normalized
    assert "dingtalk" in normalized
    assert "actual" in normalized
    assert "unknown" in normalized


def test_normalize_error_chunk_preserves_long_identifiers():
    text = "ViewDataQueryServiceTest FormDataPageChartVizAdapter veryLongFileNameForBusinessCase.ts"
    normalized = normalize_error_chunk(text)
    assert "viewdataqueryservicetest" in normalized
    assert "formdatapagechartvizadapter" in normalized
    assert "verylongfilenameforbusinesscase.ts" in normalized
    assert "<id>" not in normalized


def test_normalize_error_chunk_replaces_uuid_and_object_id():
    text = "request 550e8400-e29b-41d4-a716-446655440000 object 64b7f0d2e138237b4c9f1234"
    normalized = normalize_error_chunk(text)
    assert "<uuid>" in normalized
    assert "<object_id>" in normalized
    assert "550e8400" not in normalized
    assert "64b7f0d2e138237b4c9f1234" not in normalized


def test_chunk_similarity_same_get_js_sdk_config_is_high():
    a = "FAIL test getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown\nat test/packages/fxp-ai/errors/classify.test.ts:1:23\n123ms"
    b = "FAIL test getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown\nat test/packages/fxp-ai/errors/classify.test.ts:99:8\nabcdef123456\n1.23s"
    assert chunk_similarity(a, b) >= 0.92


def test_chunk_similarity_unrelated_failure_is_low():
    a = "FAIL test getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown"
    b = "FAIL user login form validates password\nTypeError: cannot read property token\nExpected: 200\nActual: 500"
    assert chunk_similarity(a, b) < 0.75
