> Archive: historical implementation notes; webhook references below are obsolete.

当前状态：
- analyze / analyze-local 已能生成 CiResponsibilityNotice。
- 企业微信 markdown 通知已实现并真实发送成功。
- feedback CLI 已实现：
  python -m ci_owner_agent feedback apply ...
  python -m ci_owner_agent feedback list ...
- ci_feedback 已能保存人工反馈。
- history overlay 已验证 correct_owner 生效：build 5094 人工修正 owner=lisi 后，build 5095 相同 failureSignature 成功继承 lisi。
- 当前通知里的“提交反馈”链接仍是占位 URL。
- git author -> 企业微信 userid 映射表尚未维护完成，目前通知中只用文本 @owner.name。

目标分三步完成：
1. 先做反馈页面 MVP。
2. 再接 Jenkins 自动通知。
3. 最后做 git author -> 企业微信 userid 映射，实现真正 @人。

============================================================
阶段一：反馈页面 MVP
============================================================

目标：
实现一个最小可用的 Web 反馈服务，让企业微信群通知里的“提交反馈”链接可以打开页面，查看该 build 的责任项，并提交反馈写入 ci_feedback。

不要求复杂前端。
不要求登录系统。
先用 FastAPI + 简单 HTML 表单即可。

------------------------------------------------------------
一、依赖
------------------------------------------------------------

修改 pyproject.toml，新增 optional dependency：

[project.optional-dependencies]
server = [
  "fastapi>=0.115,<1",
  "uvicorn>=0.30,<1",
  "python-multipart>=0.0.9,<1",
]

如果项目已有 optional-dependencies，请合并，不要覆盖原有 dev / llm。

------------------------------------------------------------
二、配置
------------------------------------------------------------

修改 ci_owner_agent/config.py，新增：

CI_AGENT_FEEDBACK_SERVER_HOST=127.0.0.1
CI_AGENT_FEEDBACK_SERVER_PORT=8765
CI_AGENT_FEEDBACK_SHARED_TOKEN=

Settings 增加：
- feedback_server_host: str
- feedback_server_port: int
- feedback_shared_token: str | None

public_settings 中隐藏 feedback_shared_token。

说明：
- feedback_shared_token 为空时，本地开发允许直接提交。
- feedback_shared_token 非空时，GET /feedback 和 POST /feedback 都要求 query/form 中 token 匹配。
- 这是 MVP 简单保护，避免没有任何门槛就能修改 ci_feedback。

------------------------------------------------------------
三、URL 生成
------------------------------------------------------------

修改 ci_owner_agent/services/notification_formatter.py 的 build_feedback_url。

当前格式：
{feedback_base_url}?job=...&build=...

改为支持 token：
- 如果 settings.feedback_shared_token 存在，通知链接中追加 token。
- 由于 formatter 当前只接收 feedback_base_url 和 notice，不直接接 settings，可以选择：
  方案 A：format_wecom_markdown_notice 增加 feedback_token: str | None = None 参数。
  方案 B：在 main.py 调 formatter 时拼接 base url，并额外传 token。
  推荐方案 A。

修改函数签名：

format_wecom_markdown_notice(
    notice: CiResponsibilityNotice,
    feedback_base_url: str | None = None,
    feedback_token: str | None = None,
    max_reason_chars: int = 800,
    max_evidence_chars: int = 500,
) -> str

build_feedback_url(
    base_url: str | None,
    notice: CiResponsibilityNotice,
    token: str | None = None,
) -> str | None

URL 参数：
- job
- build
- token，可选

示例：
http://ci-agent.xxx/feedback?job=services%2Ffx-code-unittest&build=5095&token=xxx

注意：
- token 不要展示成明文说明，只在链接里带。
- 后续真实登录再替换，这次只做 MVP。

------------------------------------------------------------
四、新增 Web 服务
------------------------------------------------------------

新增文件：
ci_owner_agent/server.py

实现 FastAPI app。

建议结构：

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

def create_app(settings: Settings | None = None) -> FastAPI:
    ...

app = create_app()

Endpoints：

1. GET /health

返回：
{"ok": true}

2. GET /feedback

参数：
- job: str
- build: int
- token: str | None = None

行为：
- 校验 token。
- 从 MongoHistoryStore 读取 ci_notices 中 job/build 的 notice。
- 读取 ci_feedback 中 job/build 的 active feedback。
- 返回 HTML 页面。

页面内容：
- 标题：CI 反馈 | {job} #{build}
- 构建链接：notice.buildUrl
- 原始原因：notice.failureReason
- 责任项列表：
  每个 item 展示：
  - failureTitle
  - responsibilityType
  - 当前 owner.name / owner.email
  - sourceBuildNumber
  - reason
  - 已有 active feedback 状态，如果有：
    - action
    - correctedOwner
    - reviewer
    - note
- 每个责任项下面提供一个表单。

表单字段：
- hidden job
- hidden build
- hidden failureId
- hidden token
- action select：
  - confirm_owner：判断正确
  - correct_owner：责任人不对，修正责任人
  - mark_flaky：这是偶发/环境问题
  - mark_no_owner：无高可信责任人
- ownerName input
- ownerEmail input
- ownerType select：
  - high_confidence
  - medium_confidence
- reviewer input
- note textarea
- submit button

表单提交到 POST /feedback。

3. POST /feedback

Form 字段：
- job
- build
- failureId
- action
- ownerName optional
- ownerEmail optional
- ownerType optional, default high_confidence
- reviewer optional
- note optional
- token optional

行为：
- 校验 token。
- 调 FeedbackStore.apply_feedback(...)
- 成功后 redirect 回：
  /feedback?job=...&build=...&token=...
- 或返回一个简单成功 HTML：
  反馈已提交，返回反馈页面。

错误处理：
- history store 不可用：返回 500 HTML，提示 history store unavailable。
- notice 不存在：返回 404 HTML，提示 notice not found。
- token 不匹配：返回 403 HTML，提示 forbidden。
- ValueError：返回 400 HTML，显示错误原因，不 traceback。

HTML 要求：
- 可以直接拼字符串，不引入模板引擎。
- 所有用户可控文本用 html.escape，避免 XSS。
- 页面中文显示。
- 样式简单即可，内联 CSS。

------------------------------------------------------------
五、CLI 启动服务
------------------------------------------------------------

修改 ci_owner_agent/main.py，新增子命令：

python -m ci_owner_agent serve-feedback

参数：
--host optional
--port optional
--reload optional

行为：
- 使用 uvicorn 启动 ci_owner_agent.server:create_app。
- host 默认 settings.feedback_server_host
- port 默认 settings.feedback_server_port
- reload 默认 false

注意：
create_app 需要读取 load_settings。
uvicorn 启动方式可以是：

uvicorn.run(
    "ci_owner_agent.server:app",
    host=host,
    port=port,
    reload=args.reload,
)

或者直接：
uvicorn.run(create_app(settings), host=..., port=...)

推荐避免 reload 下对象不可序列化问题，使用 import string 方式。
如果用 import string，server.py 顶层 app = create_app()。

------------------------------------------------------------
六、Web 反馈测试
------------------------------------------------------------

新增 tests/test_feedback_server.py。

至少覆盖：

1. GET /health
- 返回 200
- {"ok": true}

2. GET /feedback renders notice items
- 用 fake store 写入 ci_notice。
- 请求 /feedback?job=...&build=...
- 返回 HTML 包含：
  - CI 反馈
  - failureTitle
  - owner.name
  - action select

3. POST /feedback correct_owner
- 提交 action=correct_owner ownerName=Li Si
- 断言 ci_feedback 中 active feedback owner=Li Si
- 返回 303/200

4. token required
- settings.feedback_shared_token="secret"
- 不带 token 访问 /feedback 返回 403
- token 错误返回 403
- token 正确返回 200

5. HTML escaping
- notice 中放入 <script>alert(1)</script>
- 返回 HTML 不应包含原始 <script>
- 应被转义

6. notice not found
- 返回 404

测试可以使用 fastapi.testclient.TestClient。
如不想引入 requests 之外额外依赖，FastAPI TestClient 会通过 starlette/httpx，若缺依赖则在 optional server 中补 httpx：
server = [
  "fastapi>=0.115,<1",
  "uvicorn>=0.30,<1",
  "python-multipart>=0.0.9,<1",
  "httpx>=0.27,<1",
]

------------------------------------------------------------
七、手动验收
------------------------------------------------------------

启动 Mongo。
设置：

set CI_AGENT_HISTORY_ENABLED=true
set CI_AGENT_HISTORY_MONGO_URI=mongodb://localhost:27017
set CI_AGENT_HISTORY_MONGO_DB=ci_owner_agent
set CI_AGENT_FEEDBACK_BASE_URL=http://127.0.0.1:8765/feedback
set CI_AGENT_FEEDBACK_SHARED_TOKEN=dev-token

启动服务：

python -m ci_owner_agent serve-feedback --host 127.0.0.1 --port 8765

浏览器打开：

http://127.0.0.1:8765/feedback?job=services%2Ffx-code-unittest&build=5095&token=dev-token

提交反馈后验证：

python -m ci_owner_agent feedback list ^
  --job services/fx-code-unittest ^
  --build 5095

============================================================
阶段二：接 Jenkins 自动通知
============================================================

目标：
在 Jenkins pipeline 中失败后自动运行 analyze --notify，发送企业微信通知，并带上可点击反馈链接。

这个阶段主要补文档和脚本，不强行修改公司 Jenkinsfile。

------------------------------------------------------------
一、增加 Jenkins 集成文档
------------------------------------------------------------

新增文档：
docs/jenkins_notification_integration.md

内容包括：

1. 前置条件：
- repo cache 准备完成
- CI_AGENT_REPO_CACHE_DIR 指向 agent 专用 cache
- TS_ANALYZER_DIR 配好
- Mongo 可用
- 企业微信机器人 webhook 可用
- feedback server 已启动并能从公司网络访问

2. 推荐环境变量：

CI_AGENT_HISTORY_ENABLED=true
CI_AGENT_HISTORY_MONGO_URI=mongodb://localhost:27017
CI_AGENT_HISTORY_MONGO_DB=ci_owner_agent

CI_AGENT_WECOM_NOTIFY_ENABLED=true
CI_AGENT_WECOM_NOTIFY_DRY_RUN=false
[obsolete webhook environment variable]=******
CI_AGENT_WECOM_NOTIFY_ON_SUCCESS=false
CI_AGENT_WECOM_NOTIFY_ON_NO_OWNER=true
CI_AGENT_NOTIFICATION_DEDUP_ENABLED=true

CI_AGENT_FEEDBACK_BASE_URL=http://ci-agent.xxx/feedback
CI_AGENT_FEEDBACK_SHARED_TOKEN=******

CI_AGENT_MODEL_PROVIDER=doubao
CI_AGENT_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
CI_AGENT_MODEL_NAME=doubao-seed-2-0-lite-260428
CI_AGENT_API_KEY=******

3. Jenkins 命令：

python -m ci_owner_agent analyze ^
  --job services/fx-code-unittest ^
  --build %BUILD_NUMBER% ^
  --repo fx-code ^
  --log-tail-lines 500 ^
  --notify

4. Linux shell 示例：

python -m ci_owner_agent analyze \
  --job services/fx-code-unittest \
  --build "$BUILD_NUMBER" \
  --repo fx-code \
  --log-tail-lines 500 \
  --notify

5. 注意事项：
- SUCCESS 默认不发。
- no-owner 失败默认发，可用 CI_AGENT_WECOM_NOTIFY_ON_NO_OWNER=false 关闭。
- 通知失败不影响 analyze 主流程。
- 去重基于 job + branch + buildNumber + noticeHash + channel。
- 本地历史 build 不适合正式 analyze，正式模式目前只适合 latestSuccess 之后的新失败链。
- buildUrl 必须来自 Jenkins，否则本地 local:// 链接群里点不开。

------------------------------------------------------------
二、新增脚本
------------------------------------------------------------

新增：
scripts/jenkins_analyze_notify.ps1

参数：
- Job
- Build
- Repo
- LogTailLines default 500

实现：
python -m ci_owner_agent analyze `
  --job $Job `
  --build $Build `
  --repo $Repo `
  --log-tail-lines $LogTailLines `
  --notify

如果命令失败，脚本 exit 对应 code。

新增：
scripts/jenkins_analyze_notify.sh

同样功能，Linux shell 版。

------------------------------------------------------------
三、Jenkins 集成测试
------------------------------------------------------------

不需要真的连 Jenkins。
只补轻量测试：

1. analyze --notify-dry-run stdout 仍是纯 JSON。
已有类似测试，确认继续保留。

2. CI_AGENT_WECOM_NOTIFY_ENABLED=true 但 SUCCESS 且 CI_AGENT_WECOM_NOTIFY_ON_SUCCESS=false，不发送。

3. notification dedup 仍生效。

============================================================
阶段三：git author -> 企业微信 userid 映射
============================================================

目标：
目前通知顶部责任人是文本：
@Henry.Zeng、@lisi

后续希望映射为真实企业微信 mention。
因为 git author 不一定能映射到企业微信 userid，所以需要维护映射表。

------------------------------------------------------------
一、映射表文件
------------------------------------------------------------

新增目录：
config/

新增模板：
config/git_author_wecom_mapping.example.csv

字段：

authorName,authorEmail,wecomUserId,displayName,isActive,note

示例：

Henry.Zeng,henry.zeng@fanruan.com,Henry.Zeng,Henry.Zeng-曾纪龙,true,
lisi,lisi@fanruan.com,lisi,lisi,true,
James.Li-李炳基,James.Li@fanruan.com,James.Li,James.Li-李炳基,true,

说明：
- authorName：notice owner.name 或 git author name。
- authorEmail：notice owner.email 或 git author email。
- wecomUserId：企业微信 userid。
- displayName：通知中展示名。
- isActive：false 时忽略。
- note：备注。

------------------------------------------------------------
二、配置
------------------------------------------------------------

config.py 新增：

CI_AGENT_WECOM_AUTHOR_MAPPING_FILE=
CI_AGENT_WECOM_ENABLE_REAL_MENTION=false

Settings：
- wecom_author_mapping_file: Path | None
- wecom_enable_real_mention: bool

public_settings：
- mapping file 可显示路径。
- 不涉及 secret。

------------------------------------------------------------
三、映射加载模块
------------------------------------------------------------

新增：
ci_owner_agent/services/wecom_mapping.py

实现：

@dataclass
class WecomMention:
    display_name: str
    wecom_userid: str | None
    mention_text: str

class GitAuthorWecomMapper:
    @classmethod
    def from_csv(path: str | Path | None) -> GitAuthorWecomMapper:
        ...

    def lookup(self, *, name: str | None, email: str | None) -> WecomMention:
        ...

规则：
- CSV 不存在或未配置：返回文本 @name。
- 优先按 normalized email 匹配。
- 其次按 exact authorName 匹配。
- 只使用 isActive=true 的记录。
- email 大小写不敏感。
- authorName 去首尾空格。
- 如果找到 wecomUserId 且 enable_real_mention=true：
  - mention_text = f"<@{wecomUserId}>"
  - display_name = displayName or authorName
- 如果未找到或 enable_real_mention=false：
  - mention_text = f"@{displayName or name}"
- 不要把 mapping 文件内容打印到日志。

------------------------------------------------------------
四、通知 formatter 支持映射
------------------------------------------------------------

当前 format_wecom_markdown_notice 会把责任人格式化成：
@owner.name

改为支持可选 mapper：

format_wecom_markdown_notice(
    notice,
    feedback_base_url=None,
    feedback_token=None,
    mention_mapper: GitAuthorWecomMapper | None = None,
    enable_real_mention: bool = False,
    ...
)

责任人聚合逻辑仍然从 responsibilityItems[*].owner 来。
但聚合 key 建议使用：
- normalized email 优先
- 无 email 时用 owner.name

避免同一人重复 mention。

输出：
- mapping 命中且 enable_real_mention=true：
  **责任人**：<@userid1>、<@userid2>
- mapping 未启用：
  **责任人**：@Henry.Zeng、@lisi

责任项里的“责任人：”字段建议仍显示 displayName，不用 <@userid>，避免正文太难读。
顶部责任人用于真正提醒。

------------------------------------------------------------
五、企业微信 payload mentioned_list
------------------------------------------------------------

企业微信机器人 markdown 可以用文本 <@userid> 提醒。
不要再用 mentioned_list，机器人 markdown 消息一般靠 <@userid>。
如果后续确认企业微信机器人需要 mentioned_list，再单独加。

本阶段只实现 markdown 中 <@userid>。

------------------------------------------------------------
六、main.py 接入映射
------------------------------------------------------------

在 _notify_notice() 中：
- 根据 settings.wecom_author_mapping_file 加载 mapper。
- 调 formatter 时传 mapper 和 settings.wecom_enable_real_mention。
- 如果 mapping file 不存在：
  - 不失败。
  - stderr warning 可选，但不要影响 analyze。
  - 回退文本 @owner.name。

注意：
- dry-run 也应该能看到 <@userid> 或文本 @ 的结果。
- 如果 enable_real_mention=false，即使 mapping 文件存在，也只用 displayName 文本 @。

------------------------------------------------------------
七、映射工具增强
------------------------------------------------------------

已有 scripts/export_git_author_wecom_mapping.py。
增强它：
1. 输出 config/git_author_wecom_mapping.generated.csv 时，字段改为：
   authorName,authorEmail,wecomUserId,displayName,isActive,note
2. 对 @fanruan.com 邮箱：
   - wecomUserId 默认填邮箱前缀
   - displayName 默认 authorName
   - isActive=true
3. 对非 @fanruan.com：
   - wecomUserId 留空
   - isActive=false
   - note 写：need manual mapping
4. 保留原来的全量作者导出功能。

------------------------------------------------------------
八、映射测试
------------------------------------------------------------

新增 tests/test_wecom_mapping.py：

1. test_lookup_by_email
- CSV 中 authorEmail=henry.zeng@fanruan.com
- 输入 name 随便，email=Henry.Zeng@fanruan.com
- 命中 userid

2. test_lookup_by_name_when_email_missing
- email None
- name 命中

3. test_inactive_mapping_ignored

4. test_missing_mapping_returns_text_mention

5. test_formatter_real_mention_enabled
- mapping 命中
- enable_real_mention=true
- markdown 顶部责任人包含 <@userid>

6. test_formatter_real_mention_disabled
- mapping 命中
- enable_real_mention=false
- markdown 顶部责任人仍是 @displayName

============================================================
整体验收
============================================================

运行：

python -m pytest ^
  tests/test_feedback_server.py ^
  tests/test_wecom_mapping.py ^
  tests/test_cli_entrypoint.py ^
  tests/test_notification_formatter.py ^
  tests/test_feedback_store.py ^
  tests/test_history_store.py ^
  tests/test_wecom_notifier.py ^
  tests/test_jenkins_tools.py ^
  tests/test_langchain_agent.py

手动验收阶段一：

set CI_AGENT_HISTORY_ENABLED=true
set CI_AGENT_HISTORY_MONGO_URI=mongodb://localhost:27017
set CI_AGENT_HISTORY_MONGO_DB=ci_owner_agent
set CI_AGENT_FEEDBACK_BASE_URL=http://127.0.0.1:8765/feedback
set CI_AGENT_FEEDBACK_SHARED_TOKEN=dev-token

python -m ci_owner_agent serve-feedback --host 127.0.0.1 --port 8765

打开：
http://127.0.0.1:8765/feedback?job=services%2Ffx-code-unittest&build=5095&token=dev-token

手动验收阶段二：

python -m ci_owner_agent analyze ^
  --job services/fx-code-unittest ^
  --build <5115之后的新失败build> ^
  --repo fx-code ^
  --log-tail-lines 500 ^
  --notify

手动验收阶段三：

set CI_AGENT_WECOM_AUTHOR_MAPPING_FILE=config/git_author_wecom_mapping.example.csv
set CI_AGENT_WECOM_ENABLE_REAL_MENTION=true

python -m ci_owner_agent notify-notice ^
  --notice-file .\runs\manual-5095.notice.json ^
  --dry-run ^
  --feedback-base-url http://127.0.0.1:8765/feedback

预期顶部责任人从：
@Henry.Zeng、@lisi

变为：
<@Henry.Zeng>、<@lisi>

不要做的事：
- 不要现在接企业微信通讯录 API。
- 不要强制要求 mapping 文件存在。
- 不要让反馈服务异常影响 analyze。
- 不要把 webhook、feedback token 等 secret 打印到日志。
- 不要改掉现有 CLI feedback apply/list。
- 不要在通知正文重新展示 failureId / failureSignature。
