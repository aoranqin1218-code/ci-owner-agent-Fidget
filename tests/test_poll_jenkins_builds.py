from __future__ import annotations

from scripts.poll_jenkins_builds import (
    build_analyze_command,
    candidate_build_numbers,
    is_build_analyzed,
)


class FakeCollection:
    def __init__(self, docs: list[dict]):
        self.docs = docs

    def find_one(self, query: dict):
        for doc in self.docs:
            matched = True
            for key, expected in query.items():
                if isinstance(expected, dict) and "$exists" in expected:
                    matched = matched and ((key in doc) is bool(expected["$exists"]))
                else:
                    matched = matched and doc.get(key) == expected
            if matched:
                return doc
        return None


class FakeStore:
    def __init__(self, builds: list[dict], notices: list[dict]):
        self.builds = FakeCollection(builds)
        self.notices = FakeCollection(notices)


def test_candidate_build_numbers_uses_lookback_and_oldest_first():
    assert candidate_build_numbers(105, 3) == [103, 104, 105]
    assert candidate_build_numbers(2, 50) == [1, 2]
    assert candidate_build_numbers(0, 50) == []


def test_is_build_analyzed_requires_notice_and_build_completion_markers():
    complete = {
        "repo": "fx-code",
        "job": "services/fx-code-unittest",
        "branch": "dev",
        "buildNumber": 42,
        "analyzedAt": "2026-07-17T00:00:00Z",
    }
    assert is_build_analyzed(
        FakeStore([complete], [complete]),
        repo="fx-code",
        job="services/fx-code-unittest",
        build_number=42,
    )
    assert not is_build_analyzed(
        FakeStore([complete], []),
        repo="fx-code",
        job="services/fx-code-unittest",
        build_number=42,
    )
    assert not is_build_analyzed(
        FakeStore([], [complete]),
        repo="fx-code",
        job="services/fx-code-unittest",
        build_number=42,
    )


def test_is_build_analyzed_rejects_notification_only_notice_snapshot():
    notice = {
        "repo": "fx-code",
        "job": "services/fx-code-unittest",
        "branch": "dev",
        "buildNumber": 42,
        "notice": {},
    }
    build = {**notice, "analyzedAt": "2026-07-17T00:00:00Z"}
    assert not is_build_analyzed(
        FakeStore([build], [notice]),
        repo="fx-code",
        job="services/fx-code-unittest",
        build_number=42,
    )


def test_build_analyze_command_delegates_to_normal_cli_flow():
    command = build_analyze_command(
        python_executable="python",
        repo="fx-code",
        job="services/fx-code-unittest",
        build_number=5064,
        log_tail_lines=500,
        notify=True,
        notify_dry_run=False,
        force_notify=True,
    )
    assert command[:4] == ["python", "-m", "ci_owner_agent", "analyze"]
    assert command[command.index("--build") + 1] == "5064"
    assert "--notify" in command
    assert "--force-notify" in command
    assert "--notify-dry-run" not in command
