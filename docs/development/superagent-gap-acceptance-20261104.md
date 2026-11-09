# Super-agent 差异修复验收

- 日期：2026-11-04。
- 结论：本次审计登记的 A01–A04、B01–B08 已逐项修复并验证。仍保留下文列明的平台与供应商差异；不宣称两套系统逐行或全功能等价。
- 用户目标：Weora 内置 Super-agent；开发阶段允许调整普通消息接单目标。技术栈、平台架构和审批机制保持。
- 固定上游：[59e6c4059d513a5a63c482aee884c95e4ea89fa0](https://github.com/deepseek-ai/dsh-superagent-prototype/tree/59e6c4059d513a5a63c482aee884c95e4ea89fa0)。
- 修复前证据：[差异审计](superagent-parity-audit-20261104.md)。修复后代码指纹保存在忽略目录 `output/superagent-audit-20261104/weora-postfix-manifest.json`。

## 逐项对应

| 项目 | 已实施行为 | 主要回归证据 |
| --- | --- | --- |
| A01 等待人工 | blocked/failed 后保留自动事实但不唤醒；人类有效输入解除；入箱、派发和 Pi 追加均检查 | `tests/coordination/test_upstream_lifecycle.py` |
| A02 结束释放输入 | ready/dropped/stop 按已读、ack、answer、其他 owner 与 Decision 释放未接住消息，符合条件的人类消息回 Eval | 同上，含未读跟进、已处理与停止反例 |
| A03 批量路由 | 顶层只校验有界批形状，各项业务参数和事务独立，失败不回滚合法项 | `tests/coordination/test_service.py` |
| A04 取消订阅 | Main 显式人类请求可撤原线程订阅；Worker 的原线程保护与作者隔离仍保留 | `test_upstream_lifecycle.py` |
| B01 后台与子会话 | 五秒转后台；活动先入库；完成唤醒父会话并保留槽位；后代消息有独立日志与回执；人工等待、取消、租约丢失和停机有界回收 | `test_activities.py`、`test_managed_worker.py`、真实 PostgreSQL `test_managed_worker_postgres.py` |
| B02 代码托管 | 独立仓库入口、作者绑定、消息游标、PR/CI/评论/评审、评审线程订阅；关闭/合并/重开与人类未读输入共同影响任务状态 | `test_repository_source.py`、`test_hosting_events.py`、`test_hosting_poll.py`、生命周期反例 |
| B03 持续文件 | 同 Task 的工作区快照跨 Run、等待审批继续恢复；新审批、基线、路径、摘要、TTL、撤权及文件模式重新验证 | `tests/delivery/test_workspaces.py`、真实 Docker `test_workspace_docker.py` |
| B04 瞬时重试 | Pi SDK 仅重试当前模型请求，最多三次，15/30/60 秒；认证、配额、取消不重试；工具前已知投递失败可退避，工具后的未知结果不重放 | Pi 15 项测试、`test_session_delivery.py` 的真实 Node 断连测试 |
| B05 目标完成 | 原生 SUCCEEDED 默认 waiting；明确 ready 仍须原生验收；评审未结束继续等待；每一轮报告不能沿用旧 Run 报告 | 生命周期、服务与真实 Pi 两轮续接测试 |
| B06 上下文 | 截止绑定评估快照，前五条与触发前后五条；缺失历史明示；Worker 长正文用本任务内引用分段读取 | `test_assessment_window.py`、`test_worker_reads.py` |
| B07 角色模型 | Main Session、Worker Mandate 冻结模型；后续配置变更不污染旧会话；原生 Run 保存实际执行模型 | `test_repository_source.py`、`test_managed_worker.py` |
| B08 管理可见性 | Console 协调中心从真实来源及发布资产创建观察范围，编辑规则/模型/并发，查看判断、线程、活动、重试与投递，跳转原生审批 | Web 协调中心 7 项行为测试，真实浏览器浅深色与窄屏验收 |

上游实现由源码入口追到测试后再翻译为 Python/Pi 用例，没有执行上游安装脚本，没有引入 DSH 或生产 SQLite。
Main 会话装配、Worker 会话、子会话、活动监督、来源运输和轮询分别封装，避免一个运行函数承载全部职责。
中文注释解释租约、未知写、保留期限和审批边界；不重复叙述显而易见的语句。

## 额外边界修复

- 终态已提交、模型期限同时到达时，不再把成功提交误报为失败；终态提交继续受租约约束。
- GitHub 观察、评论和原生代码交付分别使用独立权限及缓存的短时令牌；评论凭据没有 contents 写权限。
- 来源授权缓存区分作者、来源、项目授权版本及路由，防止旧授权污染新授权。
- 后续每条消息都关联派生 Run；撤权清理包含协调日志、活动、原生历史和工作区产物。
- 原生 Task/Run 已提交、协调绑定未完成时发生撤权，仍登记过期清理依赖，避免孤立正文长期保留。
- 关闭时间使用供应商可信事实；Codeup 未提供独立可信关闭时刻时，不伪造为轮询时间，也不提前结束新恢复的安排。
- 结束目标后取消待派发工作，并持久请求取消旧 Native Run；旧租约不能再写新会话状态。
- 最后复查补齐原 PR 分支续写：当前或前序 Run 的成功分支引用可以进入新计划，分支和预期 SHA 由平台冻结并展示审批。
  真实 Git 测试验证同名分支的新提交以上次提交为父，主分支不变，远端漂移拒绝；未知结果不重放。
  草稿 PR 同时持久化已确认的源分支记录。旧批准被后续提案替换后，成功交付仍是可引用事实，但新写入必须获得新批准。
  缺少 SHA 的旧 PR 记录不猜测续写目标；跨 Task 或已撤销来源的引用拒绝。
- 独立来源也补读已订阅、但非 Weora 创建的 PR。仓库消息游标没有变化时，CI、评审和关闭仍能进入已有任务。
  GitHub/Codeup 共用既有供应商正文解析，分别验证来源与原生交付权限；没有伪造原生 Run 或交付记录。
  自动事实分片保留明确引用与截断说明，不解除 blocked/failed；新的人类跟进才恢复工作。
  每轮最多补读 20 个订阅并轮转，线程游标和失败分别记录，一个失败不阻断其他线程。
  全局普通评论开关关闭时，已有 Issue/PR 的订阅仍接收未 @ 的后续消息。

## 实际验证

| 层次 | 结果 | 证据（仓库忽略目录 output） |
| --- | --- | --- |
| 执行层初轮后端回归 | 507 通过，0 失败、0 跳过 | `parity-backend-current-20261104.xml` |
| 追加 PR 续写后的影响复验 | 213 通过，含全部 Delivery 与相关 Worker/仓库 Task 链路 | `parity-delivery-complete-20261104.xml` |
| 协调模块最终回归 | 95 通过，含独立讨论补读、失败隔离、自动事实挂起、候选开关与订阅 | `parity-coordination-complete-20261104.xml` |
| 原 PR 续写真实 Git / 业务边界 | 7 通过，包含还原原始基线、旧 SHA 拒绝、同 Task、新审批及未知结果 | `parity-branch-continuation-final-20261104.xml` |
| 整理后的受影响用例 | 15 通过；包含将三种投递失败拆为独立参数用例；与上述集合有重叠，不累加成独立总数 | `parity-quality-recheck-20261104.xml` |
| PostgreSQL / Docker / Pi | 9 通过，0 失败、0 跳过；追加分支能力后重验 | `parity-integration-complete-20261104.xml` |
| 独立仓库来源 PostgreSQL | 3 通过，原生 code_hosting Task 续接、CI 持久化和等待人工边界 | `parity-repository-pg-20261104.xml` |
| PostgreSQL 夹具整理后复验 | 2 通过 | `parity-pg-quality-recheck-20261104.xml` |
| Pi SDK | 15 通过 | `parity-pi-current-20261104.txt` |
| Web | 98 通过，17 个文件 | `parity-web-complete-20261104.txt` |
| 类型与静态检查 | 后端全部 163 个源码文件通过严格类型检查；连同选定测试共 183 个文件通过；改动范围 Ruff、Web/Pi typecheck、ESLint、构建通过 | `parity-test-types-complete-20261104.txt`、`parity-code-quality-complete-20261104.txt`、`parity-typecheck-complete-20261104.txt`、`parity-eslint-complete-20261104.txt`、`parity-build-complete-20261104.txt` |

真实 Pi 场景覆盖 observe 只记录、auto 结构化接单、Worker 报告、Outbox 结果、同一 Task 的第二轮 Run。
渠道端口使用测试替身，不能把它写成真实飞书消息投递验收。最初第二轮模型漏报完成时平台保持 waiting；
对照上游 `prompts/worker.md` 补强每轮报告约束后重验通过，没有放宽完成判断。

PostgreSQL 使用随机隔离 schema；SQLite 测试补串行不能代替行锁并发证明。
Docker 场景清理首轮容器后，第二轮恢复 UTF-8 字节、删除与可执行位，缺少新写入审批则拒绝。
初轮 507 项回归通过，仅出现飞书 SDK 内置 protobuf 的 `utcfromtimestamp` 弃用提示。

追加分支字段后，全组 514 项初验曾有 4 个测试夹具失败：旧摘要参照没有排除新可选字段，
三个 HTTP/Gateway 夹具把新增的服务端冻结字段也作为客户端参数发送。按真实协议修正夹具，
保留接口严格拒绝这些字段，并新增注入拒绝用例；213 项影响复验全部通过。
Web 在并行基础设施测试时出现一次存量 5 秒超时，单 Worker 全组 98 项重验通过，没有提高超时阈值。
最后同步普通消息入口的界面说明，受影响上下文页面 10 项复验通过；没有再沿用“普通消息不会起任务”的旧约定。
上述集合有重叠，不累加为独立覆盖总数；没有重复已证明未受影响的业务用例来凑测试数。

浏览器验收使用独立后端 8001 与前端 5174 的合成数据，完成 1440×1000 桌面、390×844 窄屏的浅深色检查。
页面无全局横向溢出，宽表格在自身容器内滚动。截图位于 `output/playwright/superagent-parity-20261104/`。
这些是实际渲染截图，不是真实飞书、GitHub 或 Codeup 数据。验收后恢复默认视口。
临时验收账号已退出，8001/5174 隔离服务已停止；正常开发服务 8000/5173 保留。
原 PR 续写审批另验桌面和窄屏，展示精确分支与旧 SHA；发现标签列挤压值后，只调整审批范围的窄屏布局，
复验无全局横向溢出且完整 SHA 可核对。截图为 `branch-approval-desktop-dark.png`、`branch-approval-mobile-dark.png`。

## 本地运行状态

本地 PostgreSQL `127.0.0.1:55432/weora` 从 `20261103_0017` 升级到 `20261104_0018`。
升级前没有活动 Run，保留 38 个成功、5 个失败、5 个取消记录；协调配置 SHA-256 升级前后一致。
API、原生 Run Worker、协调 control/main/effects、Beat 与原飞书 Receiver 统一重启。
API live 为 ok、ready 为 ready（数据库与对象存储均 ok），协调调度与效果执行有成功日志。
进程清单、日志及升级前后摘要位于 `output/superagent-audit-20261104/`。

这证明新版服务已经本地运行，不证明存量测试飞书应用的投递不稳定已修复。
按用户要求未处理该旧问题，也没有借验收向真实代码仓库发送评论、推送或创建评审。

## 保留的平台差异与接入边界

1. **审批与运行隔离**：上游 Worker 直接使用 DSH 工具和持续目录；Weora 的每次 Run 仍需本轮计划审批，
   通过快照延续文件。默认创建专用分支；原 PR 的续写需要新审批、同 Task 的已确认 Weora 分支交付和精确 SHA，不能继承旧批准或推送任意他人的分支。
2. **来源身份**：上游 GitHub 使用指定 gh 用户账号；Weora 通过已配置连接和最小权限令牌读取、回复。
   不使用用户通知收件箱接口，不将 gh 的通知轮询、ETag 或供应商轮询提示优化宣称为已复制。
3. **供应商能力**：GitHub 支持 Issue、PR、评论、行内评论、无正文评审与默认分支 CI。
   Codeup 支持 MR、评论、评审与 MR 检查；未伪造 GitHub Issue/默认分支 CI，配置不支持项明确拒绝。
4. **上下文与历史**：独立仓库从启用时刻接入，正文受平台 TTL 约束，历史不完整会明示。
   单次分页有界；超过上限或子评论缺失则报不完整且不推进游标，不声称完整扫描了任意大仓库。
5. **进程恢复**：协调状态、会话日志与文件可恢复；丢失后台进程句柄时标记需要恢复处理，
   不宣称宿主故障后可以无损续接任意命令。人工等待时释放计算资源。
6. **实网接入**：新 GitHub App/Codeup 连接仍需配置供应商权限、仓库和作者绑定。
   本轮供应商契约测试通过，真实新应用的外部事件与写入验收尚未执行。

以上边界逐项保留，不用“完全一致”掩盖差异。方案、ADR、测试与固定源码证据一起保存，后续压缩上下文后也以本记录为准。
