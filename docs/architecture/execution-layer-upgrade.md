# Weora 执行层架构升级方案

- 日期：2026-11-02。
- 状态：2026-11-04 的源码差异清单 A01–A04、B01–B08 已修复并逐项验证，见 [修复验收与保留差异](../development/superagent-gap-acceptance-20261104.md)、[ADR 0017](../adr/0017-superagent-runtime-and-repository-sources.md)。包含后台 Job/子会话、持续文件、独立仓库来源、生命周期、重试与管理界面；保留原生审批及供应商边界，不宣称与上游完全等价。其他 Runtime 仍属后续路线。
- 决策摘要：[ADR 0014](../adr/0014-continuous-agent-execution.md)。
- 2026-11-03 主 Agent 实施依据：[固定源码行为与验收清单](superagent-source-port.md)、[ADR 0016](../adr/0016-main-agent-superagent.md)。本轮目标是 Weora 内置 Super-agent，不以局部续接或分类器代替整体协作能力。
- 本地代码基线：`da3db8a817cfc5fa09c47b55602e1b36c30704f8`，调研开始时工作树干净。

Weora 在现有执行层中增加持续协调、事件路由、任务续接、异步作业、主动监听和多 Runtime 能力。Python、FastAPI、PostgreSQL、Redis、Celery、现有 Pi 执行内核及模块化单体继续使用；控制面、能力服务和执行面的总体分工保持。

用户围绕目标与结果协作：一次任务可以接收后续消息、等待审批、后台执行和恢复，不需要手工选择底层 Session。RAG、长期记忆、Skill、工作流、身份权限和资产治理继续由各自的平台边界提供。此次升级不要求先完成全部平台能力。

## 1 升级目标与范围

### 1.1 需要呈现的行为

1. 在 Console 或已授权飞书讨论中提出请求，平台识别新任务或已有任务的后续输入。
2. 多个独立任务并行推进；一个任务等待构建或审批时，协调入口仍能接收其他请求。
3. 用户通过后续消息补充信息、询问状态、改变方向或叫停，系统明确记录处理结果。
4. Worker 交付结构化证据；平台根据交付契约决定是否完成，并在适当渠道请求人类决定。
5. 经明确配置的监听规则持续观察环境，需要时唤醒任务；无事时保持安静。
6. 中断后恢复已接收输入与已知结果，未知写入先对账；重启不自动重复推送、MR 或命令。
7. 后续接入 Codex、Claude Code 和本地 Worker 时，共用 Weora 的任务、权限与审计契约。

### 1.2 目标调整

开发阶段允许调整产品目标。早期文档中的“不持续订阅话题”“普通群消息不触发运行”“首期只有单 Agent 执行”不作为本升级的禁止条件。升级采用受配置和授权约束的持续订阅与主动执行。

目标调整不等于对所有来源开放执行。来源可读、事件可接收、消息可作为上下文、请求可启动任务、动作可执行，是分别校验的事实。订阅本身不授予写入或跨群披露权限。

### 1.3 保持的技术与平台边界

| 边界 | 保持的选择 | 本次增加的能力 |
| --- | --- | --- |
| 后端与分层 | Python 3.12、FastAPI、应用层与领域层、基础设施适配器 | 执行协调用例和状态规则 |
| 权威状态 | PostgreSQL、SQLAlchemy、Alembic | 事件归属、任务输入、会话日志、作业与监听记录 |
| 调度 | Celery Worker、Celery Beat；Redis 作为队列与通知 | 有界协调、唤醒、作业执行与补偿任务 |
| 运行内核 | 保留 Pi；供应商类型不进入业务模型 | Runtime 能力协商与会话续接协议 |
| 权限与工具 | Identity、Context、Tool Gateway、Delivery 审批 | 对新增角色、后台作业和本地执行实施相同校验 |
| 平台能力 | AgentVersion、SkillVersion、上下文、项目与代码源、对象存储 | 执行层按需调用；未来接入 RAG、长期记忆 |
| 工作流 | 自有 WorkflowEngine 边界 | 将来可作为任务执行方式接入，不成为持续任务的前置依赖 |
| 客户端 | 现有 Console、HeroUI 与共享业务组件 | 任务跟进、等待决定、作业与事件状态投影 |

不引入 Elixir/OTP、Lean、Comma 的 Rust Station、DeepSeek Harness 或另一套数据库作为此次升级的运行依赖。Worker 是逻辑执行角色，Celery Worker 是承载任务的进程，两者不是同一概念。

## 2 调研时的代码基线与缺口（本轮实施前）

以下判断以当前代码为准。历史验收记录只解释当时覆盖范围，不代替本升级的验收。

| 当前实现与代码入口 | 已有基础 | 本次需要补充 |
| --- | --- | --- |
| [TaskRecord / TaskService](../../backend/src/weora/modules/collaboration/application/tasks.py)、[任务表](../../backend/src/weora/modules/collaboration/infrastructure/tasks.py) | 保存目标、发起人、受众、原会话和项目快照 | Task 下的后续输入、生命周期与关联讨论；不能改写初始 instruction 充当聊天记录 |
| [RunService.create](../../backend/src/weora/modules/runs/application/service.py) | 原子创建 Task、Run、事件与调度记录，处理提交幂等 | 当前新提交会新建 Task；需要独立的 continue_task 用例，向已有 Task 排队输入或创建下一次尝试 |
| [RunModel](../../backend/src/weora/modules/runs/infrastructure/models.py) | 固定版本、执行配置、租约、取消；每 Task 最多一个非终态 Run | 保留该约束，独立任务并行；复杂任务并行以后使用子 Task，避免直接去掉唯一约束 |
| [RunExecutor](../../backend/src/weora/modules/runs/application/execution.py)、[ExecutionStore](../../backend/src/weora/modules/runs/infrastructure/execution_store.py) | CAS 领取、心跳、fencing、取消竞争、终态事务与只读恢复 | 将会话激活、持久输入与后台作业纳入租约；闲置等待释放执行资源 |
| [bootstrap/worker.py](../../backend/src/weora/bootstrap/worker.py) | Beat 补投、工具与审批对账、渠道收发、清理 | 当前维护与派发集中在一个入口；按职责拆成有界批次和独立 Celery 任务，避免一个慢来源阻塞全部维护 |
| [PiTextRuntime](../../backend/src/weora/modules/runs/infrastructure/pi_runtime.py)、[Pi runText](../../runtimes/pi/src/runtime.ts) | 双向工具调用协议、预算、取消、环境隔离 | 每次新建 Agent，以最终文本返回；缺少平台持久 transcript、运行中输入协议和能力发现 |
| [ModulesExecutionAccess](../../backend/src/weora/modules/runs/infrastructure/execution_access.py) | 固定 Skill、上下文快照、项目和工具证据装配；总材料有大小限制 | 当前偏向整体装配；需要摘要、引用、分页、上下文预算和会话重建 |
| [RunEventModel](../../backend/src/weora/modules/runs/infrastructure/models.py)、[事件 SSE](../../backend/src/weora/modules/runs/api/routes.py) | Run 内单调序号、状态事实与断点续读 | 事件没有模型消息或工具结果正文；不能直接据此重建模型历史，需受保护的 SessionJournal |
| [FeishuChannels](../../backend/src/weora/modules/context/channels.py)、[Receiver](../../backend/src/weora/modules/context/receiver.py) | 事件/消息去重、先落库后 ACK、显式 @、路由冻结、原会话 Outbox | 当前只把合格显式消息转为新 Run；需要标准执行事件、Thread、订阅、直通与语义路由 |
| [SqlContextStore](../../backend/src/weora/modules/context/store.py) | 工作空间隔离、行锁、JSON 文档、正文清理 | 持续调度的热点查询改为带唯一约束与索引的专表；不继续依靠全量 list 扫描 |
| [ToolGateway](../../backend/src/weora/modules/tools/application/gateway.py)、[工具存储](../../backend/src/weora/modules/tools/infrastructure/store.py) | Schema、权限、预算、调用去重、UNKNOWN 写结果禁止盲重放 | 新增异步 Job 回执、角色目录、完成事件及任务级累计预算 |
| [DeliveryService](../../backend/src/weora/modules/delivery/application/service.py) | 计划版本 CAS、精确审批、沙箱、命令、浏览器、分支/MR 与完成证据 | 让新协调方式复用这些用例；Run 更换不自动继承旧批准，后台作业绑定批准版本 |

当前 `bootstrap/worker.py` 在同一 Run 中按已批准计划选择 `console / sandbox / recovery` 工具范围。这是已有角色隔离基础，不能描述为已经存在独立 Router 与多个 Worker。当前执行配置也仍由装配层选择 Pi，并与单个运行配置校验；多 Runtime 不能仅靠增加一个下拉选项实现。

当前 `backend/src/weora/modules` 未见独立 knowledge 或长期 memory 模块。本方案保留两者的产品位置与接入点，不将其写成已经交付的实现。Skill 的固定版本材料装配已有代码，可直接复用。

## 3 上游能力的采用方式

参考时间为 2026-11-02。Super-agent 基线为 `6337fc823ce20251c8de8085948165c81aaa701e`；Comma 基线为 `66ab3dc92475005aea9afd123df36a28e4ef86ab`。下表是 Weora 的设计选择，不承诺照搬其数据库或源码。

| 参考能力 | 采用结论 | Weora 落点 |
| --- | --- | --- |
| Super-agent 的 Router / Worker | 采用职责划分 | 受范围约束的协调会话分派任务，现有 RunExecutor 承接 Worker |
| Trigger / Thread / Subscription | 采用事件身份与订阅机制 | channels 接入事实，collaboration 管理任务关系，runs 处理输入与激活 |
| 程序过滤、批处理、订阅直通 | 采用 | 先做确定性检查；已确定归属直接投递；新任务与歧义交给 Router |
| Mandate | 改造为 TaskBasis 和现有授权的组合 | 记录任务依据和可推进范围；Identity、来源授权、PlanScope 仍是执行校验依据 |
| Inline & Spill、链式召回 | 采用按需读取方式 | 统一上下文摘要、受保护引用和续读，复用 Context 与项目读取接口 |
| 持久副作用队列 | 扩展现有 Outbox | 调度、输入、唤醒与通知先入库；跨模块用幂等命令和补偿连接 |
| Comma 的 Task / Conversation / Session 分离 | 采用语义，保留 Weora 模型 | Task 保存目标、TaskInput 保存交流、Run 保存尝试、SessionJournal 保存执行上下文；不把 Task 表改成 Conversation |
| Comma Runtime 驱动与 Station 协议 | 采用协议设计 | RuntimeAdapter 与后续 LocalWorker；不引入其 Elixir/TypeScript/Rust 整套实现 |
| Comma Background Loop | 先采用声明式监听 | Observer 由 Beat 和短 Celery 任务运行，变化后产生事件；不开放模型生成 C 或任意常驻脚本 |
| Watch 与监督恢复 | 采用分层监督 | 基础设施检查、任务进展检查、受限的 Agent 诊断分别处理 |
| Comma 的 CAS、意图先持久化和结果对账 | 延续现有 fencing 并扩展 | 输入接收、工具调用、结果提交、外部交付分别记账 |

不采用上游的无审批宿主机执行方式，也不以提示词替代 Weora 的工具与受众检查。Comma 原始代码标注 AGPL-3.0-only，Super-agent 标注 MIT；本方案采用设计与行为契约，若以后引入源码，单独记录来源、许可证及复用边界。

## 4 执行链路与职责归属

新链路依次经过：渠道接入、事件归一化、准入、已有任务直通或 Router 判断、Task 输入与调度、Worker 激活、工具/作业、结构化报告和原渠道交付。各步骤均有持久记录，Redis 通知只是加速。

### 4.1 逻辑角色

| 角色 | 负责 | 不获得的权限 |
| --- | --- | --- |
| Router | 识别目标、判断归属、创建或续接 Task、读取必要摘要、提出问题、协调报告和对外交付 | 任意 Shell、批准自己的计划、跨受众汇总、任意选择用户身份 |
| ConsoleAgent | 在 Task 内调查、查询 RAG/上下文/记忆、提出计划 | 获批前执行写入 |
| SandboxAgent | 在有效计划下改码、验证、形成交付证据 | 扩大批准范围、借恢复状态重放未知写入 |
| 平台代码 | 准入、租约、授权、状态提交、预算、对账和资源分配 | 把模型的推断当作执行证据 |

ConsoleAgent 与 Router 不是简单改名关系：原 ConsoleAgent 的深入调查继续放在 Worker 内；跨任务协调抽到 Router。ConsoleAgent / SandboxAgent 首期仍可在同一 Worker Run 的不同阶段运行，工具范围由平台切换。

### 4.2 协调范围

Router 会话按 `workspace_id + audience_scope + project_scope` 建立。私有范围还包含发起人；群范围包含连接和受众标识。默认不创建整个企业共享的全局 Session。

两个范围即使属于同一项目，也不能自动交换正文。跨渠道关联使用明确 Task 引用和可见性检查；新渠道受众不能因为 Task 已存在就获得其旧上下文。无法安全共享时，只建立受保护关联或创建新的 Task，不自动把私有任务转成群任务。

### 4.3 现有模块中的落点

| 模块 | 升级后的唯一写入职责 |
| --- | --- |
| context | 来源授权、上下文快照、引用与内容保留期；提供已授权读取接口 |
| channels，按第一阶段实际接入建立 | 接入标准事件、渠道 Thread 元数据、订阅采集游标和消息交付；Feishu Reader 与凭据仍复用 context |
| collaboration | Task、TaskInput、TaskBasis、Subscription、TaskReport 与人工待办；不直接操作 runs ORM |
| runs | Router 协调激活、Worker Run、SessionJournal、检查点、执行租约和调度 Outbox |
| tools | ToolInvocation、异步 Job、调用预算和工具执行记录；沙箱实际执行继续委托 delivery |
| delivery | 计划与批准、沙箱证据、分支/MR、外部写入对账和交付契约校验 |
| bootstrap | 配置与依赖装配、Celery 入口；不继续积累路由领域规则 |

新增 channels 是现有规划领域的一次模块归位，不改变模块化单体。先建立端口并让旧 `FeishuChannels` 委托新用例；不在首批同时搬迁 ContextService、Reader、全部管理 API。

## 5 任务 运行与会话语义

### 5.1 Task 与 Run

Task 表示持续目标，Run 表示一次可追踪执行尝试。Task 的初始目标、发起人及历史快照保持；后续交流追加为 TaskInput，目标变更保存显式版本。

首期保留每 Task 一个非终态 Run。不同 Task 可以并行；同一复杂目标需要并行独立成果时，再拆成有父子关联的 Task，各自拥有 Run 和授权，不提前建设任意任务图。

新提交仍可走现有 `RunService.create`。新增 `continue_task`：读取任务权限、去重 TaskInput，发现活跃 Run 则排队；没有活跃 Run 时，在一个事务内创建新尝试与 dispatch。不得调用旧 create 再伪装成同一 Task。

已有 Run 的等待审批、补上下文和只读恢复继续保留其原语义，不把原地恢复事后改写成一次新 Run。真正的新尝试与“同一 Run 的新激活”分开记录。

### 5.2 Task 生命周期与可见阶段

Task 增加最小业务生命周期 `OPEN / COMPLETED / CANCELLED`，带版本号和变更事件。工作中、等待审批、等待用户、结果待看等界面阶段由 Run、待办和 Report 投影，不再维护第二套 RunStatus。

Worker 报告 `progress / waiting / ready / blocked / failed / dropped`，附带证据引用和可选问题。`ready` 不自动等于 Task COMPLETED。平台先校验交付契约；需要人类验收的目标等待对具体结果版本确认。执行成功、结果已发送、人类已验收分别记录。

COMPLETED Task 的后续明确请求可以显式 reopen 并创建新尝试；感谢、CI 和普通消息不自动 reopen。用户取消的 Task 不因订阅事件复活；再次工作必须有新的授权请求并由平台记录重开。

### 5.3 Router 协调会话

Router 使用持久 CoordinationSession 和有界 CoordinationTurn。每个 Turn 固定输入批次、策略版本、上下文序号和执行配置，提交结果前做版本 CAS。迟到判断不能覆盖新的目标或身份状态。

CoordinationTurn 属于 runs 的执行控制记录，不伪造一个用户 Task，也不占用某个 Worker Task 的唯一活跃 Run。模型调用继续使用中立 Runtime 端口；协调轮次的成本和审计关联 `coordination_session_id / turn_id`。

首期采用一个范围一把协调租约，每轮只做归属与分派。Worker 完成报告与新消息可以形成下一批，设置批次数量、字节数及最长等待时间；积压超过阈值降级为排队和限流，不能无限扩大上下文。

Router 返回结构化 `RoutingDecision`，包含事件 ID、动作、目标 Task、理由和预期版本；动作限定为 create、continue、context、ask、stop、ignore。应用层重新验证身份、归属和版本后提交。它不能直接传入已批准标记，也不能把模型给出的用户 ID 当作认证身份。

### 5.4 输入确认与控制动作

TaskInput 区分 `received / delivered / consumed / applied`。收到不等于模型已读，模型已读也不等于动作完成。消费必须关联已提交的 SessionJournal 序号；崩溃前未提交的消费可以重投。

TaskReport 的 waiting 或问题必须成为结构化结果，不能由报告自由文本推测。现有 `TextRuntime.run -> str` 与 `RunExecutor` 的文本成功路径通过兼容适配器保留；新协议使用带类别的 TurnOutcome，区分继续、等待、需要人类、完成和失败。等待或一次普通发言不会进入当前的 SUCCEEDED 路径。

| 输入 | 平台动作 |
| --- | --- |
| 同范围内补充证据 | 追加输入，在安全步骤边界读取，不修改原目标 |
| 询问状态 | 优先读结构化状态，必要时唤醒 Worker；不创建重复研发任务 |
| 更改目标、仓库、路径或动作 | 固定新目标版本；使不适用批准失效，停止新的写入派发，要求新计划 |
| 叫停 | 绑定具体 Task；停止后续调度，尽力取消作业，保留在途动作的不确定结果 |
| 不属于当前任务 | Worker handback，记录理由；当没有其他任务持有处理责任时回 Router |
| 订阅来源的普通讨论、机器人或 CI | 作为带来源的上下文，不因此获得新任务授权 |

多个 Task 订阅同一 Thread 时，可扇出上下文；一条可执行人类请求只指定一个处理归属。需要多项工作时，由 Router 记录拆分决定与子输入，防止每个 Worker 各自执行一次。

## 6 事件准入 订阅与主动执行

### 6.1 执行事件与路由

标准 ExecutionEvent 保存来源事件 ID、连接版本、外部对象、Thread、作者类型、发生/接收时间、来源权限引用、正文引用和信任类别。唯一键包含工作空间、来源连接和外部事件身份。相同身份不同正文按冲突处理；编辑、撤回是独立变更事件。

Thread 标识包含工作空间、渠道、连接、外部讨论对象；没有供应商话题时使用明确的根消息或平台任务引用，不把整个群默认当成一个任务。Thread 与 Task 多对多，Subscription 区分 `task_followup` 和 `context_only`。

路由顺序固定为：来源和连接检查 → 确定性去重/过滤 → Task 明确引用或有效订阅 → 归属明确则直通 → 其余事件进入 Router。即使直通，也要重验任务可见性、发起权限和 TaskBasis，不复制上游“订阅绕过全部准入”的行为。

正文中出现 Task ID、平台角色或“管理员批准”不构成可信身份。正文只提供关联候选，最终关联由平台查询和权限确认。Worker 回报和来源内容不能创建批准。

### 6.2 主动策略

每来源配置 `off / observe / auto`，显式请求独立处理。新主动规则先 observe，记录是否会创建任务、证据和原因，不执行写入；验证后可为该范围开启 auto。

规则固定版本、可处理事件、允许调查范围、成本与并发上限、通知方式和动作上限。匹配规则只是任务启动依据，不能代替 Delivery 的写入批准。首期自动任务只做已授权读取、调查与提案；已有明确策略或批准覆盖的动作以后按作用域逐步放开。

TaskBasis 保存显式请求或规则依据、原身份、来源范围、策略版本和目标版本。它用于说明“为何启动”，不成为另一套 RBAC。成员撤销、来源禁用和计划失效以当前事实为准，历史快照不能保留已撤销访问权。

### 6.3 Observer

用声明式 Observer 表达“观察哪个已授权对象、从什么游标继续、何时检查、哪些变化产生事件、何时停止”。首批支持飞书已绑定话题、已有代码评审和定时状态检查；代码托管接入通过 Weora 的 Provider 端口，按实际部署选择云效或 GitHub，不强迫迁移到 GitHub。

Beat 只扫描到期记录并投递短任务，Observer 不占着 Celery Worker 睡眠。游标推进和事件持久化同事务，供应商事件/轮询双通道按稳定 ID 去重。收到事件、生成输入、模型完成、消息送达分别记账。

Observer 有 owner、租约代次、检查点、截止时间、最大运行次数、重试和唤醒预算。暂停保留已接收输入，取消禁止新接收；未知远端写入进入对账，不由 Observer 重放。任务完成或取消后停止其专属监听，可明确配置只读保留订阅。

## 7 SessionJournal 与上下文组织

新增受权限保护的 SessionJournal，保存有序输入、有效模型消息、工具意图、结果引用、摘要/压缩边界和检查点；记录 schema_version、角色、AgentVersion、模型配置、来源依赖与受众。工具参数和正文引用采用加密或现有受控存储，遵守保留期。

现有 RunEvent 保留状态时间线与 SSE 的职责，新事件通过 `payload_ref` 或明确的对象 ID 关联保护数据，不把完整 transcript 和敏感材料推送到通用 SSE。Run 内序号不与 Session 序号混用。

持久化顺序：接收输入 → 提交模型可见消息及消费位置 → 调用模型 → 提交有效响应与工具意图 → 执行工具 → 提交结果 → 下一步骤。流式草稿不当作完成事实。响应提交失败时，不能据未提交的工具意图执行写入；未知模型请求可产生额外费用，不能宣称请求 exactly-once。

Pi 协议升级采用显式版本与能力协商，继续支持 v1 旧任务。先支持步骤边界的重建与续接；不承诺在一条模型请求中间迁移进程。运行中的新输入首先落库，在下一安全边界处理；具备 steer 能力的适配器可加速，但不能绕过持久确认。

上下文按来源权限过滤，再按预算提供：近期输入与当前目标直接内联，长内容给摘要、引用和续读入口。引用可以连接 Task、Thread、Run、Artifact 和快照；每次展开都重新授权，不能只凭可猜测 ID 读取。

RAG 和长期记忆由平台服务提供检索结果，携带来源、范围、版本、覆盖信息和引用。Router 的协调摘要只保存任务与决策关系，不充当企业知识库。自动写入长期记忆另走该模块的确认与治理规则；本升级不建立一个任意可写的“全局记忆”。压缩摘要继承来源限制与 TTL，撤权后需失效或重建，不能借摘要保留已清理正文。

## 8 异步作业与多 Runtime

### 8.1 持久 Job

长构建、浏览器和部分外部操作逐步由同步工具扩展为 `start / status / cancel / result`。start 返回已持久化 Job ID，只有资源实际启动后才记录 running；工具回执与命令退出码保持分离。

Job 保存 Task、Run、调用 ID、Runtime 会话、批准版本、执行者代次、截止时间、取消请求和结果引用。首批状态为 `QUEUED / RUNNING / SUCCEEDED / FAILED / CANCELLED / UNKNOWN`。取消请求先记录，不把“已请求取消”伪装成已停止。

Job 的发起复用 ToolGateway 的 Schema、reserve、授权和预算；实际执行前再次验证身份、租约/Job 代次与批准。Job 完成写结果和完成事件，再由持久投递唤醒正确会话。重复完成只结算一次，旧代次结果只用于对账，不能复活已取消任务。

当前 `GatewayModuleAccess` 与 `DeliveryService._approved` 依赖有效 Run token。U3 需要引入平台构造的 JobExecutionScope，固定 job_id、owner/代次、原 Task/Run、目标版本和批准版本，由独立校验端口验证。不能仅让 WAITING_JOB 的 Run 绕过 token 校验；旧同步调用继续使用原 ToolScope。

Run 增加 `WAITING_INPUT / WAITING_JOB` 时，同时修改状态约束、唯一活跃判断、维护查询、取消与 UI 契约。等待释放激活租约和计算槽位，Job 拥有独立有效租约；不能依赖一个已经释放的 RunLease 继续执行。等待仍占用 Task 的逻辑活跃 Run，普通新消息不会创建第二个活跃 Run。

当前 Worker finally 会清理沙箱。异步作业阶段必须先把资源所有权迁到 Job/环境租约并校验代次，再允许 Run 挂起；不能让旧 finally 删除后台作业仍在使用的容器。

| 执行场景 | Run 状态与资源处理 |
| --- | --- |
| 有待处理输入且尚未领取 | QUEUED，等待激活 |
| 已取得激活租约，模型或同步工具在执行 | RUNNING，持有激活租约 |
| 等待人类问题答案 | WAITING_INPUT，释放激活租约，保留 Task 活跃占位 |
| 等待已持久启动的后台操作 | WAITING_JOB，释放激活租约，Job 持有独立资源租约 |
| 等待上下文或审批 | 沿用 WAITING_CONTEXT / WAITING_APPROVAL，批准与续接按既有应用规则校验 |
| 收到有效答案或 Job 结果 | 同事务记录输入并将可恢复的等待态排队；重复通知不产生第二次激活 |
| 结果未知或资源所有权不可确认 | RECOVERY_REQUIRED；先对账，不自动重放写入 |

WAITING_JOB 中的人类跟进可在 Job 继续运行时唤醒该 Run；会话激活槽位和后台资源配额分别计算。一个 Task 内仍然只有一个有效模型激活，不能让 Job 完成和人类输入各自启动一个模型进程。

### 8.2 Runtime 契约

| 能力 | 中立语义 |
| --- | --- |
| capabilities | 是否支持持久会话、步骤续接、steer、取消、流式事件、后台作业、原生工具及环境约束 |
| start / resume | 固定 AgentVersion、执行配置、Session 检查点、输入位置与权限引用 |
| deliver | 使用稳定 input_id；durable_ack 只表示已持久接收 |
| events | 规范化运行事件，包含 event_id、session_seq、activation_id 和代次 |
| cancel | 先记录请求，再分别报告本地停止与远端未知状态 |
| checkpoint / inspect | 返回恢复所需的受保护引用与可验证状态，不泄漏供应商对象到业务表 |

先通过现有 Pi v1 兼容适配器承接 TextRuntime，再实现 Pi 的持久步骤协议；随后接入第二种真实 Runtime，优先验证 Codex app-server，之后 Claude Code。当前全局 Pi 配置装配改为按冻结 profile 解析的运行时注册表；恢复时验证批准版本和能力兼容，不能静默换模型或供应商。

U2 引入协议 v2 的日志与检查点消息，U3 在协商出的能力上增加运行中输入和异步作业事件。工具目录按角色过滤只是减少误调用，Gateway 的 invoke 端仍必须根据可信角色逐项判定；不能仅靠提示词或把工具从目录隐藏来隔离 Router。

Codex/Claude 的原生 Shell 不当然经过 Python Gateway。仅在受管沙箱内、满足平台限制与工具审计的配置下启用写任务；适配器不能证明边界时只开放明确受限能力，不对产品宣称与 Pi 受控工具模式具有同等隔离。

### 8.3 后续 LocalWorker

LocalWorker 以独立 Python 执行进程接入，不要求把 Weora 后端换成 Station 的技术栈。注册 Worker 身份、能力、环境和版本，使用短时且绑定工作空间/设备/任务/代次的凭证；通过主动出站连接、心跳、输入 ACK 与结果回传运行。

任务位置在接收时固定，断连不自动换机器重做写操作。已授权范围、凭据引用和受众随输入绑定；不能继承整台个人电脑的 HOME、登录态和任意文件权限。首个验证场景使用隔离目录和只读任务，写入仍经批准及隔离执行。

## 9 持久化 接口与并发规则

### 9.1 按阶段增加的记录

以下是建议逻辑记录，实施时按真实查询确定表名与拆分，不一次性创建空模块或通用事件总线。

| 阶段 | 记录 | 关键约束 |
| --- | --- | --- |
| U1 | execution_events、source_threads | 工作空间/连接/来源事件唯一；Thread 来源身份唯一；事件正文冲突可审计 |
| U1 | task_inputs、task_subscriptions、task_basis | 输入与目标 Task 去重；订阅用途明确；TaskBasis 固定目标版本 |
| U1 | Task 生命周期、权限查询与 continuation 命令 | Task/Run/输入/dispatch 同事务；保留 active Run 唯一索引 |
| U2 | coordination_sessions、coordination_turns、routing_decisions | 范围唯一、输入归属唯一、策略版本与会话版本 CAS |
| U2 | session_journal、session_checkpoints | 会话内序号唯一、消费位置单调、内容可清理且引用可失效 |
| U2 | task_reports、attention_requests | 报告绑定具体 Run/版本；问题去重；回答显式指向问题 |
| U3 | tool_jobs、job_events、execution_effects | 调用身份唯一；代次与租约；意图/副作用投递同事务 |
| U4 | observers、observer_occurrences | 对象/配置版本/触发身份去重；游标与事件原子推进 |
| U5 | local_workers、worker_assignments | 工作空间隔离、连接代次、任务位置固定与结果 fencing |

新热点记录使用类型化关系表及索引；不将 task_inputs 或 Job 堆进 `context_documents.payload`。历史 context 文档按稳定键幂等导入或通过兼容读取连接，不进行无身份的正文全量复制。

执行副作用表只接管输入投递、唤醒、启动和通知等新增可靠执行需求。现有 `run_dispatch_outbox`、渠道回复 Outbox、Delivery 远端账本保留各自 owner；通过稳定业务键连接，不在第一阶段全部迁进“大一统 Outbox”。

### 9.2 最小接口增量

- `POST /workspaces/{workspace_id}/tasks/{task_id}/inputs`：追加消息或控制意图，携带幂等键与可选 expected_task_version。
- `GET /workspaces/{workspace_id}/tasks/{task_id}`：返回目标、版本、阶段、Runs、待办与交付状态，正文仍经 TaskAccessService 校验。
- `POST /workspaces/{workspace_id}/tasks/{task_id}/stop`：记录用户取消目标及传播进度。
- `POST /workspaces/{workspace_id}/attention/{attention_id}/answer`：回答确定的问题版本，不能替代审批 API。
- 后续增加 Job 查询/取消、Observer 配置与 observe 结果、本地 Worker 管理端点。

沿用 `/api/v1` 前缀与项目认证/CSRF/分页约定。后台入口、Router 工具和 HTTP 共用应用用例，不能各自实现权限。新增任务流只暴露脱敏投影，复用现有 SSE 序号续读思想，不合并跨范围正文流。

### 9.3 并发与预算

Task 更新和 Run 激活分别做 CAS；同一协调范围同一时刻只有一个有效 RouterTurn。输入按 Task 顺序消费，过期租约不得提交；一个范围失败不阻塞其他范围。批量路由逐项返回结果，失败项重试不重做成功项。

使用数据库租约记录限制工作空间、项目与身份的并发；不能只靠 Celery concurrency 或 Redis 锁。人的跟进在受限保留配额内优先于新主动任务，并设置公平队列，避免持续人类输入使后台任务永久饥饿。

模型预算、整体执行预算沿用 ADR 0012。另累计 Task 总成本、Router 轮次、Job 数、Observer 唤醒量和无进展次数；新 Run 或重启不自动清零 Task 预算。具体阈值由试点基线决定，不能由模型自行提高。

## 10 恢复 交付与监督

| 故障窗口 | 恢复动作 |
| --- | --- |
| 入站已落库，未创建输入 | 按事件身份补投，不改变已冻结来源和身份 |
| TaskInput 已提交，Celery 通知丢失 | 扫描未应用输入并补投，激活 CAS 去重 |
| 模型开始前或响应提交前崩溃 | 从已提交日志恢复；未提交的模型输出不授予工具执行权 |
| Job 已记录，启动结果未知 | 根据受控 execution_id 核对，无法确认则 UNKNOWN；不重新启动写命令 |
| 外部操作成功，本地结果未提交 | 使用稳定业务键回查；缺少安全回查能力则 RECOVERY_REQUIRED |
| Run 已成功，原渠道回复未知 | 保持执行结果，单独显示投递待核对；不重新运行 Agent 生成第二份交付 |
| 审批等待时收到改向或撤权 | 新版本使旧范围失效；取消待执行动作；在途动作保留结果与不确定性 |
| 设备离线或晚到结果 | 任务等待原环境或显式改派；旧代次不得写新状态 |
| 上下文撤权或过期 | 停止后续访问，清理正文、摘要与派生缓存；保留最小关联审计 |

渠道发送只对明确未发送的失败做自动退避；“已尝试但结果未知”继续遵守现有保守对账语义。原渠道原始 ID 和受众由平台保存，Router 不能根据正文自行选择发送目标。

监督分三层：Beat 检查租约、队列、Job 与连接；任务规则检查长时间无输入消费、重复同类工具结果、无新证据的反复推理；必要时启动只读诊断 Agent。诊断只能给出建议或执行预先批准的恢复操作，不能修改系统提示词、扩大权限、自动重放未知写入。用户主动停止有持久标记，监督不能将其恢复。

以 `workspace_id / task_id / run_id / session_id / activation_id / job_id / event_id / request_id` 串联追踪。指标记录入站到归属、归属到启动、等待决定时间、Job 耗时、重复抑制、过期租约拒绝、恢复成功、投递状态和成本；普通日志不保存敏感正文。对正常运行和无事监听保持安静，仅通知结果、阻塞和需要人的决定。

## 11 分阶段实施与验收

每阶段交付可运行纵向链路，不要求先完成下一阶段。测试先复用现有测试框架和夹具，新增真实不变量用例；不把文档中的通过数字当成本轮结果。

| 阶段 | 具体改动 | 可独立验收的结果 |
| --- | --- | --- |
| U1 任务续接与确定性归属 | channels 事件入口；TaskInput、订阅、TaskAccessService、continue_task；保留旧 /runs 接口 | 明确引用同 Task 的跟进归入该任务，独立新请求仍建新 Task；重复事件只处理一次；活跃 Run 排队；终态后明确续接创建新 Run；撤权和叫停有效 |
| U2 双层协调与持久上下文 | RouterTurn、结构化路由决定、SessionJournal、摘要读取、Worker 报告与人工问题 | 同范围多个独立任务并发，后续消息直通；歧义返回 Router；重启后未消费输入可继续；Router 没有研发写工具 |
| U3 异步作业与运行中输入 | Job、完成事件、Pi 协议增量、等待状态、资源所有权、优先配额 | 构建仍在后台时可接收改向和叫停；挂起不清理活跃 Job 容器；重复完成不重复推进；未知写入不重放 |
| U4 主动监听与监督 | 声明式 Observer、off/observe/auto、规则版本、Task 预算与停滞诊断 | 观察规则不产生外部写入；auto 范围内变化唤醒正确任务；无变化不调用主模型；暂停/停止/重启不丢已接收事件 |
| U5 多 Runtime 与本地执行 | 冻结 profile 解析器、能力发现、Codex、Claude、本地 Worker | 相同任务契约在 Pi 和第二 Runtime 通过；能力不足明确拒绝；设备重连续投；未知接收和执行结果不换机器重做 |

U1 首批代码位置：`collaboration/application/tasks.py`、`collaboration/infrastructure/tasks.py`、新增 channels 应用入口、`runs/application/service.py`、`runs/infrastructure/unit_of_work.py`、`bootstrap/worker.py`。先实现 continue_task 的事务路径和事件唯一约束，再接入飞书订阅；不先把旧 Receiver 大范围搬家。

U2 首批继续使用 Pi，不绑定新增供应商；独立 RouterTurn 可以跨 Task 协调，Worker 继续调用现有审批和交付用例。U3 才承诺同任务的后台作业与运行中消息续接，U1/U2 不把“已排队”展示为“模型已经处理”。

### 11.1 必须覆盖的自动回归

1. 同事件重投、同键异正文、两个事务同时续接 Task、Task/Run/dispatch 任一步失败整体回滚。
2. 同 Task 的后续输入不创建第二个活跃 Run；多个 Task 并行；人工跟进与后台唤醒公平竞争。
3. 订阅消息不扩权；未绑定作者不能发起执行；跨私有/群范围引用不泄露正文；输出重新校验受众。
4. stale RouterTurn、旧策略判断、过期 Worker/Job 代次、取消与成功竞争均不能覆盖新状态。
5. SessionJournal 消费确认、响应意图提交、Job 启动与结果提交的每个崩溃窗口可恢复；未知写入保持 UNKNOWN。
6. 旧审批不因目标变更或新 Run 自动继承；后台命令必须校验最新批准版本与连接指纹。
7. Pi 旧协议、旧 Run 状态/SSE、老幂等键保持兼容；模型预算和整体预算继续分开。
8. Observer 游标/事件同事务、重启重复事件、唤醒预算、暂停/取消和 TTL；摘要不绕过正文清理。

基础回归入口包括 `tests/api/test_runs.py`、`test_run_execution.py`、`test_run_recovery.py`、`test_tool_gateway.py`、`test_run_stream.py`，以及 `tests/bootstrap/test_worker_lifecycle.py`、`tests/context/`、`tests/delivery/`、`tests/infrastructure/test_pi_runtime.py` 和 `runtimes/pi/src/runtime.test.ts`。以实际依赖选择受影响检查，新增并发约束需要真实 PostgreSQL 验证，不能只看 SQLite。

### 11.2 真实闭环与评估

先在隔离环境完成：飞书新事件或 Console 请求 → 已有 Task 续接 → Router/Worker → 计划审批 → 长构建 → 中途补充 → 交付 → 原渠道回报；期间强杀一个执行进程、丢弃一个队列通知并重投一个输入，核对结果与外部副作用。

人工补收可验证后半链路，但与自动入站分别记账。模型和真实渠道可失败，失败也须有可解释的终态或等待状态；不能靠改数据库状态拼接成功。

固定一套可重放负载，包含无关消息、新目标、同任务跟进、取消、审批变化和工具完成。先用 FakeRuntime 验证调度，再用真实模型测试判断；同时比较归属准确率、重复任务数、人类打断次数、任务完成时间和总成本。每次记录版本、负载、配置和外部依赖，不把模拟调度提升解释成代码质量提升。

## 12 迁移 发布与回退

1. Alembic 先加新表和可空字段，旧模式继续读写。新旧执行模式对每个 Task 显式固定，不能对同一事件双写两套调度路径。
2. 试点按工作空间与来源打开 `continuous_execution`。旧 Task 默认 legacy；历史 Run、批准和幂等指纹保持原义，不批量推断历史话题归属。
3. TaskAccessService 初期沿用现有发起人/来源权限。开放同群其他成员跟进时，通过明确的参与者/来源授权查询实现，不能删掉 created_by 条件就变成全工作空间共享；原复合外键保留发起人归属意义。
4. 在 U2 后逐步把旧 FeishuChannels 迁为兼容门面；只有新入口验证通过才停止旧模式。迁移任务使用稳定业务键、分批读取和核对数量，不重复发送历史消息。
5. U3 的新等待状态与协议字段需要先完成兼容发布。旧 Worker 不认识新状态或协议时，不能领取 continuous 任务；派发包含 execution_mode 和 protocol_version。
6. 回退先关闭新主动接收和新任务派发，保留现有写入账本，等待或停止活跃 Job 并对账。保留能读取新记录的兼容版本，不把新任务直接交给旧二进制。
7. 关闭升级功能不会自动撤销已发生的远端动作。需要回滚数据库时必须针对实际目标备份并验证恢复；有新历史后不将 downgrade 当作无损撤销。

2026-11-03 已开始 U1 代码实施，迁移只在隔离测试数据库验证，未应用到现有运行环境。整体规划的模式开关、订阅和协议迁移尚未实施；本轮增量及发布边界见第 14 节。历史验收证据保持原事实。

## 13 参考证据

- 本地：[工程边界](../../AGENTS.md)、[当前研发执行设计](agent-platform-and-feishu-context.md)、[ADR 0002 Runtime 边界](../adr/0002-agent-version-and-runtime-adapter-boundary.md)、[ADR 0012 执行预算](../adr/0012-model-and-execution-budgets.md)、[实现进度](../development/weora-v0.1-progress.md)。
- Super-agent：[领域模型](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/001-domain.md)、[执行规则](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/003-execution.md)、[核心端口](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/ports.ts)。
- Comma：[运行契约](https://github.com/AFK-surf/Comma/blob/66ab3dc92475005aea9afd123df36a28e4ef86ab/docs/agent-runtime.md)、[任务与后台 Loop](https://github.com/AFK-surf/Comma/blob/66ab3dc92475005aea9afd123df36a28e4ef86ab/docs/salix/tasks-background-execution.md)、[Runtime 接口](https://github.com/AFK-surf/Comma/blob/66ab3dc92475005aea9afd123df36a28e4ef86ab/systems/station/src/agents/runtime.ts)、[Station 当前范围](https://github.com/AFK-surf/Comma/blob/66ab3dc92475005aea9afd123df36a28e4ef86ab/systems/station/README.md)。

上游提供设计依据，执行规则以 Weora 的权限、批准、数据模型及测试为准。运行结果以各阶段实际验收为准。

## 14 第一轮实施：持久任务续接（2026-11-03）

本轮按用户确认的顺序推进：先关联飞书线程与 Task，再持久保存后续输入及归属，在同一 Task 下创建新 Run 并交给 Pi 必要历史。运行中立即插话以后按实际需要实施，不作为这轮交付条件。[ADR 0015](../adr/0015-task-continuations.md) 固定本轮决策，验收记录（内部记录已移除） 区分测试和真实外部联调。

### 14.1 消息归属与调度

- `task_threads` 保存来源消息、话题和 Task 的关联。明确引用或相同话题归入已有 Task；没有关联则建新 Task。`/new `、`新任务：` 或 `新任务:` 显式开新任务，不依赖模型猜测。已有话题中显式新建的任务使用独立消息关联，后续应引用该消息继续；不会把原话题整体改绑到新任务。
- 关联范围包括工作空间、发起人、连接、群、来源、Agent 发布版本及项目分享授权。相同群里的不同作者不会自动共用 Task；更换路由范围会新建 Task。已存在的历史任务不批量推断线程关联。
- `task_inputs` 先持久接收输入，保存幂等键、顺序、处理状态和后续 Run ID。每 Task 最多 32 条未处理输入；回执列表窗口为 100 条，优先包含待处理输入。
- 同一 Task 继续保持最多一个非终态 Run。运行中、等待审批或恢复决定时，跟进为 `PENDING`。原 Run 终结后由现有 Beat 扫描，逐条创建新 Run；Run、dispatch 和输入关联同事务提交，启动失败保留输入重试。
- 叫停前已排队的输入会被拒绝，不自动唤醒已取消的执行。叫停后用户明确提交的新跟进可以新开 Run。本轮没有新增永久关闭 Task 的状态机。
- 本轮仍要求飞书显式 `@Weora`，没有开放普通消息自动运行、全话题订阅或语义 Router。后台恢复读取持久队列，不依赖常驻模型或内存会话。

### 14.2 Pi 历史与审批边界

Pi 保持 v1 协议和每 Run 一个 Agent 实例。新 Run 的输入包含本轮请求、初始目标和近期成功轮次的问答；这解除的是平台“一次消息必须新建 Task”的限制，不是 Pi 原生会话检查点恢复。

历史查询最近 8 个 Run，按时间选择其中先于当前 Run 的成功结果；每轮请求最多 2,000 字符、回答最多 4,000 字符，历史数组最多 24,000 UTF-8 字节，超限去掉最旧轮次并标记窗口内省略数。初始目标最多 4,000 字符。该窗口不是完整消息日志，也不承诺引用所有历史。每份历史重新校验来源权限和保留期。

长话题不要求无限扩大自动补齐：只有持久 TaskInput 已关联当前 Run、且存在重新授权通过的同 Task 成功历史时，外部快照仅因条数/材料预算或分页截断才允许继续。原 10 条上限、材料预算及 `partial` 覆盖事实保留，模型明确看到“外部话题仅部分可见”，需要缺失证据时继续查询或澄清。新任务、历史过期、引用读取失败或不支持的内容仍等待上下文；不得以预算错误覆盖其他缺口。新鲜快照和缓存快照执行相同规则，旧批准不因该规则自动继承。

Task 原始目标与项目快照保持不变，Run 视图使用本轮输入与本轮消息来源；飞书结果回到这轮输入对应消息。新 Run 固定原发布版本和执行配置，不复制旧计划、批准、沙箱或工具回执。后续写入仍需本 Run 的计划和人类批准；排队补充不修改当前已批准计划。

### 14.3 接口与发布

- `POST /api/v1/workspaces/{workspace_id}/runs/{run_id}/continue`：沿用认证、CSRF 和 `Idempotency-Key`；正文为 `{"instruction":"继续处理的要求"}`。返回 202，包含 `input_id / task_id / sequence / status / run_id`；`run_id=null` 表示等待，不能展示为模型已读。
- `GET /api/v1/workspaces/{workspace_id}/runs/{run_id}/inputs`：查询同 Task 的有界回执列表，仅含关联与状态，不返回消息正文。本轮未新增 Console 跟进界面。
- Alembic `20261103_0016` 增加消息序号、输入表与线程表。已有输入时禁止直接 downgrade，避免丢失已接收消息。旧 Task/Run、审批和幂等指纹保持；旧线程没有关联时首次新消息会建立新 Task。
- 本增量没有引入总体规划中的 `continuous_execution` 模式开关；升级后沿用现有显式入口使用上述确定性规则。发布须先备份、执行迁移并协调 API/Worker/Beat 版本，避免旧 Worker 在新队列运行期间混用。回退应保留新表及可读取输入的兼容版本。

以上为任务续接增量完成时的边界；后续主 Agent 增量以第 15 节为准。

## 15 内置 Super-agent 增量（2026-11-03）

按 DeepSeek Super-agent 固定 commit 的实际源码及测试补入 `coordination` 模块：候选准入、结构化评估、显式路由、Task 管理、Worker 派发、报告、人类问题、状态请求、退回、订阅、停止、Outbox、游标补偿和持久会话。源码到行为的对照见 [移植清单](superagent-source-port.md)，实际测试及限制见 [本轮验收](../development/superagent-acceptance-20261103.md)。此节替代第 14 节“尚无主 Agent / 普通消息不能接单 / Pi 仅 v1”的阶段描述。

主 Agent 是用户的协调入口；已有任务的跟进优先直达原 Worker，避免每条消息都重新调用主 Agent。Python 校验模式、规则、证据、策略版本及来源权限，事务内保存接单和待派发记录；Celery 唤醒 Pi 会话执行。主 Agent 不获得工程写工具或审批权，Worker 每个原生 Run 继续走现有固定版本、Gateway、精确计划、人工批准及结果未知对账。

Pi v2 保存真实 Agent 历史，并在活跃模型/工具循环中支持 steer、已接收和已读回执；v1 仍供旧入口使用。会话为逻辑持续会话，Node 子进程可按 Run 退出，重启从平台日志恢复，既有工具不因恢复被重新执行。Pi 运行时根据模型上下文窗口使用 Pi Coding Agent SDK 生成摘要，平台随完整日志保存摘要和压缩位置。恢复时 Pi 将摘要与后续消息传给模型，原始历史继续用于审计。

Comma 的任务与执行尝试分离、持久输入、并发、人工优先与安全恢复原则已落到 Task/Assignment/Run；没有引入它的 Rust Station。总体路线中的独立后台 Job、其他 Coding Runtime 和统一多客户端交互仍需各自实施；它们不属于复刻 Super-agent 本次所必需的外部运行依赖。已有 RAG、记忆、Skill 规划及模块边界保持，不能把本轮接单能力描述成已完成所有平台产品能力。
