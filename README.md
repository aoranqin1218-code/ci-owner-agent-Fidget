# CI Owner Agent

CI Owner Agent 是一个用于分析 CI / Jenkins 构建失败并生成“责任人判断通知”的 Python Agent 项目。它的核心目标不是简单地把失败归因给最后一次提交人，而是结合构建日志、Git diff、历史失败、人工反馈和 LLM 工具调用，判断某个失败是当前构建新引入、历史持续失败、偶发/环境问题，还是证据不足无法高可信定责。

当证据不足时，系统必须输出 `无高可信责任人`，避免误报。

---

## 1. 项目工作流程

### 1.1 总体流程

```text
Jenkins / 本地日志
  -> 读取构建结果、分支、commit、console log
  -> SUCCESS / ABORTED 状态门控
  -> FAILURE / UNSTABLE / UNKNOWN 进入失败分析
  -> Git 同步与 baseCommit..headCommit diff
  -> 通过 previousCommit 缩小 diff 范围（focusRange）
  -> 提取当前构建失败摘要 failureSummaries
  -> 必要时提取 AI failureFacts
  -> 查询 Mongo 历史失败 historyPrecheck / aiHistoryPrecheck
  -> LangChain Agent 调用工具补充证据
  -> 生成 CiResponsibilityNotice
  -> 本地 validator / scorer 校验并降级弱证据
  -> 保存 Mongo 历史记录
  -> 可选发送企业微信通知
  -> 可选进入反馈页修正责任人
  -> 可选写入 metrics JSONL
```

### 1.2 构建状态门控

系统首先判断构建结果：

| 构建结果 | 行为 |
| --- | --- |
| `SUCCESS` | 直接输出成功 notice，不进入 Agent 定责。 |
| `ABORTED` | 认为更可能是 Jenkins Pipeline、环境、节点或人工中断，不进入普通代码定责。 |
| `FAILURE` / `UNSTABLE` / `UNKNOWN` | 进入失败分析流程。 |

### 1.3 失败分析阶段

失败分析主要分为几层：

1. **Git 上下文**：读取 `baseCommit..headCommit` 之间的 commits 和 changed files。当指定 `--previous-commit` 或通过 Mongo 查到上一个构建的 headCommit 时，优先以 `previousCommit..headCommit` 作为窄 diff（focusRange），用于首次失败优先分析。
2. **失败摘要**：从日志中提取更聚焦的失败块，例如测试失败、Japa/Mocha 失败、TypeScript 编译错误等。
3. **AI failure facts**：当确定性失败摘要不足时，可用 LLM 从非结构化日志中提取内层真实失败事实，避免把 Docker / Jenkins / shell wrapper 当成根因。
4. **历史失败查询**：从 MongoDB 查询此前失败构建的 failure chunks / failure facts，判断当前失败是否是历史持续失败。
5. **Agent 工具调查**：LangChain Agent 根据当前上下文调用日志、Git、TypeScript、历史查询等工具补证据。
6. **结果校验**：本地 validator / scorer 会检查证据是否足够，不足时降级为 `无高可信责任人`。

### 1.4 责任类型

`responsibilityItems` 中每个失败项可以表达不同责任类型：

| 类型 | 含义 |
| --- | --- |
| `current_build_owner` | 当前构建引入的新失败，有日志和 diff 等证据支撑。 |
| `inherited_failure_owner` | 当前构建仍在失败，但根因来自历史构建，应继承首次失败责任人。 |
| `no_high_confidence_owner` | 证据不足、环境问题、偶发问题或无法高可信定责。 |
| `unknown` | 模型无法稳定判断，通常会被校验逻辑降级。 |

### 1.5 历史继承与反馈

开启 Mongo 历史后，系统会保存：

- `ci_builds`：构建元数据。
- `ci_notices`：最终责任通知。
- `ci_failure_chunks`：结构化失败摘要。
- `ci_failure_facts`：AI 提取的失败事实。
- `ci_feedback`：人工反馈。
- `ci_notifications`：通知发送记录。
- `ci_wecom_users`：企业微信用户映射。

人工反馈会影响后续继承：

| 反馈动作 | 后续影响 |
| --- | --- |
| `confirm_owner` | 确认原责任人，后续继承时可标记为已验证。 |
| `correct_owner` | 修正责任人，后续继承时优先使用修正后的 owner。 |
| `mark_flaky` | 标记偶发/环境问题，阻断后续历史继承。 |
| `mark_no_owner` | 标记无高可信责任人，阻断后续历史继承。 |

### 1.6 通知与反馈页

分析完成后可以发送企业微信 Markdown 通知。通知中可包含反馈链接，用户打开 `/feedback` 页面后可以对每个责任项进行确认、修正、标记偶发或标记无责任人。

企业微信 @ 人优先使用 MongoDB 中的 `ci_wecom_users` 映射；如果 Mongo 不可用，可回退到 CSV 映射；仍无法匹配时显示普通姓名。

### 1.7 metrics 记录

设置 `CI_AGENT_METRICS_ENABLED=true` 后，每次 `analyze` / `analyze-local` 会追加一行 JSONL，记录：

- 总耗时。
- 阶段耗时。
- LLM calls。
- provider 返回的 token usage。
- 责任项数量。
- inherited / current owner 数量。
- 错误和 warning。

批量脚本 `batch_analyze_company_logs.py` 会自动为每个构建启用 metrics 并写入独立文件，`summary.csv` 中汇总 token 和耗时。

metrics 只用于调试和性能分析，写入失败不会导致分析失败。

---

## 2. 目录结构

```text
ci_owner_agent/
  agents/                  # Agent 上下文、LangChain Agent、fake agent
  services/                # Jenkins、Git、Mongo history、通知、metrics、AI facts 等服务
  tools/                   # LangChain 可调用工具封装
  main.py                  # CLI 入口
  orchestrator.py          # analyze / analyze-local 主流程编排
  server.py                # 反馈页 FastAPI 服务
scripts/
  batch_analyze_company_logs.py   # 批量分析本地 Jenkins 日志
  batch_analyze_jenkins_builds.py # 批量分析 Jenkins 构建号
  clear_history_failure_chunks.py # 清理历史 failure chunks
ts-analyzer/               # TypeScript 静态分析脚本
tests/                     # pytest 测试
samples/                   # 可放脱敏日志样例
```

---

## 3. 安装

### 3.1 Python 环境

要求 Python `>=3.11`。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Linux / macOS：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

项目依赖见 `pyproject.toml`，核心依赖包括 `pydantic`、`python-dotenv`、`requests`、`langchain`、`langchain-openai`、`langsmith`、`pymongo` 等。

### 3.2 复制配置文件

```powershell
Copy-Item .env.example .env
```

然后按实际环境修改 `.env`。

### 3.3 准备 Git repo cache

`CI_AGENT_REPO_CACHE_DIR` 指向 Agent 专用的 repo 缓存目录，支持：

```text
{CI_AGENT_REPO_CACHE_DIR}/{repo}
{CI_AGENT_REPO_CACHE_DIR}/{repo}.git
```

例如：

```text
E:/ci-agent-cache/fx-code
```

注意：TypeScript 分析工具可能会对目标 repo 执行 detached checkout。不要把 `CI_AGENT_REPO_CACHE_DIR` 指向人工日常开发工作区，建议使用 Agent 专用 clone。

### 3.4 准备 TypeScript Analyzer

如果需要 `ts_find_definitions` / `ts_find_callers` 等 TypeScript 静态分析能力：

```powershell
cd ts-analyzer
npm install
```

目标业务仓库本身也需要提前安装依赖：

```powershell
cd E:/ci-agent-cache/fx-code
npm install
```

---

## 4. `.env.example` 配置说明

### 4.1 Jenkins 配置

```env
JENKINS_URL=
JENKINS_USER=
JENKINS_TOKEN=
```

| 配置 | 说明 |
| --- | --- |
| `JENKINS_URL` | Jenkins 地址，例如 `https://jenkins.example.com`。为空时 `analyze` 无法访问 Jenkins。 |
| `JENKINS_USER` | Jenkins 用户名。 |
| `JENKINS_TOKEN` | Jenkins API token 或密码。 |

### 4.2 Agent 基础配置

```env
CI_AGENT_REPO_CACHE_DIR=E:/ci-agent-cache
CI_AGENT_DEFAULT_LOG_TAIL_LINES=500
CI_AGENT_MAX_TOOL_STEPS=12
CI_AGENT_MAX_TOOL_OUTPUT_CHARS=20000
CI_AGENT_RECURSION_LIMIT=60
```

| 配置 | 说明 |
| --- | --- |
| `CI_AGENT_REPO_CACHE_DIR` | 本地 Git repo 缓存目录。 |
| `CI_AGENT_DEFAULT_LOG_TAIL_LINES` | 默认读取日志尾部行数。 |
| `CI_AGENT_MAX_TOOL_STEPS` | Agent 工具调用预算参考值。 |
| `CI_AGENT_MAX_TOOL_OUTPUT_CHARS` | 单个工具输出最大字符数，防止上下文过大。 |
| `CI_AGENT_RECURSION_LIMIT` | LangChain Agent recursion limit。 |

### 4.3 LLM 配置

```env
CI_AGENT_MODEL_PROVIDER=fake
CI_AGENT_MODEL_BASE_URL=
CI_AGENT_MODEL_NAME=
CI_AGENT_API_KEY=
CI_AGENT_MODEL_TIMEOUT_SECONDS=180
CI_AGENT_MODEL_MAX_RETRIES=2
CI_AGENT_RESPONSE_FORMAT=tool
```

| 配置 | 说明 |
| --- | --- |
| `CI_AGENT_MODEL_PROVIDER` | `fake`、`openai`、`deepseek`、`doubao`、`openai-compatible`。 |
| `CI_AGENT_MODEL_BASE_URL` | OpenAI-compatible API base URL。 |
| `CI_AGENT_MODEL_NAME` | 模型名称。 |
| `CI_AGENT_API_KEY` | 模型 API key。 |
| `CI_AGENT_MODEL_TIMEOUT_SECONDS` | 单次模型 HTTP 请求超时。 |
| `CI_AGENT_MODEL_MAX_RETRIES` | 模型请求重试次数。 |
| `CI_AGENT_RESPONSE_FORMAT` | `tool` 或 `json_text`。`tool` 使用结构化输出，`json_text` 只要求模型输出 JSON 文本。 |

`fake` 是默认离线测试模式，只返回固定 no-owner 结果，用于验证 CLI、工具链、持久化、通知、pytest 流程，不做正式定责。

豆包示例：

```env
CI_AGENT_MODEL_PROVIDER=doubao
CI_AGENT_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
CI_AGENT_MODEL_NAME=doubao-seed-2-0-lite-260428
CI_AGENT_API_KEY=your-api-key
```

通用 OpenAI-compatible 示例：

```env
CI_AGENT_MODEL_PROVIDER=openai-compatible
CI_AGENT_MODEL_BASE_URL=https://your-compatible-endpoint/v1
CI_AGENT_MODEL_NAME=your-model
CI_AGENT_API_KEY=your-key
```

### 4.4 TypeScript Analyzer 配置

```env
TS_ANALYZER_DIR=./ts-analyzer
```

指向包含 `src/find_definitions.js` 等脚本的目录。

### 4.5 LangSmith 配置

```env
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=ci-owner-agent-dev
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

| 配置 | 说明 |
| --- | --- |
| `LANGSMITH_TRACING` | 是否开启 LangSmith tracing。 |
| `LANGSMITH_API_KEY` | LangSmith API key。 |
| `LANGSMITH_PROJECT` | trace 所属项目名。 |
| `LANGSMITH_ENDPOINT` | LangSmith endpoint。 |

只有 `LANGSMITH_TRACING=true` 且存在 `LANGSMITH_API_KEY` 时才会开启 tracing。

### 4.6 Mongo 历史配置

```env
CI_AGENT_HISTORY_ENABLED=false
CI_AGENT_HISTORY_MONGO_URI=mongodb://localhost:27017
CI_AGENT_HISTORY_MONGO_DB=ci_owner_agent
CI_AGENT_HISTORY_MAX_CANDIDATES=5
CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED=true
CI_AGENT_FAILURE_CHUNK_TAIL_LINES=500
```

| 配置 | 说明 |
| --- | --- |
| `CI_AGENT_HISTORY_ENABLED` | 是否启用 Mongo 历史记录与历史失败查询。 |
| `CI_AGENT_HISTORY_MONGO_URI` | MongoDB URI。 |
| `CI_AGENT_HISTORY_MONGO_DB` | 数据库名。 |
| `CI_AGENT_HISTORY_MAX_CANDIDATES` | 历史候选失败最大数量。 |
| `CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED` | 当所有当前失败项都匹配历史 `no_high_confidence_owner` 判定、没有 inherited owner、且当前没有新强证据时，直接继承 no-owner 判定并跳过完整 Agent 分析。 |
| `CI_AGENT_FAILURE_CHUNK_TAIL_LINES` | 提取 focused failure summaries 前读取的测试阶段尾部行数。 |

历史不仅可以继承责任人，也可以继承 no-owner 判定。这个短路主要用于 Timeout / AwaitFunc / 环境抖动 / 异步等待类失败；只有当前 build 的所有失败项都被历史 no-owner 覆盖、没有可继承责任人、且没有新的错误码/失败路径等强证据时才会触发。多失败 build 中如果还有未覆盖的新失败项，会继续进入 Agent 分析；如果需要强制重新分析，可设置 `CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED=false`。

历史持续失败会优先继承 owner 或 no-owner，不重复分析大 diff。首次出现失败则优先分析 previous build 到 current build 的 focusRange；证据不足时才扩大到 last successful build 到 current build 的 fullRange。

### 4.7 metrics 配置

```env
CI_AGENT_METRICS_ENABLED=false
CI_AGENT_METRICS_FILE=./runs/metrics/ci_analysis_metrics.jsonl
```

| 配置 | 说明 |
| --- | --- |
| `CI_AGENT_METRICS_ENABLED` | 是否开启分析耗时和 token 记录。 |
| `CI_AGENT_METRICS_FILE` | JSONL 输出文件。 |

### 4.8 企业微信通知与反馈配置

```env
CI_AGENT_WECOM_NOTIFY_ENABLED=false
CI_AGENT_WECOM_WEBHOOK_URL=
CI_AGENT_WECOM_NOTIFY_DRY_RUN=true
CI_AGENT_WECOM_NOTIFY_ON_SUCCESS=false
CI_AGENT_WECOM_NOTIFY_ON_NO_OWNER=true
CI_AGENT_WECOM_USER_MAPPING_FILE=
CI_AGENT_WECOM_MENTION_MODE=userid
CI_AGENT_FEEDBACK_BASE_URL=
CI_AGENT_NOTIFICATION_DEDUP_ENABLED=true
```

| 配置 | 说明 |
| --- | --- |
| `CI_AGENT_WECOM_NOTIFY_ENABLED` | 是否默认发送企业微信通知。 |
| `CI_AGENT_WECOM_WEBHOOK_URL` | 企业微信群机器人 webhook。 |
| `CI_AGENT_WECOM_NOTIFY_DRY_RUN` | dry-run 时只生成 markdown，不真正发送。 |
| `CI_AGENT_WECOM_NOTIFY_ON_SUCCESS` | 是否通知成功构建。默认 false。 |
| `CI_AGENT_WECOM_NOTIFY_ON_NO_OWNER` | 无高可信责任人时是否仍通知。 |
| `CI_AGENT_WECOM_USER_MAPPING_FILE` | CSV 用户映射文件，Mongo 映射不可用时可回退。 |
| `CI_AGENT_WECOM_MENTION_MODE` | `userid` 或 `name`。`userid` 会尽量生成 `<@userid>`。 |
| `CI_AGENT_FEEDBACK_BASE_URL` | 反馈页基础 URL，用于通知中生成反馈链接。 |
| `CI_AGENT_NOTIFICATION_DEDUP_ENABLED` | 是否根据 notice hash 做通知去重。 |

---

## 5. 启动与命令说明

所有命令都通过模块入口运行：

```powershell
python -m ci_owner_agent <command> [options]
```

### 5.1 本地日志分析：`analyze-local`

用于分析已经下载到本地的 Jenkins console log。适合离线回放、批量测试和调试。

```powershell
python -m ci_owner_agent analyze-local `
  --repo fx-code `
  --job services/fx-code-unittest `
  --build 5064 `
  --branch dev `
  --base-commit <last-success-commit> `
  --head-commit <failed-build-commit> `
  --console-file .\samples\company_log\company-unittest-5064.log `
  --build-url local://services/fx-code-unittest/5064 `
  --last-success-build 5060 `
  --previous-build 5063 `
  --previous-commit <previous-build-head-commit>
```

参数说明：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--repo` | 是 | repo cache 中的仓库名，例如 `fx-code`。 |
| `--job` | 是 | Jenkins job 名。 |
| `--build` | 是 | 构建号。 |
| `--branch` | 否 | 分支名。 |
| `--base-commit` | 是 | diff 起点，通常是上次成功构建 commit。 |
| `--head-commit` | 是 | 当前失败构建实际 checkout commit。 |
| `--console-file` | 是 | 本地 console log 文件。 |
| `--build-url` | 是 | 展示用构建链接，可用 `local://...`。 |
| `--log-tail-lines` | 否 | 日志尾部读取行数，默认来自配置。 |
| `--result` | 否 | 手动覆盖日志中检测到的构建结果。 |
| `--ignore-checkout-commit-mismatch` | 否 | 忽略日志 checkout commit 与 `--head-commit` 不一致的保护。 |
| `--last-success-build` | 否 | 上次成功构建号，用于历史查询边界。 |
| `--previous-build` | 否 | 上一个构建号，用于标记 focusRange 来源。 |
| `--previous-commit` | 否 | 上一个构建的 head commit。提供后首次失败会优先分析 `previousCommit..headCommit`。 |
| `--notify` | 否 | 本次分析后发送通知。 |
| `--notify-dry-run` | 否 | 只预览通知，不发送。 |
| `--force-notify` | 否 | 忽略通知去重，强制发送。 |

`analyze-local` 会检查日志中的 `Checking out Revision <sha>` 或 `git checkout -f <sha>`。如果日志实际 checkout commit 和 `--head-commit` 不一致，会直接失败，避免对错误 commit 做定责。

首次出现失败会优先使用窄 diff：`previousBuild.headCommit -> currentBuild.headCommit`。`previousBuild` 在线模式优先从 MongoDB `ci_builds.headCommit` 查询；本地回放可以用 `--previous-build` / `--previous-commit` 显式指定。只有 focusRange 证据不足时，Agent 才应调用 `repo_get_diff_files(scope="full")` / `repo_get_commits_between(scope="full")` / `repo_get_file_diff(scope="full")` 扩大到 `lastSuccessfulBuild.commit -> currentBuild.commit`。最终 notice 的 `baseCommit/headCommit` 仍保留完整责任窗口，即 last success 到 current。

### 5.2 Jenkins 在线分析：`analyze`

用于直接从 Jenkins 读取构建信息和 console log。

```powershell
python -m ci_owner_agent analyze `
  --job services/fx-code-unittest `
  --build 5064 `
  --repo fx-code `
  --log-tail-lines 500 `
  --notify
```

参数说明：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--job` | 是 | Jenkins job 名。 |
| `--build` | 是 | Jenkins 构建号。 |
| `--repo` | 是 | repo cache 中的仓库名。 |
| `--log-tail-lines` | 否 | Jenkins console log tail 行数。 |
| `--notify` | 否 | 本次分析后发送通知。 |
| `--notify-dry-run` | 否 | 通知 dry-run。 |
| `--force-notify` | 否 | 忽略通知去重。 |

`analyze` 会读取当前构建和 `lastSuccessfulBuild`，用上次成功 commit 作为 base commit，再进入正式分析流程。

### 5.3 发送或预览已有 notice：`notify-notice`

```powershell
python -m ci_owner_agent notify-notice `
  --notice-file .\runs\xxx.notice.json `
  --dry-run
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `--notice-file` | 已生成的 `CiResponsibilityNotice` JSON 文件。 |
| `--dry-run` | 只打印 Markdown，不发送。 |
| `--force` | 忽略通知去重。 |
| `--feedback-base-url` | 覆盖配置中的反馈页基础 URL。 |

### 5.4 人工反馈：`feedback`

提交反馈：

```powershell
python -m ci_owner_agent feedback apply `
  --job services/fx-code-unittest `
  --build 5064 `
  --failure-id F1 `
  --action correct_owner `
  --owner-name "张三" `
  --owner-email zhangsan@example.com `
  --reviewer "reviewer" `
  --note "修正责任人"
```

查看反馈：

```powershell
python -m ci_owner_agent feedback list `
  --job services/fx-code-unittest `
  --build 5064
```

支持的 `--action`：

```text
confirm_owner
correct_owner
mark_flaky
mark_no_owner
```

### 5.5 启动反馈服务：`serve-feedback`

```powershell
python -m ci_owner_agent serve-feedback --host 0.0.0.0 --port 8765
```

服务接口：

| 路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查。 |
| `GET /feedback?job=...&build=...&token=...` | 反馈页面。 |
| `POST /feedback` | 提交反馈。 |
| `GET /api/wecom-users/search?q=...` | 搜索企业微信用户映射。 |

如果配置了 `CI_AGENT_FEEDBACK_SHARED_TOKEN`，访问反馈页和用户搜索接口时需要带 `token`。

---

## 6. 批量测试脚本说明

### 6.1 批量分析本地日志：`scripts/batch_analyze_company_logs.py`

适用于已经下载到本地的一批 Jenkins console log。脚本会按构建号排序，自动从成功构建推导后续失败构建的 base commit，同时自动追踪上一个构建的 headCommit 作为 focusRange 的 `--previous-commit`，并为每个构建自动启用 metrics 记录 token 与耗时。

示例：

```powershell
python .\scripts\batch_analyze_company_logs.py `
  --log-dir .\samples\company_log `
  --log-glob "company-unittest-*.log" `
  --repo fx-code `
  --job services/fx-code-unittest `
  --branch dev `
  --initial-base-commit <first-known-success-commit> `
  --build-from 5060 `
  --build-to 5112 `
  --timeout-seconds 900 `
  --out-dir .\runs\company-log-batch
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--log-dir` | 日志目录。 |
| `--log-glob` | 日志文件匹配规则，默认 `*.log`。 |
| `--out-dir` | 输出目录。默认 `runs/company-log-batch-时间戳`。 |
| `--env-file` | 加载的 env 文件，默认 `.env`。 |
| `--env-override` | 允许 env 文件覆盖当前环境变量。 |
| `--repo` | repo 名，默认 `fx-code`。 |
| `--job` | job 名，默认 `services/fx-code-unittest`。 |
| `--branch` | 分支名，默认 `dev`。 |
| `--build-url-prefix` | 本地 build URL 前缀。 |
| `--initial-base-commit` | 第一段失败前的已知成功 commit。 |
| `--build-from` / `--build-to` | 构建号范围过滤。 |
| `--limit` | 最多执行多少个失败构建。 |
| `--timeout-seconds` | 单个构建分析超时。 |
| `--dry-run` | 只生成命令，不执行。 |
| `--resume` | 已有 notice 时跳过。 |
| `--fetch-trace` | 从 LangSmith 拉取 trace。 |
| `--trace-wait-seconds` | 等待 trace 出现的最长时间。 |

输出目录结构：

```text
runs/company-log-batch-xxxx/
  index.jsonl
  summary.csv
  notices/*.notice.json
  stdout/*.stdout.txt
  stderr/*.stderr.txt
  traces/*.trace.json
  metrics/*.metrics.jsonl
```

`summary.csv` 会包含 owner、责任项数量、继承责任人、当前构建责任人、unresolved 数量、历史匹配信息、previousBuildNumber、previousCommit、durationSec、llmCalls、totalTokens 等字段。

### 6.2 批量分析 Jenkins 构建号：`scripts/batch_analyze_jenkins_builds.py`

适用于 Jenkins 仍可访问的场景，脚本会对指定构建号逐个执行 `python -m ci_owner_agent analyze`。

示例：

```powershell
python .\scripts\batch_analyze_jenkins_builds.py `
  --job services/fx-code-unittest `
  --repo fx-code `
  --build-from 5060 `
  --build-to 5112 `
  --log-tail-lines 500 `
  --timeout-seconds 900 `
  --out-dir .\runs\jenkins-batch
```

也可以指定离散构建号：

```powershell
python .\scripts\batch_analyze_jenkins_builds.py `
  --job services/fx-code-unittest `
  --repo fx-code `
  --builds 5088,5094,5095
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--job` | Jenkins job 名。 |
| `--repo` | repo 名。 |
| `--builds` | 逗号分隔构建号。 |
| `--build-from` / `--build-to` | 构建号范围。 |
| `--log-tail-lines` | Jenkins log tail 行数。 |
| `--out-dir` | 输出目录。 |
| `--env-file` / `--env-override` | 环境变量文件加载。 |
| `--fetch-trace` | 拉取 LangSmith trace。 |
| `--timeout-seconds` | 单个构建超时。 |
| `--resume` | 已有 notice 时跳过。 |
| `--dry-run` | 只输出命令。 |
| `--notify` | 分析后发送通知。 |
| `--notify-dry-run` | 通知 dry-run。 |
| `--force-notify` | 强制通知。 |

### 6.3 清理历史 failure chunks

当历史 chunk schema 或抽取逻辑变更后，可以清理旧历史 chunks：

```powershell
python .\scripts\clear_history_failure_chunks.py
```

等价 Mongo 命令：

```javascript
use ci_owner_agent
db.ci_failure_chunks.deleteMany({})
```

### 6.4 重复运行分析：`scripts/rerun_analyze_local.py`

重复运行 N 次 `analyze-local` 以评估 Agent 稳定性。

```powershell
python .\scripts\rerun_analyze_local.py `
  --runs 5 `
  --repo fx-code `
  --job services/fx-code-unittest `
  --build 5088 `
  --branch dev `
  --base-commit <last_success_commit> `
  --head-commit <current_commit> `
  --console-file .\samples\company_log\company-unittest-5088.log `
  --last-success-build 5068 `
  --previous-build 5087 `
  --previous-commit <previous_build_head_commit> `
  --out-dir .\runs\rerun-5088-focus
```

主要参数：

| 参数 | 说明 |
| --- | --- |
| `--runs` | 运行次数。 |
| `--timeout-sec` | 单次运行超时秒数。 |
| `--repo` | repo cache 中的仓库名。 |
| `--job` | Jenkins job 名。 |
| `--build` | 当前构建号。 |
| `--branch` | 分支名。 |
| `--base-commit` | lastSuccessfulBuild commit，即 fullRange 和 notice.baseCommit。 |
| `--head-commit` | 当前构建 commit。 |
| `--console-file` | 本地控制台日志文件。 |
| `--last-success-build` | 上次成功构建号（用于获取 base commit）。 |
| `--previous-build` | 上一个构建号（用于 focusRange 查询）。 |
| `--previous-commit` | 上一个构建的 head commit，用于缩小 diff 范围到 `previousCommit..headCommit`。 |
| `--out-dir` | 输出目录。 |
| `--fetch-trace` | 拉取 LangSmith trace。 |
| `--notify` | 分析后发送通知。 |
| `--force-notify` | 强制发送通知。 |

说明：
- `--base-commit` 为 lastSuccessfulBuild commit，即 fullRange 和 notice.baseCommit。
- `--previous-commit` 为 focusRange 起点，优先从 Mongo `ci_builds` 查找上一个 build 的 headCommit。
- 输出目录结构与 `batch_analyze_company_logs.py` 相同：`index.jsonl`、`summary.csv`、`notices/`、`stdout/`、`stderr/`。
## 7. 输出结果说明

CLI 会输出严格 JSON，主要字段包括：

```text
job
buildNumber
buildUrl
result
branch
headCommit
baseCommit
owner
failureReason
evidence
suggestions
hasHighConfidenceOwner
responsibilityItems
```

其中 `owner` 是顶层摘要，`responsibilityItems` 是更推荐关注的逐失败项责任判断。对于多失败构建，顶层可能是 `无高可信责任人`，但 `responsibilityItems` 里仍可能同时包含 inherited owner 和 current build owner。

---

## 8. 测试

运行全部测试：

```powershell
python -m pytest
```

如果只想看某个测试文件：

```powershell
python -m pytest tests/test_metrics.py
python -m pytest tests/test_history_store.py
```

---

## 9. 常见注意事项

1. **不要把 repo cache 指向人工开发目录**：TypeScript 工具会 detached checkout。
2. **不要把 Docker/Jenkins wrapper 当根因**：真正定责必须寻找内层错误，例如测试失败、TS 编译错误、依赖版本错误等。
3. **历史继承必须有足够相似度或 AI semantic match**：不能仅因为都失败在同一阶段就继承。
4. **metrics token 不估算**：只有 provider 返回 usage 时才累计 token。
5. **通知失败不应阻塞分析**：通知失败只 warning。
6. **反馈会影响后续继承**：`correct_owner` 会修正后续继承责任人，`mark_flaky` / `mark_no_owner` 会阻断继承。

---

## 10. 推荐调试顺序

首次接入时建议按以下顺序验证：

```text
1. python -m pytest
2. fake provider 下跑 analyze-local，确认 CLI / Git cache / Mongo / metrics 流程可用
3. 开启真实 LLM，单个失败构建跑 analyze-local
4. 开启 CI_AGENT_HISTORY_ENABLED=true，连续跑多次构建验证历史继承
5. 开启 CI_AGENT_METRICS_ENABLED=true，观察耗时和 token
6. 开启企业微信 dry-run，确认 Markdown 和反馈链接
7. 关闭 dry-run，正式发送通知
8. 接入 Jenkins analyze 或批量脚本
```
