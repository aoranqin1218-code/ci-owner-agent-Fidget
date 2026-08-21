# CI Owner Agent 项目全貌梳理

> 本文档由 Claude 基于当前工作区源码逐文件核实整理（含未提交的迁移改动），用于接手人快速建立对项目链路、模块、MongoDB 与 Agent 架构的完整认知。
>
> 事实来源优先级：当前源码与测试 > README.md > AGENTS.md > 飞书文档。文档与代码不一致时以源码为准。

## 0. 一句话定位

一个**分析 Jenkins/CI 构建失败、输出可审计的结构化责任判断（notice）**的 Python 项目：综合"可信 checkout SHA + Git 区间 diff + 失败日志摘要 + MongoDB 历史 + 人工反馈 + 可选 LLM 调查"，判断每个失败项是**当前构建新引入 / 历史持续失败继承 / 偶发或环境问题 / 证据不足无法定责**，最后按配置推送到企业微信，并在群里闭环收集人工反馈。

核心原则是**"宁缺毋滥"**——证据不足必须输出"无高可信责任人"，绝不硬找一个。

### 二期最新批准范围（2026-08-20）

二期已经收敛为两个交付目标：

1. 复用一期可信 checkout、Git 责任窗口、日志事实和保守定责链，分析 **Fidget 单元测试结果/Jenkins 构建日志**中的失败原因；
2. 增加**飞书通知与反馈渠道**，最终业务效果尽量与一期企业微信一致，包括分析结果展示、责任人/维护人触达以及反馈修正闭环。

本期不建设支持多个仓库、多个 Jenkins、任意测试框架或任意通知渠道的通用适配平台，也不包含 Fidget Connection/集成测试接入、自动关闭测试或 Fidget 周报。以下章节仍按当前源码描述一期现状；企业微信链路代表二期应优先复用的业务流程，但飞书 API、身份体系和交互形式的差异应封装在飞书专用边界中，不要求复制相同界面或建设任意渠道框架。

## 1. 命令入口与分发

```text
python -m ci_owner_agent <command>
   __main__.py ──► main.py (argparse 参数定义，build_parser)
                    └─► cli/commands.py (dispatch_command，按子命令分发执行)
```

| 命令                                            | 入口函数                                                                  | 做什么                      |
| ----------------------------------------------- | ------------------------------------------------------------------------- | --------------------------- |
| `analyze`                                     | [cli/commands.py](ci_owner_agent/cli/commands.py) `_run_analyze_jenkins` | 在线读 Jenkins 构建 → 分析 |
| `analyze-local`                               | `_run_analyze_local`                                                    | 离线分析本地 console log    |
| `notify-notice`                               | `_run_notify_notice`                                                    | 发送/预览已有 notice        |
| `feedback apply/list`                         | `_run_feedback`                                                         | 提交/查看人工反馈           |
| `serve-feedback`                              | `_run_feedback_server`                                                  | 启动 FastAPI 反馈 Web 服务  |
| `test-failure-stats` / `weekly-test-report` | `_run_test_failure_reporting`                                           | 统计/周报                   |
| `serve-wecom-bot`                             | `_run_wecom_bot`                                                        | 启动企微长连接机器人 Worker |

所有配置从环境变量/.env 经 [config.py](ci_owner_agent/config.py) 的 `load_settings()` 读成不可变的 `Settings` dataclass（默认值见 [.env.example](.env.example)）。

## 2. 一次完整流程的链路拆解

以 `analyze`（Jenkins 在线）为例，一条失败构建从触发到通知的完整链路：

> **整条链路一个故事（先看这个，再往下啃）**：把"一次 CI 失败定责"想成**查一起生产事故**——
> - **A 报案**：先登记这单"值不值得接"——构建成功了没？失败的话 checkout 和分支信息全不全？全且失败才立案；
> - **B 布置现场**：把代码仓库拉到最新，确认"上次成功 → 这次失败"是同一根线上的直系提交（中间没岔分支），再圈定调查范围（能窄则窄），清点改动文件和作者；
> - **C 现场取证**：用机器从日志里抠出"失败指纹"（哪条测试挂了、报什么错），再去历史档案查"有没有同款案子、同款嫌疑人"；
> - **D 翻旧档捡便宜**：如果历史上这案子已经定过"查无责任人"、这次又没新变化，直接沿用旧结论收工——省下 AI 调查的钱；
> - **E 侦探出马**：AI agent 带着 C 的材料袋做现场调查，判断"这锅该谁背"；
> - **F 复核定稿**：用真实数据核对、按严格门禁查证据够不够，不够就降级为"查无责任人"，最后归档入库；
> - **G 宣判送达**：结论写文件 / 发企微通知，群里的人还能凭反馈码纠正判断，形成闭环。
>
> **记住这几个要点就够**：
> - 全链路是**层层接力**：A 定责任窗口 → B 凑材料 → C 提指纹查历史 → D 能省则省 → E 现场定责 → F 把关落库 → G 送达；
> - **确定性优先**：能用纯代码规则（正则提指纹、git diff）就用它，AI 只在规则搞不定时兜底；
> - **宁可漏、不可错**：证据不足宁可不指认责任人，也不乱安锅——这是全链路反复出现的门禁思路。

> **A→G 一行速览（往下啃时随时回来查）**：

| 阶段 | 一句大白话 | 核心动作 | 关键代码 |
|---|---|---|---|
| **A 报案** | 这单值不值得查 | 状态门控 + 双源 checkout SHA + 找上次成功 | `analyze_jenkins`（[orchestrator.py](ci_owner_agent/orchestrator.py)） |
| **B 布置现场** | 拉齐直系提交、圈范围 | git sync + ancestry 校验 + focus/full 范围 | `analyze_failed_build` |
| **C 现场取证** | 提炼失败长相 | 正则提指纹 + 查历史 + 可选 AI facts | `_with_precomputed_failure_context` |
| **D 翻旧档** | 能不能白嫖历史结论 | 历史 no-owner 三道闸门短路 | [history_no_owner.py](ci_owner_agent/services/history_no_owner.py) |
| **E 侦探出马** | 现场定责 | ReAct agent + 材料袋 + 15 工具 | [agents/langchain_agent.py](ci_owner_agent/agents/langchain_agent.py) |
| **F 复核定稿** | 把关定稿 | 覆盖元数据 + 门禁 + 补指纹路径 + 落库 | [scorer.py](ci_owner_agent/services/scorer.py) 等 |
| **G 宣判送达** | 送到人手里 | 写文件 / 企微通知 + 反馈码闭环 | [wecom_notice_service.py](ci_owner_agent/services/wecom_notice_service.py) |

### 阶段 A：取构建信息与状态门控（[orchestrator.py](ci_owner_agent/orchestrator.py) `analyze_jenkins`，约 742 行）

> **这个阶段在干嘛（大白话）**：给这次构建"登记档案"——收集状态/分支/checkout SHA/日志尾巴这些事实，然后做"值不值得查"的门控：成功或被取消就直接收尾，失败但缺 checkout 或分支不清就关门。最后找到上次成功构建拿到 base_commit，与本次的 head_commit 组成完整责任窗口。

1. `JenkinsClient.get_build_info(job, build, log_tail_lines)`（[jenkins_client.py](ci_owner_agent/services/jenkins_client.py)）：
   - 拉 Jenkins `api/json` + `consoleText`；
   - 用 `resolve_checkout_revision_from_console_log`（日志中的 `Checking out Revision <40hex>` / `git checkout -f <40hex>`）和 metadata 侧 `_resolve_checkout_commit_from_metadata`（`lastBuiltRevision.SHA1` / `git_commit` 参数）**双源确定可信 checkout SHA**，任一歧义即置 None（宁缺毋滥）；
   - 结果封装成 `BuildInfo`（job/buildNumber/result/branch/commit/timestamp/logTail/warnings）。
2. **状态门控**：
   - `SUCCESS` → `success_notice()` 直接生成成功 notice，不分析；
   - `ABORTED` → `aborted_notice()` 无责任人 notice；
   - `FAILURE / UNSTABLE / UNKNOWN` → 继续；但若 `commit is None` 或分支无法规范化（`normalize_branch_name` 返回 None），直接 `failure_without_context` 关门。
3. `JenkinsClient.get_last_successful_build_info(job, branch, before_build_number, scan_limit)`：
   - 找当前分支上、早于当前构建的最近 `SUCCESS` 构建（先 `lastSuccessfulBuild` 快路径，再向前逐个扫描）；
   - 得到 `base_commit`（上次成功构建的 checkout SHA），`head_commit` = 当前构建 SHA → **完整责任窗口 fullRange = lastSuccess → currentHead**。
4. `log_provider = JenkinsLogProvider`，进入 `analyze_failed_build`。

> **产出（传给阶段 B）**：`analyze_jenkins` 把 `BuildInfo`（job/构建号/状态/branch/checkout SHA/logTail/warnings）、`base_commit`（上次成功构建 SHA）、`head_commit`（本次 SHA）、`log_provider`、上次成功构建号打包传进 `analyze_failed_build`。门控在 A 内分流：SUCCESS / ABORTED / 缺 checkout / 缺 base 直接出 notice 收尾，**只有失败且有完整责任窗口的构建才进 B**。

### 阶段 B：Git 同步与 ancestry 校验（[orchestrator.py](ci_owner_agent/orchestrator.py) `analyze_failed_build`，约 191 行）

> **这个阶段在干嘛（大白话）**：为 diff 打地基——把本地仓库同步到最新，确认 base 是 head 的"直路祖先"（中间岔过别的分支就拒绝定责，绝不乱指认），再决定查多宽（能窄则窄），最后读出区间内的提交和改动文件，凑齐"案发现场材料"。

5. `git_client.sync(repo)`（[git_client.py](ci_owner_agent/services/git_client.py)）：同步专用 repo cache（裸镜像 `remote update --prune` / 工作树 `fetch origin --prune --no-tags`）。正式模式同步失败直接关门。
6. `git_client.check_ancestor(base, head)`：`git merge-base --is-ancestor` 校验 **base 必须是 head 的祖先**，否则 fail closed 不输出高可信责任人。
7. `_resolve_investigation_scope`：建立**窄范围 focusRange = 上一构建 headCommit → 当前 head**（来源：显式 `--previous-commit` 或 `history_store.find_previous_build` 查上一构建），封装为 `InvestigationScope`（[investigation_scope.py](ci_owner_agent/services/investigation_scope.py)）。没有上一构建信息就用 fullRange。
8. `_read_investigation_range`：先读 focus 区间的 commits + changed files；focus 失败自动降级 fullRange。
   - `git_client.get_commits_between`：`git log` 区间内提交（author/subject/hash）；
   - `git_client.get_diff_files`：`git diff --name-status/--numstat` + 每文件作者。

> **产出（传给阶段 C）**：`_build_analysis_contexts` 把 Git 调查结果组装成 `AgentRuntimeContext`（[schemas.py](ci_owner_agent/schemas.py) 里的"材料袋"）：`commits`（区间提交+作者）、`changed_files`（文件名+增删+每文件作者）、`investigation_scope`（focus/full 范围），外加 base/head commit、log_provider、git_client、settings。阶段 C、D、E 都靠这一袋材料干活。

### 阶段 C：预计算失败证据（[orchestrator.py](ci_owner_agent/orchestrator.py) `_with_precomputed_failure_context`，约 538 行）

> **这个阶段在干嘛（大白话）**：在让 AI 干活之前，先用确定性代码把"这次失败长什么样"提炼出来：结构化失败摘要与签名、可选 AI 失败事实、历史相似失败（含可继承的责任人与人工反馈）。这些是喂给 AI 首轮的现成材料，省得 AI 从零翻日志。

> **补充：日志里挤着好几路人（谁说的话，决定能不能提指纹）**
> `consoleText` 是所有环节输出按时间混成一锅，得先分清"这句话是谁说的"：
>
> - **结构化 → 可进历史**：Japa（Fidget，输出 `✖ 标题` / 错误类型 / `at 文件:行`）——稳定，可提取失败指纹；
> - **包装类 → 不进历史，只作当前日志证据**：Jenkins、shell、Docker/BuildKit（`ERROR: failed to solve:`）、typecheck/lint 输出都不能单独形成测试责任摘要。
>
> 判断规则一句话：**只有结构化 Japa 失败块才提指纹进历史；包装类（Docker/Jenkins/shell/typecheck/lint）只作当前日志证据**。解析实现见 [log_parsing.py](ci_owner_agent/services/log_parsing.py)（log_provider.py 是薄壳，只管"日志从哪来"）。

9. **确定性失败摘要**：`log_provider.find_test_failure_summaries(tail_lines, max_chunks=5)` 调用 [log_parsing.py](ci_owner_agent/services/log_parsing.py) 的纯解析规则——直接在日志中收集最多 `max_chunks` 个 Japa `✖ 标题` 失败块，逐块提取签名 `signatureKey` + `signatureHash`（schemaVersion=3）。**Docker/Jenkins/shell/typecheck/lint 包装失败不生成历史签名**（它们不能进历史继承）。**Fidget 二期另识别 c8 `check-coverage` 门槛失败**（`Coverage for <metric> (<pct>%) does not meet threshold ... for <file>`），无 Japa 失败块时产出 `coverage_failure_block` chunk，并按 Nx task 块归属包名（`> nx run @fx/<pkg>:` + `> @fx/<pkg>@` 一致才写 `packageName`）。
10. **AI failure facts**（可选，`CI_AGENT_AI_FAILURE_FACTS_ENABLED`）：若确定性摘要为空，`extract_failure_facts_with_ai`（[failure_fact_ai.py](ci_owner_agent/services/failure_fact_ai.py)）从非结构化日志提取内层失败事实（`FailureFact`：failureKind/errorCode/filePath/symbol/message/rootCauseSummary/confidence），并按置信度 ≥0.70 过滤、本地重算 signatureKey。
11. **历史预检**：`history_search_similar_failures(enriched, maxCandidates, store)`（[history_search.py](ci_owner_agent/services/history_search.py)）——按稳定失败签名查 MongoDB 历史相似失败，附带 `inheritedOwner`（可继承的责任人）和 `feedbackOverride`（人工反馈覆盖）。
12. **AI 历史语义比对**（可选）：`history_search_similar_failure_facts`（[ai_history_search.py](ci_owner_agent/services/ai_history_search.py)）——对 AI facts 做语义比对，阈值 0.90，命中才允许基于 AI facts 继承。

> **补充：0.70 / 0.90 两个置信度怎么来的（不是算出来的）**
> 两个数都是 **AI 自评的把握分 + 配置写死的门槛**：`confidence` 由 AI 自己打（0~1），**不是公式算的**（提取 prompt 自评"失败长什么样"，比对 prompt 自评"是否同一根因"并附人话 reason）。
>
> - **0.70（AI facts 过滤线，纵向自评）**：`ai_failure_fact_min_confidence` 默认 0.70（`CI_AGENT_AI_FAILURE_FACT_MIN_CONFIDENCE` 可覆盖）——低于 0.70 的 fact 直接丢弃、不进历史；prompt 还强制"只看到外层 wrapper 时必须自评 ≤0.5"。见 [failure_fact_ai.py](ci_owner_agent/services/failure_fact_ai.py)；
> - **0.90（AI 语义比对阈值，横向比对）**：`ai_history_compare_threshold` 默认 0.90（`CI_AGENT_AI_HISTORY_COMPARE_THRESHOLD` 可覆盖）——即使 AI 判 `sameFailure=true`，confidence<0.90 也**强制改判为不是同一失败**（附"below threshold"）。定得比 0.70 严：继承责任人 = 把锅安到上次那个人头上，宁可漏继承（退回 E 重查）也不乱继承。见 [failure_fact_compare_ai.py](ci_owner_agent/services/failure_fact_compare_ai.py)。
>
> **注意**：这两条 AI 链路（`ai_failure_facts_enabled` / `ai_history_compare_enabled`）默认都是 **False**，属可选能力；确定性指纹（C1 纯正则）不受开关影响，仍是主线。

> **产出（传给阶段 D/E）**：`_with_precomputed_failure_context` 在 `AgentRuntimeContext` 原地加四样料：`failure_summaries`（C1 结构化摘要+指纹）、`failure_facts`（C2 可选）、`history_precheck`（历史预检：候选+inheritedOwner+feedbackOverride）、`ai_history_precheck`（可选语义比对）。**满料 context 是阶段 D 和阶段 E 的共同输入**。

### 阶段 D：历史 no-owner 短路（[orchestrator.py](ci_owner_agent/orchestrator.py) 约 263 行）

> **这个阶段在干嘛（大白话）**：省钱的关键一步——如果所有失败项都能被可信的"历史无责任人"结论覆盖、且这次没有新的强证据（没改新文件、没新失败特征），就跳过 AI，直接沿用历史结论出 notice。

13. **三道闸门，全过才短路**（[history_no_owner.py](ci_owner_agent/services/history_no_owner.py)），每道都是 fail closed：
    - **① 挑候选** `select_build_level_no_owner_decision(history_precheck, ai_history_precheck)`：逐失败项查历史预检——`inheritedOwner.found=true`（历史有主儿，该由 E 继承责任人）或 `noOwnerDecision.found!=true`（没有 no-owner 旧结论）任一出现就**整体放弃**；必须**所有失败项**都被 no-owner 结论覆盖才组候选（宁缺毋滥，不是大多数）；
    - **② 验真** `validate_trusted_history_no_owner_decisions(decision, current_build_number)`：每条决策须同时满足——`sourceBuildNumber` 是 int 且 `0 < source < current`（源构建真实且更早）、规范化指纹非空、`matchType` ∈ {`signature_exact` / `signature_structural` / `ai_fact_semantic`}（普通文本相似度 `text_similarity` 不算）、`relationship` 和 `reason` 非空；任一不合格**整份作废**；
    - **③ 查新料** `has_new_strong_evidence(decision, failure_summaries, failure_facts, changed_files)`：先让当前失败与历史决策**一一配对**（按指纹集合求交，数量一致、不重不漏，对不齐即视为有新料）；再逐对查两条——失败路径（testFile/topStackFile/filePath）∩ 本次 diff 改动文件 ≠ ∅（这失败可能是本次构建新引入的），或当前错误标记（errorCode/errorType/failureKind + `TS####`/`AssertionError`/`Timeout`/`ERR_*` 等）**不是历史的子集**（失败"变花样"了）。任一命中 → 有新强证据 → 不短路；
    - 总开关 `settings.history_inherit_no_owner_enabled`（默认 True，[config.py](ci_owner_agent/config.py)，`CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED=false` 强制重查，notice.suggestions 有提示）也须为 True；全过 → `build_no_owner_notice_from_history_decision` 直接构造 notice。这是省 LLM 调用的关键优化。

> **产出（二选一）**：要么短路成功 → 直接产出一个"沿用历史结论"的 no-owner notice——**跳过 E 的 AI**，但仍复用 F 的补全与落库（`enrich_responsibility_item_signatures/paths` + `_save_history`，本次结论也进历史）；要么不短路 → **带着满料 `AgentRuntimeContext` 原样进阶段 E**。

### 阶段 E：Agent 调查（[orchestrator.py](ci_owner_agent/orchestrator.py) 约 320 行）

> **这个阶段在干嘛（大白话）**：核心定责环节——创建责任分析 agent，拿着前面预计算好的上下文（构建信息、diff、失败摘要、历史预检）做多证据调查，输出结构化的责任判断 notice。

14. `create_responsibility_agent(settings, runtime_context)`（[agents/factory.py](ci_owner_agent/agents/factory.py)）：`fake` provider → `FakeResponsibilityAgent`（只回固定 no-owner）；否则 → `LangChainResponsibilityAgent`，其 `.analyze()`（[agents/langchain_agent.py](ci_owner_agent/agents/langchain_agent.py)）调用 LangChain v1 `create_agent` 跑一轮带工具的调查循环（详见第 4 节）。

> **产出（传给阶段 F）**：`agent.analyze()` 返回的**原始 `CiResponsibilityNotice`**——责任项 + 责任人 + 证据 + 理由的"模型第一版答案"。还没过关：模型可能编造元数据、证据不足、指纹缺失，全靠 F 把关。

### 阶段 F：后处理、校验、落库

> **这个阶段在干嘛（大白话）**：给 AI 的输出"把关定稿"——模型交的是"第一版答案"，可能编造元数据、证据不足还硬撑高可信、指纹缺失。F 按固定顺序走六步：**真实元数据覆盖 → 过门禁 → 补指纹/路径 → 再过门禁 → 落库**。核心分工一句话：**模型管判断（这锅该谁背），系统管事实（这是哪个构建），事实必须以真实数据为准**。

15. **覆盖元数据** `_restore_authoritative_build_metadata`（[orchestrator.py](ci_owner_agent/orchestrator.py)）：强制用真实的 repo/job/buildNumber/buildUrl/result/branch/baseCommit/headCommit 覆盖模型输出（模型可能幻觉编造），并给 `current_build_owner` 项回填真实 sourceBuildNumber/sourceBuildUrl/sourceCommit；有字段被覆盖会记 warning 留审计痕迹。
16. **过门禁** `validate_notice`（[scorer.py](ci_owner_agent/services/scorer.py)）：`high_confidence` 硬性条件——有 name+commit、confidence≥0.8、**≥2 类独立证据**、必含 `log` 证据 + 含 `diff/keyword_match/ts_symbol` 之一、证据不得全来自 node_modules；不满足直接降级 no-owner。在补全前后**各跑一次**（先看模型原样、再看补全后）。
17. **补全** `enrich_responsibility_item_signatures` + `enrich_responsibility_item_paths`（[responsibility_signature_enricher.py](ci_owner_agent/services/responsibility_signature_enricher.py) / [responsibility_path_enricher.py](ci_owner_agent/services/responsibility_path_enricher.py)）：本地确定性补全——给每个责任项补规范化 `failureSignature`/`failureId`，从摘要/facts/evidence 回填 `testFilePath`/`failureFilePath`（不猜路径，只从可信来源提取）。
18. **落库** `_save_history` → `MongoHistoryStore.save_analysis`（[history_store.py](ci_owner_agent/services/history_store.py)）+ `save_failure_facts`：写 `ci_builds` / `ci_notices` / `ci_failure_chunks` / `ci_test_file_failures` / `ci_failure_facts`——本次结论从此成为以后历史预检/继承的来源。

> **补充：最终 notice 有哪些字段（固定 schema，F 不新增）**
> 定义全在 [schemas.py](ci_owner_agent/schemas.py)。顶层 14 个 + 每条责任项 16 个 + Owner 5 个 + Evidence 5 个；F 只做"覆盖编造、把门禁、补指纹路径、落库"，不新增变量。字段多是因为一份 notice 要服务多个角色——**展示通知（@ 人）、门禁校验、历史继承、人工反馈、统计路由、审计追溯**——每个字段都有人要，不是乱堆。

**顶层 `CiResponsibilityNotice`（14 个）**

| 字段 | 干嘛的 |
|---|---|
| repo / job / buildNumber / buildUrl | 这是哪个仓库、哪次构建、在哪看日志（真实，F1 覆盖） |
| result / branch | 构建结果、分支（真实，F1 覆盖） |
| headCommit / baseCommit | 责任窗口：head=本次 SHA、base=上次成功 SHA（真实，F1 覆盖） |
| owner | 整个构建唯一高可信责任人（或 no-owner），通知 @ 人（模型填，F2 门禁） |
| hasHighConfidenceOwner | 顶层是不是 high（F2 强制） |
| failureReason | 顶层判定理由（人话） |
| evidence | 证据链（EvidenceItem 列表），支撑门禁与审计 |
| suggestions | 操作提示（如"可设 `CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED=false` 强制重查"） |
| responsibilityItems | 逐失败项列表，每个失败单独定责 |

**每条 `responsibilityItems`（16 个）**

| 字段 | 干嘛的 |
|---|---|
| failureTitle / failureSummary | 这个失败是什么（人读） |
| failureSignature / failureId | **确定性身份**：历史搜索/继承/去重/反馈全靠它（F3 补全） |
| testFilePath / failureFilePath | 测试文件/失败文件路径：通知路由与统计（F4 补全） |
| owner | 这条失败的定责结论（type/name/email/commit/confidence） |
| responsibilityType | 责任类型：current_build_owner / inherited_failure_owner / no_high_confidence_owner / unknown |
| sourceBuildNumber / sourceBuildUrl / sourceCommit | **可审计来源**：继承自哪个构建，或本次构建（D 继承 / F1 回填） |
| matchType / relationship | 与历史的匹配类型和关系（signature_exact / very_likely_same_failure 等） |
| confidence / reason | 这条的把握分与理由 |
| evidenceIds | 指向哪几条 evidence，证据与结论对得上 |

> **子结构**：`Owner`（5 个）= type / name / email / commit / confidence（责任人是"谁"、什么类型、多大把握）；`EvidenceItem`（5 个）= id / type / summary / detail / source（一条证据是什么、属于哪类、原文在哪）。

> **产出（传给阶段 G）**：**最终 notice**——真实元数据覆盖（防编造）+ 高置信门禁（证据不足降级 no-owner）+ 指纹/路径补全，且已落库（本次结论成为以后历史预检的来源）。G 拿到的是"可展示、可通知"的定稿。

### 阶段 G：输出与通知

> **这个阶段在干嘛（大白话）**：把结论送出去——notice 写文件或屏幕；若开启通知，生成反馈码、渲染企业微信 Markdown，走 Webhook 直发或 Bot 队列投递，并在群里收集人工反馈形成闭环。

19. CLI 把 notice 原子写 `--output-file`（UTF-8）或 stdout。
20. 若 `--notify`：`wecom_notice_service.maybe_notify_notice` → `notify_notice`（[wecom_notice_service.py](ci_owner_agent/services/wecom_notice_service.py)）：
    - `upsert_notice_snapshot`（[history_store.py](ci_owner_agent/services/history_store.py)）写 `ci_notices`；
    - `FeedbackContextStore.get_or_create_for_notice`（[feedback_context_store.py](ci_owner_agent/services/feedback_context_store.py)）生成**反馈码 `CI-XXXXXX`**（写 `ci_feedback_contexts`，TTL 30 天）；
    - `format_wecom_markdown_notice`（[notification_formatter.py](ci_owner_agent/services/notification_formatter.py)）渲染企微 Markdown（含 `<@userid>` @ 人、反馈码、反馈链接）；
    - [wecom_notification_routing.py](ci_owner_agent/services/wecom_notification_routing.py) 计算当前企微维护人路由和 `notification_digest` 去重摘要；
    - **两条投递链路**：
      - **Webhook 直发**：`store.notification_sent` 去重 → `send_wecom_markdown`（[wecom_notifier.py](ci_owner_agent/services/wecom_notifier.py)）POST 群机器人 → `save_notification` 落 `ci_notifications`；
      - **Bot Outbox**：`WeComNotificationOutbox.enqueue_markdown` 入队 `ci_wecom_notification_outbox` → 常驻 `serve-wecom-bot` Worker 轮询领取（租约 30s）→ SDK 长连接主动推送 → 成功 `mark_sent` / 失败退避重试 5 次后 `dead`。
    - **设计红线：通知失败不推翻定责**——`maybe_notify_notice` 里投递异常只打 WARNING，notice 结论不受通知成败影响（投递坏了 ≠ 判错了）。

> **产出（对外，链路终点）**：notice 写 `--output-file` 或 stdout；若 `--notify`，再产出企微 Markdown 通知 + 反馈码 `CI-XXXXXX`（写 `ci_feedback_contexts`，TTL 30 天）——**反馈码是闭环的钥匙**：群里收到的人凭它提交"确认/纠正/标 flaky/no-owner"，写进 `ci_feedback`，下次历史预检变成 `feedbackOverride`，把"通知"和"历史修正"串成闭环（详见第 6 节）。

### analyze-local 的差异

[analyze_local](ci_owner_agent/orchestrator.py) 用 `LocalFileLogProvider` 读本地日志，**先做 checkout SHA 校验**（日志中的可信 checkout 与 `--head-commit` 不一致直接报错拒跑），结果状态可从 `--result` 或日志 `Finished:` 推导；base/head commit 由用户显式提供；`allow_sync_failure=True`（本地模式仓库同步失败降级继续）。其余失败分析流程与在线模式完全复用同一个 `analyze_failed_build`。

## 3. 分层架构与职责边界

```
┌─ 入口层：main.py（参数）→ cli/commands.py（分发、输出、错误码）
│
├─ 编排层：orchestrator.py（流程顺序、状态门控、何时调什么 service）
│
├─ Agent 层：agents/（factory 工厂 / langchain_agent 运行与降级 / initial_input 首轮 payload / notice_parser 严格输出解析 / prompts 提示词 / context 上下文）
│   └─ tools/（Agent 可见的 15 个工具：日志/Git/TS/历史）
│
├─ Service 层：确定性领域能力，全部可单测
│   ├─ 数据获取：git_client / jenkins_client / log_provider / command_runner
│   ├─ 日志解析：log_parsing（状态、可信 checkout、聚焦片段、Japa 摘要、coverage 门槛失败）
│   ├─ 失败身份：failure_identity / failure_similarity / log_parsing(摘要)
│   ├─ 覆盖率定责：coverage_responsibility（Fidget 二期；包名归属 B/C → 确定性 owner，reconciler 唯一写入口）
│   ├─ 历史：history_store(Mongo) / history_search / ai_history_search / history_no_owner / history_inheritance
│   ├─ AI：failure_fact_ai / failure_fact_compare_ai / structured_output / llm_client
│   ├─ 校验：scorer / responsibility_*_enricher
│   ├─ 通知反馈：wecom_notice_service / notification_formatter / wecom_notification_routing / wecom_notifier / outbox / bot_worker / feedback_*
│   └─ 统计周报：test_failure_stats / test_failure_priority / weekly_test_report_*
│
├─ 工具层（Node）：ts-analyzer/（TS compiler API 静态分析，find_definitions / find_callers）
├─ 脚本层：scripts/*（批处理/轮询/回填，子进程调 CLI）
└─ 服务层：server.py（FastAPI 反馈页）
```

关键架构约定（AGENTS.md 明确约束）：**确定性逻辑必须下沉到 services，Agent 只做多证据调查判断**；`tools/` 是薄适配层不是业务归属地（`git_tools.py` 等平行实现已被删除）；CLI 不做历史匹配和投递算法；对外 JSON 用 [schemas.py](ci_owner_agent/schemas.py) 的严格 Pydantic 模型（`extra="forbid"`）。

## 4. Agent 架构详解

### 4.1 运行模型

- **工厂**（[agents/factory.py](ci_owner_agent/agents/factory.py)）：`create_responsibility_agent(settings, runtime_context)` 按 `CI_AGENT_MODEL_PROVIDER` 分流：
  - `fake` → `FakeResponsibilityAgent`：只回固定 no-owner，用于测试工具链，**不用于正式定责**；
  - `openai / deepseek / doubao / openai-compatible` → `LangChainResponsibilityAgent`，缺配置抛 `AgentConfigurationError` → orchestrator 捕获后降级为 no-owner notice。
- **模型客户端**（[llm_client.py](ci_owner_agent/services/llm_client.py) `build_chat_model`）：统一用 `langchain_openai.ChatOpenAI`（OpenAI 兼容协议），`temperature=0` 保证确定性，带 timeout（默认 90s）/ max_retries。豆包 = 火山方舟的 OpenAI-compatible 端点。
- **LangChain v1 Agent**（[agents/langchain_agent.py](ci_owner_agent/agents/langchain_agent.py) `_create_v1_agent`）：`create_agent(model, tools, system_prompt)`，响应格式两种：
  - `response_format=tool`（默认）：`ToolStrategy(schema=CiResponsibilityNotice)`，强制模型输出合法 schema；
  - `json_text`：文本 JSON，靠本地解析兜底。
- **首轮输入**（[agents/initial_input.py](ci_owner_agent/agents/initial_input.py)）：纯 payload 构造；`LangChainResponsibilityAgent._initial_input` 只保留摘要缺失时的原有兜底提取和 JSON 序列化。payload 把所有编排层预计算好的东西直接喂给模型：buildInfo、investigationScope、changedFiles（≤30）、commits（≤20）、**failureSummaries**、**failureFacts**、**historyPrecheck**（含 inheritedOwner）、**aiHistoryPrecheck**，外加来自 [agents/prompts.py](ci_owner_agent/agents/prompts.py) 的中文 `instruction`（focusRange 优先、继承规则、证据要求）。
- **输出解析与修复**（[agents/notice_parser.py](ci_owner_agent/agents/notice_parser.py) / `_repair_output`）：多级容错——`model_validate_json` → `json.loads` → 剥代码围栏 → 正则提取 JSON object → 仍失败则 `_repair_output` 再调一次 LLM 让它修复成合法 JSON → 再失败返回 no-owner notice。任何异常最终都降级为 `_failure_notice`（绝不把错误当成功）。

### 4.2 工具集（[tools/langchain_tools.py](ci_owner_agent/tools/langchain_tools.py) `build_langchain_tools`）

15 个工具，每个都包了两层护栏：

| 工具                                                                                                              | 作用                                         | 底层实现                                                                                                                                                    |
| ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `log_read_tail` / `log_search` / `log_read_range` / `log_find_error_chunks` / `log_detect_final_status` | 读日志                                       | `LogProvider`（[log_provider.py](ci_owner_agent/services/log_provider.py)）；纯文本状态/摘要规则在 [log_parsing.py](ci_owner_agent/services/log_parsing.py) |
| `repo_get_commits_between` / `repo_get_diff_files` / `repo_get_file_diff` / `repo_get_file_content`       | Git 区间查询（scope=focus/full）             | `GitClient`（[git_client.py](ci_owner_agent/services/git_client.py)）                                                                                      |
| `repo_find_paths`                                                                                               | head commit 下找真实路径                     | `path_tools` → `git ls-tree`                                                                                                                           |
| `repo_keyword_search`                                                                                           | 关键词搜索（changed_files/paths/whole_repo） | `keyword_tools` → `git grep`                                                                                                                           |
| `ts_find_definitions` / `ts_find_callers`                                                                     | TS 符号定义/调用点                           | `typescript_tools` → Node `ts-analyzer`                                                                                                                |
| `check_node_dependencies_for_analysis`                                                                          | TS 依赖就绪检查                              | `typescript_tools`                                                                                                                                        |
| `history_search_similar_failures`                                                                               | Mongo 历史相似失败                           | `history_search` service                                                                                                                                  |

护栏：① `_guard_tool_call`——**重复调用拦截**（同参不二调）+ **调用预算**（默认 12 步，超了强制要求输出最终 JSON）；② `_limit`——输出超长时按层裁剪，保证始终返回合法 JSON 不撑爆上下文。

`ts-analyzer`（Node.js，TypeScript compiler API）与 Python 的协议：Python 执行 `node <script>.js '<JSON payload>'`，payload 经 `process.argv[2]` 传入，Node 把结果 JSON 整包写 stdout，Python 解析。定义/调用点分析都要求普通工作树（bare 镜像拒绝）、先 force checkout 到目标 commit、校验 node_modules 就绪（用 `.ci-owner-agent/deps.json` marker 做依赖指纹）。

### 4.3 提示词设计（[agents/prompts.py](ci_owner_agent/agents/prompts.py)）

`LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT` 是核心约束，几类关键规则：

- **调查顺序**：优先用首轮的 failureSummaries → failureFacts → historyPrecheck；不默认读 log tail，只在证据不足时才调日志工具；TS 工具只在 diff 证据不足且有明确符号时用。
- **收束规则**：证据链足够立即输出；预算耗尽必须输出。
- **历史持续失败规则**：命中 `signature_exact/structural + very_likely_same_failure` 且历史 build 更早 → 输出 `inherited_failure_owner`（继承首次失败责任人），不再重新分析当前 diff；单个 inherited 项时顶层 owner 保持 no-owner。
- **路径规则**：不猜路径，`repo_get_file_content` 前路径必须来自 changedFiles/diff/日志堆栈等可信来源。
- **高可信证据组合**：4 种"日志 + 本次 diff/TS 关系"组合。
- **JSON schema 提示**：严格字段、禁止额外字段、no-owner 的固定写法。

### 4.4 责任模型与降级链

`CiResponsibilityNotice`（[schemas.py](ci_owner_agent/schemas.py)）顶层 `owner` + 每条 `responsibilityItems`。责任类型四种：`current_build_owner`（新引入）/ `inherited_failure_owner`（历史继承）/ `no_high_confidence_owner` / `unknown`。本地 `enforce_owner_consistency` + `_normalize_responsibility_item` 做一致性校验：多责任人并存 → 顶层强制 no-owner；`current_build_owner` 无责任人 → 降级；`inherited` 缺来源 → 降级。整个系统**五层降级兜底**：模型异常 → `_failure_notice` → `validate_notice` → schema 校验 → enricher 补全，任何一层失败都不会产生虚假的高可信定责。

## 5. MongoDB 存了什么（[history_store.py](ci_owner_agent/services/history_store.py)）

数据库默认 `ci_owner_agent`，`CI_AGENT_HISTORY_ENABLED` 开启。所有文档身份按 `repo + job + branch`（+ buildNumber）隔离。

| 集合                             | 存什么                                                        | 关键字段 / 索引 / TTL                                                                                                                                                              |
| -------------------------------- | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ci_builds`                    | 每个构建的状态与 commit                                       | result / baseCommit / headCommit / lastSuccessfulBuildNumber / buildTimestamp / analyzedAt；`(repo,job,branch,buildNumber)` 唯一                                                 |
| `ci_notices`                   | notice 快照 + 构建级责任摘要                                  | `notice`（完整 JSON）+ ownerType/ownerName/hasHighConfidenceOwner/failureReason + 责任项统计（responsibleOwnerNames/currentBuildOwnerCount 等）                                  |
| `ci_failure_chunks`            | **结构化失败摘要**（schemaVersion=3，仅 Fidget/Japa 类） | chunkSource / chunkText / normalizedChunk / chunkHash / signature(含 signatureKey) / signatureHash；历史继承的唯一确定性来源                                                       |
| `ci_failure_facts`             | AI 提取的内层失败事实                                         | factId / signatureKey / historyEligible / isGenericWrapper / failureKind + 完整`fact` 和 `notice` 快照；`(repo,job,branch,historyEligible,buildNumber)` 索引用于历史语义比对 |
| `ci_test_file_failures`        | 每构建×每测试文件的失败事件                                  | buildTimestamp / failureItemCount / failureIds / failureSignatures / responsibilityTypes / ownerNames；`(repo,job,branch,buildNumber,testFilePath)` 唯一                         |
| `ci_feedback`                  | **人工反馈**（operation 模型）                          | `_id=operation:{opId}` / action(confirm_owner/correct_owner/mark_flaky/mark_no_owner) / originalOwner / correctedOwner / isCommitted / requestHash；operationId 唯一，幂等       |
| `ci_wecom_users`               | git 作者 → 企微 userid/邮箱/显示名映射                       | wecomUserId / normalizedEmail / authorName / searchText / commitCount；索引非唯一，映射时打分排序取最优                                                                            |
| `ci_feedback_contexts`         | 反馈码 + 责任项上下文                                         | `code=CI-XXXXXX` 唯一 + responsibilityItems 快照；`expiresAt` TTL 30 天                                                                                                        |
| `ci_wecom_pending_feedback`    | 群内待确认反馈                                                | confirmationCode / cardTaskId / eventKey / 状态机(pending→applying→applied/cancelled/stale) / applyLeaseUntil 60s 租约；TTL 7 天                                                 |
| `ci_wecom_bot_events`          | 机器人消息幂等与处理租约                                      | eventKey 唯一 / status(processing→completed/failed) / leaseUntil；TTL 7 天                                                                                                        |
| `ci_wecom_notification_outbox` | **Bot 通知队列**                                        | deliveryKey 唯一 / status(pending→sending→sent/dead) / attemptCount / nextAttemptAt（指数退避）/ leaseUntil 30s；无 TTL                                                          |
| `ci_notifications`             | Webhook 直发记录与去重                                        | `(repo,job,branch,buildNumber,noticeHash,channel)` / status(sent/failed，**成功后永不降级**) / messagePreview / lastAttemptStatus                                          |
| `ci_report_notifications`      | 周报通知去重                                                  | `(notificationType,repo,job,branch,periodStart,periodEnd,channel)` 唯一                                                                                                          |

写入时机：`save_analysis` 一次性写 builds + notices + chunks + test_file_failures（构建分析后）；`save_failure_facts` 写 facts；`notify_notice` 写 notices 快照 + feedback_contexts + notifications/outbox；`feedback apply`/机器人写 feedback；周报写 report_notifications。

## 6. 闭环：通知 → 反馈 → 历史修正

这个项目不是单向告警，而是一个**反馈闭环**：

```text
分析 → 通知（含反馈码 CI-XXXXXX + 反馈页链接）
        │
        ▼
  人工反馈（两条入口）
  ├─ Web 反馈页：serve-feedback（FastAPI，/feedback 表单 + /api/wecom-users/search 用户联想）
  └─ 群内机器人：serve-wecom-bot（长连接 Worker）
       ├─ 固定命令：@机器人 CI-XXXXXX 1 判断正确 / 责任人改为 @X-Y / 标记偶发 / 无法定责 / 查看 / 帮助
       └─ 自然语言：可选 LLM 解析（WeComFeedbackAiParser，function-calling 结构化输出）
        │
        ▼
  确认卡片 → 发起人确认/取消（防误操作）→ 写 ci_feedback（isCommitted）
        │
        ▼
  历史搜索时 feedbackOverride 生效 → 修正/阻断/继承责任人 → 影响后续构建的定责
```

机器人 Worker（[wecom_bot_worker.py](ci_owner_agent/services/wecom_bot_worker.py)）同时做两件事：消费 `ci_wecom_notification_outbox` 主动推送通知（transport=bot 时），以及处理群内反馈消息（事件幂等 + 确认卡片状态机）。[wecom_feedback_service.py](ci_owner_agent/services/wecom_feedback_service.py) 只处理反馈权限、状态迁移、幂等和落库；[wecom_feedback_cards.py](ci_owner_agent/services/wecom_feedback_cards.py) 只渲染当前已验证的企业微信模板卡与长度限制。Web 反馈页、固定命令、AI 解析三条入口都写同一张 `ci_feedback` 表，历史查询时通过 [history_inheritance.py](ci_owner_agent/services/history_inheritance.py) 把它作为 feedbackOverride 叠加上去。飞书反馈/卡片尚未实现。

## 7. 周边能力（非主链路）

- **测试文件失败统计 + 周报**（[test_failure_stats.py](ci_owner_agent/services/test_failure_stats.py) / [weekly_test_report_service.py](ci_owner_agent/services/weekly_test_report_service.py)）：按 `repo+job+branch+testFilePath` 聚合周期失败次数/频率/连续失败，按 [weekly-test-report.yml](config/weekly-test-report.yml) 阈值判"重点"（连续失败≥2、周失败≥3、失败率≥20% 等），生成 Markdown 周报并推企微；测试维护人路由（[test_maintainer_mapping.py](ci_owner_agent/services/test_maintainer_mapping.py)）只用于**通知路由，不参与定责**。
- **批处理/轮询/回填脚本**（[scripts/](scripts/)）：批量分析本地日志或 Jenkins 构建号、单日志重复运行评估稳定性、守护式轮询新构建、回填历史测试失败事件。全部以**子进程调主 CLI**，带超时/进程树终止（`_runtime.py`）、resume 成功标记（`<notice>.success.json` + SHA256 校验）、LangSmith trace 拉取、metrics JSONL 汇总。
- **Metrics**（[metrics.py](ci_owner_agent/services/metrics.py)）：记录阶段耗时、LLM 调用数、token 用量（provider 实报）、责任项数，写 JSONL。
- **LangSmith**：可选 tracing，`_configure_langsmith` 注入环境变量，批处理脚本可 `--fetch-trace` 拉取。

## 8. 给接手人的"读代码顺序"建议

如果后续要实现已经收敛的二期（fxp-fidget）范围，建议按这个顺序读：

1. **[AGENTS.md](AGENTS.md)** → 业务不变量和边界，尤其是"二期迁移范围"部分；
2. **[orchestrator.py](ci_owner_agent/orchestrator.py) 的 `analyze_failed_build`** → 整条链路的骨架，顺着它的函数调用逐层看；
3. **[schemas.py](ci_owner_agent/schemas.py)** → 所有对外数据结构的真相，`CiResponsibilityNotice` 是核心；
4. **[agents/langchain_agent.py](ci_owner_agent/agents/langchain_agent.py) + [agents/prompts.py](ci_owner_agent/agents/prompts.py) + [tools/langchain_tools.py](ci_owner_agent/tools/langchain_tools.py)** → Agent 怎么工作；
5. **[services/history_store.py](ci_owner_agent/services/history_store.py) + history_search + history_no_owner** → 历史继承和 no-owner 短路；
6. **[services/log_provider.py](ci_owner_agent/services/log_provider.py) + [services/log_parsing.py](ci_owner_agent/services/log_parsing.py) + jenkins_client.py** → 日志来源、失败摘要和可信 checkout 怎么来；
7. **[services/wecom_notice_service.py](ci_owner_agent/services/wecom_notice_service.py) + [notification_formatter.py](ci_owner_agent/services/notification_formatter.py) + [wecom_notification_routing.py](ci_owner_agent/services/wecom_notification_routing.py) + [wecom_feedback_service.py](ci_owner_agent/services/wecom_feedback_service.py)** → 参考并复用现有通知、人员触达、副作用隔离、去重、格式预算和反馈修正闭环；把飞书 API 与交互差异留在飞书边界。

二期最相关的点：**Fidget 是 npm workspace 多包工程，单元测试使用 Japa，且 c8 配置 `check-coverage` 100% 门槛**。当前 [log_parsing.py](ci_owner_agent/services/log_parsing.py) 直接识别 Japa `✖` 失败块和 c8 覆盖率门槛失败，并兼容 `packages/<package>/test/...` 路径；覆盖率失败由 [coverage_responsibility.py](ci_owner_agent/services/coverage_responsibility.py) 确定性定责（coverage-only 构建跳过 Agent，reconciler 为唯一写入口，Agent 输入过滤 + prompt 禁令 + 按稳定签名移除误生成项三道防线）。仍需按验证顺序使用真实 Fidget 日志和 Jenkins 单构建确认。除这些实际差异外，应尽量复用一期的编排、schema、历史、责任判断、通知和反馈语义。不要把范围扩张成通用 Project Profile、任意测试框架适配器或通用通知总线；飞书只隔离渠道 API、身份映射、格式和交互差异，并保持通知失败不破坏分析结果。
