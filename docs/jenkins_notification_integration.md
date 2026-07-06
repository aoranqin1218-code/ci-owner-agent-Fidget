# Jenkins Notification Integration

This guide describes how to run `ci-owner-agent` from Jenkins so failed builds are analyzed and sent to the WeCom test group with a clickable feedback link.

## Prerequisites

- The repo cache is prepared and kept as an agent-only checkout.
- `CI_AGENT_REPO_CACHE_DIR` points to that repo cache.
- `TS_ANALYZER_DIR` points to the TypeScript analyzer directory.
- MongoDB is available for history, notifications, and feedback.
- A WeCom group robot webhook is available.
- The feedback server is running and reachable from the company network.

## Recommended Environment

```env
CI_AGENT_HISTORY_ENABLED=true
CI_AGENT_HISTORY_MONGO_URI=mongodb://localhost:27017
CI_AGENT_HISTORY_MONGO_DB=ci_owner_agent

CI_AGENT_WECOM_NOTIFY_ENABLED=true
CI_AGENT_WECOM_NOTIFY_DRY_RUN=false
CI_AGENT_WECOM_WEBHOOK_URL=******
CI_AGENT_WECOM_NOTIFY_ON_SUCCESS=false
CI_AGENT_WECOM_NOTIFY_ON_NO_OWNER=true
CI_AGENT_NOTIFICATION_DEDUP_ENABLED=true

CI_AGENT_FEEDBACK_BASE_URL=http://ci-agent.xxx/feedback
CI_AGENT_FEEDBACK_SHARED_TOKEN=******

CI_AGENT_MODEL_PROVIDER=doubao
CI_AGENT_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
CI_AGENT_MODEL_NAME=doubao-seed-2-0-lite-260428
CI_AGENT_API_KEY=******
```

Do not print webhook URLs, API keys, or feedback tokens in Jenkins logs.

## Windows Jenkins Step

```powershell
python -m ci_owner_agent analyze ^
  --job services/fx-code-unittest ^
  --build %BUILD_NUMBER% ^
  --repo fx-code ^
  --log-tail-lines 500 ^
  --notify
```

Or use the helper script:

```powershell
.\scripts\jenkins_analyze_notify.ps1 `
  -Job services/fx-code-unittest `
  -Build $env:BUILD_NUMBER `
  -Repo fx-code
```

## Linux Jenkins Step

```bash
python -m ci_owner_agent analyze \
  --job services/fx-code-unittest \
  --build "$BUILD_NUMBER" \
  --repo fx-code \
  --log-tail-lines 500 \
  --notify
```

Or use the helper script:

```bash
./scripts/jenkins_analyze_notify.sh \
  --job services/fx-code-unittest \
  --build "$BUILD_NUMBER" \
  --repo fx-code
```

## Notes

- `SUCCESS` builds do not notify by default. Set `CI_AGENT_WECOM_NOTIFY_ON_SUCCESS=true` only when you explicitly want success notifications.
- Builds with no current high-confidence owner notify by default during rollout. Set `CI_AGENT_WECOM_NOTIFY_ON_NO_OWNER=false` to suppress them.
- Notification failures do not fail the `analyze` command; the notice JSON remains the primary command output.
- Deduplication is based on `job + branch + buildNumber + noticeHash + channel`.
- Local historical builds are not a substitute for formal Jenkins analysis. Formal Jenkins mode is intended for new failure chains after `lastSuccessfulBuild`.
- `buildUrl` should come from Jenkins. `local://` links from local dry-runs are not useful in group notifications.
