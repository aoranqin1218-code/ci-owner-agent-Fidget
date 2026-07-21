#!/usr/bin/env python3
"""Download the latest completed Jenkins console log for ci-owner-agent samples.

Default output example:
    samples/company_log/company-unittest-5060.log

The script reads Jenkins connection settings from the project's .env via
ci_owner_agent.config.load_settings:
    JENKINS_URL
    JENKINS_USER        (optional)
    JENKINS_TOKEN       (optional)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[1] if SCRIPT_PATH.parent.name == "scripts" else Path.cwd()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ci_owner_agent.config import load_settings  # noqa: E402

DEFAULT_JOB = "services/fx-code-unittest"
DEFAULT_OUTPUT_DIR = "samples/company_log"
DEFAULT_PREFIX = "company-unittest"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a Jenkins console log as company-unittest-<build>.log"
    )
    parser.add_argument(
        "--job",
        default=DEFAULT_JOB,
        help=f"Jenkins job path (default: {DEFAULT_JOB})",
    )
    parser.add_argument(
        "--build",
        type=int,
        default=None,
        help="Download a specific build instead of Jenkins lastCompletedBuild",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory, relative paths use the repository root (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"Output filename prefix (default: {DEFAULT_PREFIX})",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Environment file, relative paths use the repository root (default: .env)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP connect/read timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists",
    )
    args = parser.parse_args(argv)

    if args.build is not None and args.build <= 0:
        parser.error("--build must be a positive integer")
    if args.timeout <= 0:
        parser.error("--timeout must be a positive integer")
    if not str(args.job).strip("/"):
        parser.error("--job must not be empty")
    if not str(args.prefix).strip():
        parser.error("--prefix must not be empty")
    return args


def resolve_from_repo(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def jenkins_job_path(job: str) -> str:
    parts = [quote(part, safe="") for part in job.strip("/").split("/") if part]
    return "/job/".join(parts)


def request_json(
    session: requests.Session,
    url: str,
    *,
    auth: tuple[str, str] | None,
    timeout: int,
) -> dict[str, Any]:
    response = session.get(url, auth=auth, timeout=(10, timeout))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Jenkins returned unexpected JSON from {url}")
    return data


def resolve_build_number(
    session: requests.Session,
    *,
    base_url: str,
    job: str,
    requested_build: int | None,
    auth: tuple[str, str] | None,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    job_url = f"{base_url}/job/{jenkins_job_path(job)}"
    if requested_build is None:
        metadata_url = f"{job_url}/lastCompletedBuild/api/json"
    else:
        metadata_url = f"{job_url}/{requested_build}/api/json"

    metadata = request_json(session, metadata_url, auth=auth, timeout=timeout)
    try:
        build_number = int(metadata["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Jenkins build metadata does not contain a valid build number") from exc

    if requested_build is not None and build_number != requested_build:
        raise RuntimeError(
            f"Jenkins returned build #{build_number}, expected #{requested_build}"
        )
    if metadata.get("building") is True:
        raise RuntimeError(
            f"Jenkins build #{build_number} is still running; wait for completion before saving a test sample"
        )
    return build_number, metadata


def download_console_log(
    session: requests.Session,
    *,
    url: str,
    destination: Path,
    auth: tuple[str, str] | None,
    timeout: int,
) -> int:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    total_bytes = 0
    try:
        with session.get(
            url,
            auth=auth,
            timeout=(10, timeout),
            stream=True,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type.lower():
                raise RuntimeError(
                    "Jenkins returned HTML instead of console text; check authentication and job permissions"
                )
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    total_bytes += len(chunk)
                output.flush()
                os.fsync(output.fileno())

        if total_bytes == 0:
            raise RuntimeError("Jenkins console log is empty")
        temporary.replace(destination)
        return total_bytes
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_file = resolve_from_repo(args.env_file)
    output_dir = resolve_from_repo(args.output_dir)

    try:
        settings = load_settings(env_file)
    except Exception as exc:
        print(f"ERROR: unable to load configuration from {env_file}: {exc}", file=sys.stderr)
        return 2

    base_url = str(settings.jenkins_url or "").rstrip("/")
    if not base_url:
        print("ERROR: JENKINS_URL is not configured", file=sys.stderr)
        return 2

    user = str(settings.jenkins_user or "").strip()
    token = str(settings.jenkins_token or "").strip()
    auth = (user, token) if user and token else None

    session = requests.Session()
    session.headers.update({"User-Agent": "ci-owner-agent-log-downloader/1.0"})

    try:
        build_number, metadata = resolve_build_number(
            session,
            base_url=base_url,
            job=args.job,
            requested_build=args.build,
            auth=auth,
            timeout=args.timeout,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / f"{args.prefix}-{build_number}.log"
        if destination.exists() and not args.force:
            print(
                f"SKIPPED: {destination} already exists; use --force to overwrite it"
            )
            return 0

        console_url = (
            f"{base_url}/job/{jenkins_job_path(args.job)}/{build_number}/consoleText"
        )
        total_bytes = download_console_log(
            session,
            url=console_url,
            destination=destination,
            auth=auth,
            timeout=args.timeout,
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        print(f"ERROR: Jenkins HTTP request failed (status={status}): {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"ERROR: Jenkins request failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    result = str(metadata.get("result") or "UNKNOWN")
    print(f"Downloaded Jenkins build #{build_number} ({result})")
    print(f"Saved to: {destination}")
    print(f"Size: {total_bytes} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
