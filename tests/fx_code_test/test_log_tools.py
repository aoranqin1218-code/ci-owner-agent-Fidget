from __future__ import annotations

from ci_owner_agent.services.log_parsing import log_detect_final_status
from ci_owner_agent.services.log_provider import LocalFileLogProvider


def test_detect_final_status_uses_finished_line(logs):
    assert log_detect_final_status(logs["success"].read_text(encoding="utf-8")) == "SUCCESS"
    assert log_detect_final_status("ERROR only\nFAILED only\n") == "UNKNOWN"


def test_local_log_tools(logs):
    provider = LocalFileLogProvider(logs["auth_failed"], max_output_chars=200)
    tail = provider.read_tail(lines=2).model_dump()
    assert tail["startLine"] == 3
    assert tail["endLine"] == 4
    assert "Finished: FAILURE" in tail["content"]

    search = provider.search("file_size_exceeded", context_lines=1, max_matches=2)
    assert search["matches"][0]["line"] == 2

    subset = provider.read_range(2, 3)
    assert "classify.ts" in subset["content"]

    chunks = provider.find_error_chunks(chunk_lines=2, max_chunks=1)
    assert chunks["chunks"]
    assert provider.detect_final_status() == "FAILURE"
