# ADR 0027 本地 Agent 会话寻址与跨设备消息

- 状态：已实施核心链路，原生客户端兼容性持续验证。
- 日期：2026-11-12。
- 承接 [任务协作与跨成员执行](0023-task-collaboration.md) 和 [本地 Agent 联调设计](../architecture/weora-local-agent-collaboration.md)。

## 业务与技术决策

以工作空间和设备限定访问，以 Weora 会话 UUID 定向投递。目录展示 `成员/会话@设备`、运行时、仓库、分支和设备在线状态。原生会话由设备、运行时、配置实例和原生 ID 共同标识，恢复会话会取回原地址。直聊由收件会话所有者逐成员授权。任务联调复用 Task 的共享范围，参与者各自接入，任务关闭后停止投递。任务消息与显式发布的文本证据在任务详情查看。目录的仓库字段用于识别会话。

Python 连接器通过 HTTPS 与 WSS 连到现有 Weora 服务。短期一次性配对码从登录后的控制台签发，兑换为 30 天可撤销设备凭据。设备端使用系统钥匙串。PostgreSQL 权威保存消息、发送及领取幂等键、回复关系、唤醒尝试和处理状态。提交后用 PostgreSQL 通知唤醒订阅，WSS 仅发送引用，断线重连和周期查询补偿通知丢失。

投递严格针对一个会话。原生队列收下提示后，消息仍为待领取。会话用稳定幂等键领取正文，回复引用原消息并在同一事务更新状态。领取超时保留原处理权并标记待确认。连接器调用原生通道前先持久化唤醒尝试，失败或结果不明时可主动领取。每对会话设消息速率上限。消息正文由领取工具返回，原生唤醒仅携带 Weora 引用和领取指令。

## Reason Network 源码对照

上游固定在 `5442c8b551a6897b596c7695e51b0329551aaa6c`。实现使用 Weora 自身的 Python 接口和模型。

| 源码 | 对齐的行为 |
| --- | --- |
| [会话注册](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/src/daemon/sessions.rs#L51) 与 [目录寻址](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/src/daemon/directory.rs#L499) | 通过原生会话身份登记，按成员和会话查找，重名时要求选定目标 |
| [持久化发送和投递](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/src/daemon/mail.rs#L151) | 定向发送、幂等重试、离线待取、回复引用、FYI 自然时机领取及对话限流 |
| [原生通道选择](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/src/daemon/mail.rs#L511) | Codex 使用同配置目录的 `queue --thread`，Claude Code 可用时走原生 inbox，其他能力回退为 hook 或主动领取 |
| [Claude Code 会话记录](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/src/claude.rs#L130) 与 [socket 帧](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/src/agent.rs#L463) | 以原生会话 ID 识别本机 inbox。Windows 使用 named pipe 和本机 peer token，Unix 使用会话 socket |

Weora 采用中心化平台授权和 PostgreSQL 收件箱。Reason Network 自己的组网、团队邀请、P2P 传输和 SQLite 存储无需部署。设备对同一操作系统账户内的进程使用本机信任边界。仓库元数据与收到的正文均视为外部数据。

新增客户端沿用[同一提交的投递分级](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/DESIGN.md#L321)。OpenCode 参考其[服务插件](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/src/opencode-plugin.ts)，Pi 参考其[会话扩展](https://github.com/reason-machines/reason-network/blob/5442c8b551a6897b596c7695e51b0329551aaa6c/src/pi-extension.ts)。Weora 插件用 stdio 订阅一条已绑定会话的收件引用，不需要开放本机端口。Cursor 使用会话 Hook，DeepSeek Harness 使用 MCP 主动领取。Kimi Code 使用[官方 ACP](https://moonshotai.github.io/kimi-code/en/reference/kimi-acp.html)启动或加载由连接器持有的会话，由会话内的 MCP 工具领取和回复。仅通过 ACP 恢复持久化记录无法确认另一进程已打开的会话收到提示。

## 入口

Web 从「设置 → 本地 Agent」配对及管理直聊权限，协作任务详情展示任务会话、消息和证据。CLI 安装、配对、hook 和命令见 [Weora CLI](../../apps/cli/README.md)。

服务端 API 根路径是 `/api/v1/workspaces/{workspace_id}/agent-collaboration`。Web 使用 `/devices`、`/my-sessions`、`/members`、`/task/{task_id}`。设备使用 `/devices/pair`、`/sessions`、`/messages`、`/pending`、`/events` 和会话的任务及领取子资源。

迁移 `20261112_0031` 创建设备、会话、授权、收件和证据表及通知触发器。回退该迁移会移除本功能持久数据。原设计里的附件对象存储、专用协作审计页面、主动更新设备凭据和任意 Windows Codex Desktop 窗口接管仍属于待验证或待交付范围。

## 验证

- 隔离 PostgreSQL 17 完成迁移回退和重升。并发发送重试只生成一条消息，目标会话之外的领取返回空结果。撤权后重新授权不会重新激活旧消息。
- 同一台 Windows 电脑上的两套独立账号和设备进程经本机开发代理用 HTTP 和 WebSocket 发送消息。真实 Codex App Server 接收唤醒提示，并在原 thread 开始新一轮。领取和关联回复由测试 CLI 进程执行，模型使用隔离本机测试端点。
- Windows 的真实 named pipe 验证了 Claude Code 适配器的本机认证帧和收件帧。真实 Claude Code 会话、任意 Windows Codex Desktop 原窗口及两台实体电脑仍待联调。
- Chrome 验证了设备配对、直聊授权、任务接入、发问指令和共享文本证据。检查覆盖深色设置页、浅色任务页及 390px 宽度。
