from __future__ import annotations

from ci_owner_agent.services.log_provider import LocalFileLogProvider, log_detect_final_status
from ci_owner_agent.tools.log_tools import log_find_error_chunks, log_read_range, log_read_tail, log_search


def test_detect_final_status_uses_finished_line(logs):
    assert log_detect_final_status(logs["success"].read_text(encoding="utf-8")) == "SUCCESS"
    assert log_detect_final_status("ERROR only\nFAILED only\n") == "UNKNOWN"


def test_local_log_tools(logs):
    provider = LocalFileLogProvider(logs["auth_failed"], max_output_chars=200)
    tail = log_read_tail(provider, lines=2)
    assert tail["startLine"] == 3
    assert tail["endLine"] == 4
    assert "Finished: FAILURE" in tail["content"]

    search = log_search(provider, "file_size_exceeded", contextLines=1, maxMatches=2)
    assert search["matches"][0]["line"] == 2

    subset = log_read_range(provider, 2, 3)
    assert "classify.ts" in subset["content"]

    chunks = log_find_error_chunks(provider, chunkLines=2, maxChunks=1)
    assert chunks["chunks"]
    assert provider.detect_final_status() == "FAILURE"
