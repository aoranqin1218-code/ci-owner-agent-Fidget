from __future__ import annotations

from pathlib import Path

from ci_owner_agent.services.log_provider import LocalFileLogProvider


def make_local_log_provider(console_file: str | Path, max_output_chars: int) -> LocalFileLogProvider:
    return LocalFileLogProvider(console_file, max_output_chars=max_output_chars)
