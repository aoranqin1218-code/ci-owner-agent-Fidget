from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def value(flag: str) -> str | None:
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def main() -> int:
    if sys.argv[1:3] != ["-m", "ci_owner_agent"] or sys.argv[3] not in {"analyze-local", "analyze"}:
        print("unexpected command prefix", file=sys.stderr)
        return 64
    mode = os.environ.get("FAKE_ANALYZE_MODE", "success")
    supported = {"success", "missing_notice", "nonzero_missing", "invalid_json", "invalid_schema", "nonzero", "write_then_timeout", "spawn_descendant_then_timeout", "stdout_noise", "metadata_mismatch"}
    if mode not in supported:
        print(f"unsupported FAKE_ANALYZE_MODE: {mode}", file=sys.stderr)
        return 64
    diagnostic = os.environ.get("FAKE_ANALYZE_DIAGNOSTIC_FILE")
    if diagnostic:
        Path(diagnostic).write_text(json.dumps({"cwd": os.getcwd(), "pythonpath": os.environ.get("PYTHONPATH", ""), "marker": os.environ.get("CI_AGENT_TEST_ENV_MARKER")}), encoding="utf-8")
    output = value("--output-file")
    if mode in {"missing_notice", "nonzero_missing"}:
        return 3 if mode == "nonzero_missing" else 0
    if output is None:
        return 65
    if mode == "invalid_json":
        Path(output).write_text("{invalid", encoding="utf-8")
        return 0
    if mode == "invalid_schema":
        Path(output).write_text('{"repo":"fx-code"}', encoding="utf-8")
        return 0
    notice = {
        "repo": value("--repo"), "job": value("--job"), "buildNumber": int(value("--build") or 0),
        "buildUrl": value("--build-url") or f"jenkins://{value('--job')}/{value('--build')}", "result": value("--result") or "UNKNOWN", "branch": value("--branch"),
        "headCommit": value("--head-commit"), "baseCommit": value("--base-commit"),
        "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0},
        "failureReason": "tests-only launcher", "evidence": [], "suggestions": [], "responsibilityItems": [], "hasHighConfidenceOwner": False,
    }
    if mode == "metadata_mismatch":
        field = os.environ.get("FAKE_ANALYZE_MISMATCH_FIELD", "repo")
        replacements = {"repo": "wrong-repo", "job": "wrong-job", "buildNumber": 9999, "branch": "feature/test",
                        "result": "UNSTABLE", "baseCommit": "wrong-base", "headCommit": "wrong-head"}
        if field not in replacements:
            print(f"unsupported FAKE_ANALYZE_MISMATCH_FIELD: {field}", file=sys.stderr)
            return 64
        notice[field] = replacements[field]
    Path(output).write_text(json.dumps(notice, ensure_ascii=False), encoding="utf-8")
    print('{"unrelated": true}')
    if mode == "spawn_descendant_then_timeout":
        marker = os.environ["FAKE_ANALYZE_MARKER_FILE"]
        subprocess.Popen([sys.executable, "-c", "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile True:\n p.open('a').write('x')\n time.sleep(.05)", marker])
        time.sleep(30)
    if mode == "write_then_timeout":
        time.sleep(30)
    return 3 if mode == "nonzero" else 0


if __name__ == "__main__":
    raise SystemExit(main())
