# Fidget 集成测试日志协议 V1

> 固化依据：真实个人 Jenkins 构建（最终 runner `bb5df970`）。
> 本协议是长期资产，随 `samples/fidget_log/integration/` 样本一起受 Git 管理；
> `.trellis` 内同名研究文档必须与本文档同步，不作为唯一协议来源。
> 本协议只描述 runner 边界 marker 与 Japa 证据的形态与优先级，供失败分析作为 hard gate；不定义失败分类/定责算法。

## 1. 协议范围

本协议覆盖三类日志证据：

1. **runner marker**：`FIDGET_INTEGRATION_V1` 前缀的单行 key=value 事件，定位集成测试的边界（前置、suite、汇总、清理）。
2. **Japa 结构化输出**：`✖`（失败锚点）与 `❯`（详细错误段），定位最内层的测试/业务错误。
3. **Jenkins 框架输出**：`Checking out Revision <sha>`、`[Pipeline] { (Integration Tests)`、`Finished: <STATUS>`，提供可信 checkout、stage 边界与最终状态。

三者职责分离：marker 只定位边界，Japa 定位内层错误，Jenkins 提供身份与终态。**marker 不能替代 Japa 错误；wrapper 输出不能覆盖更内层的数据库/断言错误。**

## 2. Runner marker 规范

### 2.1 前缀与字段

前缀固定 `FIDGET_INTEGRATION_V1`。后续为空格分隔的 `key=value`，`value` 只允许：

- 受控枚举（见状态机）
- 整数（exit、total/passed/failed/not_started）
- 规范化 run id（`[a-z0-9.-]+`，由 Job 前缀 + 完整 Job 名 hash + 构建号组成）

**禁止**：空格、自由文本、secret、真实数据库地址。具体错误信息由相邻日志/Japa 提供，不塞进 marker。

### 2.2 实际字段

| phase | step/字段 | 取值 | 说明 |
|---|---|---|---|
| preflight | image_pull | start / end(exit) / failed(exit) | Mongo 镜像拉取 |
| preflight | network_create | start / end / failed | 隔离网络创建 |
| preflight | mongo_start | start / end / failed | Mongo 容器启动 |
| preflight | mongo_ready | start / end / failed(reason=timeout) | readiness 硬门禁 |
| config | mongo_target | 容器服务名 | 运行时 override 指向 |
| suite | suite | 固定 8 枚举 | 见 2.3 |
| suite | status | start / end | end 时带 exit |
| summary | status | success / failed | |
| summary | total/passed/failed/not_started | 整数，total=8 | |
| cleanup | status | success / failed | 独立于测试结果 |

### 2.3 固定 suite 枚举（8 个）

`select-integration`、`stream-select-integration`、`delete-integration`、`insert-integration`、`update-integration`、`upsert-integration`、`bulk-integration`、`shadow-integration`。

### 2.4 状态机与硬约束

```text
preflight(image_pull -> network_create -> mongo_start -> mongo_ready)
  -> config(mongo_target)
  -> suite(0..8 次，每个 suite 至多一次，start/end 成对，end 带整数 exit)
  -> summary(passed + failed + not_started = 8)
  -> cleanup(success | failed)
```

- `run_id` 全程一致。
- `mongo_ready failed` 后，suite 不应开始（`not_started` 应 = 8）。
- cleanup 结果独立；`cleanup=failed` 使整轮不完整（即使 summary=success）。
- marker 缺失/重复/乱序/冲突只能降级为 `incomplete`，**不可补造成功**。

### 2.5 image_pull 失败归类

`image_pull failed` 属于 **Pipeline/基础设施失败**（Docker 镜像拉取），**不是** Mongo 数据库环境失败——即便镜像名含 `mongo` 字样。Mongo 数据库不可达的证据只能来自 `mongo_ready failed` 或 suite 内数据库连接错误。

## 3. Japa 证据规范

### 3.1 失败锚点

- `✖ <test>`：单行，结构化失败锚点（如 `✖ F-1003: Group by main field ... (36.18ms)`）。
- 紧随的 `❯ <group> / <test>` 是详细错误段（含 AssertionError diff 与 stack）。

### 3.2 优先级

内层 Japa assertion/业务错误 > 数据库 driver/setup 错误 > runner/Docker/Jenkins wrapper。

- 有 `✖` 且能提取 suite + test + 业务/测试栈 → 代码/断言失败候选。
- Setup hook 里的数据库连接错误即便带 Japa 形态，也按「数据库错误证据」判断环境资格，不能仅因 `✖` 进入确定性测试历史。
- 无 `✖` 的 setup/连接/容器/Pipeline 失败可进入 AI failure facts，但不得伪装成确定性测试历史。

## 4. 解析前置处理

解析 marker/Japa 前必须：

1. 剥离 ANSI 转义序列（`\x1b[...m`）。
2. 剥离 Docker BuildKit 前缀（`#23 47.62` 这类 `#<n> <ts>` 前缀）。
3. 单行匹配 marker，避免与 Japa 文本串扰。

## 5. fail-closed 行为

| 情形 | 行为 |
|---|---|
| marker 缺失 | 该阶段视为 unknown，不推断成功 |
| suite start 无 end | incomplete，该 suite 不计入 summary |
| 重复 suite / 乱序 | incomplete，需人工复核 |
| summary 与 suite 计数不一致 | 信任失败侧，降级 incomplete |
| cleanup 缺失或 failed | 整轮 incomplete |
| 内层错误与 wrapper 冲突 | 以最内层错误为准 |

## 6. 可信 checkout 与终态

- checkout 以 `Checking out Revision <40-hex-sha> (refs/remotes/origin/main)` 为准，**不采信** `Obtained ci/... from git ...` 或历史 marker 中出现的其它 SHA。
- 终态以 `Finished: SUCCESS|FAILURE|ABORTED|UNSTABLE` 为准。
- 多 SHA 同时出现时，必须区分「源码 checkout」与「共享库 checkout」（`@libs/...` 或 global-libraries 的 master 检出）——后者不是源码身份。
- stage 边界以 `[Pipeline] { (Integration Tests)` 与 `+ bash ci/run-integration-tests.v2.sh` 为准。

## 7. 样本状态（与 manifest 一致）

- **已知缺陷基线样本**：`failure-assertion.log`（build #24，7/8，仅 select 的 F-1003/O-0503 两个已知 PG 断言失败）。不再是"成功样本"，也不要求 8/8 全绿（D-009）。
- **Pipeline/image-pull 失败样本**：`failure-pipeline-image-pull.log`（build #25，`image_pull failed`，not_started=8）。
- **Protonbase 外部库失败样本**：`failure-protonbase-unavailable.log`（build #26，PG ECONNREFUSED，7 failed / shadow passed）。
- **Mongo readiness 真实样本**：按 D-010 延后至子任务 4 在线验证补采；子任务 3 先以合成 fixture 覆盖 Mongo readiness/connection 分类。

后续解析不得假定未验证形态（如 Mongo readiness 失败的真实报错文本）。
