import datetime as dt
import sys
from types import SimpleNamespace

from scripts._langsmith_trace import (
    find_matching_root_run,
    serialize_langsmith_run,
    summarize_trace_payload,
    write_trace_artifact,
)


def test_find_matching_root_run_returns_newest_matching_run(monkeypatch):
    older = SimpleNamespace(id="older", start_time=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc))
    newer = SimpleNamespace(id="newer", start_time=dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc))
    records = {
        "older": SimpleNamespace(id="older", metadata={"buildNumber": "1"}),
        "newer": SimpleNamespace(id="newer", metadata={"buildNumber": "2"}),
    }

    class FakeClient:
        def list_runs(self, **_kwargs):
            return [older, newer]

        def read_run(self, run_id, **_kwargs):
            return records[run_id]

        def get_run_url(self, **_kwargs):
            return "https://trace.example/newer"

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=FakeClient))
    clock = iter([0.0, 0.0])
    monkeypatch.setattr("scripts._langsmith_trace.time.time", lambda: next(clock))

    result = find_matching_root_run(
        project_name="test-project",
        started_at=dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc),
        wait_seconds=1,
        metadata_matches=lambda metadata: metadata.get("buildNumber") == "2",
    )

    assert result["ok"] is True
    assert result["run"] is records["newer"]
    assert result["url"] == "https://trace.example/newer"


def test_trace_artifact_serialization_and_summary_are_shared(tmp_path):
    child = SimpleNamespace(
        id="child",
        name="repo_diff",
        run_type="tool",
        error=None,
        inputs={"token_usage": {"total_tokens": 3}},
    )
    root = SimpleNamespace(
        id="root",
        name="analysis",
        run_type="chain",
        start_time=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        child_runs=[child],
    )
    trace_path = tmp_path / "trace.json"

    payload = write_trace_artifact(
        trace_path,
        root,
        "https://trace.example/root",
        serializer=serialize_langsmith_run,
    )
    summary = summarize_trace_payload(payload)

    assert trace_path.exists()
    assert payload["_langsmith_url"] == "https://trace.example/root"
    assert summary["childRunCount"] == 1
    assert summary["toolRunCount"] == 1
    assert summary["usageDictCount"] == 1
