# Weora 第一版运行与调试手册

- 日期：2026-10-17。
- 对应：[W01–W11 开发计划](weora-v0.1-implementation-plan.md)、[当前实现证据](weora-v0.1-progress.md)。
- 本文是待执行的调试步骤，不是外部联调已经成功的证明。不要用模板覆盖已有 `.env`，不要在聊天、命令行参数或 Run 指令中填写密钥。

## 1. 先确认调试环境

代码托管的多连接登记、凭据引用、平台能力与迁移后旧计划的处理，见 [W10 可替换接入手册](weora-w10-code-hosting.md)。云效新版草稿创建未验证时保持禁用，不能仅因 Git 连通或提供了凭据就创建正式评审。

准备隔离的 PostgreSQL、Redis、DeepSeek 服务、飞书测试自建应用及测试群。沙箱与测试 Git 远端稍后分别配置。禁止先接生产群或生产仓库。

本轮同步新依赖时，默认 `backend/.venv` 遇到 ACL/包元数据错误，没有删除原环境；另创建并验证了被 Git 忽略的 `backend/.venv-v01`。本机后续调试使用下列环境，其他干净环境可使用默认 `.venv`：

```powershell
cd D:/projects/weora/backend
$env:UV_PROJECT_ENVIRONMENT = '.venv-v01'
uv sync --frozen
```

依赖只能由一个进程更新。多个 Worker、测试或 Agent 不应同时运行 `uv lock`/`uv sync`。测试时使用 `uv run --no-sync`；改依赖后先由同一操作者更新锁并同步。当前执行内核固定 Pi Agent Core / pi-ai **1.3.0**，Node.js 至少 22.19.0，不读取个人 Pi 登录态。

在根目录构建：

```powershell
pnpm install --frozen-lockfile --ignore-scripts --store-dir D:/.pnpm-store
pnpm --filter @weora/pi-runtime build
pnpm --filter @weora/web build
```

`--store-dir` 应与本机已有 `node_modules/.modules.yaml` 一致，其他机器不必照抄。不要强制清空已有依赖目录。

## 2. 迁移与启动

2026-11-03 更新：当前 head 为 `20261103_0017`。0016 增加持续 Task 输入及线程关联；0017 增加加密协调状态。下文 0012 的说明保留为历史背景。升级前确认目标库、备份、暂停接收并停止旧 API/Worker，迁移后整体加载新代码。0016/0017 有业务记录时拒绝有损 downgrade；回退使用兼容代码和备份恢复流程，不能直接丢表。

启用 Super-agent 时，除 Run Worker、API、Beat 和 Receiver，还须分别消费三个队列；不能只启动旧 `weora.runs` Worker。Windows 本地可使用以下参数，Linux 生产并发需另行验证：

```powershell
uv run --no-sync celery -A weora.bootstrap.worker:celery_app worker --pool=solo --queues=weora.coordination.control --hostname=weora-control@%h --loglevel=INFO
uv run --no-sync celery -A weora.bootstrap.worker:celery_app worker --pool=threads --concurrency=2 --queues=weora.coordination.main --hostname=weora-main@%h --loglevel=INFO
uv run --no-sync celery -A weora.bootstrap.worker:celery_app worker --pool=threads --concurrency=3 --queues=weora.coordination.effects --hostname=weora-effects@%h --loglevel=INFO
```

主 Agent 的模型轮次在 main 队列，读取补偿、回复、取消和原生派发由独立 control/effects 队列处理，工程模型不会占住控制队列。需要多个原生 Run 并发时，本地 runs Worker 使用 `--pool=threads --concurrency=4`；本文旧的 solo 命令只适合串行检查。Beat 每 2 秒补投协调待办，数据库状态为权威。Receiver 提交 Inbox 后立即异步通知控制队列；未收到实时事件时，游标补偿每轮结束后等待 60 秒，二者在回执中分别标识。

通过 `/docs` 的 Super-agent API 管理配置：先建立 Context 连接、来源、用户身份绑定、授权及冻结路由，再调用 `PUT /api/v1/workspaces/{workspace_id}/coordination/config`，提供来源、route_key、主/Worker 已发布版本、允许作者、规则、模式和 expected_revision。新配置 revision 从 0 开始；更新使用 GET 返回的当前 revision，冲突返回 409。默认 observe；只有管理员明确设为 auto 才启动推断任务。仅在指定来源/话题启用，不会把整个企业的消息交给模型。模型没有修改配置、审批或任意 Git 写入的工具。

`GET /api/v1/workspaces/{workspace_id}/coordination` 提供协调任务、报告、副作用错误和渠道回执；每个原生 Run 的“来源与资料”页可区分普通消息、显式 @、实时事件和游标补偿。未知投递必须只读核对远端，禁止盲重试。暂停配置会停止新评估、派发和回复，已入站输入保留；人工停止的任务不自动复活。配置目前使用受认证/CSRF 保护的管理 API，尚无独立配置页面。

最新验收与容量边界见 [Super-agent 验收记录](superagent-acceptance-20261103.md)。现有基础 Compose 未自动包含这些执行进程，不能只运行 Compose 就宣称启用了主 Agent。

先确认目标是隔离数据库并备份，停旧 Worker，再在 `backend` 执行：

```powershell
uv run --no-sync alembic heads
uv run --no-sync alembic upgrade head
```

此前 W11 验收的迁移 head 为 `20261019_0012`。004–005 是运行底座；006 是 Context 文档存储；007 是计划、审批、顺序化证据与远端交付；008 是工具预算/调用；009 是任务原会话；010 是数据库级只读恢复标志；011 保留工具调用复合主键并清理冗余唯一声明；012 为工具增加可空的执行者与 fencing 标识，支持强制退出后的精准补偿。原实施阶段仅验证 SQLite；后续已创建并迁移独立 PostgreSQL `127.0.0.1:55432/weora`，未修改原 `5432` 实例。0012 前备份与真实故障证据见 [W11 验收记录](weora-w11-recovery-acceptance-20261019.md)。009 的回退遇到飞书任务会拒绝有损降级；不得把 downgrade 当线上无损回滚。

在三个终端保持相同安全配置并分别启动：

```powershell
uv run --no-sync uvicorn weora.main:app --host 127.0.0.1 --port 8000
uv run --no-sync celery -A weora.bootstrap.worker:celery_app worker --pool=solo --queues=weora.runs --loglevel=INFO
uv run --no-sync celery -A weora.bootstrap.worker:celery_app beat --loglevel=INFO --schedule "$env:TEMP/weora-beat-schedule"
```

Windows 本地使用 `solo`；Linux 生产参数需单独验收。**Beat 是必需进程**：负责数据库补投、过期租约、审批状态投影、Inbox/Outbox、正文保留期和孤儿容器补偿。Redis 不是业务真相来源；清空 Redis 后仍以 PostgreSQL 待投递记录恢复，不重跑已成功的写工具。

当前 Beat **只投递调度任务**，维护逻辑也需要 `weora.runs` Worker 消费。Worker 全部停止时，仅保留 Beat 不会立即把运行投影成恢复态；重新启动 Worker 后才执行租约与工具补偿。值班流程：先确认数据库/Redis → 启动 Worker 和 Beat → 等待扫描 → 查看 `RECOVERY_REQUIRED` 与 `UNKNOWN` → 人工只读恢复，不直接重跑写操作。

另在根目录启动 Web：`pnpm --filter @weora/web dev`。现有 Docker Compose 是此前的基础拓扑，不能据此认为 Pi/Receiver/沙箱已自动启用；第一轮采用上述显式进程启动。

## 3. 先调 DeepSeek 与 Console

安全配置项见 `backend/.env.example`。只补充自己的本地值：

- `WEORA_RUN_RUNTIME=pi`
- `WEORA_PI_RUNTIME_ENTRY=D:/projects/weora/runtimes/pi/dist/index.js`
- `WEORA_PI_MODEL`：明确的模型 ID，不自动选 latest 或回退其他模型。
- `WEORA_DEEPSEEK_API_KEY`：仅后端 SecretStr 安全配置。
- `WEORA_CELERY_BROKER_URL`、模型时长/输出和租约参数。

API、Worker、Beat 修改配置后一起重启。旧 Run 的模型快照不会改用新配置；配置不匹配时应失败而不是偷偷换模型。

在 `/agents` 发布不绑定 Skill 的测试 Agent，即可不依赖 OSS。到 `/runs` 选择明确版本提交，在详情检查排队、运行、持久回答、工具事实与事件序号。刷新、SSE 断线和 JSON 补读不再次调用模型。API 使用 Cookie；写入还需要会话绑定 CSRF；幂等键只在当前用户/工作空间生效。

必须记录成功、失败、超时、重复投递、取消、跨用户拒绝的脱敏结果。不要把脚本化提供方测试写成 DeepSeek 真实请求成功。

## 4. 再调只读仓库工具

登记一个不包含密钥的只读测试仓库目录，同时指定 `WEORA_READONLY_REPOSITORY_WORKSPACE_ID`。不给模型目录参数，模型只传相对 `path`；其他工作空间不可读。宿主文件工具只使用只读文件句柄并核对最终路径，拒绝穿越、链接逃逸、Git 元数据和常见凭据文件。目录应由管理员保持只读，不把此能力等同于操作系统容器隔离。

工具白名单、参数 Schema、Actor、角色、租约、预算和调用结果由平台负责。Pi 只收到声明并发出 JSONL `tool_call`，所有结果经 Gateway 返回。没有原始 Shell、任意 URL、插件安装、个人凭据或通用 OpenAPI 代理。

先验证一次“读 README 再回答”，再验证非法路径、未声明工具、重复调用 ID、超时和预算耗尽。工具调用上限 24；上下文另有更严格的 10 次内容读取、300 条消息和约 12000 token 共享预算，重启不清零。

## 5. 飞书连接、授权与历史

先提供两个**稳定且可安全备份**的部署密钥：

- `WEORA_CONTEXT_ENCRYPTION_KEY`：Fernet 格式密钥，仅用于加密连接凭据与渠道正文。
- `WEORA_CONTEXT_CURSOR_KEY`：至少 32 字节的随机签名材料。

不得在每个 Worker 启动时临时生成，也不能在丢失后无损恢复已有凭据。正文默认保留 7 天，可配置为 1–7 天。缺少配置时接口返回 `CONTEXT_NOT_CONFIGURED`，不选择本机其他账号。

管理员到 `/context`：创建应用连接 → 覆盖保存 Secret（不回显）→ 独立探测应用、入群与历史读取 → 登记 `oc_` 群来源 → 为有效成员显式授权 → 显式绑定连接内 `open_id` 与 Weora user ID。关联身份不自动获得群授权；群作者也不自动成为 Weora 用户。集合只组织已有来源，不扩大权限。

到私有 Run 的上下文面板手动查询历史。默认窗口以服务端 `context_cutoff` 为截止点，单页默认30/最多50条，最多30天。验证连续两页、同时间消息、话题归属、引用消息、partial、限流及撤权。客户端使用签名游标，不接触供应商 page_token；刷新后用快照保存的 query/window 原样续读。没有搜索关键词工具。

四个 Agent 工具是 `context.sources.list`、`feishu.messages.list`、`feishu.thread.read`、`feishu.message.get`。Agent 决定来源、窗口、分页和展开话题；平台注入身份/截止点/授权并校验本 Run `ctx:...` 引用。外部讨论是普通证据，不提升为系统指令。

## 6. 飞书 @ 入口

管理员配置 `/context` 中的明确 Agent 发布版本路由，默认只响应显式 @ 本应用机器人，不接受普通消息、编辑、撤回、其他机器人或表情触发。开启邻近消息需显式路由配置。首次消息先冻结原始连接/群/话题/消息/用户及路由版本，重连重投不创建第二个 Run。

在 `backend` 的第四个终端运行：

```powershell
uv run --no-sync python -m weora.bootstrap.feishu_receiver --workspace 工作空间UUID --connection 连接UUID
```

Receiver 使用官方 `lark-oapi==1.7.5` 长连接运输，只有 Inbox 持久提交后才成功 ACK；数据库失败返回失败，SDK 可重投。Receiver 不执行模型，Beat 异步创建 Run。SDK 在独立接收进程延迟加载，避免 API/Worker 启动加载全部上游 API 类型。更换应用 Secret 后需要重启接收进程；禁用连接会立即阻断读取和交付，不回退个人登录态。

生产部署建议使用独立飞书应用的接收身份，避免与 HiFox、CLI 事件总线或其他部署争抢事件。**当前测试阶段按用户决定继续使用现有应用，不以隔离为验收前置条件，也不再推进新建应用。** 遇到争抢可以有界重试测试请求；同一消息的重复投递按业务键去重，已被其他接收端确认的历史事件不假定会自动补投。人工补收须明确标记，不冒充长连接入站。仅停止本机已知进程不能证明外部没有接收端，也不能为排障擅自停止未知服务、修改权限或重置共享密钥。

排障时对照开发者后台事件 ID、hookType、返回码和本地持久事件，不能把云端 SUCCESS 等同于已进入 Weora。已知辅助事件只持久元数据后 ACK，不产生任务；未知处理器错误仍需核对订阅和分发。

首模型请求前自动补齐：引用优先、同话题、显式启用的近期邻近消息；固定触发时刻、近2小时、最多10条、3秒和约2000token。不读未知身份历史，不跨群展开引用，话题失败不悄悄改读整群。第一版对自动补齐不完整采取保守等待 `WAITING_CONTEXT`，通过 Console 补充并选择只读恢复分析。

群 Run 只能读原群；Console 私有多来源结果没有发送到群的入口。Outbox 按持久原会话回报，执行结果与投递状态分开。等待审批可配置 `WEORA_CONSOLE_PUBLIC_URL` 生成 Console 链接；生产只允许 HTTPS，未配置则报告运行 ID，不拼造公网链接。

群任务详情的“飞书入口与原会话回报”读取 `/context/runs/{run_id}/channel-status`，每次重新鉴权，只显示本任务元数据。只有 SENT 是本地已确认送达；SENDING/RECOVERY_REQUIRED 不可盲重试。代码托管交付在另一独立区块；模型自述成功不代替实际工具与渠道回执。

2026-10-19 接收故障中，用户明确要求补投指定真人消息，采用本机单条运维补收：从飞书读回原文、验证身份/群/授权/@/保留期、保留原时间并按原消息业务键写 Inbox，标记 `manual_history` 和无正文审计，不构造事件或修改旧 Run。该方式不改变默认触发规则，也不是第一版新增的公网补收接口。页面显示“人工补收，不代表长连接事件已收到”；正常后半链路可以验收，自动入站仍需独立验证。

### 可选：使用官方 CLI 读取消息

可配置 `WEORA_FEISHU_READER_BACKEND=cli`，配合固定 `1.0.80` 原生二进制绝对路径 `WEORA_FEISHU_CLI_EXECUTABLE`；默认仍为 `http`，不会自动使用本机个人 CLI 账号。只替换历史/话题/单消息读取，SDK 接收和 HTTP Outbox 不变。启用、回滚、安全边界与真实只读对照见 CLI 读取验收说明（内部记录已移除） 和 [ADR 0005](../adr/0005-feishu-cli-read-adapter.md)。

## 7. 审批、沙箱与浏览器

沙箱需要独立 Docker 执行节点。登记固定镜像 `@sha256`、仓库 tar 绝对路径、tar SHA256、原始完整 revision、repository_id 和唯一获准工作空间。归档不能含 `.git`、符号链接、硬链接、设备或穿越路径；镜像预装依赖，不让运行时安装包或临时访问外网。准备镜像的参考见 `runtimes/sandbox/README.md`。

本地构建镜像可以登记完整 `sha256:<64位hex>` image ID，必须记录构建输入及基础镜像摘要；仍拒绝 tag/latest 或短 ID。默认 CLI 为 `docker`、无前置参数。Windows 可由管理员配置 `WEORA_SANDBOX_CLI_EXECUTABLE` 为 `C:/Windows/System32/wsl.exe`，`WEORA_SANDBOX_CLI_ARGS` 为固定 JSON 数组，如 `["-d","Ubuntu","-u","root","--exec","docker","--host","unix:///var/run/docker.sock","--config","/var/lib/weora/docker-cli"]`。必须使用 `--exec`，不能写成 `-- docker`：后者仍经 WSL 默认 shell 解释，会破坏结构化 argv 并导致 Python 参数或安全负例失真。这些是部署配置，不是任务参数；不通过 shell 拼接，不开放未经认证的 Docker TCP，也不读取个人 Docker context/登录凭据。

计划需明确目标仓库/revision、路径前缀、命令 argv、风险和每项动作。Console 人类批准/拒绝/补充/撤销；审批不注册为 Tool。实质变化形成新版本并失效旧批准，审批界面绑定审阅的 plan_id，旧确认不能批准新版本。

批准后同一 Run 重新领取新 fencing token 并由平台切到 sandbox 角色。每次工具重新鉴权；长命令还持续检查撤权、审批到期和租约。容器使用非 root、只读根、tmpfs 工作区、network none、无宿主目录/Socket/长期凭据、无多余 capabilities，并限制 CPU、内存、进程数、输出和时间。不会修改开发者当前工作树。

只展示真实写入/命令/退出码/Diff/浏览器事实。平台用同一计划版本的持久证据序号检查“最后写入之后执行测试”，并要求非空 Diff；验证命令还必须绑定执行前后相同且与最终Diff一致的仓库状态。批准的命令目前保守地都视为必要验证；若格式化/生成命令改变仓库，需再次运行到稳定状态并重新跑测试，不沿用修改前结果。声明了浏览器或 PR 时还需对应成功证据。模型自己声称通过不算验收，旧计划或旧测试不能复用。

浏览器驱动固定安装于镜像只读 `/opt/weora-browser/node_modules/playwright`，不能从仓库加载同名恶意模块。设置 `WEORA_SANDBOX_BROWSER_PORT` 后只访问当前容器对应 localhost 服务与相对路径，拒绝其他端口、外部跳转/WebSocket/下载。未配置、服务未就绪、断言失败、截图失败与成功分别记录。纯后端任务不声明该动作即可不适用。

Worker finally 清理本实例容器；Beat 对无有效租约的同部署容器按不可变容器 ID 回收。所有权标签不完整、其他部署或未知 Run 失败关闭，不做广泛删除。清理失败记录 `CLEANUP_REQUIRED` 并在后续周期补偿。不能把 Fake CLI 测试当真实资源限制通过。

## 8. 草稿 PR 与投递

2026-10-19 团队确认使用**云效**，不使用 GitHub，本轮 W11 不做远端写入。Git 克隆、提交、推送可复用通用协议；创建/查询评审请求及未知结果核对仍需云效适配。以下 GitHub 配置仅说明已有适配器，不是当前团队部署前置，也不能当作云效已经支持的证据。云效凭据后续通过本机安全配置提供，不粘贴在聊天里。

首版供应商适配为管理员登记的 GitHub 测试仓库。配置 App ID、Installation ID、私钥、remote_id、repository、目标分支；App 仅有测试仓库 contents/pull_requests 所需最小权限。没有配置就返回 `PR_NOT_CONFIGURED`。

GitHub App 在后台代理签发安装令牌，仅在后台内存复用最多15分钟，不下放模型或沙箱。**这不是供应商令牌只有15分钟有效**；远端通常更长，正常关闭/轮换时尽力吊销，进程硬崩溃后依赖供应商到期。

`git.commit`、`git.push`、`pr.create`、remote 与目标分支需分别批准，不从“修复”推断远端写授权。不合并 PR、不部署生产。凭批准 Diff 通过固定官方 HTTP API 创建提交、受控分支和 draft PR。

远端已开始但结果未知时进入 `UNKNOWN/RECOVERY_REQUIRED`。Console“核对远端”或 `delivery.pr.reconcile` 只读查询原业务键；查不到不代表未成功，不能盲目重建。飞书发送未知结果也保持待核对，不自动重发；首版没有自动消除所有飞书发送未知状态的回查保证。

## 9. 取消、恢复与撤权

- Worker 硬退出后，在途工具由后台按其原执行尝试转成 `UNKNOWN / TOOL_INTERRUPTED`；已成功调用不改变。升级前无尝试标识的旧记录先等整个 Run 不活跃再补偿，不推测旧 owner。看到 UNKNOWN 应核对证据，不能更换 call_id 盲目重放。
- 接收日志只输出固定脱敏代码。`FEISHU_CHANNEL_CONNECTED` 仅证明 SDK 已连接；需同时检查实际用户消息对应的 event/Inbox。消息在历史 API 可读而 Inbox 缺失时，检查事件订阅、应用多长连接和上游投递记录，不补造原事件或把人工导入称为真实接收。
- QUEUED、等待上下文、等待审批和恢复态可直接终止；RUNNING 先登记取消请求，受控进程退出后才确认终态。远端请求/计费不承诺可撤回。
- 租约过期先转 `RECOVERY_REQUIRED`，不自动重跑。`POST /runs/{id}/recover` 仅接受 `{ "mode": "read_only" }`：保留预算与事实，数据库永久禁止该 Run 再写。需要新的研发执行时新建任务并重新批准。
- 已批准的中断 Run 不自动重新排队；先只读对账。已提交的待审批提案可恢复审批界面，但仍需人类决定。审批已提交、Run投影未提交的窗口由 Beat 幂等补偿。
- 每次预览、缓存命中、工具结果、派生回答、计划、产物、下载、恢复与通知重新校验来源。成员解绑、来源撤权和连接禁用分别生效；同名或重新绑定的其他用户不继承原任务。
- 撤权先同步拒绝访问；到期清理再删除快照、Run输出、Task正文、工具参数/结果、计划与产物副本及渠道正文，保留 ID/摘要/状态审计。已传给外部模型或已送出的消息不能保证召回。

## 10. 调试验收顺序

1. 确认真实 PostgreSQL 迁移、并发领取/租约、Redis补投和 Cookie/CSRF。
2. Console真实模型成功、失败、超时、取消及刷新续读。
3. 真实只读工具调用与结果续推理。
4. 飞书单群授权、两页历史、话题/引用及撤权；再联模型主动查询。
5. Receiver 重连/重复事件、自动补齐、原会话回报和出站失败。
6. 人工审批后真实 Docker 改一个测试 Bug、修改后测试、Diff、取消与资源/网络拒绝。
7. 浏览器一条成功、一条失败，检查真实截图和禁止越界。
8. 测试 Git 远端 draft PR、未知结果只读核对、飞书原会话交付。
9. 停 Worker/清队列/撤权/缩短保留期，验证恢复、补投、清理与审计。

记录具体环境、固定版本、运行 ID、事件序号、事实退出码与脱敏截图。前端自动测试通过不代表视觉与鼠标交互已验收；浏览器可用性以当轮证据为准。2026-11-04 协调中心已通过内置浏览器实测，具体操作与截图见当轮验收记录。

## 11. Super-agent 协调中心与代码平台来源

先升级 `alembic upgrade head` 到 `20261104_0018`，再统一重启 API、Run Worker、协调
control/main/effects Worker 和 Beat。旧版本不识别新增的 code_poll 持久效果，不能混跑新旧协调进程。
迁移只扩展 Task 来源约束；降级前必须确认没有 code_hosting Task，迁移会拒绝有数据的危险降级。

管理员在现有 Console 打开“协调中心”，从“新建观察范围”选择飞书或已登记的代码仓库，
使用已发布的 Main/Worker 版本，绑定实际作者。先使用 observe 检查判断记录，再按产品需要切换 auto。
没有权限、发布资产或连接时应先补配置，不在模型提示中粘贴令牌，也不通过任意 URL 创建来源。
配置保存使用 revision；冲突后先刷新，不强行覆盖别人更新。

飞书来源使用已有渠道、作者绑定和项目授权。代码来源通过 `/coordination/code-config` 的应用用例配置，
只引用现有仓库/远端/项目；当前 UI 可绑定管理员自己的外部账号，API 支持明确绑定多个有效成员。
未绑定的作者不会借管理员身份接单。后续消息必须继续通过当前成员、项目、来源和保留期限检查。

GitHub App 除既有代码交付权限外，来源观察需要 Issues/Pull requests/Contents/Checks/Commit statuses
相应读取权限；回复需要 Issues 或 Pull requests 写入权限。系统分别申请读取、评论及原生交付令牌，
评论令牌没有 Contents 写权限。权限须由新应用管理员在供应商侧配置，平台不会自动扩大安装权限。
Codeup 使用现有受信连接的令牌文件，只启用 MR/评论/评审与 MR 检查；Issue、默认分支 CI 明确不支持。

后台活动只能在已有审批和原生 Run 预算内执行；等待人工时释放资源。进程句柄丢失、外部写结果未知、
无法确认取消时先只读恢复，不重放命令。新 Run 恢复文件仍需新审批。
PR 评审后继续修改时，Worker 读取 `orchestrator_read(resource=deliveries)` 的本 Task 分支引用，
新计划指定 `source_delivery_id` 并重新申请 git.commit/git.push。审批界面展示原分支与预期 SHA，
批准后使用 delivery.branch.push 续写，不再创建重复 PR。远端 SHA、基线或授权变化则拒绝，先核对和重新审批。
过去没有保存源提交的 PR 不会猜测 SHA；未知推送不重放。
独立来源对已有 Issue/PR 的订阅优先于全局普通评论开关；已跟进的外部 PR 也持续补读 CI/评审。
每轮最多读取 20 个订阅并轮转；失败线程保留自己的游标及错误，健康线程继续处理。
源正文撤权或到期后拒绝历史和模型读取，再由既有保留期清理流程清除派生副本。

当轮固定源码、差异清单、测试与本地进程状态见 [2026-11-04 验收](superagent-gap-acceptance-20261104.md)。
