# Ubuntu 24.04 定时分析与每周失败周报

本文使用两个一次性脚本，并交给 systemd timer 负责定时：

- `scripts/poll_jenkins_builds.py`：扫描 Jenkins 最近已完成构建，跳过 MongoDB 中已经完整分析的构建，对遗漏构建调用现有 `python -m ci_owner_agent analyze` 流程。
- `scripts/send_weekly_failure_report.py`：调用现有 `weekly-test-report --period previous-week` 流程，发送上一自然周的高频失败报告。

## 1. 前置配置

`.env` 至少需要配置：

```dotenv
JENKINS_URL=https://jenkins.example.com
JENKINS_USER=your-user
JENKINS_TOKEN=your-token

CI_AGENT_HISTORY_ENABLED=true
CI_AGENT_HISTORY_MONGO_URI=mongodb://127.0.0.1:27017
CI_AGENT_HISTORY_MONGO_DB=ci_owner_agent

CI_AGENT_WECOM_NOTIFY_ENABLED=true
CI_AGENT_WECOM_NOTIFY_TRANSPORT=bot
CI_AGENT_WECOM_BOT_ENABLED=true
CI_AGENT_WECOM_BOT_NOTIFY_CHAT_ID=your-chat-id
CI_AGENT_WECOM_NOTIFY_DRY_RUN=false
```

Bot transport 下，`serve-wecom-bot` 必须作为常驻服务运行，两个定时脚本只负责分析和写入通知 Outbox。

## 2. 手工验证

先只查看将要分析的构建：

```bash
cd /opt/ci-owner-agent
.venv/bin/python scripts/poll_jenkins_builds.py \
  --repo fx-code \
  --job services/fx-code-unittest \
  --lookback-builds 50 \
  --max-builds-per-run 3 \
  --dry-run
```

执行真实分析并按当前通知 transport 推送：

```bash
.venv/bin/python scripts/poll_jenkins_builds.py \
  --repo fx-code \
  --job services/fx-code-unittest \
  --lookback-builds 50 \
  --max-builds-per-run 3 \
  --notify
```

预览上一周报告：

```bash
.venv/bin/python scripts/send_weekly_failure_report.py \
  --repo fx-code \
  --job services/fx-code-unittest \
  --branch dev \
  --dry-run
```

正式发送：

```bash
.venv/bin/python scripts/send_weekly_failure_report.py \
  --repo fx-code \
  --job services/fx-code-unittest \
  --branch dev
```

## 3. 安装 systemd timer

仓库中的 unit 默认假设：

- 安装目录：`/opt/ci-owner-agent`
- 运行用户：`ci-owner-agent`
- 虚拟环境：`/opt/ci-owner-agent/.venv`
- Jenkins job：`services/fx-code-unittest`
- repo：`fx-code`
- branch：`dev`

先按实际部署路径、用户和 Jenkins job 修改四个 unit，然后执行：

```bash
sudo cp deploy/systemd/ci-owner-agent-poll.service /etc/systemd/system/
sudo cp deploy/systemd/ci-owner-agent-poll.timer /etc/systemd/system/
sudo cp deploy/systemd/ci-owner-agent-weekly-report.service /etc/systemd/system/
sudo cp deploy/systemd/ci-owner-agent-weekly-report.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now ci-owner-agent-poll.timer
sudo systemctl enable --now ci-owner-agent-weekly-report.timer
```

查看调度时间：

```bash
systemctl list-timers --all | grep ci-owner-agent
```

立即手工触发一次：

```bash
sudo systemctl start ci-owner-agent-poll.service
sudo systemctl start ci-owner-agent-weekly-report.service
```

查看日志：

```bash
journalctl -u ci-owner-agent-poll.service -n 200 --no-pager
journalctl -u ci-owner-agent-weekly-report.service -n 200 --no-pager
```

## 4. 去重与重试语义

轮询脚本只有在同一 `repo + job + buildNumber` 同时存在：

- `ci_builds.analyzedAt`
- `ci_notices.analyzedAt`

时，才认定构建已完整分析并跳过。子进程超时、返回非零或未写入完整 MongoDB 标记时，本次返回失败，下一次 timer 会重新尝试。

脚本使用本机文件锁防止同一 job 的轮询任务并发执行。默认每次扫描最近 50 个已完成构建，最多分析 3 个遗漏构建；可通过 `--lookback-builds` 和 `--max-builds-per-run` 调整。

周报沿用项目已有的 `ci_report_notifications` 去重逻辑。默认不使用 `--force`，同一统计周期不会重复发送；systemd timer 的 `Persistent=true` 会在服务器错过周一 10 点后，于下次启动时补执行。

## 5. 待处理：fx-code SSH 访问去个人化

当前生产 repo cache 的 Git 同步依赖个人账号 `matureleek` 的 SSH 身份。若该账号的代码仓库权限被回收，`git fetch` 将失败，正式 Jenkins 分析会停止，不能将该情况误报为“无责任人”。

后续需将该依赖迁移为非个人服务身份：在 `FX/fx-code` 配置仓库级只读 SSH Access Key；由服务器专用 `ci-owner-agent` 系统账号保存对应私钥并运行相关 systemd 服务；使用该账号完成 `git fetch origin --prune --no-tags` 验证并观察多个定时轮询周期后，再回收个人账号的旧密钥或仓库权限。

该迁移不影响本地源码改动、pytest 或 compileall 开发验证；在迁移完成前，生产环境仍需保留现有可用凭据。
