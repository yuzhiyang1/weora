# W01：Pi 文本执行与 Worker 运行手册

- 日期：2026-10-17。
- 范围：固定版本的无工具文本执行、租约、取消与持久结果；不是 W01 全部完成。
- 当前缺口：真实 PostgreSQL/Redis/Celery 和 DeepSeek 联调、SSE、Console 运行页。

## 1. 已落地的边界

Weora 负责 Task/Run、权限、版本、待调度记录和运行状态。独立 Celery Worker 通过 `runs/application/execution.py` 领取运行，再通过中立 `RuntimeInput` 调用适配器。Python 不导入 Pi 类型，也不在业务表中保存 Pi 会话文件。

`runtimes/pi/` 是执行面 Node 子进程，不是前端客户端。它只依赖两个精确版本的包，并提交 pnpm 锁文件：

- `@earendil-works/pi-agent-core@1.3.0`：Agent 循环。
- `@earendil-works/pi-ai@1.3.0`：模型提供方适配。

只注册 DeepSeek 提供方，不引入 Coding CLI，也不发现个人扩展、会话、Shell 或文件工具。当前工具列表为空，出现工具请求立即拒绝；不能把它当作已经交付 W02 的工具循环。

## 2. 固定版本与上游依据

固定上游 release 为 [v1.3.0](https://github.com/earendil-works/pi/releases/tag/v1.3.0)，而不是 main 或浮动 latest。使用的公开接口参考：

- [Agent Core 契约](https://github.com/earendil-works/pi/blob/v1.3.0/packages/agent/README.md)：Agent、订阅事件、abort、finishTurn。
- [模型集合与显式提供方](https://github.com/earendil-works/pi/blob/v1.3.0/packages/ai/README.md)：createModels、streamSimple、显式 apiKey、脚本化测试提供方。
- [DeepSeek 提供方工厂](https://github.com/earendil-works/pi/blob/v1.3.0/packages/ai/src/providers/deepseek.ts)：固定官方 API 地址，不开放用户 URL 代理。
- [包运行要求](https://github.com/earendil-works/pi/blob/v1.3.0/packages/agent/package.json)：Node.js >=22.19.0。

本机实际检查的固定包模型目录包含 `deepseek-flash` 与 `deepseek-v4-pro`。这只是包支持列表，不代表账号已具备额度或本次已请求模型。模型名必须由管理员显式配置，不自动选择、不回退到其他账号或模型。厂商服务背后的模型可能更新；Weora 固定的是请求的模型 ID 和 Pi 包版本，不保证供应商模型权重永远不变。

新 Run 保存 `execution_profile`，固定 Runtime 标识、提供方、模型 ID 和超时。新 Run 的输出 Token 上限由 Pi 根据模型和剩余上下文计算，历史 Run 续接时沿用快照中的显式上限。密钥、Node 路径和个人登录态不进入该快照。Worker 配置与快照不同则失败，不悄悄换模型。启用 Runtime 前创建的无配置任务也不会自动套用新模型；需显式创建新任务。

## 3. 调度与恢复语义

1. HTTP 原子保存 Task、Run、步骤、事件和待调度记录，不在请求中调用模型。
2. Beat 每 5 秒扫描数据库待调度记录，发送至独立 `weora.runs` 队列，确认发送后再标记 DISPATCHED。
3. 队列成功但本地未标记时可能重复发送；Worker 用 `QUEUED -> RUNNING` 的 CAS 领取，只有一个实例执行。
4. 已投递但 30 秒仍未领取的 QUEUED 任务可以补投；Redis 丢失通知不等于丢失持久化目标。
5. Worker 写入 owner、递增 fencing token 和租约，心跳重新校验租约与取消请求。PostgreSQL 使用数据库时钟；SQLite 测试使用可注入时钟。
6. 所有终态提交匹配工作空间、owner、fencing token、RUNNING 和未过期租约；输出、步骤、事件序号和状态同事务提交。
7. 过期 RUNNING 转为 `RECOVERY_REQUIRED`，不自动再次请求模型。当前没有人工恢复 API；不通过直接改表或清空 Redis“恢复”任务。

取消与迟到结果竞争时，取消请求优先，结果不保存。排队取消可以直接记为 CANCELLED；已经启动的运行先保留 RUNNING 并显示 `cancellation_requested_at`。本地进程终止后再写 CANCELLED，同时保留 `REMOTE_COMPLETION_UNCONFIRMED`：本地进程退出不保证供应商已停止计算或计费。已投递的队列消息不能召回，后续领取会因 Run 状态被拒绝。

## 4. 子进程与凭据边界

- 只运行管理员配置的入口路径，不接受 HTTP/模型传入的命令、路径、提供方或环境变量。
- 一次进程处理一个请求，协议为 `weora.runtime.v1`；模型凭据通过专用 stdin 传入，不出现在命令行、事件和普通 API 响应。
- 子进程使用独立临时 cwd，不继承个人模型凭据、HOME、NODE_OPTIONS 或代理；只继承最小操作系统环境。
- 无效协议、超大输入/输出、进程异常、空回答、截断、工具请求和模型失败均不能变成成功。
- 原始 SDK 错误和 stderr 不进入 Run；输出若包含本次 API Key 则拒绝保存。
- 父适配器和 Node 入口都有请求期限；取消等待本地进程树退出。Node 自身也会在请求到期时 abort，避免只依赖父 Worker 存活。
- SDK 自动请求重试关闭。需要重试的请求必须由平台明确创建新的尝试，不能在未知执行结果下盲目重放。

这不是 W08 的操作系统级沙箱。当前安全边界是没有任何模型可调用的宿主机工具；不能据此开放任意 Shell、文件写入或第三方插件。

## 5. 本地配置与启动

以下是运行指引，本次未对开发者真实数据库执行迁移，也未启动连接真实 Redis 的后台服务。

### 5.1 构建执行子进程

在仓库根目录：

```powershell
pnpm install --frozen-lockfile --ignore-scripts
pnpm --filter @weora/pi-runtime build
```

Node.js 需要 >=22.19.0。若本机 pnpm 提示已有依赖目录使用不同 store，应先核对 `node_modules/.modules.yaml`，使用相同 store 参数；不要直接强制清空已有依赖目录。本机验证使用 `--store-dir D:\.pnpm-store`。

### 5.2 配置后端

在已有的 `backend/.env` 中补充下列键，不用模板覆盖已有凭据文件。仅在准备好后启用：

```dotenv
WEORA_RUN_RUNTIME=pi
WEORA_PI_NODE_EXECUTABLE=node
WEORA_PI_RUNTIME_ENTRY=D:/projects/weora/runtimes/pi/dist/index.js
WEORA_PI_MODEL=请填写明确的模型ID
WEORA_DEEPSEEK_API_KEY=仅在本地填写真实值
WEORA_RUN_MODEL_TIMEOUT_SECONDS=120
WEORA_RUN_LEASE_SECONDS=30
WEORA_CELERY_BROKER_URL=redis://127.0.0.1:6379/0
```

`WEORA_RUN_MODEL_MAX_TOKENS` 只用于兼容历史快照和旧设置 API。

不要把密钥粘贴到聊天、命令行参数、Run 输入或前端。未准备好时保持 `WEORA_RUN_RUNTIME=disabled`。这不是动态热更新配置；修改后需重启 API、Worker 和 Beat，新任务才使用新快照。

确认隔离开发数据库、备份且停用旧进程后，在 `backend` 执行 `uv run alembic upgrade head`。005 增加配置、租约、取消与输出列；回退会丢失新增列中的内容，不能当无损回滚。

### 5.3 启动三个独立进程

先准备可用 PostgreSQL 和 Redis，再分别在 `backend` 目录的三个终端启动：

```powershell
# API
uv run uvicorn weora.main:app --host 127.0.0.1 --port 8000

# Worker：Windows 本地使用 solo；这不是 Linux 多进程部署参数。
uv run celery -A weora.bootstrap.worker:celery_app worker --pool=solo --queues=weora.runs --loglevel=INFO

# Beat：单独进程，调度状态放本机临时目录，不写入仓库。
uv run celery -A weora.bootstrap.worker:celery_app beat --loglevel=INFO --schedule "$env:TEMP/weora-beat-schedule"
```

API、Worker 和 Beat 必须使用一致的数据库、队列和执行配置。未启动 Beat 时，没有持续补投或过期扫描；不能只启动 API 就期待任务自动运行。

### 5.4 通过 API 验证

Console 已有 `/runs` 与 `/runs/:id` 页面，可直接发起与查看任务；也可通过 OpenAPI/已有会话调用：

1. 登录并发布一个明确版本的 Agent；纯文本 Agent 可不绑定 Skill，也不要求 OSS。
2. 带现有会话 Cookie、`X-CSRF-Token` 和新 `Idempotency-Key`，向 `/api/v1/workspaces/{workspace_id}/runs` POST `agent_id`、`agent_version`、`instruction`。
3. 回读 Run 详情和事件，区分 QUEUED、RUNNING、FAILED、SUCCEEDED、取消请求及恢复状态。成功时 `output_text` 是受权限保护的持久化回答。
4. 验证取消、刷新回读、模型失败和真实 DeepSeek 请求；记录模型 ID、Runtime 版本、状态、事件序号及脱敏结果，不记录 Key。

## 6. 本轮验证与尚未验证

已验证：开启外键的 SQLite 事务、并发重复领取、失效/错误 fencing token、超时、取消与结果竞争、成员撤销、模型配置漂移、固定版本材料、故障回滚、补投、Node 子进程协议、环境隔离和取消清理。实际 Pi 1.3.0 的 Agent 循环使用官方脚本化提供方完成测试；另以实际编译入口验证未知模型在联网前被拒绝。

未验证：真实 PostgreSQL 并发/锁语义、Redis/Celery 联合执行、真实 DeepSeek 请求和远端取消效果、容器部署及进程硬崩溃恢复。固定版本安装和 Fake 测试通过不代表这些联调已通过。SSE、运行页和 W02–W11 已在后续完整计划实现中加入，当前运行步骤见[第一版运行手册](weora-v0.1-operations.md)，不要以本节首轮历史数字作为当前总验收证据。

本轮结果：后端 119 项测试、Pi 6 项测试及现有前端 9 项测试通过；静态检查与 Pi 构建通过。依赖扫描另有 3 项现有 Web 路由库中风险告警（无 high/critical），详见[实现进度](weora-v0.1-progress.md)，未将扫描结果描述为“所有依赖安全”。
