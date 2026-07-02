你是资深 Python / LangChain / CI 工程师。请在当前仓库中实现一个“CI 测试失败自动定责 Agent”项目。技术栈固定为 Python + LangChain，不要使用 Java/Spring Boot。请优先实现 MVP 闭环，保证本地测试模式可运行，然后再接入 Jenkins 和 TypeScript Analyzer。

# 项目目标

输入 Jenkins job + buildNumber，系统先判断构建是否成功。

如果构建成功，直接输出结构化 JSON，说明构建成功，无需定责。

如果构建失败，进入 LangChain Agent 调查流程。Agent 通过工具逐步读取日志、获取 Git 变更、查看文件 diff、搜索关键词、必要时调用 TypeScript Compiler API 分析函数定义和调用，最终输出结构化 JSON 通知，包含责任人、失败原因、证据、建议和构建链接。

没有足够证据时必须输出“无高可信责任人”。

# 核心原则

1. 使用 Python + LangChain 实现 Agent 和工具编排。
2. 不要维护复杂的多测试框架日志正则解析。
3. 日志可能有上万行，不要一次性把完整日志塞给模型。
4. 提供稳定的日志读取、搜索、局部读取工具，让 Agent 自己探索日志。
5. Agent 只能基于工具返回的证据判断责任人，不得编造文件、commit、作者、构建链接。
6. 责任人不是“某个提交作者”，而是“能解释失败现象的 diff 对应 commit 作者”。
7. 没有至少两类相互支撑的证据时，不得输出高可信责任人。
8. TypeScript Compiler API 是最后增强手段，不要每次一开始就全仓库深度分析。
9. 正式 Jenkins 模式和本地测试模式必须复用同一套 Agent 判断流程。
10. MVP 优先支持 analyze-local，确保本地日志 + base/head commit + 本地 Git 仓库能跑通完整 JSON 输出。
11. MVP 第一阶段允许先实现 RuleBasedResponsibilityAgent 或 FakeResponsibilityAgent，不强制真实调用 LLM。真实 LangChain LLM Agent 可以在后续阶段接入，但接口要预留。
12. ABORTED 构建默认不进入普通代码定责流程，除非后续明确支持 Jenkinsfile / Pipeline 定责。

# 推荐技术栈

请使用：

* Python 3.11+
* LangChain
* Pydantic v2
* Typer 或 argparse，用于 CLI
* python-dotenv，用于读取 .env
* requests 或 httpx，用于 Jenkins API
* subprocess，用于安全调用 git 和 node
* pytest，用于测试
* Node.js + typescript npm 包，用于 TypeScript Compiler API 子模块

不要把模型调用、Git 命令、Jenkins API、日志读取全部写在一个文件里，要分层实现。

# 推荐目录结构

如果当前仓库没有更合适结构，可创建或调整为类似结构：

ci-owner-agent/
pyproject.toml
README.md
.env.example

ci_owner_agent/
**init**.py
main.py
config.py
schemas.py

```
agents/
  __init__.py
  responsibility_agent.py
  prompts.py

tools/
  __init__.py
  jenkins_tools.py
  log_tools.py
  git_tools.py
  keyword_tools.py
  typescript_tools.py
  local_tools.py

services/
  __init__.py
  jenkins_client.py
  git_client.py
  log_provider.py
  scorer.py
  command_runner.py
```

ts-analyzer/
package.json
tsconfig.json
src/
analyze_changed_functions.ts
find_definitions.ts
find_callers.ts

samples/
company_log/
company-unittest-5064.log
cases/
case-5064.json

tests/
conftest.py
test_log_tools.py
test_git_tools.py
test_keyword_tools.py
test_local_analyze.py
test_scorer.py
test_jenkins_tools.py
test_typescript_tools.py

# 配置

提供 `.env.example`：

JENKINS_URL=
JENKINS_USER=
JENKINS_TOKEN=

CI_AGENT_REPO_CACHE_DIR=E:/ci-agent-cache
CI_AGENT_DEFAULT_LOG_TAIL_LINES=500
CI_AGENT_MAX_TOOL_STEPS=12
CI_AGENT_MAX_TOOL_OUTPUT_CHARS=20000

CI_AGENT_MODEL_PROVIDER=openai
CI_AGENT_MODEL_BASE_URL=
CI_AGENT_MODEL_NAME=
CI_AGENT_API_KEY=

TS_ANALYZER_DIR=./ts-analyzer

实现 `config.py`，从环境变量读取配置。

要求：

1. 不要把 Jenkins token 和 LLM API key 打印到日志。
2. 所有配置都有合理默认值或明确错误提示。
3. 支持 Windows 路径和 Linux 路径。
4. 测试环境默认不访问真实 Jenkins、不调用真实 LLM。

# CLI

使用 Typer 或 argparse 实现两个主要命令。

## 1. 正式 Jenkins 模式

命令示例：

```bash
python -m ci_owner_agent analyze \
  --job services/fx-code-unittest \
  --build 5064 \
  --repo fx-code \
  --log-tail-lines 500
```

流程：

1. 调用 Jenkins 工具获取指定构建信息。
2. 如果 result 是 SUCCESS，直接输出结构化 JSON，说明构建成功，无需定责。
3. 如果 result 是 ABORTED，默认不进入普通代码定责流程，直接输出 no_high_confidence_owner，并说明更可能是 Jenkins Pipeline / 环境 / 中止类问题。
4. 如果 result 是 FAILURE 或 UNSTABLE，继续。
5. 获取同一 job、同一分支或同一关键参数下的上次成功构建。
6. 获取 failedCommit 和 lastSuccessfulCommit。
7. 同步本地仓库缓存。
8. 获取 baseCommit..headCommit 的 commit 列表和 diff 文件列表。
9. 进入 Agent 调查流程。
10. 输出最终 JSON。

## 2. 本地测试模式

命令示例：

```bash
python -m ci_owner_agent analyze-local \
  --repo fx-code \
  --job services/fx-code-unittest \
  --build 5064 \
  --branch dev \
  --base-commit def456 \
  --head-commit abc123 \
  --console-file samples/company_log/company-unittest-5064.log \
  --build-url local://services/fx-code-unittest/5064 \
  --log-tail-lines 500
```

本地模式不访问 Jenkins。它用手动输入的日志文件、baseCommit、headCommit 构造 BuildInfo，然后复用正式模式失败后的 Agent 调查流程。

# Pydantic 数据结构

在 `schemas.py` 中定义以下模型。最终输出必须严格符合这些模型，并序列化为 JSON。

## LogTail

* startLine: int
* endLine: int
* content: str

## BuildInfo

* job: str
* buildNumber: int
* result: str
* buildUrl: str
* branch: str | None
* commit: str | None
* timestamp: str | None
* durationMs: int | None
* logTail: LogTail | None
* warnings: list[str] = []

## SuccessfulBuildInfo

* buildNumber: int
* result: str
* commit: str | None
* buildUrl: str
* warnings: list[str] = []

## CommitInfo

* hash: str
* authorName: str
* authorEmail: str
* subject: str
* timestamp: str | None

## FileAuthor

* name: str
* email: str | None
* commits: list[str] = []

## ChangedFile

* path: str
* status: str
* additions: int | None
* deletions: int | None
* authors: list[FileAuthor] = []

## KeywordMatch

* keyword: str
* file: str
* line: int
* text: str
* changedInRange: bool

## EvidenceItem

* id: str
* type: Literal["log", "diff", "commit", "keyword_match", "ts_symbol", "file_content", "reasoning", "build_info"]
* summary: str
* detail: str
* source: str | None = None

## Owner

* type: Literal["high_confidence", "medium_confidence", "no_high_confidence_owner"]
* name: str
* email: str | None
* commit: str | None
* confidence: float

## CiResponsibilityNotice

* job: str
* buildNumber: int
* buildUrl: str
* result: str
* branch: str | None
* headCommit: str | None
* baseCommit: str | None
* owner: Owner
* failureReason: str
* evidence: list[EvidenceItem]
* suggestions: list[str]
* hasHighConfidenceOwner: bool

# 命令执行安全

实现 `services/command_runner.py`。

要求：

1. 所有 git / node 命令必须通过 subprocess 参数数组调用，不要拼接 shell 字符串。
2. 必须有 timeout。
3. 必须捕获 stdout、stderr、exit code。
4. 出错时返回结构化错误，不要直接崩溃。
5. 工具输出必须限制最大字符数，防止模型上下文爆炸。
6. 不要把密钥打印到日志。
7. 路径参数和 commit 参数要做基础校验，避免命令注入。

# 日志工具

不要做复杂框架正则。实现统一 LogProvider。

`services/log_provider.py`：

抽象接口：

* read_tail(lines: int) -> LogTail
* search(query: str, context_lines: int, max_matches: int) -> dict
* read_range(start_line: int, end_line: int) -> dict
* find_error_chunks(chunk_lines: int, max_chunks: int) -> dict
* detect_final_status() -> str

实现两个 Provider：

1. LocalFileLogProvider：从本地日志文件读取。
2. JenkinsLogProvider：从 Jenkins console log 读取。

对 Agent 暴露统一工具名，不要让 Agent 关心日志来自 Jenkins 还是本地文件。

## 内部函数：log_detect_final_status

实现内部函数：

```python
log_detect_final_status(log_content: str) -> Literal["SUCCESS", "FAILURE", "ABORTED", "UNKNOWN"]
```

要求：

1. 从日志末尾向前查找最后一个：

   * `Finished: SUCCESS`
   * `Finished: FAILURE`
   * `Finished: ABORTED`
2. 如果找不到，返回 `UNKNOWN`。
3. 严禁仅凭 `ERROR`、`FAILED`、`Exception`、`Timeout` 等词判断构建最终状态，因为成功日志中间也可能出现这些词。
4. 本地测试模式可以优先用该函数判断日志最终状态，但如果 CLI 显式传入 result，则以 CLI 入参为准。

## 工具 1：log_read_tail

输入：

```json
{
  "lines": 500
}
```

输出：

```json
{
  "startLine": 10320,
  "endLine": 10820,
  "content": "..."
}
```

## 工具 2：log_search

输入：

```json
{
  "query": "TypeError",
  "contextLines": 30,
  "maxMatches": 10
}
```

输出：

```json
{
  "matches": [
    {
      "line": 8421,
      "startLine": 8391,
      "endLine": 8451,
      "content": "..."
    }
  ]
}
```

要求：

* 普通文本搜索即可。
* 支持大小写不敏感。
* 不要写针对 Jest、Mocha、JUnit、pytest 等框架的大量正则。

## 工具 3：log_read_range

输入：

```json
{
  "startLine": 8200,
  "endLine": 8450
}
```

输出：

```json
{
  "startLine": 8200,
  "endLine": 8450,
  "content": "..."
}
```

## 工具 4：log_find_error_chunks

输入：

```json
{
  "chunkLines": 200,
  "maxChunks": 5
}
```

输出：

```json
{
  "chunks": [
    {
      "startLine": 8200,
      "endLine": 8400,
      "score": 0.87,
      "content": "..."
    }
  ]
}
```

要求：

* 只做粗召回，不做测试框架适配。
* 可以按通用高信号词评分：Error、Exception、AssertionError、TypeError、ReferenceError、FAIL、FAILED、npm ERR、Expected、Received、Cannot read、Timeout、stack trace。
* 这是日志探索工具，不是最终判断依据。
* 成功日志中间也可能出现 Error 或 Failed，所以 find_error_chunks 不能用于判断最终状态。

# Git 工具

实现 `services/git_client.py` 和 `tools/git_tools.py`。

仓库缓存目录来自：

```text
CI_AGENT_REPO_CACHE_DIR
```

假设每个 repo 对应：

```text
{CI_AGENT_REPO_CACHE_DIR}/{repo}
```

或者 bare mirror：

```text
{CI_AGENT_REPO_CACHE_DIR}/{repo}.git
```

请同时兼容普通 clone 和 bare repo。

## 工具 1：repo_sync

输入：

```json
{
  "repo": "fx-code"
}
```

要求：

* 如果是 bare repo，执行 `git remote update --prune`。
* 如果是普通 repo，执行 `git fetch origin --prune --no-tags`。
* 如果仓库不存在，返回明确错误和初始化建议，不要自动猜远程地址。
* 同步后不要 checkout。
* 后续 diff/log 应尽量直接基于 commit 对象运行。

## 工具 2：repo_get_commits_between

输入：

```json
{
  "repo": "fx-code",
  "baseCommit": "def456",
  "headCommit": "abc123"
}
```

内部命令：

```bash
git log --pretty=format:%H%x09%an%x09%ae%x09%at%x09%s base..head
```

输出 CommitInfo 列表。

## 工具 3：repo_get_diff_files

输入：

```json
{
  "repo": "fx-code",
  "baseCommit": "def456",
  "headCommit": "abc123"
}
```

内部命令：

```bash
git diff --name-status base head
git diff --numstat base head
```

输出 ChangedFile 列表。

要求：

* 每个文件尽量带 authors。
* authors 可通过 `git log base..head -- path` 统计。
* status 支持 A、M、D、R。
* 删除文件不要尝试读取 head 内容。

## 工具 4：repo_get_file_diff

输入：

```json
{
  "repo": "fx-code",
  "baseCommit": "def456",
  "headCommit": "abc123",
  "path": "server/routes/middlewares/extensions.ts",
  "contextLines": 8
}
```

内部命令：

```bash
git diff -U8 base head -- path
```

输出：

```json
{
  "path": "...",
  "diff": "...",
  "authors": [...]
}
```

## 工具 5：repo_get_file_content

输入：

```json
{
  "repo": "fx-code",
  "commit": "abc123",
  "path": "server/routes/middlewares/extensions.ts",
  "startLine": 780,
  "endLine": 920
}
```

内部可用：

```bash
git show commit:path
```

要求：

* 支持按行截取。
* 文件过大时限制最大输出长度，并标记 truncated。
* commit 或 path 不存在时返回清晰错误。

# 关键词搜索工具

实现 `tools/keyword_tools.py`。

工具：repo_keyword_search

输入：

```json
{
  "repo": "fx-code",
  "commit": "abc123",
  "keywords": ["shouldRedirectToJsyPassport", "authentication/OTP"],
  "scope": "changed_files",
  "baseCommit": "def456",
  "headCommit": "abc123",
  "paths": [],
  "maxMatches": 50
}
```

scope 支持：

1. changed_files：只搜 base..head 变更文件。
2. paths：只搜指定文件或目录。
3. whole_repo：全仓库搜索。

内部优先使用 git grep：

```bash
git grep -n -I keyword commit -- path
```

要求：

1. Agent 默认应先用 changed_files，再由 Agent 决定是否 whole_repo。
2. 关键词为空时返回空列表，不要报错。
3. 结果数量受 maxMatches 限制。
4. 返回字段：

   * keyword
   * file
   * line
   * text
   * changedInRange

# Jenkins 工具

实现 `services/jenkins_client.py` 和 `tools/jenkins_tools.py`。

## 工具 1：jenkins_get_build_info

输入：

```json
{
  "job": "services/fx-code-unittest",
  "buildNumber": 5064,
  "logTailLines": 500
}
```

输出 BuildInfo。

要求：

* 获取 result、url、branch、commit、duration、timestamp。
* 获取日志最后 N 行。
* 如果 Jenkins API 取不到 commit，尝试从 actions、changeSet、parameters、environment 或 console log 中寻找。
* 提取不到 commit 时 commit 为 null，并附加 warning。
* 不要因为缺少可选字段导致程序崩溃。

## 工具 2：jenkins_get_latest_build_info

输入：

```json
{
  "job": "services/fx-code-unittest",
  "logTailLines": 500
}
```

输出 BuildInfo。

## 工具 3：jenkins_get_last_successful_build_info

输入：

```json
{
  "job": "services/fx-code-unittest",
  "branch": "dev",
  "beforeBuildNumber": 5064
}
```

输出 SuccessfulBuildInfo。

要求：

* 尽量确认上次成功构建与当前失败构建属于同一分支或同一关键参数。
* 如果不能确认同分支，返回 warning，Agent 最终降低置信度。

# TypeScript Analyzer

实现 `ts-analyzer` 子模块。

技术栈：

* Node.js
* typescript npm 包
* ts-node 或编译为 dist 后由 Python 调用

Python 工具通过 subprocess 调用 node 脚本。

## 工具 1：ts_analyze_changed_functions

输入：

```json
{
  "repo": "fx-code",
  "baseCommit": "def456",
  "headCommit": "abc123",
  "files": ["server/routes/middlewares/extensions.ts"],
  "tsconfig": "tsconfig.json"
}
```

输出：

```json
{
  "changedFunctions": [
    {
      "name": "shouldRedirectToJsyPassport",
      "file": "server/routes/middlewares/extensions.ts",
      "startLine": 873,
      "endLine": 889,
      "changeType": "modified"
    }
  ]
}
```

要求：

* 只分析 .ts/.tsx 文件。
* 可通过 git diff 的 changed line ranges 与 AST 函数范围求交，判断改动函数。
* 不要全仓库构建完整调用图。
* tsconfig 不存在或 Program 创建失败时返回明确错误，Python Agent 降级继续。

## 工具 2：ts_find_definitions

输入：

```json
{
  "repo": "fx-code",
  "commit": "abc123",
  "symbols": ["shouldRedirectToJsyPassport"],
  "tsconfig": "tsconfig.json"
}
```

输出 definitions。

## 工具 3：ts_find_callers

输入：

```json
{
  "repo": "fx-code",
  "commit": "abc123",
  "symbol": "shouldRedirectToJsyPassport",
  "definitionFile": "server/routes/middlewares/extensions.ts",
  "tsconfig": "tsconfig.json",
  "maxResults": 50
}
```

输出 callers。

要求：

* 优先使用 TypeChecker 的 symbol 匹配，不能只靠名字。
* 对 get、find、save 等常见方法名要谨慎，无法确认时 resolvedByTypeChecker=false。
* 结果过多时截断。
* TypeScript 工具失败不能导致整个分析失败。

# LangChain Agent

实现 `agents/responsibility_agent.py` 和 `agents/prompts.py`。

可以使用 LangChain 的 tool calling agent。工具函数用 `@tool` 包装，输入输出建议使用 Pydantic schema 或 dict。

MVP 第一阶段允许先实现 RuleBasedResponsibilityAgent / FakeResponsibilityAgent，用于跑通 analyze-local 和 pytest。后续阶段再接真实 LangChain LLM Agent。

Agent 的调查顺序必须符合：

1. 阅读 BuildInfo 和 logTail。
2. 判断日志尾部是否有明确线索：文件名、函数名、测试名、接口路径、异常、模块名。
3. 查看 base..head 的 diff 文件列表和 commit 列表。
4. 优先检查日志中提到的、且位于 diff 文件列表中的文件。
5. 对可疑 diff 文件调用 repo_get_file_diff。
6. 判断 diff 是否能解释失败现象。
7. 如果能解释，基于相关 commit 作者生成候选责任人。
8. 如果不能解释或证据不足，调用 repo_keyword_search，优先 scope=changed_files。
9. 如果 changed_files 搜索不足，再按需 whole_repo 搜索。
10. 如果仍不足，调用 log_find_error_chunks 或 log_search/log_read_range 进一步查看日志。
11. 如果仍不足，且涉及 TypeScript 文件或符号，再调用 ts_analyze_changed_functions、ts_find_definitions、ts_find_callers。
12. 仍无法建立证据链时，输出无高可信责任人。

Agent system prompt 必须包含：

```text
你是 CI 测试失败自动定责 Agent。

你只能基于工具返回的证据判断责任人，禁止编造文件、commit、作者、构建链接。

没有足够证据时，owner.type 必须为 no_high_confidence_owner，owner.name 必须为“无高可信责任人”，hasHighConfidenceOwner 必须为 false。

只有至少两类独立证据互相支撑时，才能输出 high_confidence。

调查顺序：
1. 先阅读构建信息和日志尾部。
2. 判断日志中是否出现明确文件、函数、测试名、接口、异常。
3. 优先检查本次 diff 文件。
4. 如果日志线索和 diff 文件强关联，获取对应文件 diff。
5. 如果 diff 能解释失败，基于该 diff 的相关 commit 作者给出责任人。
6. 如果不能解释，优先在 diff 文件中做关键词搜索。
7. 如果仍不足，再全仓库关键词搜索。
8. 如果仍不足，再使用 TypeScript Compiler API 查函数定义和调用。
9. 仍不足则输出无高可信责任人。

输出必须严格符合 CiResponsibilityNotice JSON schema。
不要输出 Markdown，不要输出解释文字，只输出 JSON。
```

# Agent 外层 Orchestrator

不要把所有步骤都交给模型自由发挥。实现一个外层 orchestrator。

## 正式模式

1. jenkins_get_build_info
2. 如果 result 是 SUCCESS，直接生成成功 JSON。
3. 如果 result 是 ABORTED，直接生成 no_high_confidence_owner JSON，不进入普通代码定责流程。
4. 如果 result 是 FAILURE 或 UNSTABLE，继续。
5. jenkins_get_last_successful_build_info
6. repo_sync
7. repo_get_commits_between
8. repo_get_diff_files
9. 把 buildInfo、base/head commit、commit list、diff file list、logTail 作为初始上下文交给 Agent。
10. Agent 根据需要继续调用工具。
11. 最终输出结构化 JSON。

## 本地模式

1. 从参数构造 BuildInfo。
2. LocalFileLogProvider 读取 logTail。
3. 用 log_detect_final_status 从日志末尾识别最终状态；如果 CLI 入参显式传了 result，则以 CLI 入参为准。
4. 如果 result 是 SUCCESS，直接生成成功 JSON。
5. 如果 result 是 ABORTED，直接生成 no_high_confidence_owner JSON，不进入普通代码定责流程。
6. 如果 result 是 FAILURE、UNSTABLE 或 UNKNOWN，继续。
7. repo_sync
8. repo_get_commits_between
9. repo_get_diff_files
10. 把上下文交给同一个 Agent。
11. 最终输出结构化 JSON。

# 高可信责任人规则

请实现一层简单 scorer 或 validation，用于辅助 Agent 或校验 Agent 输出。

规则：

1. 至少需要两类独立证据互相支撑，才能 owner.type=high_confidence。
2. 可接受证据组合：

   * 日志出现函数/文件/模块 + 本次 diff 修改同一函数/文件。
   * 失败测试指向模块 + 本次 diff 修改模块核心文件 + 文件 diff 能解释失败。
   * 日志关键词命中变更文件 + TS 调用关系显示改动函数影响失败路径。
3. 只有“某人在区间里有 commit”不能判高可信。
4. 只有“某文件被修改”不能判高可信。
5. 如果证据不足，强制降级为 no_high_confidence_owner。
6. 如果是 medium_confidence，hasHighConfidenceOwner 仍为 false。
7. owner.name 为“无高可信责任人”时，email 和 commit 必须为 null，confidence 必须为 0。
8. 所有 JSON 示例中的 email 字段必须是普通字符串，例如 `"email": "zhangsan@example.com"`，不要输出 Markdown 链接格式。

# 最终输出示例

## 成功构建

```json
{
  "job": "services/fx-code-unittest",
  "buildNumber": 5064,
  "buildUrl": "https://jenkins.xxx/job/services/job/fx-code-unittest/5064/",
  "result": "SUCCESS",
  "branch": "dev",
  "headCommit": "abc123",
  "baseCommit": null,
  "owner": {
    "type": "no_high_confidence_owner",
    "name": "无高可信责任人",
    "email": null,
    "commit": null,
    "confidence": 0
  },
  "failureReason": "构建成功，无需定责。",
  "evidence": [
    {
      "id": "E1",
      "type": "build_info",
      "summary": "Jenkins 构建结果为 SUCCESS",
      "detail": "build 5064 succeeded",
      "source": "jenkins"
    }
  ],
  "suggestions": [],
  "hasHighConfidenceOwner": false
}
```

## 失败且高可信

```json
{
  "job": "services/fx-code-unittest",
  "buildNumber": 5064,
  "buildUrl": "https://jenkins.xxx/job/services/job/fx-code-unittest/5064/",
  "result": "FAILURE",
  "branch": "dev",
  "headCommit": "abc123",
  "baseCommit": "def456",
  "owner": {
    "type": "high_confidence",
    "name": "张三",
    "email": "zhangsan@example.com",
    "commit": "abc123",
    "confidence": 0.86
  },
  "failureReason": "认证跳转相关测试失败，日志出现 shouldRedirectToJsyPassport，且本次失败区间修改了 server/routes/middlewares/extensions.ts 中同名方法，该 diff 能解释当前失败现象。",
  "evidence": [
    {
      "id": "E1",
      "type": "log",
      "summary": "日志中出现 shouldRedirectToJsyPassport 相关错误",
      "detail": "log lines 10380-10420",
      "source": "log:10380-10420"
    },
    {
      "id": "E2",
      "type": "diff",
      "summary": "server/routes/middlewares/extensions.ts 在 base..head 区间被修改",
      "detail": "+12 -4, commit abc123 by 张三",
      "source": "git diff def456..abc123 -- server/routes/middlewares/extensions.ts"
    }
  ],
  "suggestions": [
    "优先检查 shouldRedirectToJsyPassport 对 OTP 场景的判断条件。",
    "本地单独运行认证跳转相关测试。",
    "确认测试 mock 的登录态和 passport 跳转配置是否一致。"
  ],
  "hasHighConfidenceOwner": true
}
```

## 证据不足

```json
{
  "job": "services/fx-code-unittest",
  "buildNumber": 5064,
  "buildUrl": "local://services/fx-code-unittest/5064",
  "result": "FAILURE",
  "branch": "dev",
  "headCommit": "abc123",
  "baseCommit": "def456",
  "owner": {
    "type": "no_high_confidence_owner",
    "name": "无高可信责任人",
    "email": null,
    "commit": null,
    "confidence": 0
  },
  "failureReason": "构建失败，但当前日志线索未能和本次 diff 建立明确关联；关键词搜索和 TypeScript 调用分析也未找到足够证据。",
  "evidence": [],
  "suggestions": [
    "人工查看完整 Jenkins 日志中首次失败位置。",
    "确认本次失败构建和上次成功构建是否属于同一分支和同一参数集。",
    "本地复现失败测试以获取更明确的堆栈。"
  ],
  "hasHighConfidenceOwner": false
}
```

## ABORTED 构建

```json
{
  "job": "services/fx-code-unittest",
  "buildNumber": 5090,
  "buildUrl": "local://services/fx-code-unittest/5090",
  "result": "ABORTED",
  "branch": "dev",
  "headCommit": null,
  "baseCommit": null,
  "owner": {
    "type": "no_high_confidence_owner",
    "name": "无高可信责任人",
    "email": null,
    "commit": null,
    "confidence": 0
  },
  "failureReason": "构建被中止，更可能是 Jenkins Pipeline 执行上下文、环境问题或人工中断，不进入普通业务代码定责流程。",
  "evidence": [
    {
      "id": "E1",
      "type": "build_info",
      "summary": "Jenkins 构建结果为 ABORTED",
      "detail": "普通代码定责流程跳过",
      "source": "jenkins"
    }
  ],
  "suggestions": [
    "优先检查 Jenkins Pipeline、post 阶段、节点上下文和执行环境。",
    "确认是否存在人工中止或 Jenkins agent 异常。",
    "如需定责 Pipeline 配置，请后续单独实现 Jenkinsfile / Pipeline 定责逻辑。"
  ],
  "hasHighConfidenceOwner": false
}
```

# 测试要求

请使用 pytest 补充测试。

为了保证测试稳定，不允许测试调用真实 Jenkins、真实 LLM、真实公司仓库。

请使用 pytest fixture 动态创建一个最小 Git 仓库 sample-ts-repo，包含 baseCommit 和 headCommit。

请准备本地日志 fixture，包括：

* auth-failed.log
* unknown-failed.log
* success.log
* aborted.log

请实现 FakeResponsibilityAgent 或 FakeChatModel，测试中默认不调用真实模型。

所有测试必须能通过 pytest 在离线环境运行。

测试不要依赖固定绝对路径，必须使用 tmp_path。

必须覆盖：

1. 成功构建：输入 result=SUCCESS，程序直接输出成功，无需定责。
2. 成功日志中包含 ERROR / FAILED 字样，但最终 `Finished: SUCCESS` 时，程序仍判断为成功。
3. ABORTED 日志不进入普通代码定责流程，输出“无高可信责任人”。
4. 本地日志失败：给定 console log 文件、baseCommit、headCommit，能走完整 analyze-local。
5. 失败高可信：日志证据和 Git diff fixture 能互相支撑时，输出 high_confidence。
6. 失败证据不足：日志失败但 diff 不能解释失败时，必须输出“无高可信责任人”。
7. Git diff 文件列表：能正确返回 changed files、numstat、authors。
8. repo_keyword_search：changed_files 和 whole_repo 两种 scope 都可用。
9. 日志工具：tail、search、read_range、find_error_chunks、detect_final_status 可用。
10. TypeScript 工具失败时不能导致整个分析失败，应降级继续。
11. 工具输出过长时会被截断。
12. Git 命令失败时返回结构化错误。

# 真实日志测试数据说明

我会提供一个真实 Jenkins 日志压缩包 `company_log(1).zip`，里面只有日志文件，没有人工标注的“成功、可定责失败、不可定责失败”。

请不要要求我提前手动标注这些日志。测试代码应该自动根据日志末尾的 Jenkins 结果判断构建状态：

* `Finished: SUCCESS` 视为成功构建样例。
* `Finished: FAILURE` 视为失败构建样例。
* `Finished: ABORTED` 视为中止构建样例。
* 找不到 `Finished: xxx` 时视为 UNKNOWN。

注意：成功日志中间也可能出现 `ERROR`、`FAILED`、`timeout` 等字样，所以不能简单通过是否包含 `ERROR` 判断构建失败。必须以 Jenkins 最终状态为准。

建议优先使用以下日志作为测试样例：

成功构建样例：

* `company-unittest-5060.log`
* `company-unittest-5062.log`
* `company-unittest-5091.log`

明确断言失败样例：

* `company-unittest-5059.log`
* `company-unittest-5061.log`

ESM/目录导入失败样例：

* `company-unittest-5069.log`

外部授权/环境类失败样例：

* `company-unittest-5070.log`
* `company-unittest-5071.log`
* `company-unittest-5079.log`
* `company-unittest-5081.log`

Jenkins Pipeline 中止样例：

* `company-unittest-5090.log`

请把“是否可定责”设计成由 Git diff fixture 决定，而不是只由日志决定。

示例：

1. 使用 `company-unittest-5061.log`，构造 fake repo 的 base/head diff 修改 `test/packages/fxp-ai/errors/classify.test.ts` 或 `packages/fxp-ai/errors/classify.ts`，并包含 `FILE_SIZE_EXCEEDED`、`limit`、`10MB` 等关键词。这个场景可以期望 high_confidence。

2. 同样使用 `company-unittest-5061.log`，但构造 fake repo 的 base/head diff 只修改 `README.md`。这个场景必须输出“无高可信责任人”。

3. 使用 `company-unittest-5090.log`，因为结果是 ABORTED 且错误是 Jenkins Pipeline 上下文缺失，应输出“无高可信责任人”，failureReason 应说明更像 Pipeline 配置/执行上下文问题，而不是明确代码单测失败。

测试必须覆盖：

* 成功日志中包含 ERROR 但最终 Finished: SUCCESS 时，程序仍判断为成功。
* 失败日志中如果 diff 不能解释失败，不能强行指定责任人。
* ABORTED 日志不应进入普通代码定责流程，除非后续明确设计 Jenkinsfile 定责。

# README

请编写 README.md，说明：

1. 项目目标。
2. 安装依赖。
3. .env 配置。
4. 本地仓库缓存要求。
5. analyze 正式模式运行方法。
6. analyze-local 本地测试模式运行方法。
7. TypeScript Analyzer 安装方法。
8. 输出 JSON 字段说明。
9. 高可信责任人的判断规则。
10. ABORTED 构建的处理策略。
11. 真实日志测试数据如何使用。
12. 已知限制。

# 实现优先级

请按以下顺序实现。

## 第一阶段，必须完成

1. Python 项目结构。
2. Pydantic schemas。
3. CLI。
4. LocalFileLogProvider。
5. log_detect_final_status。
6. Git 工具。
7. repo_keyword_search。
8. RuleBasedResponsibilityAgent 或 FakeResponsibilityAgent。
9. analyze-local 闭环。
10. 结构化 JSON 输出。
11. README 和基础 pytest。

## 第二阶段

1. Jenkins API 工具。
2. analyze 正式模式。
3. JenkinsLogProvider。

## 第三阶段

1. TypeScript Analyzer。
2. ts_analyze_changed_functions。
3. ts_find_definitions。
4. ts_find_callers。

## 第四阶段

1. 更完善的 LangChain Agent 调查策略。
2. 真实 LLM tool calling agent。
3. scorer / validator。
4. 更多测试。

# 交付要求

实施时请：

1. 先检查当前仓库结构。
2. 给出简短实现计划。
3. 开始编码。
4. 优先保证 analyze-local 可运行。
5. 每个工具都要有清晰错误处理。
6. 不要把密钥写入代码。
7. 不要大规模重构无关文件。
8. 最后运行 pytest 或 smoke test。
9. 最终总结：

   * 新增/修改了哪些文件
   * 如何安装依赖
   * 如何运行 analyze-local
   * 如何运行 analyze
   * 如何运行测试
   * 当前已知限制

请现在开始实现。
