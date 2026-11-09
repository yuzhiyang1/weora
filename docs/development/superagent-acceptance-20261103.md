# Weora 内置 Super-agent 验收记录

日期：2026-11-03。目标及源码出处见 [实施对照](../architecture/superagent-source-port.md)、[ADR 0016](../adr/0016-main-agent-superagent.md)。这次按完整的主 Agent / Worker 协作目标实施；早期“不订阅话题、普通消息不接单”的产品约定已被明确替代。

## 已接入的流程

飞书实时事件 / 游标补偿 → 原有 Context 身份、授权、冻结路由和 Inbox → PostgreSQL 协调状态 → 程序准入 → Pi 主会话读取证据并提交结构化决定 → 持久 Ticket/Assignment/launch → 原生 Task/Run → Pi Worker → 原生权限、工具网关及交付验收 → 持久报告 → 主会话 Outbox → 原话题回复。

已有任务的普通跟进直接送到原 Worker，完成后仍沿用同一个 Task 建新 Run。活跃 Run 支持 steer；待投递、子进程已接收、模型已读分别记账。主 Agent 不获得沙箱写工具或审批权，仓库仍取自 Weora 项目配置及固定提交。

## 自动检查

| 检查 | 实际结果与范围 |
| --- | --- |
| Ruff / Mypy | 通过；Mypy 覆盖本次生产代码、协调测试及 PG/真实 Pi 集成测试，共 158 个文件 |
| 协调与提供方超时 | 最终 40 项通过；涵盖模式、方向与规则校验、重复接单、人工停止、过期决策、暂停时间、问题回执、退回、线程关联、容量、撤权及游标补偿 |
| 受影响 Context / Runs / Projects / Pi 回归 | 首轮 300 项中 297 通过；两项新增 TTL 测试写错了 Inbox 业务键，已修正并在最终协调测试通过；一项独立引导子进程在全量负载下超过既有 10 秒测试时限，单独复验通过。没有通过提高产品超时或修改其断言掩盖该问题 |
| 修正项复验 | 协调服务、提供方超时和独立引导子进程合计 10 项通过；后续最终协调 40 项亦通过 |
| 既有治理回归 | Run 恢复、Tool Gateway、SSE 和 Worker 生命周期，22 项通过 |
| PostgreSQL 状态并发和迁移 | 2 项通过；随机隔离 schema，验证并发去重、决策事务、租约争用、同键新 generation、不允许旧 owner 完成新工作、加密恢复与迁移往返 |
| PostgreSQL 原生 Task/Run 全链路 | 1 项通过；不是只验证协调表，包含创建真实 Run、同 Task 续接、渠道投影、长报告和全部派生 Run 保留期 |
| 真实 Pi / DeepSeek | 1 项通过；独立业务库，observe 不建 Task、auto 接单、原生 Worker、报告和主会话回报，并连续执行第二轮 prefix 需求。渠道使用明确 Fake，不把这个测试称为飞书联调 |
| Pi TypeScript | 12 项通过；真实 Agent Core 配脚本提供方，包含持久历史、工具等待中的 steer、去重、退出取消、凭据与协议拒绝。typecheck/lint/build 通过 |
| Web | 渠道面板 5 项通过，typecheck/lint/build 通过；普通未 @、实时/补偿、未知发送与撤权隐藏旧回执分别验证 |

真实 PostgreSQL 测试只使用 `127.0.0.1:55432/weora` 上新建的随机 schema，结束后清理自有 schema；没有操作原 5432 实例。少量库警告来自现有飞书 SDK protobuf 的废弃时间 API，不视为功能失败。

本轮检查发现并修复的实际问题：

1. 模型把 observe 误判为 unavailable。按上游 admission 的语义明确提示和工具契约：模型仍提交 accept/ignore，后端在 observe 将 accept 转为 observed/would_accept。旧失败判断保留，未改库伪造成功。
2. Worker launch 幂等键拼接后超过 PostgreSQL 128 字符限制。真实 PG 测试先复现 StringDataRightTruncationError，再改为确定性摘要键；兼容此前冻结但未写入的超长键。
3. Pi 恢复时会再次报告历史已读 ID；旧适配器把它记到新 Assignment，触发 STALE_ASSIGNMENT。现在保持历史回执所属安排不变，只更新尚未读的新输入；真实 Pi 测试扩展为两轮后通过。
4. 新原生 Run 接管后旧失败报告仍可见，会误导主会话再次重试。绑定新 Run 时清除旧当前报告，历史 Run 事实仍保留。
5. 原生文本结果过长会被协调报告上限拒绝。现在保存有界摘要和完整 Run 引用，不截断原 Run 输出。
6. HTTP 提供方 TimeoutError 未转换为上下文安全错误。补充转换及故障注入测试；不让来源超时泄漏成 Pi 的整体超时事实。

## 真实飞书验收

只在已授权的 **飞书示例群** 新话题“【Weora Super-agent 主会话验收 20261103】”启用配置，未对群里其他讨论开放监听。固定 main/worker 已发布版本均为 `0.1.0`，模型沿用 `deepseek-flash`，不选择工程项目、不修改仓库、不执行外部业务工具。

| 环节 | 保存的事实 |
| --- | --- |
| 话题 | `omt_198c36f94e8fdb53`，scope `4ce9298f513527851f87b4cfd99151a19d8aaa65323fba91dc896955fc3a1cdb` |
| observe | 普通消息 `om_example`，经补偿入站，最终 `ignore/observed`、`would_accept=true`；无 Task/Run/对外回复 |
| auto 接单 | 普通消息 `om_example`，**真实事件入站**，`addressed=false`，主 Agent 结构化 accept，产生一个 Ticket |
| Task | `2dfb2dca-4a16-5c46-9d18-256f0e2a595b`，后续需求沿用此 ID |
| 首轮成功 Run | `967666aa-1079-5dac-990b-1895fc80c7a3`，原生 `SUCCEEDED`，Worker 报告由平台验证后转 ready |
| 首轮结果 | `om_example`，原话题 SENT，并用 bot 身份调用消息读取确认远端存在 |
| 第二轮需求 | `om_example`，同任务增加 prefix；修复历史回执后，通过新的人类消息 `om_example` 明确重试，均未 @、经补偿入站 |
| 第二轮成功 Run | `52618366-f05d-5569-9d78-e8c9faa4d3c8`，同 Task、新 Run，恢复 Pi 历史并完成 prefix 改动 |
| 第二轮结果 | `om_example`，SENT，远端读回含 prefix 更新后的函数和样例；实际取出送达函数经 AST 白名单检查，在隔离命名空间运行 4 个断言，全部通过 |

保留两条失败 Run：`e8eec36d-581a-53f3-b876-11dc925730cf` 为重试竞争中被新安排取代后的 ACCESS_REVOKED；`179971a4-dbab-56b2-a216-86ade9de3bf9` 为修复前历史回执触发的 STALE_ASSIGNMENT。原生 Task 历史保留四次尝试，不能只根据当前 Assignment 指向的 Run 列表漏掉失败事实。没有修改失败 Run 状态。

此合成任务按要求只生成代码与样例。Worker 文字中的“自测通过”不算外部命令证据；验收人另外执行的四个断言是独立验证。没有把此结果当作 Docker 沙箱、仓库推送、评审或真实工程交付的证明；这些仍沿用已有审批机制，相关恢复/Gateway 回归已执行，本轮未另行发布远端代码。

有一条 auto 消息确认经真实事件入站，其余明确标为游标补偿。两种运输共用幂等 Inbox。按用户要求，不继续调查旧测试机器人长连接的存量不稳定，也不以一次实时成功承诺该旧应用持续稳定；上线新应用应独立绑定和验收。

运行中插话已通过真实 Node 双向协议和真实 Pi Agent Core 的工具等待测试。此次飞书外部联调验证的是接单、重启后恢复、完成后追加及渠道回报，未把它描述成“真实飞书长工具运行中插话”的外部实测。

## Console 与启动环境

通过真实登录访问上述成功 Run 的“来源与资料”页，检查浅/深色、1440×1000 和 390×844。复用 HeroUI 原面板，显示“普通消息（未 @）”、实际运输方式、主 Agent 管理及真实送达回执；窄屏无整页横向溢出。状态标签只作自然高度的布局修正，未新增主题或控件。

截图：`output/playwright/superagent-20261103/{dark,light}-{desktop,mobile}.png`。与 `.agents/skills/weora-ui/references/screenshots/run-detail.png` 对照，实际查看浅色桌面和深色窄屏。使用 [Weora UI Skill](../../.agents/skills/weora-ui/SKILL.md) 和本机 Playwright；CLI VM 无本地文件读取能力，因此登录验收用 Playwright 库脚本读取本地测试配置，凭据及 Cookie 不打印、不保存 storageState。

本地已启动 API 8000、Web 5173、PostgreSQL 55432、Redis 6379、runs/control/main/effects 四类 Worker、Beat 和 Receiver。0017 已迁移，迁移前备份 `volumes/postgres-local/backups/weora-before-0017-20261103-185127.dump`（226531 字节）已验证可列出目录；本轮未演练整库恢复。进程与状态证据在忽略目录 `output/superagent-live-20261103/`，不作为版本库业务配置。

## 当前限制与运维入口

- 配置通过管理员 API + CSRF + revision CAS 管理，目前没有独立配置 UI；主 Agent 无权自改模式和来源。
- 当前文本 Runtime 无视觉理解；图片证据需要时显式不可用。
- 请求 262,144 字节、检查点 240,000 字节、每轮 24 次工具、单协调 scope 状态 16 MiB；未做无限历史压缩和大规模长时间压测。
- 不扩大同群成员权限。其他作者必须独立绑定及授权；上下文订阅不会继承原 Task 发起人权限。
- 测试配置只作用于该话题，当前 auto、revision 2。生产切换新飞书应用须重新配置身份、来源、路由及准入。
- 新运行没有继承旧批准，未知外部写/发送仍需只读对账。本轮没有新增任意 Shell、隐式 GitHub 或绕过审批路径。

启动与管理步骤见 [运行手册](weora-v0.1-operations.md)。本次没有执行 Git 暂存、提交、推送或切换分支；保留原工作树和无关 `.openidea/`。
