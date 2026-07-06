#!/usr/bin/env bash
set -euo pipefail

JOB=""
BUILD=""
REPO=""
LOG_TAIL_LINES="500"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --job)
      JOB="${2:-}"
      shift 2
      ;;
    --build)
      BUILD="${2:-}"
      shift 2
      ;;
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --log-tail-lines)
      LOG_TAIL_LINES="${2:-500}"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 --job JOB --build BUILD --repo REPO [--log-tail-lines 500]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$JOB" || -z "$BUILD" || -z "$REPO" ]]; then
  echo "Missing required arguments: --job, --build, --repo" >&2
  exit 2
fi

python -m ci_owner_agent analyze \
  --job "$JOB" \
  --build "$BUILD" \
  --repo "$REPO" \
  --log-tail-lines "$LOG_TAIL_LINES" \
  --notify
