# CI Owner Agent 项目约定

## 每次开工检查

- 每次开始新的用户任务前，必须先完整阅读本文件，并以其中的路径、仓库用途、可修改范围和验证约束为当前工作基线；不得仅凭记忆、默认工作区或先前结论推断。
- 用户对本机参考/测试仓库的实际用途作出更具体说明时，先核对该仓库的 `remote`、当前分支与未提交改动，再按用户明确授权执行；Git 定责缓存限制不等同于禁止在获授权的测试仓库中修改代码。

## 适用范围与事实来源

- 本文件适用于整个仓库。后续判断应同时参考当前用户要求、源码与测试、`README.md`，以及下列两篇飞书文档：

  - 项目介绍：[https://fanruan-x.feishu.cn/wiki/WynLw41XZiV8zykRwYwcrEJhnQb](https://fanruan-x.feishu.cn/wiki/WynLw41XZiV8zykRwYwcrEJhnQb)
  - 部署维护手册：[https://fanruan-x.feishu.cn/wiki/SJwHwOOK3iTsmUkp7jac2xVenzc](https://fanruan-x.feishu.cn/wiki/SJwHwOOK3iTsmUkp7jac2xVenzc)
- 二期迁移还应参考最新需求文档：

  - Fidget 单元/集成测试通知优化：[https://fanruan-x.feishu.cn/wiki/N8rNwNR2dihxCzkEf8xclopsnng](https://fanruan-x.feishu.cn/wiki/N8rNwNR2dihxCzkEf8xclopsnng)
- 产品目标和业务取舍以项目介绍为背景；当前行为以工作区源码和测试为准；使用与架构说明以当前 `README.md` 为准；生产巡检、发布和恢复以部署维护手册为准。
- 最新需求文档定义二期迁移方向，但不等于其中能力已经实现。涉及 fxp-fidget、简道云 Jenkins、飞书通知、自动治理或新的外部环境时，必须以当前源码、配置和验证结果确认实现状态，不得把规划写成现状。
- 文档与代码不一致时，不要静默猜测。先用源码、测试、配置和只读运行结果确认现状，再说明差异并决定是否同步文档。
- Git 分支与业务分析分支是两个概念。例如生产发布 `main` 不代表周报统计参数应自动从 `dev` 改为 `main`。

## 项目目标

本项目分析 Jenkins/CI 构建失败，输出可审计的结构化责任判断，并结合 MongoDB 历史、企业微信通知与反馈、测试文件失败统计和周报，提高 fx-code 测试失败的可见性、归属明确度与治理效率。二期把一期已经验证的分析、历史、责任判断、通知和反馈流程迁移到 fxp-fidget，只针对 Fidget 仓库、Jenkins 日志和飞书渠道的实际差异做必要修改。飞书作为新增通知媒介，最终业务效果应尽量与企业微信一致；本期仍不建设面向任意仓库、Jenkins、测试体系或通知渠道的通用平台。

核心原则不是“必须找到人”，而是“只在证据足够时找到正确的人”。不得简单把失败归给最后提交者；证据不足、环境或 Pipeline 问题、偶发问题都应明确输出“无高可信责任人”。

## 面向阶段二接手人的沟通方式

- 默认把当前用户视为阶段二接手人：了解业务目标，但不假设熟悉一期代码、内部术语、历史设计或调用链。首次出现 `notice`、责任项、reconciler、历史继承等术语时，先用一句中文说明它在业务流程里负责什么。
- 评审、排障和交付说明必须先回答“这会造成什么用户可见问题、为什么现在要处理”，再说明原因、修改方案和涉及文件；不要直接堆砌函数名、行号、测试编号或没有前因后果的问题清单。
- 同时发现多个问题时，按业务结果分组，并明确区分“必须修复，否则会错报/漏报”和“只影响文档、可维护性或后续扩展”。必要时用一个构建示例说明修复前后差异。
- 最终回复优先给出可执行结论：现在是否合格、是否可以进入下一阶段、还有什么阻塞。源码位置和技术细节作为结论证据放在后面，不能让用户自行从代码细节推导结论。
- 用户表示看不懂或缺少背景时，后续回复应主动降低术语密度、补齐数据流和上下文，不重复原来的技术列表。

## 二期迁移：fxp-fidget

### 迁移范围与现状边界

- 二期最终交付聚焦两项：分析 Fidget 单元测试结果（以 Jenkins 构建日志为主要输入），输出失败原因及证据充分时的责任判断；新增飞书渠道，承接与一期企业微信尽量一致的通知、人员触达和反馈闭环。
- 二期目标仓库是 Bitbucket 项目 `FX/fxp-fidget`（工程名 `@fx/fidget`），它是数据引擎框架，为表单和其他业务对象提供统一数据访问层与多种存储引擎支持。
- 目标 Jenkins 位于 `https://jenkins.jdydevelop.com/`，需求文档给出的流水线是 `npm/fxp-fidget/fidget-build`。它与一期 fx-code 的 Jenkins、仓库缓存、凭据和运行环境是不同外部系统；迁移时分别配置、验证和审计，不得覆盖一期配置或复用人工开发工作区。
- 当前源码已经具备 Fidget/Japa 失败块解析、Git 责任窗口、MongoDB 历史、测试维护人路由、企业微信通知与反馈等可复用能力；是否已支持某个 Fidget job、飞书渠道或自动治理必须由代码和测试证明。
- 复用现有服务边界和严格 notice schema，但只为 Fidget 的实际差异做必要修改。不要为了潜在的第三个项目预先引入项目注册中心、通用 Project Profile、任意测试框架适配器或通用通知总线。
- 一期生产部署手册仍是当前生产运维基线。二期未形成并评审新的部署配置、回退点、凭据方案和运行手册前，不得直接把 Fidget 迁移结果按一期路径部署为生产任务。

### Fidget 工程背景与本期分析范围

- Fidget 是 npm workspace 多包工程，包含 `fidget-core`、`fidget-lake`、`fidget-mongo`、`fidget-postgres`、`fidget-sql`、`fidget-sdk`。解析路径、模块名和测试身份时要兼容 workspace/package 维度，不能沿用 fx-code 的目录假设。
- 本期实现和验收聚焦 Fidget 单元测试失败。质量检查 `npm run typecheck`、`npm run lint`、`npm run lint:test` 仍是 CI 背景；日志命中这些阶段时要与单元测试失败区分，不能误标为测试用例责任，但本期不要求建设完整的 TypeScript/Lint 通用分析器。
- 单元测试使用 Japa，覆盖率使用 c8。单元测试位于 `packages/*/test/**/*Test.ts`，入口包括 `npm run test`、`npm run test:coverage` 和 `npm run lint:test`。
- 单元测试要求 6 个 package 全部通过；100% 覆盖率目标适用于 `fidget-core`、`fidget-lake`、`fidget-mongo`、`fidget-postgres`、`fidget-sql`，需求文档未把 `fidget-sdk` 列入该覆盖率集合。不要擅自扩大或缩小口径。
- Connection 测试仍不在交付范围。`packages/fidget-sdk/test/integration` 集成测试失败分析已接入（Fidget 二期）：集成日志以 `FIDGET_INTEGRATION_V1` runner marker 为协议锚点，Japa 失败块按行号关联 Jenkins stage 与 suite；环境/基础设施失败（preflight/cleanup marker 失败、外部库 ECONNREFUSED 等连接错误）确定性 no-owner 且不进历史继承，代码/断言失败（含 AssertionError）继续走可信 Git 定责。混合失败时两类各自独立，环境项不得清空代码责任。日志若包含外部数据库不可用、版本不兼容或连接失败证据，优先判断为环境/基础设施问题，不得直接归责代码提交者。
- 本地参考环境为 MongoDB 4.2+、PostgreSQL 18+，但需求文档明确提示 SELECT 集成测试可能报错；测试环境使用 MongoDB 4.2+ 和 Protonbase。连接信息属于外部凭据，只能从授权配置或技术支持获取，不得写入仓库、日志或提示词。

### Fidget 二期当前代码基线与本地验证资料

- 当前 Python 工作区为 `F:\FanRuan\Project_3\ci-owner-agent-Fidget`；本机 Fidget 参考工作区为 `F:\FanRuan\fxp-fidget\fxp-fidget`。后者只用于只读核对 package 配置、Nx/Japa/c8 行为和本地复现；Git 定责仍必须使用 Agent 专用 repo cache，不得把人工工作区接入会 fetch、checkout 或切换提交的 Git service。
- 稳定 CLI 入口是 `python -m ci_owner_agent <command>`。Fidget 本地复现常用 `npm --workspace @fx/fidget-sql run test` 与 `npm --workspace @fx/fidget-sql run test:coverage`；Windows 下需要 `WT_SESSION=1` 才能稳定得到当前解析器识别的 Japa `✔/✖` 符号。不得把本机路径或环境变量值硬编码进生产逻辑。
- `samples/fidget_log/` 是二期离线回放入口：`err1-source-tampered.log` 为多 Japa 失败样本，`err2-test-assert.log` 为单 Japa 失败样本，`fidget-build-dev-313.log` 为 SUCCESS/5 包 coverage 基线，`cov-gap-single-file.log` 为 c8 coverage-gap 样本。样本只能证明其覆盖到的协议；真实 Jenkins 多包失败输出、行交错和渠道投递仍需按上线验证顺序单独确认。
- 当前 Fidget 结构化测试失败以 Japa `✖` 失败行为锚点；解析前会剥离 Docker/BuildKit `#N <time>` 行前缀和 ANSI 控制码，以兼容本地输出与 Jenkins 合并日志。不得重新把一期 Mocha stage/make 锚点作为 Fidget 主路径，也不得把 Docker、Jenkins、shell、typecheck 或 lint wrapper 当成可继承的测试失败事实。
- c8 coverage 失败只认 `ERROR: Coverage for <metric> (<pct>%) does not meet [global] threshold (<thr>%) [for <file>]`，且只有 `pct < threshold` 才成立；`Coverage summary` 或 sdk 的 `text` 表格低于 100% 不能单独触发门槛失败。per-file 路径是包内相对路径，global 行没有文件路径，禁止伪造 `failureFilePath`。
- coverage 解析和定责采用方案 X：coverage failure facts 可以保留在完整 `failure_summaries` 供审计和 reconciler 使用，但不得作为 Agent 的责任项输入；coverage-only 构建可跳过责任分析 Agent；`coverage_responsibility` reconciler 是 coverage `ResponsibilityItem` 的唯一写入口，并应移除 Agent 意外生成的 coverage 项后再写 canonical item。
- coverage 包定位采用受约束 B+C：B 只使用能确认边界且 Nx task 包头与 npm lifecycle 包名一致的已有日志块；B 不可靠或缺失时，C 在可信 `headCommit` 上批量校验 5 个 gating 包的完整候选路径，只有唯一命中才接受。B/C 冲突、零命中、多命中、路径不存在或日志疑似交错时必须 fail closed；不得修改 Fidget 生产代码只为给日志补包名。
- coverage deterministic owner 的上限是 `medium_confidence`：完整文件路径存在、可信 `base..head` 责任窗口有效、相关 diff 非空且只有一个作者时才可建 `current_build_owner`；多作者、无 diff、范围无效或 Git 校验失败都输出 `no_high_confidence_owner`。禁止选择最后、最近或提交最多的作者；若未来需要 high，必须另建受确定性校验的证据增强阶段，不能恢复 Agent 与 reconciler 双写。
- Japa 与 coverage 可以在同一构建中同时失败，日志服务必须合并两类摘要，不能因已有 Japa chunk 丢掉 coverage；即使达到 chunk 预算，也必须通过完整 totals/coverage 文件记账保证 coverage 事实不被静默丢失。coverage 不进入 Japa 确定性历史继承，也不写入 `ci_test_file_failures`；feedback 可使用 `failureFilePath`，无 owner 的维护人路由应读取 `testFilePath or failureFilePath`，但不得把 Maintainer 写成 causal owner。
- 集成测试（`FIDGET_INTEGRATION_V1` 协议）失败分类是纯函数、可离线回放：`build_integration_protocol_index` 严格解析 marker 并校验 run_id 一致/格式、phase 枚举、preflight step 枚举、suite 成对、summary/cleanup 缺失重复、状态机顺序、summary 与 suite 结果一致性，冲突/残缺 fail-closed 记入 `conflicts`；协议完整性校验仅在日志含 V1 marker 时执行（避免普通 Unit 日志被伪造协议冲突）。`classify_all_japa_failures` 全量逐块判 assertion（有 AssertionError）/connection（连接错误）/unknown，不受展示预算限制，且逐块 stage 门控——仅 Integration Tests stage 或 suite range 内的块做集成分类，Unit stage 块标 `unit` 保持 Unit 语义。connection/unknown 类块从 `chunks` 隔离（不进历史白名单、不进 Agent 输入、不写 `ci_test_file_failures`），计入 `totals.integrationEnv`；preflight/cleanup marker 失败同样 no-owner。集成日志的 code chunk 由 `build_integration_failure_summaries` 直接按行号生成，与分类一一对应，不经过 `build_test_failure_summaries`（后者在大量相邻 ✖ + 交错 ❯ 下会膨胀重复 chunk）。`_is_integration_env_only_failures` 在「有环境失败或协议冲突、且无代码 chunk」时确定性短路为 no-owner notice、不调责任 Agent，且短路前跳过 AI failure facts 提取（防环境事实误入历史）；`failure_fact_ai` prompt 强制数据库/基础设施事实 historyEligible=false。混合构建进 Agent 后由 `reconcile_integration_responsibilities` 写 canonical no-owner 并移除 Agent 误生成的环境 owner。X/Y 数据库错误业务 Topic 的失败含 AssertionError，不会被判环境。
- Fidget 二期长期回归测试必须命名为 `test_*.py`，确保当前 `pyproject.toml` 的默认 `python -m pytest -q` 能发现；仅显式运行成功的 `stage2_*.py` 不算进入标准门禁。归档任务可从 `.trellis/tasks/archive/2026-08/08-21-fidget-coverage-gap-recognition/` 查设计背景，但 `task.json.status=completed`、聊天总结或 commit message 都不能替代 `task.py validate`、默认测试发现、源码逐项验收和真实日志回放。

### 二期责任路由与治理原则

- “总是能找到人”应解释为“总能给出可执行的处理路径”，不能降低高可信责任人的证据门槛：有可信 checkout、责任窗口、失败事实和相关 diff 时可指向引入提交者；证据不足时保持 `no_high_confidence_owner`，再按明确映射通知待确认 Maintainer。
- Maintainer 是处置/关注路由，不等于根因责任人。通知、schema、MongoDB 和反馈中必须区分 causal owner、待确认维护者以及兜底通知对象，不能为了满足 @ 人需求篡改责任结论。
- Git、历史、通知和统计身份至少按 `repo + job + branch` 隔离。接入多个 Jenkins 实例前还要审计 Jenkins 实例标识是否需要进入缓存、去重键和持久化身份，避免不同系统的同名 job 混用。
- 自动治理、按频率关闭测试和 Fidget 周报不属于本期交付。飞书反馈闭环属于二期范围，应优先复用一期反馈 operation、历史修正、幂等和持久化语义；只有飞书身份、消息和交互能力确实不同时才增加渠道专用实现。
- Fidget 可能一次产生大量失败项。通知必须优先保证根因、责任类型、关键证据和处理入口可读；按渠道的 UTF-8 字节/卡片限制做整体预算、摘要、分组或下钻，不能简单丢弃未展示失败。

### 本期实现与上线约束

- Fidget 仓库路径、Jenkins URL/job 和单元测试特征应集中在明确的配置或 Fidget 边界中，不能散落在核心编排里；但不要求把它们抽象为支持任意项目的通用框架。
- 企业微信是一期当前已验证渠道，既有能力继续保持兼容；二期新增飞书渠道。可以在确有重复时提取通知与反馈的共享业务逻辑，但不要求预先重构全部企微实现或建设支持任意渠道的通用分发框架。
- 飞书最终效果原则上与企微一致，包括分析结果展示、责任人/维护人触达、消息长度控制、去重、失败降级、反馈提交与历史修正闭环。飞书 API 能力与企微不完全一致时，追求业务结果一致，不强求卡片外观或交互形式逐像素一致。
- 飞书发送、用户身份映射与 @、互动消息/卡片、反馈回写、幂等和测试覆盖完成并经过真实环境验证前，不得宣称已经达到企微同等效果。
- 二期生产模型必须使用获准的国产模型服务，例如火山方舟或通义千问等；保持 OpenAI-compatible/模型客户端边界，不把供应商密钥、模型名或 URL 硬编码进业务逻辑。
- 当前集成测试父任务的验证顺序收敛为：Fidget 日志样本离线回放 -> 本地 Fidget 单元测试日志 -> Jenkins 单构建在线分析并向已确认的企业微信测试群直接发送（D-011），至此由 4 个子任务收口。飞书 dry-run、飞书测试群真实发送与 @、反馈闭环和目标群/生产配置评审按 D-012 后续另建独立 Trellis 任务，本次不创建。Connection 测试仍不在交付范围。
- 二期项目最终交付仍包括精简方案、Fidget 日志分析测试/验收证据、实际 Jenkins 接入、飞书测试群与目标群投递证据，以及必要的部署/回退说明；其中飞书能力按 D-012 由后续独立 Trellis 任务承接，不属于当前四子任务父任务。文档中的“两到三周”只是原需求节奏参考，不应转化为代码中的硬期限。

## 不可破坏的业务不变量

- 只信任 Jenkins 实际 checkout SHA 或等价的可信构建证据。普通日志中的任意 SHA、环境变量打印或分支指针不能替代 checkout 事实。
- 执行 Git diff 前必须确认逻辑分支、`baseCommit`、`headCommit`，并校验 base 是 head 的祖先。无法确认时 fail closed，不输出高可信代码责任。
- 保留完整责任窗口 `fullRange = last successful commit -> current head`；有上一构建 commit 时可先查更窄的 `focusRange`，但不得丢失完整窗口语义。
- `SUCCESS` 直接生成成功 notice；`ABORTED` 默认无责任人；`FAILURE`、`UNSTABLE`、`UNKNOWN` 才进入失败分析。不能只靠进程退出码判断分析有效性，必须校验 notice 内容与 schema。
- 优先提取最内层失败事实。Docker、BuildKit、Jenkins、shell、Make 等外层 wrapper 不能在存在内层证据时充当根因。
- 确定性历史继承只使用足够稳定的结构化测试失败摘要（当前为 Fidget/Japa 失败块）。非结构化失败通过可选 AI failure facts 处理；generic wrapper 或低置信事实不得形成历史继承。
- 历史责任继承必须能追溯到更早的可信构建和匹配失败项；人工反馈可确认、修正或阻断继承。跨 `repo + job + branch` 的记录不得混用。
- 对外 JSON 使用 `schemas.py` 中的严格 Pydantic 模型（`extra="forbid"`）。新增或修改责任字段时，保持构建级 owner、`responsibilityItems`、`hasHighConfidenceOwner` 和历史来源字段一致；不一致时应保守降级。
- 通知是 notice 的下游副作用，通知失败不得覆盖或破坏已生成的 notice。Webhook 直发与 Bot Outbox 是两条不同链路，不能混淆其依赖、去重和重试语义。
- 企业微信 Markdown 有 4096 字节限制。涉及通知格式时按 UTF-8 字节验证整条消息，而不是仅限制字符数或单个字段；目标群、真实 @ 人和敏感信息也必须在真实发送前检查。
- 批处理、超时和 resume 逻辑默认 fail closed。超时、非零退出、清理失败、notice/marker 校验失败后的残留产物不可信，不得当作成功结果复用。

## 代码地图

- `ci_owner_agent/__main__.py`、`ci_owner_agent/main.py`：稳定 CLI 入口和 argparse 参数定义；命令执行不再堆积在 `main.py`。
- `ci_owner_agent/cli/commands.py`：解析参数后的命令分发、输出边界、服务启动和 CLI 级错误处理；它调用领域 service，不承载历史匹配或通知投递算法。
- `ci_owner_agent/orchestrator.py`：构建状态门控、Git 调查范围、失败摘要/事实、历史预检、Agent 调用与结果保存的核心编排；历史匹配和历史 no-owner 规则已下沉到独立 service。
- `ci_owner_agent/schemas.py`：对外输入输出和责任模型的唯一 schema 中心。
- `ci_owner_agent/agents/`：Agent 运行上下文、工厂、提示词、fake 与 LangChain 实现。
- `ci_owner_agent/tools/`：只保留 Agent 可见的薄适配和工具组装，包括 `langchain_tools.py`、`typescript_tools.py`、路径与关键词工具。Git、Jenkins、日志和历史算法的真实实现属于 services，不要重新建立已删除的平行 `*_tools.py` 业务模块。
- `ci_owner_agent/services/git_client.py`、`jenkins_client.py`、`log_provider.py`：Git、Jenkins 和日志的确定性能力；CLI、编排器和 Agent 工具应复用这些实现。
- `ci_owner_agent/services/history_store.py`：MongoDB 集合、索引和持久化接口；`history_search.py` 负责结构化失败历史查询，`ai_history_search.py` 负责 AI failure facts 的保守语义预检，`history_no_owner.py` 负责历史 no-owner 的信任校验、强证据回退和严格 notice 构造。
- `ci_owner_agent/services/wecom_notice_service.py`：企业微信 notice 格式化、映射、去重、Webhook/Bot 投递及失败隔离；`notification_formatter.py` 只负责企业微信消息渲染，不应重新承担投递副作用。
- `ci_owner_agent/services/structured_output.py`：模型返回 JSON 对象的共享解析边界；failure fact 提取和对比等调用方应复用它，避免复制宽松解析器。
- `ci_owner_agent/services/` 其余模块：失败身份与继承、反馈、企业微信 Bot、统计、周报和 metrics 等确定性领域服务。
- `scripts/`：轮询、批处理、重复运行、回填、导入导出和周报等运维入口；共享路径、timeout、原子 marker 和 Windows 进程树终止语义集中在 `_runtime.py`，批处理环境/产物/序列化集中在 `_batch_common.py`，LangSmith root run 查询、trace 落盘和统计集中在 `_langsmith_trace.py`。
- `ts-analyzer/`：基于 TypeScript compiler API 的静态分析辅助工具，由 Python tool 层调用。
- `tests/`：pytest 单元与集成测试；新增行为应优先在对应同名测试文件中覆盖，跨进程行为放在 `tests/integration/`。
- `config/`：周报阈值和测试维护者映射；`deploy/systemd/`：生产调度模板；`.env.example`：公开配置契约。

## 实现约定

- 运行环境为 Python 3.11+；统一入口是 `python -m ci_owner_agent <command>`。参数契约留在 `main.py`，执行逻辑进入 `cli/commands.py`；不要另造平行 CLI、绕过输入校验，或把命令实现重新堆回 parser 文件。
- 优先把可确定的解析、归一化、校验、匹配、去重和降级写成 services；Agent 只负责需要多证据调查和判断的部分。
- `tools/` 是 Agent 调用适配层，不是领域逻辑归属地。历史查询、Git/Jenkins/日志访问等能力应先形成可直接测试的 service，再由 `langchain_tools.py` 包装；不得恢复已删除的 `history_tools.py`、`ai_history_tools.py`、`git_tools.py`、`jenkins_tools.py`、`log_tools.py` 或 `local_tools.py` 平行实现。
- `orchestrator.py` 只决定流程顺序、状态门控和何时调用领域规则。历史候选排序、继承可信度、no-owner 短路和 notice 构造应留在 `history_search.py`、`ai_history_search.py`、`history_no_owner.py` 等 service 中。
- CLI 层不得直接重建企业微信格式化、去重和发送流程；统一调用 `wecom_notice_service.py`，并保持通知失败不覆盖 notice 的既有语义。
- 批处理和重复运行脚本不得互相导入对方的业务入口。共享环境加载、产物清理、序列化和 LangSmith 查询分别复用 `_batch_common.py`、`_langsmith_trace.py`；进程超时与回收统一复用 `_runtime.py`。
- 在输入边界规范化分支名、仓库路径、失败签名、时间和配置值。MongoDB 身份字段使用规范化值，避免同一实体出现多种表示。
- 保持显式错误和保守默认值。外部依赖不可用、上下文不完整或证据冲突时，不要为了流程继续而伪造成功、owner、commit 或历史来源。
- 读写输出文件时保持 UTF-8 和原子替换；不得重新引入已知会导致中文乱码、部分 notice 或错误 resume 的写法。
- 新增环境变量时同步更新 `Settings`、`load_settings()`、`.env.example`、README 和配置测试。密钥字段的日志与对象表示必须脱敏。
- 修改公共 schema、MongoDB 文档结构、通知格式、失败签名或去重键时，先检索所有生产者、消费者、历史兼容逻辑和相关测试，避免只改单点。
- 修改 `ts-analyzer/` 时遵循现有 CommonJS/TypeScript compiler API 结构，并验证 Python 调用端与 Node 输出协议。
- 保留用户工作树中无关的未提交改动；不要顺手格式化、回退或清理不在当前任务范围内的文件。

## 外部副作用与生产安全

- 默认只做本地、fake provider、history 关闭、通知关闭或 `dry-run` 验证。真实 Jenkins、模型、MongoDB 写入、企业微信发送、反馈写入、服务器/systemd 操作和部署都需要用户当前任务明确授权。
- 不读取、展示、复制或提交 `.env` 中的真实凭据。日志和答复中不得出现 Jenkins token、模型 API key、Webhook URL、Bot secret、chatid、反馈 token、SSH 私钥或密码。
- Git 工具必须使用 Agent 专用 repo cache，不要指向人工开发工作区；它可能执行 fetch、detached checkout 或读取指定 commit。
- 真实通知前依次验证：notice 证据、内容与目标群、@ 人映射、敏感信息、UTF-8 字节数、去重行为；先测试群后正式群。
- 部署前先只读确认实际目录、运行用户、unit、timer、Git 状态、目标 commit、`.env` 权限、MongoDB 与 repo cache。手册中的路径和 unit 名是基线，不得跳过现场确认。
- 发布只允许 fast-forward 到批准的 `main` commit，并以全量测试通过为门禁。轮询服务运行中、生产工作树有未知修改、无法确定回退点或依赖异常时停止发布。
- 不猜测或擅自修改周报统计分支、通知目标、维护者映射、数据库、服务名或生产配置。
- `scripts/clear_history_failure_chunks.py` 等全量清理操作具有破坏性；未经明确授权和目标/备份确认不得执行。

## 验证要求

- 改动前先查看 `git status --short`，并根据影响面定位实现、调用方和同名测试。
- 先运行最小相关测试，再按风险扩展。Python 代码的标准门禁是：

  ```powershell
  python -m compileall ci_owner_agent scripts tests
  python -m pytest -q
  ```
- 仅文档变更可不跑全量测试，但必须检查链接、命令、文件名与当前源码一致，并审阅 `git diff --check` 和目标文件 diff。
- 改动 `ts-analyzer/` 时至少对变更的 JS 执行 `node --check`；依赖验证使用锁文件与 `npm ci`，不要无故重写 `package-lock.json`。
- 外部集成验证一般遵循：fake/local -> 真实模型但不通知 -> Jenkins 单构建 -> MongoDB/周报 dry-run -> 通知 dry-run -> 测试群真实发送 -> 定时任务/生产链路。Fidget 集成子任务 4 是已记录例外：按 D-011 在 Jenkins 在线分析阶段直接发送企业微信测试群；正式群、飞书、反馈和生产链路仍按逐级验证执行。
- 为当前任务临时新增的测试或验证脚本，在验证成功后立即删除，并同步移除 `package.json` 中仅为该脚本新增的命令。保留项目原有测试、用户明确要求长期保留的自动化测试，以及仍需继续定位失败的脚本。

## 文档同步

- 用户可见的命令、配置、目录结构、schema、部署或运维语义变化时，同步更新 `README.md` 和相应 `docs/`；生产流程变化还要指出部署维护手册需要更新。
- `PROJECT_OVERVIEW.md` 是面向接手人的项目全貌梳理（流程链路、模块职责、MongoDB 集合、Agent 架构）。改动编排流程、Agent/工具、`schemas.py`、MongoDB 集合结构或通知链路时，检查并同步该文档，避免它偏离当前实现。
- 文档示例必须标明 dry-run 与真实副作用的区别，不得写入真实凭据或把本机/历史环境值包装成通用默认值。
