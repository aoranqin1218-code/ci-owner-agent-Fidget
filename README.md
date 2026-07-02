# CI Owner Agent

CI Owner Agent is a Python MVP for investigating CI test failures and producing a strict JSON responsibility notice. Phase 1 focuses on local analysis: a console log file plus `baseCommit..headCommit` in a local Git cache.

## Project Goal

The agent decides whether a build needs responsibility analysis. `SUCCESS` exits immediately. `ABORTED` is treated as a pipeline/environment/interruption class by default. `FAILURE`, `UNSTABLE`, and `UNKNOWN` enter the local investigation flow.

When evidence is insufficient, the output must say `无高可信责任人`.

## Install

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

On Linux or macOS, activate with `source .venv/bin/activate`.

## Configuration

Copy `.env.example` to `.env` and edit values as needed. Do not put secrets in source files.

Important settings:

```text
CI_AGENT_REPO_CACHE_DIR=E:/ci-agent-cache
CI_AGENT_DEFAULT_LOG_TAIL_LINES=500
CI_AGENT_MAX_TOOL_OUTPUT_CHARS=20000
CI_AGENT_MODEL_PROVIDER=fake
TS_ANALYZER_DIR=./ts-analyzer
```

Phase 1 does not call a real LLM or Jenkins.

## Local Repo Cache

The Git cache must already exist. The tool does not guess or clone remote URLs.

Supported layouts:

```text
{CI_AGENT_REPO_CACHE_DIR}/{repo}
{CI_AGENT_REPO_CACHE_DIR}/{repo}.git
```

Normal clones and bare mirrors are both supported. Analysis reads commits directly and does not checkout revisions.

## Run analyze-local

```bash
python -m ci_owner_agent analyze-local ^
  --repo fx-code ^
  --job services/fx-code-unittest ^
  --build 5064 ^
  --branch dev ^
  --base-commit def456 ^
  --head-commit abc123 ^
  --console-file samples/company_log/company-unittest-5064.log ^
  --build-url local://services/fx-code-unittest/5064 ^
  --log-tail-lines 500
```

Optional `--result SUCCESS|FAILURE|UNSTABLE|ABORTED|UNKNOWN` overrides the status detected from `Finished: ...` in the log.

## Run analyze

```bash
python -m ci_owner_agent analyze ^
  --job services/fx-code-unittest ^
  --build 5064 ^
  --repo fx-code ^
  --log-tail-lines 500
```

Jenkins analyze mode is implemented. It reads build metadata and console logs from Jenkins, short-circuits `SUCCESS` and `ABORTED`, and for failed builds compares the failed commit against `lastSuccessfulBuild` before running the existing responsibility analysis.

## TypeScript Analyzer

The TypeScript analyzer lives under `ts-analyzer/` and is called by Python through `node` subprocesses. It provides:

- `ts_analyze_changed_functions`
- `ts_find_definitions`
- `ts_find_callers`

Install its Node dependency before using the successful TypeScript paths:

```bash
cd ts-analyzer
npm install
```

If Node.js, `typescript`, `tsconfig.json`, or Program creation is unavailable, the Python tools return structured errors and the main analysis continues.

## Output JSON

Output is a `CiResponsibilityNotice` with:

- `job`, `buildNumber`, `buildUrl`, `result`, `branch`
- `headCommit`, `baseCommit`
- `owner`: `high_confidence`, `medium_confidence`, or `no_high_confidence_owner`
- `failureReason`
- `evidence`
- `suggestions`
- `hasHighConfidenceOwner`

The CLI prints JSON only, without Markdown wrapping.

## High Confidence Rules

High confidence requires at least two supporting evidence types. Phase 1 accepts:

- log clue plus changed diff file
- log keyword plus hit inside changed files
- changed file diff that contains the same symbol, file, or keyword found in logs

Only "someone committed in the interval" is not enough. Only "a file changed" is not enough.

## ABORTED Strategy

`ABORTED` builds skip normal code responsibility analysis. The notice explains that the likely class is Jenkins Pipeline context, environment, or manual interruption. Jenkinsfile/Pipeline responsibility analysis is intentionally left for a later phase.

## Real Log Data

Real Jenkins logs can be placed under `samples/company_log/`. The system detects status from the final Jenkins line:

- `Finished: SUCCESS`
- `Finished: FAILURE`
- `Finished: ABORTED`
- missing marker => `UNKNOWN`

Do not classify failures from `ERROR`, `FAILED`, `Exception`, or `Timeout` alone because successful logs may contain those words.

## Tests

```bash
python -m pytest
```

Tests create temporary Git repositories under `tmp_path`; they do not call Jenkins, a real LLM, or any company repository.

## Known Limits

- Jenkins API tools and `JenkinsLogProvider` are implemented, but require `JENKINS_URL` and optional credentials in `.env`.
- TypeScript Compiler API tools are implemented as subprocess-backed optional analysis helpers.
- The current agent is rule based, not a real LangChain tool-calling LLM agent.
- `repo_sync` errors are warnings in local analysis so temporary repos without remotes can still be analyzed; formal Jenkins mode should treat sync failure as blocking for high-confidence ownership.
- The rule engine is intentionally conservative and prefers `无高可信责任人` when evidence is weak.
