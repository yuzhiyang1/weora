# Weora 内置 Super-agent：源码移植与验收对照

> 2026-11-04：完整源码复核登记的 A01–A04、B01–B08 已修复并验证，见 [当前验收与保留差异](../development/superagent-gap-acceptance-20261104.md)。下文是初版行为契约与历史验证记录，不能仅依据其旧测试计数证明完全等价；当前判断以逐项验收、代码指纹及明确的平台差异为准。

- 日期：2026-11-03。
- 范围：在 Weora 中提供 Super-agent 的主 Agent、任务与执行会话协作能力；不是只做未 @ 分类器。
- 源码基线：DeepSeek `dsh-superagent-prototype@59e6c4059d513a5a63c482aee884c95e4ea89fa0`，与用户提供的具体链路一致。
- 方式：先读取实际实现和测试，再将规则翻译为 Python 平台用例与 Pi 会话协议；保留逐项出处。以下是行为契约，实际落点与验证见末尾“实施对照”；真实联调及失败修复见验收记录。
- 参考文件副本：本机忽略目录 `output/superagent-59e6c405/`。不执行上游安装脚本或运行其平台，不用上游文章替代代码证据。

## 本次完成条件

用户只与主 Agent 协作；主 Agent 接收显式请求和经配置允许的普通消息，读取上下文、作出结构化接单或路由决定，创建和管理多个 Worker。已有任务的消息优先直达其 Worker；主 Agent 接收报告、处理退回、回答问题和转交结果。整个过程具备持久状态、策略版本验证、重启恢复、暂停、停止和可追踪回报。

沿用 Python/FastAPI、PostgreSQL、Redis/Celery、Pi/Node、模块化单体。上游 Ticket 映射为持续 Task 的管理记录，Assignment 映射为受代次保护的执行安排，Run 仍是可审批的执行尝试。主 Agent 的协调轮次独立于用户 Task，不能为了调用模型创建伪造的用户任务。

## 源码行为清单

下表保留源码要求，后续评审不能只凭功能名称判断一致。上游地址均固定到上述 commit：[源码树](https://github.com/deepseek-ai/dsh-superagent-prototype/tree/59e6c4059d513a5a63c482aee884c95e4ea89fa0)。

| 行为 | 上游代码与测试 | Weora 实施与验收要求 |
| --- | --- | --- |
| 接收去重、过滤、线程关联 | `core/intake.ts:20-63`、`sources/feishu-trigger.ts` | 按消息 ID 幂等；自身机器人排除；来源、作者、受众和主线程可追踪；过滤也留下最小处置记录 |
| 显式与推断准入分离 | `policy.ts:166-242`、`tests/policy.spec.ts` | 显式请求走路由；未 @ 经配置的观察范围及准入表才评估；默认 observe；off 不评估推断请求 |
| 规则与方向三事实 | `policy.ts:13-68`、`app/assessment.ts:21-37` | 每个启用规则必须且只能评估一次；实施意图、目标依据、是否仍需设计决定结构化；接受须有证据；observe 仅持久记录 would_accept，不开 Worker |
| 线程批处理与最强依据 | `core/evals.ts:78-120` | 同线程最多一个活跃评估；合并待评估触发；显式优先、同强度取更新依据；已接单消息不再贡献新授权 |
| 已有任务优先决定跟进 | `core/model.ts:62-83`、`core/intake.ts:36-59`、`tests/followups.spec.ts` | 普通跟进直达原 Worker，主 Agent 不重复接单；仅上下文订阅不取得接单所有权；Worker 退回后才可重新评估 |
| 实时唤醒与兜底调度 | `app/passes.ts:22-68`、`tests/passes.spec.ts` | 提交消息后异步通知，不等待 Beat；持久队列为真相；合并重复唤醒；2 秒检查待办与租约，通知丢失后仍能恢复 |
| 持久副作用与代次 | `app/effects.ts:37-145`、`tests/effects.spec.ts` | kind/key 唯一、FIFO、同键合并和 generation；旧尝试完成不能删除后来排入的工作；失败退避；停机保留待办 |
| 评估送达、超时与暂停 | `core/evals.ts:32-77` | 送达模型会话后计时；暂停移动截止时间；最多重新评估两次；超时/过期结果不可再落地 |
| 策略变化与旧决策拒绝 | `core/evals.ts:221-230,281-303`、`policy.ts:122-165` | 策略版本 CAS；策略变更重评，旧工具调用拒绝；执行前重新校验来源和授权依据，模型不能自行改模式扩权 |
| 候选按需读取 | `app/assessment.ts:139-270`、`tests/assessment.spec.ts` | 先候选窗口，按消息/根/父/话题、游标和文本 offset 继续读；引用只限已交付集合，结束评估不能继续读旧缓存 |
| 结构化路由与隔离失败 | `app/tools/main.ts:38-153` | accept/attach/stop/ignore；批量每项独立事务和结果；推断接单必须通过 eval_decide；重复消息不建重复 Task |
| 接单原子性 | `core/evals.ts:305-322`、`core/tickets.ts:37-60` | Decision、Task 管理记录、授权依据、线程订阅和 launch 同事务；真正建 Task/Run 通过 Runs 应用用例，使用确定性幂等键补偿跨模块窗口 |
| 主 Agent 与 Worker 分权 | `app/sessions.ts:61-77`、`executor/dsh.ts:477-514` | 主 Agent 只有协调、受限读取与回复工具；不获得沙箱命令、改码、批准和任意跨会话工具；Worker 仍走现有 Gateway 与计划审批 |
| 持久会话及运行中输入 | `app/delivery.ts:34-85`、`app/sessions.ts`、`executor/dsh.ts:290-355` | Pi 会话日志、消息 ID 去重、待投递/已送达/已读分别记账；活跃调用支持 steer；重启恢复不得重复消息或写工具；旧 v1 调用继续兼容 |
| Worker 并发与人工优先 | `executor/dsh.ts:171-203`、`tests/runtime-concurrency.spec.ts` | Worker 槽位有持久租约；人工跟进优先于新自动任务，保留额外一个人工唤醒机会；等待审批释放计算资源，旧代次不能写新状态 |
| Worker 报告与人类问题 | `app/tools/executor.ts:70-104`、`core/assignments.ts:96-135` | progress/ready/blocked/failed/dropped；当前问题可保留、替换、清除；问答必须指向本任务已收到的人类输入；ready 还须通过 Weora 的交付证据验证 |
| 结果回主 Agent | `app/notices.ts`、`tests/followups.spec.ts` | 报告先持久化再通知主会话；正常进度可静默，不重复唤醒；无报告的空闲 Worker 可请求状态，不恢复实施 |
| 状态读取、备注与重试 | `app/tools/main.ts:159-192`、`core/tickets.ts:146-185` | 主 Agent 查看任务/会话、问状态、传递事实、在明确人类请求下重试失败/阻塞安排；备注不增加授权，不自动解开等人状态 |
| 线程订阅与退回 | `core/tickets.ts:74-135,185-220`、`app/tools/executor.ts:39-69` | 任务自己的线程、附加线程和仅上下文线程分开；自身线程不能被 Worker 当无关取消；退回每消息每任务一次；人工停止的任务不自动复活 |
| 暂停、停止与恢复 | `core/tickets.ts:157-170,239-329`、`core/assignments.ts:73-90` | pause 停新评估/派发/回复，已接收输入保留；stop 有持久人工终止标记；恢复重新校验授权；未知外部写入仍按现有机制对账 |
| 回复规则与 Outbox | `core/replies.ts`、`app/outbox.ts`、`sources/feishu-channel.ts` | acknowledgement/answer/result/attention 受状态和受众约束；结果须有已验证 ready；attention 匹配当前问题；确定性键去重；撤销未发的旧问题 |
| 游标补偿与订阅话题 | `sources/feishu-loops.ts:127-267`、`sources/poll.ts` | 首次只建立游标，不启动历史任务；每轮结束后 60 秒再扫；分页完整后推进游标，重叠窗口按消息 ID 去重；话题、根/父和历史初始化边界保留 |
| 保留期和撤权 | Weora `ContextService` 与本次新增存储 | 新事件正文、评估证据、会话日志、备注和报告继承来源限制与保留期；撤权不能通过主会话历史绕过；普通日志不存正文和密钥 |
| 新旧入口兼容 | Weora 现有 Run、FeishuChannels、Pi v1 测试 | 未启用主 Agent 的路由维持既有显式 @；旧幂等键、任务、审批及恢复不重解释；主 Agent 模式的结果只走一个回复 owner |

## 必须保留的 Weora 差异

1. 上游 SQLite 事务与本地副作用队列改为 PostgreSQL 事务、租约和 Celery 通知。不能把 Redis 或常驻 Node 的内存当业务状态。
2. DSH Session 改为 Pi 会话适配器；会话日志由 Python 平台保存，Node 只执行模型和工具循环。保留旧协议，新增能力须有协议及崩溃窗口测试。
3. GitHub 固定仓库、worktree 和 PR 规则映射到 Weora 已配置项目、选中仓库、冻结提交及 CodeSource/Delivery 接口；不引入硬编码 GitHub 地址、主分支或凭据。评估只读、写入须现有精确计划及人类批准。
4. 上游的 Mandate 是接单依据；在 Weora 不是执行许可。来源权限、活动身份、项目分享、Tool Gateway、沙箱和审批继续独立强制执行。
5. 群中的角色与任务参与者须显式关联；上下文扇出不等于授权扇出。模型不得指定任意 workspace/user/channel 或以管理员身份读取私有任务。
6. 上游报告 ready 不直接替代 Weora 测试、Diff、远端交付等证据校验。结果未知不因主 Agent 重试而重放。
7. 当前测试机器人存量连接不继续排查，按用户要求留作环境限制；上线使用独立新应用并重新绑定用户身份、来源授权和路由。真实测试必须区分自动入站、游标补偿、模型执行和渠道送达。

## 实施顺序与测试出口

先建立纯 Python 协调状态机及对照测试，再接入加密持久存储和迁移；随后接入 Pi 主/Worker 会话、工具及权限端口；最后连接实时通知、补偿、管理 API 和运行观测。这个顺序用于安全实施，不缩减本次范围。每个出口的未通过项必须保留原因及证据，不能用一个分类测试或一个成功任务替代全链路验收。

验收包含：未 @ 的无关消息/明确实施请求、off/observe/auto、规则缺失和伪造证据、显式路由、同线程批量、策略变化、评估超时、重启和通知丢失、多个 Worker、已有任务跟进、运行中消息、退回和重新接单、人工停止、暂停恢复、等待人类、状态请求、报告与原会话回报、游标补读去重、正文到期和来源撤权。先跑确定性测试与真实 PostgreSQL 并发，再用真实 Pi/模型和飞书合成消息逐段验收；不以 Fake 结果冒充真实外部调用。

## 实施对照与明确差异

本次已将上述协调主链路接入 Weora。下面路径均为仓库内实际文件，测试可以独立运行；不以文档存在代替测试结果。完整结果见 [2026-11-03 验收记录](../development/superagent-acceptance-20261103.md)。

| 行为组 | 实现 | 主要验证 |
| --- | --- | --- |
| 准入、规则、observe/auto/off、策略变化及显式路由 | `coordination/policy.py`、`engine.py`、`tools.py` | `tests/coordination/test_engine.py`，真实 Pi observe/auto，真实飞书未 @ 接单 |
| 线程去重、最强依据、已有 Worker 优先、退回与订阅、人工停止 | `engine.py`、`service.py` 的 topic 根关联、Context Inbox | `test_engine.py`、`test_service.py` 的有/无 root_id 两种续接 |
| 评估送达、暂停、超时、过期工具拒绝 | `engine.py` 的 eval 生命周期 | `test_engine.py` 的 delivered/timeout/pause/stale 断言 |
| 接单事务、代次、副作用 CAS、恢复与租约 | `records.py`、`ports.py`、`store.py`、迁移 0017 | `test_coordination_postgres.py` 的并发去重、同键新代次、旧 owner 拒绝、迁移回退 |
| 异步通知和独立调度 | `bootstrap/coordination_notify.py`、`bootstrap/coordination.py`、`worker.py` | 本地四类 Worker/Beat/Receiver 实际运行；持久队列和租约测试 |
| 有界证据与配置仓库读取 | `service.py`、Context 签名游标、Projects 固定 revision 文件/目录接口 | `test_project_reads.py`、`test_service.py` 的偏移、伪造引用、预算、结束评估缓存拒绝 |
| Worker 槽位与人工保留槽 | `worker_dispatch.py`，原生 Run priority | `test_service.py` 三任务、自动容量及仅一个额外人工槽；槽位保留随有效效果租约延续 |
| Task/Run 跨模块幂等和晚到输入 | `worker_dispatch.py`、`service.py:reconcile`、Runs 的 create/continue；`engine.py:report` | `test_coordination_run_postgres.py` 覆盖 PG 真正建 Run、同 Task 续接与 TTL；新增提交返回丢失及停止窗口测试 |
| Pi 持久会话、运行中 steer、接收/已读回执、进程退出 | `RuntimeSession`、`pi_runtime.py`、`worker_runtime.py`、Pi `runtime.ts/index.ts/session-input.ts` | Python 真实 Node 协议测试、TS 真实 Pi Agent 脚本提供方测试、真实模型连续两轮 |
| 报告、人类问题、备注、状态、重试 | `engine.py`、Main/Worker 受限工具 | `test_engine.py` 的问题保留/清除/撤销、报告验证、状态等待限定、停止与重试权限；原生 Gateway 回归 |
| 回复状态、受众、幂等与未知发送 | `engine.py:reply`、`service.py:deliver/channel_deliveries` | Fake 渠道幂等/撤权测试、真实飞书确认与结果读回、Console 回执测试 |
| 游标补偿、话题发现、分页失败和重复事件 | `polling.py`、Feishu Reader 消息解析 | `test_service.py` 游标/卡页/去重；真实普通消息经实时事件及补偿两种入口进入同一路径 |
| 撤权、正文保留期、固定版本及旧入口 | `authorized_state`、Context `channel_run_ids/link_channel_run`、原生访问检查 | 来源撤权清空派生历史，所有派生 Run TTL 测试；既有 Context/Run/恢复/Gateway 回归 |

这里是行为移植，不将上游 DSH/SQLite 作为运行依赖。保留的差异如下，后续不能把它们误报成已照搬的能力：

- 配置由 Weora 管理 API 的管理员、Cookie/CSRF 和 revision CAS 控制。没有向主 Agent 暴露上游 settings_save 之类的配置写工具，也没有新增配置页面；这是保留平台控制面的权限边界。
- Git 输入来自已配置项目、选中仓库和冻结提交。评估只有受限文件/目录读取；代码执行、推送、评审仍调用既有 Tool Gateway、Delivery、CodeSource，不能写死 GitHub 或由接单推导写入批准。
- 来源作者必须映射到已授权 Weora 成员。同话题其他作者不会自动继承原 Task 的发起人权限；必要时独立评估和建 Task。
- 图片和附件能识别及受限定位，但当前 Pi 为文本 Runtime；视觉证据需要时明确返回能力不可用，不声称已看懂图片。上游 DSH 的视觉能力未凭空补成文本能力。
- 审批等待释放执行资源；主 Agent 的状态询问不能执行写工具。ready 必须等待原生 Run 的既有交付验收，未知写/未知渠道发送均不自动重放。
- 持续会话有容量上限：父进程请求 262,144 字节、单会话检查点 240,000 字节、每轮最多 24 次工具调用、单 scope 加密前状态 16 MiB。尚无无限历史压缩、大规模负载及长期留存压测；超限报错，不暗中丢弃写入或扩大预算。

## 上游许可声明

本轮功能验收后的结构整理及故障注入结果见 [代码质量回查](../development/superagent-code-quality-review-20261103.md)。

参考实现的许可为 MIT。根据源码翻译的行为、算法和测试保留以下声明：

MIT License

Copyright (c) 2026 DeepSeek Harness Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
