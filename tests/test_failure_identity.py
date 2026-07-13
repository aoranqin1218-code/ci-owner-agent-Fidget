from __future__ import annotations

import re

import pytest

from ci_owner_agent.schemas import FailureFact
from ci_owner_agent.services.failure_identity import (
    build_failure_fact_signature,
    build_responsibility_signature,
    canonicalize_failure_message,
    canonicalize_failure_signature,
    has_meaningful_failure_identity,
)


OBJECT_A = "6a546a6c5740ffb771af5462"
OBJECT_B = "7b1234567890abcdef123456"


def mongo_signature(object_id: str, *, collection: str = "finex.bpm_tasks", index: str = "_id_") -> str:
    return build_responsibility_signature(
        failure_title=f"E11000 duplicate key error on {collection}.{index}",
        failure_summary=(
            f"MongoDB duplicate key error collection: {collection}\n"
            f"index: {index}\ndup key: {{ _id: ObjectId('{object_id}') }}"
        ),
        existing_signature=f"E11000_duplicate_key_{collection}_{index}_{object_id}",
        error_code="E11000",
    )


def test_mongodb_object_ids_produce_same_structured_signature():
    expected = "mongodb_duplicate_key|e11000|finex.bpm_tasks|_id_"
    assert mongo_signature(OBJECT_A) == expected
    assert mongo_signature(OBJECT_B) == expected


def test_mongodb_collection_and_index_remain_identity_dimensions():
    assert mongo_signature(OBJECT_A, collection="finex.bpm_tasks") != mongo_signature(OBJECT_A, collection="finex.users")
    assert mongo_signature(OBJECT_A, index="_id_") != mongo_signature(OBJECT_A, index="task_id_1")


@pytest.mark.parametrize(
    "value",
    [
        f"ObjectId('{OBJECT_A}')",
        f'ObjectId("{OBJECT_A}")',
        f"new ObjectId('{OBJECT_A}')",
        OBJECT_A,
        f"_id_{OBJECT_A}",
        f"id={OBJECT_A}",
    ],
)
def test_object_id_forms_are_canonicalized(value):
    assert OBJECT_A not in canonicalize_failure_signature(value)
    assert "<object_id>" in canonicalize_failure_signature(value)


@pytest.mark.parametrize(
    ("value", "placeholder"),
    [
        ("550e8400-e29b-41d4-a716-446655440000", "<uuid>"),
        ("2026-07-13T10:20:30.123Z", "<timestamp>"),
        ("2026-07-13 10:20:30", "<timestamp>"),
        ("1752388888123", "<timestamp>"),
        ("12345ms", "<duration>"),
        ("12.5s", "<duration>"),
        ("3 minutes", "<duration>"),
        (r"C:\Users\me\AppData\Local\Temp\abc123", "<tmp_path>"),
        ("/tmp/build-123", "<tmp_path>"),
        ("localhost:54321", "<port>"),
        ("port 54321", "<port>"),
        ("requestId=abcdef123456", "<requestid>"),
        ("traceId=abcdef123456", "<traceid>"),
        ("sessionId=abcdef123456", "<sessionid>"),
    ],
)
def test_dynamic_values_are_canonicalized(value, placeholder):
    assert placeholder in canonicalize_failure_message(value)


@pytest.mark.parametrize(
    ("value", "terms"),
    [
        ("typescript_compile_error_ts2305_classifyErrorMessage", ("ts2305", "classifyerrormessage")),
        ("workflow_back_task_timeout_123456789012", ("workflow", "back_task", "timeout")),
        ("payment_order_retry_stage_20260713", ("payment", "retry")),
        ("module_resolution_error_ts2305_export_symbol", ("module", "ts2305", "symbol")),
        ("BackTaskCmdTest_retry_123456789012", ("backtaskcmdtest", "retry")),
        ("server_workflow_MongoQueryRunner_insert_20260713", ("workflow", "mongoqueryrunner", "insert")),
    ],
)
def test_long_semantic_identifiers_are_preserved(value, terms):
    canonical = canonicalize_failure_signature(value)
    assert canonical != "<random>"
    assert "<random>" not in canonical
    assert all(term in canonical for term in terms)


@pytest.mark.parametrize(
    ("value", "placeholder"),
    [
        ("requestId=abcdef1234567890abcdef1234567890", "<requestid>"),
        ("traceId:abcdef1234567890abcdef1234567890", "<traceid>"),
        ("sessionId=abcdef1234567890abcdef1234567890", "<sessionid>"),
        ("correlationId=abcdef1234567890abcdef1234567890", "<correlationid>"),
        ("1234567890abcdef1234567890abcdef12345678", "<hash>"),
    ],
)
def test_explicit_long_dynamic_values_are_still_canonicalized(value, placeholder):
    assert placeholder in canonicalize_failure_message(value)


def test_line_numbers_are_removed_but_path_is_preserved():
    assert canonicalize_failure_message("test/a.ts:12:3 server/a.ts:80") == "test/a.ts server/a.ts"


@pytest.mark.parametrize("code", ["TS2305", "E11000", "HTTP500"])
def test_semantic_error_codes_are_not_destroyed(code):
    assert code.lower() in canonicalize_failure_signature(code)


def test_canonicalizer_is_idempotent_and_empty_safe():
    value = f"Error ObjectId('{OBJECT_A}') at test/a.ts:12:3 after 12.5s"
    canonical = canonicalize_failure_signature(value)
    assert canonicalize_failure_signature(canonical) == canonical
    assert canonicalize_failure_signature(None) == ""
    assert canonicalize_failure_message(None) == ""


def test_failure_fact_rebuilds_dynamic_model_signature_and_id():
    def make(object_id: str) -> FailureFact:
        return FailureFact(
            signatureKey=f"wrong-model-signature-{object_id}",
            historyEligible=True,
            failureKind="mongodb_duplicate_key",
            errorCode="E11000",
            errorType="MongoServerError",
            message=(
                "E11000 duplicate key error collection: finex.bpm_tasks "
                f"index: _id_ dup key: {{ _id: ObjectId('{object_id}') }}"
            ),
            rootCauseSummary="MongoDB duplicate key error collection: finex.bpm_tasks index: _id_",
            confidence=0.95,
        )

    first = make(OBJECT_A)
    second = make(OBJECT_B)
    assert first.signatureKey == "mongodb_duplicate_key|e11000|finex.bpm_tasks|_id_"
    assert second.signatureKey == first.signatureKey
    assert second.factId == first.factId
    assert re.fullmatch(r"fact-[0-9a-f]{12}", first.factId or "")


def test_ineligible_failure_fact_still_has_stable_identity():
    first = FailureFact(
        signatureKey=f"generic-{OBJECT_A}",
        historyEligible=False,
        isGenericWrapper=True,
        failureKind="generic_wrapper",
        message=f"wrapper id={OBJECT_A}",
        rootCauseSummary="generic wrapper",
        confidence=0.4,
    )
    second = first.model_copy(update={"signatureKey": f"generic-{OBJECT_B}", "message": f"wrapper id={OBJECT_B}", "factId": None})
    second = FailureFact.model_validate(second.model_dump())
    assert build_failure_fact_signature(first) == build_failure_fact_signature(second)
    assert first.factId == second.factId


@pytest.mark.parametrize(
    ("message", "root_cause"),
    [
        ("", ""),
        (f"ObjectId('{OBJECT_A}')", "<timestamp>"),
        ("ERROR: process did not complete successfully", "generic wrapper"),
    ],
)
def test_unusable_failure_fact_is_downgraded(message, root_cause):
    fact = FailureFact(
        signatureKey="model_claimed_identity",
        historyEligible=True,
        isGenericWrapper=False,
        failureKind="",
        message=message,
        rootCauseSummary=root_cause,
        confidence=0.99,
    )

    assert fact.signatureKey == "unknown_failure"
    assert fact.historyEligible is False
    assert fact.isGenericWrapper is True
    assert re.fullmatch(r"fact-[0-9a-f]{12}", fact.factId or "")
    assert has_meaningful_failure_identity(fact) is False


def test_unknown_failure_fact_id_is_stable():
    first = FailureFact(
        signatureKey="first-model-shell",
        historyEligible=True,
        failureKind="",
        message="",
        rootCauseSummary="",
        confidence=0.99,
    )
    second = FailureFact(
        signatureKey="second-model-shell",
        historyEligible=True,
        failureKind="",
        message=f"ObjectId('{OBJECT_B}')",
        rootCauseSummary="<uuid>",
        confidence=0.99,
    )

    assert first.signatureKey == second.signatureKey == "unknown_failure"
    assert first.factId == second.factId


@pytest.mark.parametrize(
    "message",
    [
        'ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 1',
        'ERROR: process "/bin/bash -c make test" did not complete successfully: exit code: 2',
        'ERROR: failed to solve: process "/bin/sh -c npm install" did not complete successfully: exit code: 1',
        'ERROR: failed to solve: executor failed running [/bin/sh -c npm run build]: exit code: 1',
        "Dockerfile:27 ERROR: failed to solve",
        "make: *** [Makefile:10: test] Error 2",
        "make: *** [docker-test] Error 1",
        "gmake[2]: *** [target] Error 1",
        "script returned exit code 1",
        "Jenkins shell returned exit code 1",
        "hudson.AbortException: script returned exit code 1",
        "Process exited with code 1",
        "command terminated with exit code 1",
        "exit status 1",
        "npm ERR! command failed",
        "npm ERR! command sh -c npm run build",
        "yarn run failed with exit code 1",
        "pnpm run build exited with code 1",
    ],
)
def test_real_ci_wrapper_fact_is_downgraded(message):
    fact = FailureFact(
        signatureKey="model-wrapper",
        historyEligible=True,
        isGenericWrapper=False,
        failureKind="build_failure",
        message=message,
        rootCauseSummary="command failed",
        confidence=0.99,
    )

    assert fact.signatureKey == "unknown_failure"
    assert fact.historyEligible is False
    assert fact.isGenericWrapper is True


@pytest.mark.parametrize(
    "message",
    [
        f"id=ObjectId('{OBJECT_A}')",
        "timestamp=<timestamp>",
        "duration=<duration>",
        "uuid=<uuid>",
        "port=<port>",
        "requestId=<requestid>",
        "traceId=<traceid>",
        "sessionId=<sessionid>",
        "correlationId=<correlationid>",
    ],
)
def test_placeholder_only_fact_is_downgraded(message):
    fact = FailureFact(
        signatureKey="model-placeholder",
        historyEligible=True,
        failureKind="",
        message=message,
        rootCauseSummary="timestamp=<timestamp>",
        confidence=0.99,
    )

    assert fact.signatureKey == "unknown_failure"
    assert fact.historyEligible is False
    assert fact.isGenericWrapper is True


@pytest.mark.parametrize(
    ("message", "root_cause", "expected_signature"),
    [
        (
            'ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 1\n'
            "TS2305: Module has no exported member 'Foo'",
            "Module has no exported member Foo",
            None,
        ),
        (
            "ERROR: failed to solve\nnpm ERR! code ETARGET\nNo matching version found for @ai-sdk/provider",
            "No matching version found for @ai-sdk/provider",
            None,
        ),
        (
            "make: *** [test] Error 2\nMongoServerError: E11000 duplicate key error "
            "collection: finex.bpm_tasks index: _id_",
            "Mongo duplicate key",
            "mongodb_duplicate_key|e11000|finex.bpm_tasks|_id_",
        ),
        (
            "script returned exit code 1\nTypeError: Cannot read properties of undefined\nat server/workflow/service.ts:42",
            "TypeError in service",
            None,
        ),
    ],
)
def test_wrapper_with_inner_failure_remains_eligible(message, root_cause, expected_signature):
    fact = FailureFact(
        signatureKey="model-inner-failure",
        historyEligible=True,
        isGenericWrapper=False,
        failureKind="build_failure",
        message=message,
        rootCauseSummary=root_cause,
        confidence=0.99,
    )

    assert fact.historyEligible is True
    assert fact.isGenericWrapper is False
    assert fact.signatureKey != "unknown_failure"
    if expected_signature:
        assert fact.signatureKey == expected_signature
