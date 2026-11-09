# ADR 0005：通过官方 CLI 承载飞书上下文读取

- 日期：2026-10-19
- 状态：已实现，可配置启用；本轮未切换既有服务
- 范围：上下文读取优化，不扩大第一版产品能力

## 决策

保留 `FeishuReader` 端口和 Context Service。新增 `FeishuCliReader`，通过官方 `lark-cli` 原生二进制承载历史消息、话题页和单消息读取。部署选择 `http` 或 `cli`，默认 `http` 兼容已部署服务；不存在失败时偷偷切换实现的降级。

职责保持以下边界：

| 组件 | 负责内容 |
| --- | --- |
| 平台代码 | 工作空间与来源授权、机器人绑定、令牌缓存/刷新、上下文预算、固定截止时间、游标、解析完整性、快照与审计 |
| Agent | 是否需要继续查询、选择已授权来源与时间范围、分析和引用返回证据 |
| Tool Gateway | 维持已有来源列举、消息列表、话题读取、单消息读取四个工具，不暴露原始 CLI 或 shell |
| CLI 读取适配器 | 固定机器人身份、固定两个 GET 路径、单页执行、受限 JSON 输出、脱敏错误、超时和进程回收 |
| 原有 SDK / Outbox | SDK 长连接接收与 Inbox ACK 不变；HTTP 回复、幂等键与未知发送结果处理不变 |

## 为什么不用消息快捷命令直接替换

已核对 `1.0.80` 的快捷命令：它们会将消息正文转换为展示格式，部分自动展开话题；群列表还添加卡片格式与话题根消息参数。这与 Weora 已有的原始正文解析、完整性标记和显式预算并非同一契约。

因此采用 `lark-cli api GET` 的受限用法，固定允许：

1. `/open-apis/im/v1/messages`：群历史或话题，每次最多 50 条。
2. `/open-apis/im/v1/messages/{message_id}`：单消息。

查询参数通过 stdin JSON 传入，不经 shell、不接受模型提供的方法/路径/命令、不启用 `--page-all`、资源下载或表情扩展。CLI 外层 `ok=true`、`identity=bot` 与退出码必须同时符合约定，不能把外层当成 OpenAPI `code=0`。

本次的收益是建立可复用的官方 CLI 接入边界，不是增加搜索/附件理解能力，也不宣称 CLI 一定比 HTTP 更快。现有 HTTP 实现仍保留用于显式回滚；应用凭证签发与少量探测/回复仍使用 HTTP，没有全部迁移。

## 身份和进程安全

- 固定使用 Weora 已登记连接，不使用当前电脑上的默认 CLI Profile 或用户 OAuth。
- 复用现有按连接 ID 与凭据版本隔离的短期 `tenant_access_token` 缓存；只有令牌失效时最多刷新一次。
- 子进程仅获得该连接的 App ID 和短期应用令牌；长期 App Secret、数据库密码、模型密钥不下放。
- 每次调用独立临时工作目录、配置目录和环境白名单；不继承个人 token、代理、认证 sidecar、个人 Profile 或其他 Agent 的环境。
- 令牌不进入 argv、stdin 参数、临时配置文件或日志。使用子进程环境注入并不对宿主机管理员形成安全隔离；应使用专用服务账号，不能把本适配器当作容器沙箱。
- 仅允许管理员指定的原生二进制绝对路径，Windows 不使用 npm PowerShell/CMD 包装器；不使用运行时 `npx` 下载。
- 固定验证 `lark-cli version 1.0.80`；关闭更新/Skill 通知，不自动升级。后续升级要另做契约回归。
- 默认单个 CLI 进程读取超时 2.5 秒，stdout 上限 2,000,000 字节、stderr 上限 65,536 字节；取消、超时或超限后终止进程、排空管道并删除本次临时目录。

平台原有自动补齐总预算仍是 3 秒，不因为 CLI 启动或令牌刷新延长；不足时仍保守等待。CLI 内部 HTTP 行为属于固定上游版本，未声称具备与旧 aiohttp 实现完全相同的重定向、内部重试或内存策略。

## 上游事实来源

- 仓库：<https://github.com/larksuite/cli>，MIT。
- 固定 tag：`v1.0.80`，commit：`29a97dbde8d2bef4f87145a68869ae04f75a0fd4`。
- `extension/credential/env/env.go`、`internal/envvars/envvars.go`：环境凭证来源、机器人身份限制与令牌变量。
- `internal/core/workspace.go`：`LARKSUITE_CLI_CONFIG_DIR` 隔离路径。
- `cmd/api/api.go`、`internal/client/response.go`：stdin 参数和成功信封解包。
- `errs/category.go`、`errs/subtypes.go`：错误类型和子类型。
- `shortcuts/im/im_chat_messages_list.go`：消息快捷命令的额外参数与展示转换。
- 官方 API：[会话历史](https://open.feishu.cn/document/server-docs/im-v1/message/list)、[单消息](https://open.feishu.cn/document/server-docs/im-v1/message/get)。

只使用其公开可执行程序与文档契约，没有复制上游业务源码。

启用方法和实测记录见 CLI 读取验收说明（内部记录已移除）。
