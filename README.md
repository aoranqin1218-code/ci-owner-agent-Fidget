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
CI_AGENT_MAX_TOOL_STEPS=12
CI_AGENT_MAX_TOOL_OUTPUT_CHARS=20000
CI_AGENT_RECURSION_LIMIT=60
CI_AGENT_MODEL_PROVIDER=fake
CI_AGENT_MODEL_TIMEOUT_SECONDS=90
CI_AGENT_MODEL_MAX_RETRIES=1
CI_AGENT_RESPONSE_FORMAT=tool
TS_ANALYZER_DIR=./ts-analyzer
LANGSMITH_TRACING=false
LANGSMITH_PROJECT=ci-owner-agent-dev
CI_AGENT_HISTORY_ENABLED=false
CI_AGENT_HISTORY_MONGO_URI=mongodb://localhost:27017
CI_AGENT_HISTORY_MONGO_DB=ci_owner_agent
CI_AGENT_HISTORY_MAX_CANDIDATES=5
CI_AGENT_FAILURE_CHUNK_TAIL_LINES=500
```

`CI_AGENT_MODEL_PROVIDER=fake` is the default offline test mode. It uses the rule-based MVP agent only to verify the toolchain and tests; it is not the formal analysis mode.

OpenAI-compatible real LLM providers are supported with `openai`, `deepseek`, `doubao`, and `openai-compatible`.

Doubao example:

```env
CI_AGENT_MODEL_PROVIDER=doubao
CI_AGENT_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
CI_AGENT_MODEL_NAME=doubao-seed-2-0-lite-260428
CI_AGENT_API_KEY=your-api-key
```

DeepSeek example:

```env
CI_AGENT_MODEL_PROVIDER=deepseek
CI_AGENT_MODEL_BASE_URL=https://api.deepseek.com
CI_AGENT_MODEL_NAME=deepseek-chat
CI_AGENT_API_KEY=your-api-key
```

Generic OpenAI-compatible example:

```env
CI_AGENT_MODEL_PROVIDER=openai-compatible
CI_AGENT_MODEL_BASE_URL=https://your-compatible-endpoint/v1
CI_AGENT_MODEL_NAME=your-model
CI_AGENT_API_KEY=your-key
```

Use `openai-compatible` when a vendor exposes an OpenAI-compatible API but should not be configured as `deepseek` or `doubao`.

LangSmith tracing is optional:

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your-langsmith-key
LANGSMITH_PROJECT=ci-owner-agent-dev
```

Tracing is enabled only when both `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` are present. Keys are not printed.

Historical failure recall is optional and disabled by default. When `CI_AGENT_HISTORY_ENABLED=true`, the agent stores build metadata, notices, and normalized focused failure chunks in MongoDB, then exposes `history_search_similar_failures` to detect pre-existing failures. MongoDB write/search failures do not block analysis.

MongoDB history chunks use schemaVersion 3 failure summaries and signatures extracted from focused Test stage / `make docker-test` logs. The older generic `find_error_chunks` windows and schemaVersion 2 Test-stage tails are still available to the Agent for log exploration, but are not written as primary history chunks and are ignored by history search. `CI_AGENT_FAILURE_CHUNK_TAIL_LINES` controls the focused Test stage tail size used before summary extraction and defaults to 500 lines.

After switching to schemaVersion 3, clear old noisy or schemaVersion 2 chunks manually:

```bash
python scripts/clear_history_failure_chunks.py
```

Manual Mongo equivalent:

```javascript
use ci_owner_agent
db.ci_failure_chunks.deleteMany({})
```

## Local Repo Cache

The Git cache must already exist. The tool does not guess or clone remote URLs.

Supported layouts:

```text
{CI_AGENT_REPO_CACHE_DIR}/{repo}
{CI_AGENT_REPO_CACHE_DIR}/{repo}.git
```

Ordinary Git analysis supports both normal clones and bare mirrors: diff files, commit lists, keyword search, and file content can read commit objects directly without checkout.

TypeScript Program analysis is different. `ts_find_definitions` and `ts_find_callers` only support a normal working-tree repo. They checkout the agent-owned analysis repo to the requested commit in detached HEAD mode, then create the TypeScript Program from the real project `tsconfig`. Do not point `CI_AGENT_REPO_CACHE_DIR` at a human developer working copy. Use a dedicated agent cache, for example `E:/workspace/temp/fx-code`.

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

`analyze-local` also checks whether the console log contains `Checking out Revision <sha>` or `git checkout -f <sha>`. If that commit differs from `--head-commit`, the command fails before LLM analysis so responsibility is not assigned against the wrong commit. Rerun with the checkout SHA shown in the error, or pass `--ignore-checkout-commit-mismatch` only when you intentionally want to bypass this guard.

## Run analyze

```bash
python -m ci_owner_agent analyze ^
  --job services/fx-code-unittest ^
  --build 5064 ^
  --repo fx-code ^
  --log-tail-lines 500
```

Jenkins analyze mode is implemented. It reads build metadata and console logs from Jenkins, short-circuits `SUCCESS` and `ABORTED`, and for failed builds compares the failed commit against `lastSuccessfulBuild` before running the existing responsibility analysis.

In real LLM mode, the outer orchestrator still performs deterministic gates first. `SUCCESS` and `ABORTED` never enter the Agent. Failed builds enter a LangChain tool-calling Agent, which reads logs through tools instead of receiving the full Jenkins log at once.

The real Agent uses LangChain v1 `create_agent` with `response_format=CiResponsibilityNotice`. Agent invocation uses the v1 `messages` input format. If a provider cannot produce structured output, the project still falls back to text parsing plus one repair attempt, followed by local validator/scorer checks.

If an OpenAI-compatible model hangs during structured output / ToolStrategy, try:

```env
CI_AGENT_MODEL_TIMEOUT_SECONDS=90
CI_AGENT_MODEL_MAX_RETRIES=1
CI_AGENT_RESPONSE_FORMAT=json_text
```

`json_text` mode omits LangChain structured `response_format`; the model is still required by prompt to output JSON, and the result still goes through local JSON parse, one repair attempt, and validator/scorer.

Recommended LangChain v1 packages:

```text
langchain>=1.3,<2
langchain-openai>=1.0,<2
langgraph>=1.2,<2
```

Verify the environment with:

```bash
python -c "from langchain.agents import create_agent; print('ok')"
python -m pip check
```

## TypeScript Analyzer

The TypeScript analyzer lives under `ts-analyzer/` and is called by Python through `node` subprocesses. It provides:

- `ts_analyze_changed_functions`
- `ts_find_definitions`
- `ts_find_callers`

`TS_ANALYZER_DIR` must point to the directory containing `src/find_definitions.js`, for example:

```text
E:/workspace/lanchain/ci-owner-agent/ts-analyzer
```

Install its Node dependency before using the successful TypeScript paths:

```bash
cd ts-analyzer
npm install
```

If Node.js, `typescript`, `tsconfig.json`, or Program creation is unavailable, the Python tools return structured errors and the main analysis continues.

`ts_find_definitions` and `ts_find_callers` analyze the requested `commit` by checking out the dedicated analysis repository to that commit in detached HEAD mode before creating the TypeScript Program. The project intentionally does not create temporary worktrees or temporary checkout directories; the repo cache is assumed to be agent-owned. In the LangChain Agent wrapper these tools use `force_checkout=True`, because the analysis repository is considered Agent-owned. This operation does not run `git clean -fdx`, so it does not delete `node_modules`.

The analyzer always uses the real project `tsconfig` passed by the caller, defaulting to `tsconfig.json`. It does not provide a fallback tsconfig and does not generate `tsconfig.ci-agent.json`.

Prepare target repo dependencies once in the agent-owned repo:

```bash
cd E:/ci-agent-cache/fx-code
npm install
```

`git checkout --detach --force <commit>` does not delete `node_modules`, and this project never runs `git clean -fdx`, so installed dependencies are reused across later checkouts. You usually only need to reinstall when `package.json` or a lock file changes, or when `node_modules` is deleted. If `tsconfig.json` extends an npm package such as `nstarter-tsconfig`, that package must already exist under the target repo's `node_modules`.

Use `check_node_dependencies_for_analysis(repo, tsconfig="tsconfig.json")` to check whether `node_modules`, `tsconfig`, extended config files, and dependency marker hashes are ready. It writes `.ci-owner-agent/deps.json` with hashes of `package.json`, `package-lock.json`, `pnpm-lock.yaml`, and `yarn.lock`. The check does not install by default. If explicitly called with `install=True`, it chooses `npm install`, `pnpm install`, or `yarn install` from the lock file and returns structured install errors instead of crashing.

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

High confidence requires at least two supporting evidence types. Accepted combinations include:

- log clue plus changed diff file
- log keyword plus hit inside changed files
- changed file diff that contains the same symbol, file, or keyword found in logs
- log symbol plus TypeScript definition/caller relationship plus relevant diff

Only "someone committed in the interval" is not enough. Only "a file changed" is not enough. Only a keyword match is not enough. `node_modules` definitions can be background type evidence but cannot be responsibility files. Validator/scorer downgrades weak or contradictory outputs to `无高可信责任人`.

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
- `ts_find_definitions` and `ts_find_callers` may detach-checkout the agent-owned analysis repository to the requested commit; do not point `CI_AGENT_REPO_CACHE_DIR` at a human developer working copy.
- TypeScript dependency checks do not auto-install unless explicitly requested with `install=True`; real project `tsconfig` and installed npm dependencies must be available in the target repo.
- Real LLM mode requires LangChain v1, `langchain-openai`, and provider credentials. Fake mode remains the default for offline pytest.
- `repo_sync` errors are warnings in local analysis so temporary repos without remotes can still be analyzed; formal Jenkins mode should treat sync failure as blocking for high-confidence ownership.
- The rule engine is intentionally conservative and prefers `无高可信责任人` when evidence is weak.

## Smoke Test

```powershell
cd E:\workspace\lanchain\ci-owner-agent
python -m pytest -q

$env:CI_AGENT_MODEL_PROVIDER="fake"
python -m ci_owner_agent analyze-local `
  --repo fx-code `
  --job services/fx-code-unittest `
  --build 5064 `
  --branch dev `
  --base-commit <base> `
  --head-commit <head> `
  --console-file samples/company_log/company-unittest-5064.log `
  --build-url local://services/fx-code-unittest/5064
```

Real LLM example:

```powershell
$env:CI_AGENT_MODEL_PROVIDER="doubao"
$env:CI_AGENT_MODEL_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
$env:CI_AGENT_MODEL_NAME="doubao-seed-2-0-lite-260428"
$env:CI_AGENT_API_KEY="..."
$env:LANGSMITH_TRACING="true"
$env:LANGSMITH_API_KEY="..."
$env:LANGSMITH_PROJECT="ci-owner-agent-dev"

python -m ci_owner_agent analyze-local `
  --repo fx-code `
  --job services/fx-code-unittest `
  --build 5104 `
  --branch dev `
  --base-commit <base> `
  --head-commit <head> `
  --console-file samples/company_log/company-unittest-5104.log `
  --build-url local://services/fx-code-unittest/5104
```

## Batch Company Logs

`scripts/batch_analyze_company_logs.py` scans local Jenkins console logs by build number, keeps the previous successful build commit, and calls `analyze-local` for runnable failed builds. It writes `index.jsonl`, `summary.csv`, stdout/stderr, notices, and optional traces under the output directory.

Useful parameters:

```powershell
python .\scripts\batch_analyze_company_logs.py `
  --log-dir .\samples\company_log `
  --out-dir .\runs\company-log-batch-5072-5076-dryrun `
  --env-file .\.env `
  --build-from 5072 `
  --build-to 5076 `
  --dry-run
```

- `--build-from`: only execute/report logs with `buildNumber >= value`.
- `--build-to`: only execute/report logs with `buildNumber <= value`.
- Range filtering happens after scanning all logs, so a failed build inside the range still gets the correct previous successful commit and `--last-success-build` from earlier logs outside the range.
- `--limit` applies after build range filtering.
- `--dry-run` prints commands without calling the real LLM.
