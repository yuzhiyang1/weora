# Weora 与 Super-agent 实现差异复核

> 本文保留修复前审计证据。2026-11-04 用户授权后的修复、测试、本地部署和保留差异见 [最终验收](superagent-gap-acceptance-20261104.md)，不要把本文的历史缺口当作当前代码仍未修改。

- 日期：2026-11-04。
- 结论：主 Agent → 结构化决策 → Worker → 报告与回复的协调链路已经接通；**尚未完整实现固定版本 Super-agent 的执行能力与生命周期规则**。此前的 24 项清单和通过测试不足以证明完整对齐。
- 用户目标：在 Weora 中实现 Super-agent；保持现有技术栈、平台边界和审批机制，Git 仓库使用可配置项目。开发阶段允许调整早期产品目标，不能再用“不监听普通消息”等旧约定缩小范围。
- 本轮操作：源码对照、内存隔离复现和文档订正。没有修复下列生产代码，没有重启服务，没有向飞书发送消息，没有执行 Git 暂存、提交或切分支。

## 1. 基线与证据保存

上游固定为 [`deepseek-ai/dsh-superagent-prototype@59e6c4059d513a5a63c482aee884c95e4ea89fa0`](https://github.com/deepseek-ai/dsh-superagent-prototype/tree/59e6c4059d513a5a63c482aee884c95e4ea89fa0)，沿用最初分析基线，不将其他版本或文章愿景混进当前实现。上游 README 明确说明公开原型与文章并非完全相同；本表比较实际源码。

本轮重新下载完整固定版本，共 216 个文件。此前本地参考副本只有 38 个文件；忽略 UTF-8 BOM、CRLF 和末尾换行后，这 38 个文件与本轮完整源码内容一致。因此，这次发现的主要问题是此前覆盖范围与行为验证不足，不能归因于上游版本变化。

证据保存在仓库忽略目录 `output/superagent-audit-20261104/`：

| 文件 | 用途 |
| --- | --- |
| `upstream/` | 固定 commit 的完整源码；只读取，没有执行安装或部署脚本 |
| `upstream-manifest.json` | 216 个文件的 SHA-256、Git blob，以及下载归档 SHA-256 |
| `github-files.json` | GitHub 连接器读取的根目录、src 和 tests 顶层文件 Git blob |
| `weora-manifest.json` | 本轮审查的协调模块与关键运行适配器的文件指纹，以及 Git HEAD；工作区存在未提交实现，不能仅凭 HEAD 复用结论 |
| `probe_parity.py` / `probe-results.json` | 六个内存复现及其实际结果 |

归档 SHA-256：`2aa9fadd7ef75c5f58ea2913943363907236beb104d7d0dc49a9029555355377`。下载内容与 GitHub 返回的 77 个文件 Git blob 全部一致。这里的 77 是独立抽核文件数，不表示只下载了 77 个文件，也不表示已逐行审阅全部 216 个文件。

以下路径中，上游路径相对 `upstream/`，Weora 路径相对仓库根；行号对应本次文件指纹。核心领域、Main/Worker 工具和执行调度做了行为对照，Source、模型、面板等按对应入口定向核查。没有运行上游 DSH 集成测试，也没有重新声称全量等价。

## 2. 已复现的行为差异

这里的“已复现”表示运行了当前 Weora 的真实领域/工具代码，并对照上游实现及测试判定差异；不是运行了两套真实飞书环境。

### A01：blocked / failed 后，自动备注仍会唤醒 Worker（P1）

- 上游：`src/core/model.ts:345-352` 的 `waitsForPerson` 判断最新报告及其后的人类输入；`src/core/tickets.ts:234-244` 将自动备注和机器人消息保留为事实，不加入待投递、不唤醒；`src/core/assignments.ts:126` 还清除阻塞时已有的自动待投递项。
- Weora：`backend/src/weora/modules/coordination/engine.py:171-192` 的 `_wake` 只检查暂停、人类停止、安排结束和 attention，没有检查 blocked/failed；`note:574-583` 会追加 undelivered 并唤醒。工具说明虽然写“等待人类时不唤醒”，实现只覆盖了 attention，未覆盖无问题文本的 blocked/failed。
- 复现：先结算原 worker effect，再报告 blocked 或 failed，然后追加备注，两次均把 effect 从 `done` 改回 `pending`，generation 增加。
- 影响：标记需要人工处理的任务可能被自动事实再次派发；这是重新排队的确认结果，不等于已观察到审批被绕过。
- 对齐条件：实现明确的等待人类规则；自动事实继续落库；人类有效输入、明确 retry 或后续非阻塞报告按上游条件解除；覆盖阻塞前已有自动待办和阻塞后新增待办。

### A02：任务结束时，未处理跟进没有按上游释放并退回 Eval（P1）

- 上游：`src/core/tickets.ts:280-307,338-362` 根据投递、readBefore、ack、answer、handback 和既有 Decision 判断哪些输入尚未接住；结束时释放所有权，未被其他 Ticket 决定的有效人类消息返回 Eval。
- Weora：`engine.py:694-704` 对 ready 时的未读消息新建同 Ticket 的 Assignment；对 dropped 直接结束。两种情况都没有等价的 letGo/untaken 处理。
- 复现：初始请求标为已读，再接收未读跟进。dropped 后 Ticket 已结束，跟进仍保留原 fanout，没有 Eval；ready 后同 Ticket 增至两个 Assignment，跟进仍没有 Eval。
- 影响：dropped 的消息可能停在已结束任务；ready 则直接假设晚到内容属于原工作，省略上游重新判断其归属的步骤。
- 对齐条件：按上游所有权与接住语义处理晚到输入，包括已读时间屏障、确认/回答过的输入、context-only、其他 owner 和停止来源消息。不能仅把所有未读内容一律重开原 Worker。

### A03：批量路由的参数错误仍会拒绝整批（P2）

- 上游：`src/app/tools/main.ts:44-81,128-150` 刻意把路由项形状与业务字段校验分开，每项校验、独立执行和返回错误；`tests/tools.spec.ts:136-185` 覆盖字段缺失、超限和一项失败不影响其他项。
- Weora：`backend/src/weora/modules/coordination/tools.py:59-72,114` 将每项 reason 等约束放入整个数组 schema；`service.py:441-443` 在进入逐项事务前校验整批。
- 复现：一个合法 ignore 与一个 reason 为空、缺少 ticket_id 的 attach 混合调用，返回整批 `INVALID_TOOL_PARAMETERS`，没有逐项结果。
- 对齐条件：保持顶层结构有界、拒绝非法工具名；将各路由类型的业务必填和长度校验放到项内，合法项仍执行。保留事务、租约和权限校验。

### A04：主 Agent 的取消线程订阅被错误套用了 Worker 限制（P2）

- 上游：`src/app/tools/main.ts:239-245` 的主会话工具可移除线程上的所有 Ticket 订阅；`src/core/tickets.ts:214-227` 为相应领域操作。Worker 的“自己的原线程不能判无关”是另一条规则。
- Weora：Main 工具要求 ticket_id，并调用 `Engine.unsubscribe`；`engine.py:541-549` 一律拒绝 own thread。Main 与 Worker 缺少对应的角色差异。
- 复现：对接单来源线程执行现有取消订阅用例，返回 `OWN_THREAD_REQUIRED`。
- 对齐条件：分开人类授权的 Main 取消订阅和 Worker 判无关用例；Main 可按范围移除订阅，Worker 保持限制。普通观察内容不能冒充人类管理请求。

## 3. 静态确认的能力缺口与语义差异

以下项目存在明确源码落点差异，但本轮没有用真实模型、代码托管和长时进程重现端到端结果。它们不能登记为“已通过验收”。

| ID / 优先级 | 上游实际能力与出处 | Weora 当前落点与差异 | 对齐验收条件 |
| --- | --- | --- | --- |
| B01 / P1 | `src/executor/background.ts` 与 `dsh.ts:584-670` 管理后台 Shell 作业和 Worker 子 Agent；回合结束仍有后台工作时保留槽位，完成后唤醒；超时、handoff、安排结束和停机有不同清理规则。`docs/003-execution.md:27-45` 说明 5 秒转后台、30 分钟后台限制、停机等待等 | `coordination/worker_runtime.py` 和既有 Delivery 工具没有对应 Job/子会话注册、结果通知、后代回收和槽位状态。Celery 并行执行多个 Run、Pi 运行中追加消息均不能替代这组生命周期能力 | 通过既有 Gateway/审批启动有界 Job/子执行会话；覆盖完成唤醒、保留槽、handoff、超时、取消后代、停机与恢复，不直接引入上游免审批 Shell |
| B02 / P1 | `src/sources/github.ts`、`github-trigger.ts`、`github-pull.ts` 将 Issue、PR、行内评论、无正文的评审决定、CI、关闭和重开转成 Trigger/线程事实；`core/assignments.ts:128-132` 根据 followsDone 决定 ready 是否结束 | Weora 有配置化 Projects/CodeSource，以及 Delivery 的推送、评审创建和对账；当前协调入口主要接飞书，没有对应代码托管事件 Source 与 Ticket follows 的 PR/CI 生命周期。`engine.py:694-696` ready 直接结束，未等待所跟进评审线程 | 根据配置的 CodeSource 接入事件和轮询游标；CI/评审结果路由给原 Worker；关闭/合并/重开与晚到人类输入共同决定状态。GitHub 专有逻辑放适配器，其他托管平台明确支持范围；推送和合并仍走审批 |
| B03 / P1 | `src/executor/dsh.ts:237-259,435-461` 为 Ticket 准备目录和恢复 Worker Session；目录、工作上下文跨 Assignment 延续 | Weora 的 Pi 会话消息可以恢复，但 Docker 环境按 Run 命名（`delivery/infrastructure/docker.py:132-134`），`bootstrap/worker.py:196-199` 清理当前 Run 环境。没有等价的 Task 持续工作区或未提交工作恢复契约 | 区分模型历史与工作文件；在隔离、权限和新 Run 审批不变的前提下保存/恢复 Task 工作区或受校验补丁。证明失败、等待审批、续接后可以继续已有修改，并防止跨 Task 污染 |
| B04 / P2 | `src/executor/dsh.ts:30,545-575` 对明确瞬时模型/网络故障最多重试 3 次，15 秒起指数退避；`docs/003-execution.md:11` 规定输入投递失败退避；模型重试状态由 `app/retry-health.ts` / `sessions.ts` 暴露 | `runtimes/pi/src/runtime.ts:225-226` 设置 maxRetries=0；`bootstrap/coordination.py:118-154` 只对 deferred、租约和 poll 等分支退避，普通 Main/Worker 错误结算为失败。`service.py:906-930` 将 Worker effect 失败记为 failed。不存在等价的瞬时模型错误分类与重试状态 | 区分确定未投递、纯模型请求失败和已执行外部写的未知结果；前两类有界重试、退避、暂停/停止感知和健康状态；未知外部写保持对账，不能借此重放写工具 |
| B05 / P1 | `src/executor/dsh.ts:657-669` 空闲且没有完成报告时报告 waiting；`core/assignments.ts:128-132` 的 ready 还受所跟进线程状态约束 | `coordination/service.py:818-831` 默认将原生 Run 的 SUCCEEDED 投影为 ready；即使只报 progress，未另带 human_action 时也可能落到默认 ready。原生模型循环成功与持续 Ticket 目标完成被合并；有明确 waiting 报告时可以保留等待 | 单独建模回合成功、待新输入、等待人类/评审、目标完成。明确报告和平台交付验收共同决定完成，不能把任意正常返回都等价为 Ticket 完成；增加无报告、progress、晚到输入和评审等待用例 |
| B06 / P2 | `src/sources/admission-context.ts:5-40` 构建线程前 5 条及触发消息前后各 5 条，并标明省略和不完整；`app/assessment.ts:138-204` 缓存初始窗口，支持继续读取 | Weora `coordination/service.py:552,602-609` 将截止设为触发时间 +1ms；`context/service.py:363-410` 从该截止前 1 小时读取、每页最多 20 条。有分页和受限引用，但不是同一初始窗口；触发后已有的消息被截止排除，跨日线程可能缺关键邻居 | 构建与触发位置相关的有界窗口，标注缺口和下一步读取；测试长线程、跨日、根消息与分页。保留 ACL、TTL 和版本签名；必须遵守历史截止的场景明确标注为能力差异 |
| B07 / P2 | `src/app/role-models.ts:8-22` 为新 Main/Worker Session 分角色解析和固定模型，保留已存在会话的选择 | `bootstrap/coordination.py:65` 与 `bootstrap/run_profile.py` 使用部署级 execution_profile；Main/Worker Agent 版本可分别配置，但它们不等价于分别配置模型/provider/reasoning | 平台配置中提供按角色选择的运行配置，并在会话/Run 固定快照；修改配置不悄悄切换正在执行的模型，权限仍归平台 |
| B08 / P2 | `src/app/resources.ts`、`app/api.ts`、`app/views/*` 和 `docs/005-panel.md` 提供 Ticket/Thread/Assignment/Decision、健康、队列、模型重试、配置和提示词历史的管理入口 | Weora 已有 Task/Run、来源状态、渠道回执及协调管理 API，但没有等价的协调面板；Main 的 read 只有 overview/ticket/trigger/notices。无模型重试健康和完整线程处置视图 | 在现有 Console 复用页面/组件提供协调状态与人工操作；只展示脱敏事实，动作受现有管理员/成员权限保护。配置和提示词写工具是否暴露给 Main 另按平台权限设计，不照搬上游授权模型 |

B01 在原总体方案中已经写为后续阶段，所以不能说以前完全没有提到；但它确实是原版已有、Weora 尚未实现的能力。**“排到后续”不等于“已实现完整 Super-agent”，也不能自行视为用户接受了缩减目标。**

## 4. 应当保留的平台差异

这些差异不是要求退回上游技术栈或权限模型；但相关功能不足仍须按第 2、3 节补齐，不能用下面的边界解释掉。

| 保留差异 | 现有约束及实际影响 |
| --- | --- |
| PostgreSQL / Redis / Celery，而非 SQLite / DSH 插件内调度 | 平台负责权威状态、事务、租约、幂等和补偿。当前 scope 使用加密状态聚合及 revision/CAS；功能一致性应看行为，不看是否使用 SQLite |
| Pi / Node 适配器，而非 DSH Session | 持续消息历史、运行中 steer 和接收/已读回执已有实现；不要求复用 DSH。但历史恢复不能替代 B01 后台执行或 B03 文件恢复 |
| Tool Gateway、计划审批、原生交付验收和未知写对账 | 上游 Worker 以操作者 OS 用户免审批执行命令；Weora 必须保留既有审批。上游提示词中的自动合并约定不能自动变成 Weora 的写授权 |
| 配置化项目与仓库、固定版本资产 | 不写死 GitHub；使用已授权项目、仓库、冻结提交和 Agent/Skill 版本。B02 仍需按 CodeSource 提供事件生命周期 |
| 平台管理员控制配置和提示词 | 不照搬 Main 的 settings_save / policy_sync / prompts_save 权限；现有管理员 API、CSRF、版本 CAS 与资产发布可继续承担控制面。B08 的状态可见性不足仍是独立缺口 |
| 成员映射、来源 ACL、正文保留期、受众隔离 | 同线程其他人不继承原 Task 发起人的权限；撤权和过期清理派生内容。上游 `sources/poll.ts:6-13` 对活跃任务及结束/活跃后 14 天线程补读，与 Weora 1–7 天正文 TTL 并非同一约束，不能把它们当成已对齐；过期来源不能凭旧历史恢复读取 |
| 当前 Pi 文本证据与有界会话 | 图片可定位不代表已理解图片；视觉不可用须明确返回。会话检查点、消息和工具轮次有上限；历史压缩、大规模负载、长期状态增长不在已验证范围，不能承诺无限持续运行 |

RAG、长期记忆、Skill、工作流仍是平台既有边界。它们与 Super-agent 协调/执行生命周期的补全可以同时存在。

## 5. 已有主链路与证据边界

当前并非只有分类器：消息去重与过滤、observe/auto、结构化规则与方向判断、同线程评估、已有任务优先接收跟进、主会话受限工具、Ticket/Assignment、原生 Task/Run 续接、Pi 历史与 steer、worker 报告、outbox、暂停/停止和来源撤权均有实际代码。落点和此前测试见 [源码移植对照](../architecture/superagent-source-port.md)、[验收记录](superagent-acceptance-20261103.md) 和 [质量回查](superagent-code-quality-review-20261103.md)。

这里确认的是这些机制存在，不代表每一项边界都与上游相同。A01–A04 就是在已存在机制中进一步查出的不一致。此前测试仍可证明其覆盖场景；不能把“测试通过”扩大为“上游能力全部覆盖”。

本轮独立复现命令，在 `backend/` 执行：

```powershell
uv run python ../output/superagent-audit-20261104/probe_parity.py
```

结果：6/6 差异在当前代码中复现，77/77 GitHub blob 核对一致。脚本断言的是“当前差异仍存在”，**不是期望行为验收通过**。生产修复后应将上游期望迁为正式回归测试，删除或更新审计断言。

未执行项目：上游 DSH 平台集成、真实代码托管事件、长时后台 Job/子 Agent、进程重启下的持续工作区恢复和完整面板验收。本轮也没有重跑既有全量测试，未把此前通过结果冒充新能力验证。

## 6. 补齐顺序与后续接续规则

1. 先修领域语义：A01、A02、A03、A04 及 B05。先从对应上游测试挑选反例，加入 Weora 行为测试；保持原生 Run 审批与来源授权。
2. 补持续执行基础：B01、B03、B04。Task 工作状态、Job 和子会话通过明确应用端口关联；后台唤醒、槽位与取消受平台统一管理，未知外部写不重放。
3. 补事件闭环：B02、B06。按配置的代码托管来源补评审/CI/关闭/重开；上下文窗口与输入归属共同验收。
4. 补运行配置与管理可见性：B07、B08。沿用平台资产、权限和 Console，不把管理员权交给模型。

新接续先读取本文件、`superagent-source-port.md`、ADR 0016 和相关指纹；生产文件有变化时，只重验受影响结论。每项使用 A/B 编号记录源码、变更、测试、真实集成及未验收部分。没有对应证据不能把项目标成完成；本表之外新发现的差异继续登记，不因本轮已经列出清单就推定其他行为完全相同。
