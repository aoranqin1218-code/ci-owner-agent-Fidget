# CI Owner Agent

CI Owner Agent 是一个用于分析 Jenkins / CI 构建失败并生成结构化责任判断通知的 Python 项目。

它不会简单地把失败归给“最后一次提交人”，而是综合构建状态、可信 checkout SHA、Git 提交与 diff、失败日志、历史失败、人工反馈和可选 LLM 工具调用，判断失败属于：

- 当前构建新引入的问题；
- 历史持续失败，应继承历史责任结论；
- 偶发、环境或 Pipeline 问题；
- 证据不足，无法高可信定责。

当证据不足时，系统必须输出 `无高可信责任人`，避免为了给出结果而误报。

本文档以当前代码为准。项目统一通过以下模块入口运行：

```powershell
python -m ci_owner_agent <command> [options]
```

---

## 1. 项目工作流程

### 1.1 总体流程

```text
Jenkins 在线构建 / 本地 console log
  -> 解析构建状态、逻辑分支和可信 checkout commit
  -> SUCCESS / ABORTED 状态门控
  -> FAILURE / UNSTABLE / UNKNOWN 进入失败分析
  -> 同步专用 Git repo cache
  -> 校验 baseCommit 是 headCommit 的祖先
  -> 读取 focusRange 或 fullRange 的 commits / changed files
  -> 提取结构化 failure summaries
  -> 必要时调用 LLM 提取 AI failure facts
  -> 查询 MongoDB 历史失败、责任结论和人工反馈
  -> 可选复用可信的历史 no-owner 结论
  -> LangChain Agent 调用日志、Git、TypeScript、历史工具补充证据
  -> 生成 CiResponsibilityNotice
  -> 本地校验、补充 failure signature / path，并降级弱证据
  -> 可选保存 MongoDB 历史、测试文件失败事件和 metrics
  -> 可选将企业微信通知写入 MongoDB Outbox
  -> 由 serve-wecom-bot 长连接 Worker 投递通知并处理群内反馈
```

### 1.2 三种主要分析入口

| 入口 | 数据来源 | 适用场景 |
| --- | --- | --- |
| `analyze` | Jenkins API + 本地 Git cache | Jenkins 可访问时分析指定构建。 |
| `analyze-local` | 本地 console log + 本地 Git cache | 离线回放、调试、批量分析。 |
| `scripts/*.py` | 本地日志目录、构建号或重复运行参数 | 批量验证、回归测试、稳定性测试。 |

### 1.3 构建状态门控

| 构建结果 | 行为 |
| --- | --- |
| `SUCCESS` | 直接生成成功 notice，不进入 Agent 定责。 |
| `ABORTED` | 生成无责任人 notice，提示优先检查 Pipeline、节点、环境或人工中止。 |
| `FAILURE` / `UNSTABLE` / `UNKNOWN` | 进入失败分析。 |
| `NOT_BUILT` | 主 CLI 不接受该本地覆盖值；批处理和 rerun 脚本会将其作为不可分析状态处理。 |

### 1.4 Git 调查范围

系统始终保留完整责任窗口：

```text
fullRange = last successful commit -> current head commit
```

如果显式传入 `--previous-commit`，或 MongoDB 中存在同一 `repo + job + branch` 的上一构建 `headCommit`，则优先建立较窄范围：

```text
focusRange = previous build head commit -> current head commit
```

Agent 应先调查 `focusRange`。只有证据不足或窄范围读取失败时，才扩大到 `fullRange`。最终 notice 的 `baseCommit` / `headCommit` 仍表示完整责任窗口。

在读取 diff 前会执行 ancestry 校验。若 `baseCommit` 不是 `headCommit` 的祖先，系统不会输出高可信代码责任结论。

### 1.5 失败证据层级

1. **可信构建信息**：状态、逻辑分支、构建号、构建链接和 checkout SHA。
2. **Git 上下文**：commit 区间、changed files、文件 diff 和作者信息。
3. **确定性失败摘要**：测试失败、Make / Docker 测试失败、TypeScript 编译错误等聚焦片段。
4. **AI failure facts**：确定性摘要不足时，从非结构化日志提取真实内层失败事实。
5. **历史失败查询**：按签名、结构和可选 AI semantic comparison 查找历史相似失败。
6. **人工反馈**：确认、修正、标记偶发或标记无责任人的 active feedback。
7. **本地校验**：验证责任项证据、责任类型、来源构建和 owner 一致性。

### 1.6 责任类型

`responsibilityItems` 中每个失败项可以使用以下类型：

| 类型 | 含义 |
| --- | --- |
| `current_build_owner` | 当前构建引入的新失败，具有当前 diff、日志等证据。 |
| `inherited_failure_owner` | 当前仍失败，但可信根因来自更早的历史构建。 |
| `no_high_confidence_owner` | 证据不足、环境问题、偶发问题或无法可靠归责。 |
| `unknown` | 模型未稳定判断，通常会在本地校验中降级。 |

顶层 `owner` 是构建级摘要。多失败构建可能包含多个不同责任项，此时顶层 owner 可以保持 `无高可信责任人`，应优先查看 `responsibilityItems`。

### 1.7 历史 no-owner 短路

开启历史后，如果当前所有失败项都能被可信历史 no-owner 结论覆盖，并且：

- 没有可继承责任人；
- 历史来源构建早于当前构建；
- 匹配类型属于允许的可信集合；
- 当前 changed files、错误码、路径等没有出现新的强证据；

系统可以直接继承 no-owner 结论，跳过完整 Agent 分析。该行为由 `CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED` 控制。

---

## 2. 目录结构

```text
ci_owner_agent/
  agents/                         # Agent 上下文、工厂、LangChain Agent、提示词
  services/                       # Jenkins、Git、Mongo、历史、通知、反馈、metrics 等服务
  tools/                          # Agent 可调用的日志、Git、TypeScript、历史工具
  __main__.py                     # python -m ci_owner_agent 入口
  main.py                         # CLI 参数与命令分发
  config.py                       # 环境变量读取与 Settings
  orchestrator.py                 # analyze / analyze-local 核心编排
  schemas.py                      # Pydantic 输入输出模型
  server.py                       # FastAPI 反馈服务

scripts/
  _runtime.py                     # 脚本共享路径、进程超时和 resume marker 逻辑
  batch_analyze_company_logs.py   # 批量分析本地 Jenkins 日志
  batch_analyze_jenkins_builds.py # 批量分析 Jenkins 构建号
  rerun_analyze_local.py          # 同一日志重复运行并汇总稳定性
  backfill_test_file_failures.py  # 从已有历史回填测试文件失败事件
  clear_history_failure_chunks.py # 手工清空 ci_failure_chunks

config/
  weekly-test-report.yml          # 每周测试失败报告阈值
  test-maintainers.example.yml    # 测试文件维护人路由示例

ts-analyzer/                      # TypeScript 静态分析 Node.js 脚本
tests/                            # pytest 单元、集成和真实子进程测试
samples/                          # 可存放脱敏日志样例
.github/workflows/ci.yml          # GitHub Actions 测试工作流
.env.example                      # 配置模板
pyproject.toml                    # Python 依赖和可选 extras
```

### 2.1 核心模块职责

| 模块 | 职责 |
| --- | --- |
| `main.py` | 定义全部 CLI 命令、输出 JSON、通知、反馈、周报和服务启动。 |
| `orchestrator.py` | 状态门控、Git 范围、失败摘要、历史预检、Agent 调用和历史保存。 |
| `schemas.py` | `BuildInfo`、`CiResponsibilityNotice`、`ResponsibilityItem` 等严格模型。 |
| `services/history_store.py` | MongoDB 集合、索引、构建历史、notice、失败事实和测试文件事件。 |
| `services/wecom_notification_outbox.py` | 通知入队、去重、租约、重试和 dead 状态。 |
| `services/wecom_bot_worker.py` | 企业微信长连接、Outbox 消费和反馈消息处理。 |
| `services/weekly_test_report_*` | 周期计算、阈值配置、统计和周报通知。 |
| `scripts/_runtime.py` | 任意 cwd 路径解析、有界进程终止、严格 success marker 校验。 |

---

## 3. 安装

### 3.1 Python 环境

要求 Python `>=3.11`。GitHub Actions 当前使用 Python 3.12。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Linux / macOS：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 3.2 安装依赖

仅安装运行时依赖：

```powershell
python -m pip install -e .
```

开发、pytest 和反馈 Web 服务：

```powershell
python -m pip install -e ".[dev]"
```

企业微信 API 模式机器人长连接：

```powershell
python -m pip install -e ".[wecom-bot]"
```

同时需要测试、反馈服务和机器人：

```powershell
python -m pip install -e ".[dev,wecom-bot]"
```

核心依赖包括 Pydantic 2、LangChain / LangGraph、LangSmith、PyMongo、Requests、PyYAML 和 `tzdata`。FastAPI / Uvicorn 属于 `dev` 或 `server` extra；企业微信 SDK 属于 `wecom-bot` extra。

项目未在 `pyproject.toml` 中声明独立 console script，文档统一使用：

```powershell
python -m ci_owner_agent ...
```

### 3.3 创建配置文件

Windows：

```powershell
Copy-Item .env.example .env
```

Linux / macOS：

```bash
cp .env.example .env
```

`.env` 默认使用 `override=False` 加载，不会覆盖进程中已经存在的环境变量。批处理脚本可通过 `--env-override` 显式允许 env 文件覆盖当前环境。

### 3.4 准备专用 Git repo cache

`CI_AGENT_REPO_CACHE_DIR` 支持：

```text
{CI_AGENT_REPO_CACHE_DIR}/{repo}
{CI_AGENT_REPO_CACHE_DIR}/{repo}.git
```

例如：

```text
E:/ci-agent-cache/fx-code
```

不要把它指向人工日常开发目录。Git 同步和 TypeScript 工具可能执行 fetch、detached checkout 或读取指定 commit，应使用 Agent 专用 clone / mirror。

### 3.5 准备 TypeScript Analyzer

需要 `ts_find_definitions`、`ts_find_callers` 等工具时：

```powershell
cd ts-analyzer
npm install
```

目标业务仓库也应安装自身依赖：

```powershell
cd E:/ci-agent-cache/fx-code
npm install
```

### 3.6 可选基础设施

| 组件 | 何时需要 |
| --- | --- |
| Jenkins | 使用 `analyze` 或 Jenkins 构建号批处理时。 |
| MongoDB | 历史继承、反馈、测试文件统计、周报、通知 Outbox 和机器人必需。 |
| LangSmith | 需要 trace 或批处理 `--fetch-trace` 时。 |
| 企业微信 API 模式机器人 | 真实投递通知和群内反馈时。 |

---

## 4. `.env.example` 配置说明

### 4.1 Jenkins、仓库和 Agent 限制

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `JENKINS_URL` | 空 | Jenkins 地址。为空时 `analyze` 返回结构化 no-owner notice，不会访问 Jenkins。 |
| `JENKINS_USER` | 空 | Jenkins 用户名。 |
| `JENKINS_TOKEN` | 空 | Jenkins API token 或密码。 |
| `CI_AGENT_REPO_CACHE_DIR` | `repos` | 专用 Git repo cache。 |
| `CI_AGENT_DEFAULT_LOG_TAIL_LINES` | `500` | 默认日志尾部行数。 |
| `CI_AGENT_JENKINS_SUCCESSFUL_BUILD_SCAN_LIMIT` | `100` | 在线模式向前扫描同分支成功构建的上限。代码支持该变量，未配置时使用 100。 |
| `CI_AGENT_MAX_TOOL_STEPS` | `12` | Agent 工具调用预算参考值。 |
| `CI_AGENT_MAX_TOOL_OUTPUT_CHARS` | `20000` | 单次工具输出字符上限。 |
| `CI_AGENT_RECURSION_LIMIT` | `max(MAX_TOOL_STEPS × 4, 40)` | LangGraph recursion limit；默认步骤数下为 48。 |

### 4.2 LLM 配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CI_AGENT_MODEL_PROVIDER` | `fake` | `fake`、`openai`、`deepseek`、`doubao`、`openai-compatible`。 |
| `CI_AGENT_MODEL_BASE_URL` | 空 | DeepSeek、豆包和 OpenAI-compatible provider 必填。 |
| `CI_AGENT_MODEL_NAME` | 空 | 非 fake provider 必填。 |
| `CI_AGENT_API_KEY` | 空 | 非 fake provider 必填。 |
| `CI_AGENT_MODEL_TIMEOUT_SECONDS` | `90` | 单次模型请求超时。 |
| `CI_AGENT_MODEL_MAX_RETRIES` | `1` | 模型请求重试次数。非法值会回退默认值。 |
| `CI_AGENT_RESPONSE_FORMAT` | `tool` | `tool` 或 `json_text`；非法值回退 `tool`。 |

`fake` provider 用于离线测试，只返回固定 no-owner 结果，不用于正式责任判断。

豆包示例：

```env
CI_AGENT_MODEL_PROVIDER=doubao
CI_AGENT_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
CI_AGENT_MODEL_NAME=your-model
CI_AGENT_API_KEY=your-api-key
```

OpenAI-compatible 示例：

```env
CI_AGENT_MODEL_PROVIDER=openai-compatible
CI_AGENT_MODEL_BASE_URL=https://your-endpoint.example/v1
CI_AGENT_MODEL_NAME=your-model
CI_AGENT_API_KEY=your-api-key
```

### 4.3 TypeScript Analyzer 与 LangSmith

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `TS_ANALYZER_DIR` | `./ts-analyzer` | TypeScript Analyzer 目录。 |
| `LANGSMITH_TRACING` | `false` | 是否启用 LangSmith tracing。 |
| `LANGSMITH_API_KEY` | 空 | LangSmith API key。 |
| `LANGSMITH_PROJECT` | `ci-owner-agent-dev` | 默认项目名。 |
| `LANGSMITH_ENDPOINT` | `https://api.smith.langchain.com` | LangSmith endpoint。 |

实际 tracing 还要求存在有效 API key。批处理脚本可以通过 `--langsmith-project` 覆盖项目名。

### 4.4 MongoDB 历史与测试周报

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CI_AGENT_HISTORY_ENABLED` | `false` | 启用 MongoDB 历史、反馈、统计和 Outbox。 |
| `CI_AGENT_HISTORY_MONGO_URI` | `mongodb://localhost:27017` | MongoDB URI。 |
| `CI_AGENT_HISTORY_MONGO_DB` | `ci_owner_agent` | 数据库名。 |
| `CI_AGENT_WEEKLY_TEST_REPORT_CONFIG_FILE` | `config/weekly-test-report.yml` | 周报阈值配置。 |
| `CI_AGENT_HISTORY_MAX_CANDIDATES` | `5` | 历史相似失败候选上限。 |
| `CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED` | `true` | 是否允许可信历史 no-owner 短路。 |
| `CI_AGENT_FAILURE_CHUNK_TAIL_LINES` | `500` | 提取 failure summary 的日志尾部行数；低于 50 会回退默认值。 |

### 4.5 AI failure facts 与 AI history comparison

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CI_AGENT_AI_FAILURE_FACTS_ENABLED` | `false` | 确定性失败摘要不足时启用 AI failure facts。 |
| `CI_AGENT_AI_FAILURE_FACT_MIN_CONFIDENCE` | `0.70` | AI fact 最低置信度，限制在 0～1。 |
| `CI_AGENT_AI_FAILURE_FACT_MAX_LOG_CHARS` | `12000` | 送入 fact extractor 的最大日志字符数。 |
| `CI_AGENT_AI_HISTORY_COMPARE_ENABLED` | `false` | 对 AI facts 启用历史 semantic comparison。 |
| `CI_AGENT_AI_HISTORY_COMPARE_THRESHOLD` | `0.90` | AI 历史匹配阈值，限制在 0～1。 |
| `CI_AGENT_AI_HISTORY_MAX_FACT_CANDIDATES` | `20` | 历史 fact 候选上限。 |
| `CI_AGENT_AI_HISTORY_MAX_COMPARE_CALLS` | `20` | 单次分析允许的 AI 比较调用上限。 |

### 4.6 Metrics

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CI_AGENT_METRICS_ENABLED` | `false` | 是否记录分析 metrics。 |
| `CI_AGENT_METRICS_FILE` | `./runs/metrics/ci_analysis_metrics.jsonl` | JSONL 输出路径。 |

metrics 包含总耗时、阶段耗时、LLM calls、provider 返回的 token usage、责任项数量、warning 和 error。metrics 写入失败只输出 warning，不改变分析 notice。

### 4.7 企业微信通知与维护人路由

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CI_AGENT_WECOM_NOTIFY_ENABLED` | `false` | 分析命令是否默认触发通知流程。 |
| `CI_AGENT_WECOM_NOTIFY_DRY_RUN` | `true` | 默认仅格式化，不向 Outbox 入队。 |
| `CI_AGENT_WECOM_NOTIFY_ON_SUCCESS` | `false` | 是否通知成功构建。 |
| `CI_AGENT_WECOM_NOTIFY_ON_NO_OWNER` | `true` | 无高可信责任人时是否仍通知。 |
| `CI_AGENT_WECOM_USER_MAPPING_FILE` | 空 | Mongo 用户映射不可用时的 CSV fallback。 |
| `CI_AGENT_TEST_MAINTAINER_MAPPING_FILE` | 空 | 测试文件维护人 YAML；仅用于通知路由，不参与定责。 |
| `CI_AGENT_WECOM_MENTION_MODE` | `userid` | `userid` 或 `name`。 |
| `CI_AGENT_WECOM_FALLBACK_USERIDS` | 空 | 逗号分隔的兜底 userid。 |
| `CI_AGENT_NOTIFICATION_DEDUP_ENABLED` | `true` | 是否按通知 digest 去重。 |

测试维护人不是失败责任人。no-owner 责任项仍保持 `no_high_confidence_owner / 无高可信责任人`，维护人只显示在通知的“待确认维护人”区域。

`config/test-maintainers.example.yml` 示例：

```yaml
version: 1
rules:
  - repo: fx-code
    job: services/fx-code-unittest
    paths:
      - "test/service/view/**"
      - "test/**/ViewDataQueryServiceTest.ts"
    maintainers:
      - name: "Charlie.Guo"
        wecomUserId: "charlie.guo"
```

规则按顺序匹配，第一条同时满足 `repo`、`job` 和任一 `paths` glob 的规则生效。示例 userid 仅为占位值。

### 4.8 企业微信长连接机器人

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CI_AGENT_WECOM_BOT_ENABLED` | `false` | 启用 API 模式机器人 Worker。 |
| `CI_AGENT_WECOM_BOT_ID` | 空 | Bot ID。 |
| `CI_AGENT_WECOM_BOT_SECRET` | 空 | Bot Secret。 |
| `CI_AGENT_WECOM_BOT_DISCOVER_CHAT_ID` | `false` | 临时发现目标群 chatid；正常运行必须关闭。 |
| `CI_AGENT_WECOM_BOT_NOTIFY_CHAT_ID` | 空 | 通知目标群 chatid。 |
| `CI_AGENT_WECOM_BOT_NOTIFY_POLL_SECONDS` | `2` | Outbox 轮询间隔。 |
| `CI_AGENT_WECOM_BOT_NOTIFY_LEASE_SECONDS` | `30` | 单条通知发送租约。 |
| `CI_AGENT_WECOM_BOT_NOTIFY_MAX_ATTEMPTS` | `5` | 最大投递次数。 |
| `CI_AGENT_WECOM_BOT_CONFIRM_TTL_SECONDS` | `300` | 群内反馈确认有效期。 |
| `CI_AGENT_WECOM_FEEDBACK_CODE_TTL_DAYS` | `30` | 构建反馈码 TTL。 |
| `CI_AGENT_WECOM_BOT_EVENT_TTL_DAYS` | `7` | 机器人事件幂等记录 TTL。 |
| `CI_AGENT_WECOM_BOT_LLM_ENABLED` | `false` | 启用可选自然语言反馈解析。 |
| `CI_AGENT_WECOM_BOT_LLM_MAX_INPUT_CHARS` | `2000` | 自然语言解析最大输入字符数。 |

固定反馈命令不依赖 LLM。正式使用自然语言解析时，应配置真实模型；fake parser 只适合测试。

### 4.9 反馈服务

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CI_AGENT_FEEDBACK_BASE_URL` | 空 | 通知中反馈链接的基础 URL，也作为机器人卡片跳转地址。 |
| `CI_AGENT_FEEDBACK_SERVER_HOST` | `127.0.0.1` | FastAPI 监听地址。 |
| `CI_AGENT_FEEDBACK_SERVER_PORT` | `8765` | FastAPI 端口。 |
| `CI_AGENT_FEEDBACK_SHARED_TOKEN` | 空 | 可选共享 token；用于反馈页和用户搜索接口的简单访问保护。 |

不要提交真实 API key、Bot Secret、Jenkins token、chatid 或共享 token。

---

## 5. 启动与命令说明

### 5.1 命令总览

| 命令 | 作用 |
| --- | --- |
| `analyze-local` | 分析本地 console log。 |
| `analyze` | 在线读取并分析 Jenkins 构建。 |
| `notify-notice` | 发送或预览已有 notice。 |
| `feedback apply/list` | 提交或查看人工反馈。 |
| `serve-feedback` | 启动反馈 Web 服务。 |
| `test-failure-stats` | 查询测试文件失败统计。 |
| `weekly-test-report` | 生成或发送测试失败周报。 |
| `serve-wecom-bot` | 启动企业微信长连接 Worker。 |

### 5.2 本地日志分析：`analyze-local`

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
  --previous-commit <previous-build-head-commit> `
  --build-timestamp 2026-07-13T16:35:00+08:00 `
  --output-file .\runs\5064.notice.json
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--repo` | 是 | repo cache 中的仓库名。 |
| `--job` | 是 | Jenkins job 名。 |
| `--build` | 是 | 构建号。 |
| `--base-commit` | 是 | 完整责任窗口起点。 |
| `--head-commit` | 是 | 当前构建可信 checkout commit。 |
| `--console-file` | 是 | 本地 console log。 |
| `--build-url` | 是 | 展示用 URL，可使用 `local://...`。 |
| `--branch` | 否 | 逻辑分支名。 |
| `--result` | 否 | `SUCCESS`、`FAILURE`、`UNSTABLE`、`ABORTED`、`UNKNOWN`。 |
| `--log-tail-lines` | 否 | 覆盖默认日志尾部行数。 |
| `--ignore-checkout-commit-mismatch` | 否 | 忽略日志 checkout SHA 与 `--head-commit` 不一致保护。仅限明确知道风险时使用。 |
| `--last-success-build` | 否 | 上次成功构建号。 |
| `--previous-build` / `--previous-commit` | 否 | 显式提供 focusRange 来源。 |
| `--build-timestamp` | 否 | 带时区 ISO 8601 构建时间。无时区值会拒绝。 |
| `--notify` | 否 | 触发通知流程。 |
| `--notify-dry-run` | 否 | 不入队通知。 |
| `--force-notify` | 否 | 忽略通知去重。 |
| `--output-file` | 否 | 原子写入 UTF-8 notice 文件。 |

`analyze-local` 会从日志中解析可信 checkout 行，并校验其 SHA 与 `--head-commit`。不一致时默认返回输入错误，防止对错误 commit 定责。

### 5.3 Jenkins 在线分析：`analyze`

```powershell
python -m ci_owner_agent analyze `
  --job services/fx-code-unittest `
  --build 5064 `
  --repo fx-code `
  --log-tail-lines 500 `
  --output-file .\runs\5064.notice.json
```

在线模式会：

1. 读取指定 Jenkins 构建；
2. 规范化逻辑分支；
3. 查找早于当前构建、同一分支、状态为 `SUCCESS` 且具有可信 commit 的最近成功构建；
4. 使用 Jenkins checkout SHA，而不是本地分支指针；
5. 通过 ancestry 校验后进入分析。

如果 `JENKINS_URL` 未配置，当前实现会输出结构化 no-owner notice，原因说明无法访问 Jenkins，并以命令成功状态结束；调用方应检查 notice 内容，而不是只看进程退出码。

### 5.4 发送或预览已有 notice：`notify-notice`

```powershell
python -m ci_owner_agent notify-notice `
  --notice-file .\runs\5064.notice.json `
  --dry-run
```

| 参数 | 说明 |
| --- | --- |
| `--notice-file` | `CiResponsibilityNotice` JSON，支持 UTF-8 和 UTF-8 BOM。 |
| `--dry-run` | 打印 Markdown，不向 Outbox 入队。 |
| `--force` | 忽略通知去重。 |
| `--feedback-base-url` | 覆盖反馈 URL。 |

`CI_AGENT_WECOM_NOTIFY_DRY_RUN=true` 也会使该命令进入 dry-run。

`analyze` / `analyze-local` 的 `--notify-dry-run` 会执行格式化但不会打印 Markdown；需要人工预览时，先用 `--output-file` 保存 notice，再运行 `notify-notice --dry-run`。

Windows PowerShell 5.1 可能通过管道或 `Set-Content -Encoding utf8` 引入 BOM / 转码问题。推荐始终使用 `--output-file` 让 Python 原子写入 JSON。

### 5.5 人工反馈：`feedback`

提交反馈：

```powershell
python -m ci_owner_agent feedback apply `
  --repo fx-code `
  --job services/fx-code-unittest `
  --branch dev `
  --build 5064 `
  --failure-id F1 `
  --action correct_owner `
  --owner-name "张三" `
  --owner-email zhangsan@example.com `
  --owner-type high_confidence `
  --reviewer reviewer `
  --note "修正责任人"
```

查看反馈：

```powershell
python -m ci_owner_agent feedback list `
  --repo fx-code `
  --job services/fx-code-unittest `
  --branch dev `
  --build 5064
```

支持的 action：

```text
confirm_owner
correct_owner
mark_flaky
mark_no_owner
```

反馈命令要求 MongoDB history store 可用。可通过 `--failure-id` 或 `--failure-signature` 定位责任项；`correct_owner` 可补充 owner commit 和来源构建号。

### 5.6 启动反馈服务：`serve-feedback`

```powershell
python -m ci_owner_agent serve-feedback --host 0.0.0.0 --port 8765
```

需要安装 `dev` 或 `server` extra，并启用 MongoDB history。

| 路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查。 |
| `GET /feedback?repo=...&job=...&branch=...&build=...&token=...` | 构建反馈页。 |
| `POST /feedback` | 提交反馈。 |
| `GET /api/wecom-users/search?q=...&limit=...&token=...` | 搜索企业微信用户。 |

配置 `CI_AGENT_FEEDBACK_SHARED_TOKEN` 后，上述反馈页和搜索接口必须携带相同 token。它是共享访问门槛，不是完整用户认证系统。

### 5.7 测试文件失败统计：`test-failure-stats`

```powershell
python -m ci_owner_agent test-failure-stats `
  --repo fx-code `
  --job services/fx-code-unittest `
  --branch dev `
  --period current-week `
  --top 20 `
  --format json
```

`--job` 和 `--branch` 可重复传入，也可在一个参数中使用英文逗号分隔。输出格式支持 `json`、`csv`、`markdown`。

时间范围可使用：

```text
--period current-week
--period previous-week
```

也可同时提供带时区的：

```text
--period-start <ISO-8601>
--period-end <ISO-8601>
```

自定义起止时间必须同时提供，且开始时间早于结束时间。

### 5.8 每周测试失败报告：`weekly-test-report`

```powershell
python -m ci_owner_agent weekly-test-report `
  --repo fx-code `
  --job services/fx-code-unittest `
  --branch dev `
  --period previous-week `
  --notify
```

| 参数 | 说明 |
| --- | --- |
| `--top` | 覆盖配置中的 topN。必须为正整数。 |
| `--config-file` | 覆盖 `CI_AGENT_WEEKLY_TEST_REPORT_CONFIG_FILE`。 |
| `--notify` | 生成后向 Outbox 入队。 |
| `--dry-run` | 输出并模拟通知，不入队。 |
| `--force` | 为本次投递生成新的 delivery key。 |

`config/weekly-test-report.yml` 使用严格 schema，支持：

- `weeklyFailedBuildCount`；
- `consecutiveFailureCount`；
- `weeklyFailureRate`；
- `totalFailedBuildCount`；
- `minimumCompletedBuildCount`；
- 规则级 `matchMode: all | any`。

`topN` 只限制展示数量，不会把未达阈值的测试文件提升为重点项目。

### 5.9 启动企业微信机器人：`serve-wecom-bot`

```powershell
python -m ci_owner_agent serve-wecom-bot
```

前置条件：

- 安装 `wecom-bot` extra；
- `CI_AGENT_HISTORY_ENABLED=true`；
- MongoDB 可连接；
- `CI_AGENT_WECOM_BOT_ENABLED=true`；
- 配置 Bot ID 和 Secret；
- 如果真实通知开启，还需配置目标群 chatid。

命令行 `--bot-id` / `--secret` 优先于环境变量。

获取目标群 chatid 时：

1. 暂时设置 `CI_AGENT_WECOM_NOTIFY_ENABLED=false`；
2. 设置 `CI_AGENT_WECOM_BOT_DISCOVER_CHAT_ID=true`；
3. 启动 Worker；
4. 在目标群 @机器人发送一条消息；
5. 记录进程输出的第一次群聊 chatid；
6. 停止 Worker，将 chatid 写入 `CI_AGENT_WECOM_BOT_NOTIFY_CHAT_ID`；
7. 将 discovery 重新关闭后再启动常驻 Worker。

固定反馈命令：

```text
@机器人 CI-XXXXXX 1 判断正确
@机器人 CI-XXXXXX 1 责任人改为 @Lisi-李四
@机器人 CI-XXXXXX 2 标记偶发
@机器人 CI-XXXXXX 1 无法定责
@机器人 查看 CI-XXXXXX
@机器人 帮助
```

修改类反馈会先生成确认卡片。只有该反馈的发起人可以确认或取消；这是防误操作措施，不是业务管理员权限控制。

---

## 6. 批量测试脚本说明

### 6.1 批量分析本地日志：`batch_analyze_company_logs.py`

```powershell
python .\scripts\batch_analyze_company_logs.py `
  --log-dir .\samples\company_log `
  --log-glob "company-unittest-*.log" `
  --repo fx-code `
  --job services/fx-code-unittest `
  --branch dev `
  --initial-base-commit <known-success-commit> `
  --build-from 5060 `
  --build-to 5112 `
  --timeout-seconds 900 `
  --out-dir .\runs\company-log-batch
```

核心规则：

- 只信任独立的 `Checking out Revision <40位SHA> (...)` 或 `git checkout -f <40位SHA>`；
- 两个不同的可信 checkout SHA 会成为 validation failure；
- `GIT_COMMIT`、`HEAD_COMMIT` 或普通文本 SHA 不作为 checkout 证据；
- checkout ref 必须能规范化为业务分支，tag、pull ref、`HEAD`、未知 ref 会拒绝；
- `SUCCESS` 仅更新同分支基线；
- `FAILURE`、`UNSTABLE`、`UNKNOWN` 执行分析；
- `ABORTED`、`NOT_BUILT` 正常跳过；
- manifest-only 记录可补充历史和成功基线，但失败状态必须具有真实 console log 才能分析；
- 每个构建自动启用独立 metrics 文件。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--log-dir` | 必填，本地日志目录。 |
| `--log-glob` | 默认 `*.log`。 |
| `--repo` | 默认 `fx-code`。 |
| `--job` | 默认 `services/fx-code-unittest`。 |
| `--branch` | 默认 `dev`，必须是逻辑分支。 |
| `--python` | 子进程 Python，默认当前解释器。 |
| `--build-url-prefix` | 本地 build URL 前缀。 |
| `--initial-base-commit` | 已知成功基线。 |
| `--manifest-file` | 可选 JSON 构建历史补充。 |
| `--build-from` / `--build-to` | 构建范围。 |
| `--limit` | 最多执行的 runnable build 数量；0 表示不限。 |
| `--timeout-seconds` | 单构建超时，默认 900。 |
| `--resume` | 仅复用 notice + 严格 success marker 均可信的历史结果。 |
| `--dry-run` | 只写命令和记录，不启动分析函数。 |
| `--fetch-trace` | 拉取 LangSmith trace。 |

输出结构：

```text
runs/company-log-batch-xxxx/
  index.jsonl
  summary.csv
  notices/*.notice.json
  notices/*.notice.json.success.json
  stdout/*.stdout.txt
  stderr/*.stderr.txt
  traces/*.trace.json
  metrics/*.metrics.jsonl
```

### 6.2 批量分析 Jenkins 构建号：`batch_analyze_jenkins_builds.py`

范围模式：

```powershell
python .\scripts\batch_analyze_jenkins_builds.py `
  --job services/fx-code-unittest `
  --repo fx-code `
  --build-from 5060 `
  --build-to 5112 `
  --timeout-seconds 900 `
  --out-dir .\runs\jenkins-batch
```

离散构建号：

```powershell
python .\scripts\batch_analyze_jenkins_builds.py `
  --job services/fx-code-unittest `
  --repo fx-code `
  --builds 5088,5094,5095
```

`--builds` 可与范围合并，最终去重排序。`--build-from` 和 `--build-to` 必须成对出现。脚本逐个执行：

```text
python -m ci_owner_agent analyze
```

输出包含 `index.jsonl`、`summary.csv`、notice、success marker、stdout、stderr 和可选 trace。

### 6.3 重复运行本地分析：`rerun_analyze_local.py`

```powershell
python .\scripts\rerun_analyze_local.py `
  --runs 5 `
  --repo fx-code `
  --job services/fx-code-unittest `
  --build 5064 `
  --branch dev `
  --base-commit <base> `
  --head-commit <head> `
  --console-file .\samples\company_log\company-unittest-5064.log `
  --result FAILURE `
  --timeout-sec 900 `
  --out-dir .\runs\rerun-5064
```

每次 run 单独创建：

```text
run-01/
  notice.json
  stdout.log
  stderr.log
  metrics.jsonl
  command.txt
  trace.json              # 可选
  trace-summary.json      # 可选
```

根目录生成：

```text
summary.csv
summary.json
```

每次运行独立完成 prepare、旧产物清理、执行、timeout 和 notice metadata 校验。单次失败形成独立 row，并继续后续运行。

### 6.4 回填测试文件失败事件：`backfill_test_file_failures.py`

```powershell
python .\scripts\backfill_test_file_failures.py `
  --repo fx-code `
  --job services/fx-code-unittest `
  --branch dev `
  --dry-run
```

该脚本只读取已有 `ci_notices`、`ci_failure_chunks`、`ci_failure_facts` 和构建记录，不调用 LLM。确认 dry-run 结果后去掉 `--dry-run`；需要替换已有事件时使用 `--overwrite`。

### 6.5 清空历史失败块：`clear_history_failure_chunks.py`

```powershell
python .\scripts\clear_history_failure_chunks.py
```

该脚本会删除当前数据库中 **全部** `ci_failure_chunks`。它是历史 schema 切换时的手工维护工具，不是日常清理命令，执行前应确认数据库和备份。

### 6.6 共享路径、timeout 与 resume 语义

三个批量 / rerun 脚本均可从任意 cwd 启动：

- 显式传入的相对输入、输出、env 路径相对启动 cwd；
- 未显式提供的 `.env` 和 `runs/...` 默认路径相对仓库根目录；
- 子进程固定在仓库根目录运行；
- `PYTHONPATH` 会将仓库根目录放在最前，同时保留原值。

company 和 Jenkins batch 使用共享有界进程管理：

- POSIX：独立 session + 终止整个进程组；
- Windows：`taskkill /PID ... /T /F`，自身带 timeout，并有直接 kill fallback；
- tree termination、grace reap、direct kill、final reap 异常不会把 timeout 改成 execution；
- `terminationReaped` 表示是否确认回收，`terminationWarning` 保留诊断。

旧产物清理失败采用 fail-closed：当前 build 不启动分析函数、不读取残留产物，记录 `errorKind=cleanup` 后继续其他 build。

`--resume` 不会仅凭 notice 文件存在而跳过。只有以下全部满足时才允许复用：

1. 子进程此前返回 `0`；
2. notice schema 和 metadata 有效；
3. 父进程成功写入 `schemaVersion=1` success marker；
4. marker 使用严格 JSON schema，禁止未知字段和宽松类型转换；
5. marker metadata 与当前任务一致；
6. marker 中的 SHA-256 与当前 notice 内容一致。

marker 文件名：

```text
<notice-file>.success.json
```

marker 只用于防止失败执行、残留文件和部分写入被错误 resume，不防御拥有同一输出目录写权限的恶意修改者。

`executionSkipped` 的含义是“本次处理是否调用了 `run_analyze_local` / `run_analyze`”：

- cleanup、validation、dry-run、合法 resume、非 runnable row：`true`；
- 正常执行、非零、timeout、notice 错误、marker 创建错误：`false`。

---

## 7. 输出结果说明

### 7.1 `CiResponsibilityNotice`

主 CLI 输出严格 JSON，当前 schema 的主要字段为：

```text
repo
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
responsibilityItems
hasHighConfidenceOwner
```

简化示例：

```json
{
  "repo": "fx-code",
  "job": "services/fx-code-unittest",
  "buildNumber": 5064,
  "buildUrl": "local://services/fx-code-unittest/5064",
  "result": "FAILURE",
  "branch": "dev",
  "headCommit": "...",
  "baseCommit": "...",
  "owner": {
    "type": "no_high_confidence_owner",
    "name": "无高可信责任人",
    "email": null,
    "commit": null,
    "confidence": 0
  },
  "failureReason": "证据不足，无法高可信定责。",
  "evidence": [],
  "suggestions": [],
  "responsibilityItems": [],
  "hasHighConfidenceOwner": false
}
```

### 7.2 责任项字段

每个 `responsibilityItems` 可以包含：

- `failureId` / `failureSignature`；
- `failureTitle` / `failureSummary`；
- `testFilePath` / `failureFilePath`；
- `owner`；
- `responsibilityType`；
- `sourceBuildNumber` / `sourceBuildUrl` / `sourceCommit`；
- `matchType` / `relationship`；
- `confidence`；
- `reason`；
- `evidenceIds`。

历史来源字段由本地可信历史逻辑补充。普通模型输出的 no-owner 项不能自行声明历史来源，校验阶段会清理不可信字段。

### 7.3 CLI 与脚本退出状态

| 情况 | 常见退出状态 |
| --- | --- |
| 主 CLI 正常生成 notice | `0` |
| 参数、输入、配置、反馈或服务启动错误 | `2` |
| batch / rerun 存在失败 row | `1` |
| batch / rerun 全部成功或正常跳过 | `0` |

不要仅依赖 `analyze` 的退出码判断是否得到有效责任人。某些上下文不足场景会正常输出 no-owner notice。

### 7.4 Batch 记录

`index.jsonl` 保留每个 build 的完整记录；`summary.csv` 是便于筛选的扁平视图。常见诊断字段：

```text
returnCode
errorKind
error
noticeValid
noticeValidationError
resumable
successMarkerValid
successMarkerError
resumeValidated
resumeInvalidReason
executionSkipped
cleanupWarning
terminationReaped
terminationWarning
```

主要错误类型包括：

```text
checkout_validation
branch_validation
status_validation
manifest_validation
cleanup
execution
timeout
notice_missing
notice_schema / notice_validation
notice_metadata
resume_marker
```

### 7.5 Metrics

metrics JSONL 主要记录：

```text
durationMs
stages
llmCalls
inputTokens
outputTokens
totalTokens
tokenWarning
responsibilityItemCount
warnings
errors
```

token 只累计 provider 实际返回的 usage，不估算缺失值。

### 7.6 MongoDB 集合

启用 history 后，当前代码初始化并使用以下主要集合：

| 集合 | 用途 |
| --- | --- |
| `ci_builds` | 构建状态、commit、`buildTimestamp`、`analyzedAt`。 |
| `ci_notices` | notice 快照和构建级责任摘要。 |
| `ci_failure_chunks` | 结构化 failure summaries。 |
| `ci_failure_facts` | AI failure facts。 |
| `ci_test_file_failures` | 每构建、每测试文件的失败事件。 |
| `ci_feedback` | 人工反馈和操作审计。 |
| `ci_wecom_users` | 企业微信 userid、邮箱和显示名映射。 |
| `ci_feedback_contexts` | 构建反馈码及责任项上下文，带 TTL。 |
| `ci_wecom_pending_feedback` | 等待确认的群内反馈，带 TTL 和租约。 |
| `ci_wecom_bot_events` | 机器人消息幂等与处理租约。 |
| `ci_wecom_notification_outbox` | 待发送、发送中、已发送或 dead 的通知。 |

代码还初始化 `ci_notifications`、`ci_report_notifications` 等辅助集合；当前企业微信真实投递主路径使用 `ci_wecom_notification_outbox`。

`buildTimestamp` 表示 Jenkins / 输入提供的真实构建时间；`analyzedAt` 表示 Agent 执行时间。没有 `buildTimestamp` 的历史仍可参与总量统计，但不能可靠归入自然周。

### 7.7 企业微信通知链路

真实通知流程不是分析进程直接调用 webhook：

```text
analyze / notify-notice / weekly-test-report
  -> 格式化 Markdown
  -> 写入 ci_wecom_notification_outbox
  -> serve-wecom-bot 获取租约
  -> 通过企业微信长连接主动投递
  -> sent / pending retry / dead
```

Outbox 使用稳定 `deliveryKey` 去重。`--force` / `--force-notify` 会生成新的投递键。失败重试使用指数退避，达到最大次数后进入 `dead`。

### 7.8 测试文件统计

统计按以下身份隔离：

```text
repo + job + branch + testFilePath
```

同一测试文件在同一构建只保存一条事件，`failureItemCount` 保留该文件实际责任项数量。

周报分母仅包含 `SUCCESS`、`FAILURE`、`UNSTABLE`。完整构建序列用于计算连续失败；`ABORTED` / `NOT_BUILT` 跳过，其他没有该测试文件失败的有效构建会中断连续失败。

---

## 8. 测试

### 8.1 运行全部测试

```powershell
python -m pytest -q
```

### 8.2 语法检查

```powershell
python -m compileall ci_owner_agent scripts tests
```

### 8.3 常用聚焦测试

```powershell
python -m pytest tests/test_batch_analyze_company_logs.py -q
python -m pytest tests/test_batch_analyze_jenkins_builds.py -q
python -m pytest tests/test_rerun_analyze_local.py -q
python -m pytest tests/test_script_runtime.py -q
python -m pytest tests/test_history_store.py -q
python -m pytest tests/test_weekly_test_report_service.py -q
python -m pytest tests/test_wecom_bot_cli.py -q
python -m pytest tests/integration -q
```

### 8.4 GitHub Actions

`.github/workflows/ci.yml` 在以下情况运行：

- push 到 `main`；
- 面向 `main` 的 pull request；
- 手工 `workflow_dispatch`。

当前 CI 环境：

```text
ubuntu-latest
Python 3.12
pip install -e ".[dev]"
python -m pytest -q
```

Windows 专属进程终止分支通过平台无关 mock 单测验证；POSIX descendant 终止由 Ubuntu 真实子进程测试覆盖。生产 CLI 不包含测试模式环境变量，测试 launcher 完全位于 `tests/`。

---

## 9. 常见注意事项

1. **使用专用 repo cache**：不要指向人工开发工作区，TypeScript 和 Git 工具可能 detached checkout。
2. **只信任真实 checkout 证据**：普通 SHA 文本、环境变量打印或分支指针不能替代 Jenkins 实际 checkout commit。
3. **统一逻辑分支名**：`refs/remotes/origin/dev`、`*/dev` 等在输入边界规范化为 `dev`；Mongo 身份字段不保存原始 ref。
4. **不要把 wrapper 当根因**：Docker、Jenkins、shell、Make 外层错误只有在没有更内层证据时才可作为结论。
5. **fake provider 不做正式定责**：它用于验证流程和测试，结果固定保守。
6. **history 关闭会禁用多项能力**：反馈、周报、测试文件统计、Outbox 和机器人均要求 MongoDB history store。
7. **默认通知是 dry-run**：`.env.example` 中 `CI_AGENT_WECOM_NOTIFY_DRY_RUN=true`。真实入队前必须显式关闭并配置 chatid。
8. **分析通知失败不会覆盖 notice**：`analyze` / `analyze-local` 将通知异常降级为 warning；独立 `notify-notice` 和 `weekly-test-report` 会以非零状态报告通知失败。
9. **success marker 不是安全签名**：它保证失败执行和残留产物不会被误 resume，但不能防御有输出目录写权限的攻击者。
10. **批处理默认 fail-closed**：旧 notice、marker、stdout、stderr、trace 或 metrics 无法清理时，不会启动当前子进程。
11. **timeout 后产物不可信**：notice、owner、责任项、metrics 和 trace 都不作为成功结果。
12. **共享 token 不是完整鉴权**：反馈 Web 服务的 shared token 只提供简单访问保护。
13. **群内反馈没有管理员白名单**：群成员可以发起反馈；只有发起人可以确认或取消该次操作，所有写入均保留审计信息。
14. **PowerShell 编码需谨慎**：优先使用 `--output-file`；已经发生中文乱码的 JSON 无法通过改编码恢复。
15. **metrics token 不估算**：provider 不返回 usage 时保持缺失，不使用字符数推算。
16. **清理脚本具有破坏性**：`clear_history_failure_chunks.py` 会删除整个集合。
17. **路径基准不同**：显式相对路径相对启动 cwd，脚本默认 `.env` 和 `runs/...` 相对仓库根目录。
18. **`JENKINS_URL` 缺失不一定返回非零**：在线 `analyze` 会生成 no-owner notice；自动化调用方必须校验 notice。

---

## 10. 推荐调试顺序

首次接入建议按以下顺序进行：

1. **安装并运行全量测试**

   ```powershell
   python -m pip install -e ".[dev]"
   python -m pytest -q
   ```

2. **准备专用 repo cache**

   确认目标仓库可 fetch，并且 `baseCommit`、`headCommit` 均存在。

3. **使用 fake provider 跑单个 `analyze-local`**

   ```env
   CI_AGENT_MODEL_PROVIDER=fake
   CI_AGENT_HISTORY_ENABLED=false
   CI_AGENT_WECOM_NOTIFY_ENABLED=false
   ```

   验证 checkout SHA、Git ancestry、JSON 输出和路径配置。

4. **启用真实模型分析单个失败构建**

   配置 provider、model、API key 和必要的 base URL，先不要开启通知。

5. **启用 MongoDB history**

   连续分析多个同分支构建，检查 `ci_builds`、`ci_notices`、failure chunks / facts 和历史继承。

6. **启用 metrics 与可选 LangSmith**

   观察阶段耗时、LLM calls、token usage 和 trace metadata。

7. **验证本地批处理和 rerun**

   先使用 `--dry-run`，再执行少量构建；检查 `index.jsonl`、`summary.csv`、notice、marker 和 timeout 诊断。

8. **启动反馈 Web 服务**

   配置 feedback URL 和可选 shared token，验证页面、用户搜索和反馈写入。

9. **验证通知格式**

   ```powershell
   python -m ci_owner_agent notify-notice `
     --notice-file .\runs\example.notice.json `
     --dry-run
   ```

10. **发现企业微信群 chatid**

    短暂开启 discovery，取得正确群 chatid 后立即关闭。

11. **启动常驻 `serve-wecom-bot`**

    先确认 MongoDB 和 Bot 认证成功，再关闭通知 dry-run，让分析或周报向 Outbox 入队。

12. **验证群内反馈确认流程**

    测试固定命令、确认卡片、TTL、重复消息幂等和反馈审计。

13. **验证测试文件统计与周报**

    ```powershell
    python -m ci_owner_agent test-failure-stats `
      --repo fx-code --period current-week --format markdown

    python -m ci_owner_agent weekly-test-report `
      --repo fx-code --period previous-week --dry-run
    ```

14. **最后接入 Jenkins 在线分析或定时任务**

    Jenkins 可在构建结束后调用 `analyze`，并由外部调度器每周运行 `weekly-test-report`。Python 进程本身不负责常驻周调度。
