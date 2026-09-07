# CI Owner Agent

CI Owner Agent 是一个面向 Jenkins / CI 构建失败的分析与责任判断系统。它综合可信的构建版本、Git 变更、测试日志、历史失败和人工反馈，输出可审计的结构化 `notice`（构建分析结果），并可按配置发送到企业微信或飞书。

项目的核心原则是：**只在证据足够时找到正确的人**。它不会简单把失败归给最后提交者；遇到环境问题、流水线问题、偶发失败或证据冲突时，会明确输出“无高可信责任人”。

## 当前范围

当前代码同时保留一期 `fx-code` 能力，并重点支持 Fidget 二期：

- 解析 Fidget/Japa 单元测试失败块，提取测试、错误和代码路径；
- 识别 c8 `check-coverage` 门槛失败，并展示实际覆盖率和阈值；
- 解析 `FIDGET_INTEGRATION_V1` 集成测试协议，区分代码断言与数据库/基础设施失败；
- 使用 Jenkins 实际 checkout SHA 和可信 Git 区间调查责任人；
- 通过 MongoDB 复用可信历史结论、人工反馈和测试失败统计；
- 支持企业微信通知、群内反馈闭环和每周报告；
- 支持飞书测试群 Webhook 单向通知、人员映射、消息预算和去重。

尚未完成或不属于当前交付的内容：

- 飞书群内反馈和历史修正闭环尚未实现；
- Fidget Connection Tests 未接入当前交付；
- 正式 Jenkins Job、目标群和生产部署尚需单独评审与验收；
- 本项目不是面向任意仓库、测试框架和通知渠道的通用平台。

## 工作流程

```text
Jenkins 构建 / 本地日志
  -> 校验构建状态、逻辑分支和可信 checkout SHA
  -> 建立上次成功提交到当前提交的责任窗口
  -> 同步 Agent 专用 Git repo cache，并校验 ancestry
  -> 提取 Japa、coverage 和 Integration 失败事实
  -> 查询历史失败、责任结论和人工反馈
  -> 确定性规则或 Agent 调查
  -> 本地复核并保守降级弱证据
  -> 生成 CiResponsibilityNotice
  -> 可选保存 MongoDB、记录 metrics、发送通知
```

几个关键边界：

- `SUCCESS` 直接生成成功 notice；`ABORTED` 默认无责任人；失败状态才进入调查。
- 只信任 Jenkins 实际 checkout SHA 或等价构建证据，不信任普通日志中的任意 SHA。
- Git diff 前必须确认 `baseCommit` 是 `headCommit` 的祖先，否则停止高可信定责。
- Docker、Jenkins、shell 等外层 wrapper 不能覆盖更内层的测试或编译错误。
- 通知是 notice 的下游副作用；通知失败不会破坏已经生成的分析结果。

## 快速开始

### 1. 安装

要求 Python 3.11+。GitHub Actions 当前使用 Python 3.12。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Linux / macOS：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

如需企业微信长连接机器人，再安装：

```powershell
python -m pip install -e ".[wecom-bot]"
```

### 2. 创建配置

```powershell
Copy-Item .env.example .env
```

首次本地验证建议保持无外部副作用：

```env
CI_AGENT_MODEL_PROVIDER=fake
CI_AGENT_HISTORY_ENABLED=false
CI_AGENT_WECOM_NOTIFY_ENABLED=false
CI_AGENT_FEISHU_NOTIFY_ENABLED=false
```

完整配置及默认值见 [`.env.example`](.env.example)。不要把 `.env`、API Key、Jenkins Token、Webhook、Bot Secret、chatid、反馈 Token 或数据库凭据提交到仓库。

### 3. 准备 Git repo cache

`CI_AGENT_REPO_CACHE_DIR` 必须指向 Agent 专用 clone 或 mirror，例如：

```text
E:/ci-agent-cache
└── fidget-xiaoqin
```

不要使用日常开发工作区。Git 服务可能执行 fetch、读取指定 commit 或进入 detached checkout。

如需 TypeScript 调用关系分析，再安装 `ts-analyzer` 依赖：

```powershell
Set-Location ts-analyzer
npm ci
```

## 常用命令

稳定入口统一为：

```powershell
python -m ci_owner_agent <command> [options]
```

| 命令 | 用途 |
| --- | --- |
| `analyze` | 在线读取并分析一个 Jenkins 构建 |
| `analyze-local` | 使用本地 console log 离线回放 |
| `notify-notice` | 预览或发送已有 notice |
| `feedback apply/list` | 提交或查询人工反馈 |
| `serve-feedback` | 启动反馈 Web 服务 |
| `test-failure-stats` | 查询测试文件失败统计 |
| `weekly-test-report` | 生成或发送每周失败报告 |
| `serve-wecom-bot` | 启动企业微信长连接 Worker |

查看完整参数：

```powershell
python -m ci_owner_agent --help
python -m ci_owner_agent analyze-local --help
```

### Fidget 手工验证快捷入口

根目录 `package.json` 只包装现有 Python CLI，不需要执行 `npm install`：

```powershell
# 分析个人 Fidget Jenkins Job，并保存 notice
npm run analyze:jenkins -- 31

# 分析本地日志；base/head 必须来自可信构建事实
npm run analyze:local -- .\path\to\console.log 31 <base-commit> <head-commit>

# 在终端预览 notice，不发送
npm run notice:preview -- .\runs\manual\jenkins-build-31.notice.json
```

快捷命令默认使用个人验证环境：

```text
job:    npm/fxp-fidget/fidget-xiaoqin-pipeline
repo:   fidget-xiaoqin
branch: main
```

注意：`npm run analyze:jenkins` 本身不传 `--notify`，但如果 `.env` 已启用企业微信或飞书自动通知，仍可能真实发送。要确保纯分析，必须同时保持：

```env
CI_AGENT_WECOM_NOTIFY_ENABLED=false
CI_AGENT_FEISHU_NOTIFY_ENABLED=false
```

### Jenkins 在线分析

```powershell
python -m ci_owner_agent analyze `
  --job npm/fxp-fidget/fidget-xiaoqin-pipeline `
  --build 31 `
  --repo fidget-xiaoqin `
  --output-file .\runs\manual\jenkins-build-31.notice.json
```

系统会从 Jenkins 查找同分支的上次可信成功构建，并使用实际 checkout SHA 建立责任窗口。调用方必须检查 notice 内容，不能只看进程退出码；例如 Jenkins 配置缺失时，命令可能正常产出一份 no-owner notice。

### 本地日志分析

```powershell
python -m ci_owner_agent analyze-local `
  --repo fidget-xiaoqin `
  --job npm/fxp-fidget/fidget-xiaoqin-pipeline `
  --build 31 `
  --branch main `
  --base-commit <last-success-commit> `
  --head-commit <failed-build-commit> `
  --console-file .\path\to\console.log `
  --build-url local://fidget/build-31 `
  --result FAILURE `
  --output-file .\runs\manual\local-build-31.notice.json
```

`analyze-local` 会校验日志中的 checkout SHA 与 `--head-commit`。两者不一致时默认拒绝分析，防止对错误版本定责。

### 通知预览与发送

先 dry-run 审阅内容：

```powershell
python -m ci_owner_agent notify-notice `
  --notice-file .\runs\manual\jenkins-build-31.notice.json `
  --channel wecom `
  --dry-run

python -m ci_owner_agent notify-notice `
  --notice-file .\runs\manual\jenkins-build-31.notice.json `
  --channel feishu `
  --dry-run
```

确认 notice、目标测试群、人员映射、敏感信息和消息长度后，移除 `--dry-run` 才会真实发送。使用 `--force` 可忽略通知去重；它只应用于本次指定渠道。

## 失败分析与责任语义

### Fidget 单元测试

以 Japa `✖` 失败块为锚点，清理 ANSI 和 Docker/BuildKit 前缀后，提取测试名称、错误类型、代码路径和稳定签名。结构化签名可用于历史匹配；普通 wrapper 不会形成可继承的测试失败事实。

### 覆盖率

只认 c8 的规范门槛错误：

```text
ERROR: Coverage for <metric> (<pct>%) does not meet [global] threshold (<threshold>%) [for <file>]
```

只有实际覆盖率低于阈值才算失败。文件级 coverage 会在可信 diff 中寻找唯一作者，最高为 `medium_confidence`；多作者、无 diff、路径无法验证或范围异常时输出 no-owner。coverage 责任项完全由确定性逻辑生成，不交给 Agent 猜测。

### Fidget 集成测试

集成日志以 `FIDGET_INTEGRATION_V1` marker 为协议锚点：

- AssertionError 等代码断言失败继续走可信 Git 定责；
- preflight、cleanup、ECONNREFUSED、镜像拉取和数据库不可用等环境失败确定性 no-owner；
- 环境失败不进入历史继承，也不会清空同一构建中的代码责任；
- marker 冲突、缺失或状态机不完整时 fail closed。

协议与脱敏样本见 [`samples/fidget_log/integration/PROTOCOL.md`](samples/fidget_log/integration/PROTOCOL.md)。

### 责任类型

每个 `responsibilityItem` 独立记录失败、责任人、来源构建、置信度、原因和证据：

| 类型 | 含义 |
| --- | --- |
| `current_build_owner` | 当前责任窗口内的新引入问题 |
| `inherited_failure_owner` | 从更早的可信同类失败继承 |
| `no_high_confidence_owner` | 环境问题或证据不足，不能可靠定责 |
| `unknown` | 未形成有效结论，最终校验通常会保守降级 |

Maintainer 只负责问题处置和通知路由，不等于根因责任人。

## 历史、反馈与通知

启用 `CI_AGENT_HISTORY_ENABLED=true` 后，MongoDB 用于保存构建、notice、失败事实、人工反馈、通知去重、统计和 Bot Outbox。历史只能在规范化后的 `repo + job + branch` 范围内复用，并须通过失败签名、来源构建和 Git 连续性校验。

企业微信支持：

- Webhook 或 Bot Outbox 主动通知；
- userid 映射、维护人路由和兜底通知；
- 反馈码、确认卡片、人工修正和历史覆盖；
- UTF-8 4096 字节预算、去重、有限重试和失败隔离。

飞书当前支持测试群自定义机器人 Webhook 单向通知，包括 interactive card、open_id 映射、维护人路由、消息预算、去重和失败审计；群内反馈闭环仍待实现。

详细接入说明见：

- [`docs/jenkins_notification_integration.md`](docs/jenkins_notification_integration.md)
- [`docs/history_failure_summary.md`](docs/history_failure_summary.md)
- [`docs/ai_failure_facts.md`](docs/ai_failure_facts.md)
- [`docs/ubuntu_scheduled_analysis.md`](docs/ubuntu_scheduled_analysis.md)

## 目录结构

```text
ci_owner_agent/
  main.py, cli/        CLI 参数与命令分发
  orchestrator.py      状态门控和核心编排
  schemas.py           严格 notice / evidence / owner 模型
  agents/              Agent 上下文、提示词和实现
  tools/               Agent 可见的薄适配层
  services/            Git、Jenkins、日志、历史、通知和反馈领域服务
scripts/               手工、轮询、批处理、回填和周报入口
tests/
  fx_code_test/        一期兼容与共享能力测试
  fidget_test/         Fidget unit / integration / coverage 测试
samples/fidget_log/    Fidget 离线回放样本
config/                维护人和周报配置
deploy/systemd/        Linux 定时任务模板
ts-analyzer/           TypeScript compiler API 辅助工具
```

完整代码链路与模块职责见 [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md)。

## 配置分类

全部配置均从环境变量或 `.env` 读取，详细字段见 [`.env.example`](.env.example)。常用分组如下：

| 分组 | 关键配置 |
| --- | --- |
| Jenkins | `JENKINS_URL`、`JENKINS_USER`、`JENKINS_TOKEN` |
| Git / Agent | `CI_AGENT_REPO_CACHE_DIR`、工具步数和输出预算 |
| 模型 | `CI_AGENT_MODEL_PROVIDER`、`BASE_URL`、`MODEL_NAME`、`API_KEY` |
| MongoDB | `CI_AGENT_HISTORY_ENABLED`、`MONGO_URI`、`MONGO_DB` |
| 企业微信 | `CI_AGENT_WECOM_NOTIFY_*`、Webhook、Bot、人员映射 |
| 飞书 | `CI_AGENT_FEISHU_NOTIFY_*`、Webhook、Secret、open_id 映射 |
| 反馈 | `CI_AGENT_FEEDBACK_*` |
| 可观测性 | `LANGSMITH_*`、`CI_AGENT_METRICS_*` |

生产模型应使用获准的 OpenAI-compatible 服务边界，不能把供应商地址、模型名或密钥硬编码进业务逻辑。

## 测试与质量门禁

标准验证命令：

```powershell
python -m compileall ci_owner_agent scripts tests
python -m pytest -q
```

修改 `ts-analyzer` 时还要执行：

```powershell
node --check ts-analyzer/src/common.js
node --check ts-analyzer/src/find_callers.js
node --check ts-analyzer/src/find_definitions.js
```

GitHub Actions 会在推送到 `main`、面向 `main` 的 Pull Request 和手工触发时，在 Ubuntu + Python 3.12 环境运行完整 pytest。

## 安全与上线

- 凭据只能来自授权环境配置或 Jenkins Credentials，不得进入源码、日志、notice、历史记录或提示词。
- Git 定责必须使用 Agent 专用 repo cache，不能操作人工开发工作区。
- 默认使用 fake provider、关闭 history 和通知，并保持通知 dry-run。
- 外部验证按 local/fake → 真实模型无通知 → Jenkins 单构建 → MongoDB/通知 dry-run → 测试群真实发送逐级进行。
- 正式群、生产定时任务和部署必须重新确认目标、人员映射、敏感信息、回退点和运行环境。
- 批处理、超时和 resume 默认 fail closed；残留产物不能当作成功结果复用。
- `scripts/clear_history_failure_chunks.py` 会删除整类历史数据，未经备份和明确授权不得执行。

部署与维护以 [`docs/ubuntu_scheduled_analysis.md`](docs/ubuntu_scheduled_analysis.md) 及内部部署维护手册为准。仓库内文档示例均不应替代现场配置确认。

## 项目资料

- [项目介绍（飞书）](https://fanruan-x.feishu.cn/wiki/WynLw41XZiV8zykRwYwcrEJhnQb)
- [部署维护手册（飞书）](https://fanruan-x.feishu.cn/wiki/SJwHwOOK3iTsmUkp7jac2xVenzc)
- [Fidget 单元/集成测试通知优化（飞书）](https://fanruan-x.feishu.cn/wiki/N8rNwNR2dihxCzkEf8xclopsnng)
