# Super-agent 执行层代码质量回查

范围：本次协调模块、Pi 会话适配、Task 续接、Context 授权及 Celery 接入。保持既有技术栈、模块化单体、公开 HTTP 契约和审批语义；没有暂存、提交、切换分支或处理无关 `.openidea/`。

## 设计判断

本轮需要整理的原因是调用方必须了解内部状态和副作用顺序，不是文件超过某个行数。

| 证据 | 原来的理解或修改成本 | 本轮选择 |
| --- | --- | --- |
| `coordination/api.py` 直接读 store 并调用服务私有 `_read` | HTTP 层同时掌握权限、撤权清理和状态投影 | `CoordinationService.list_scopes` 统一用例；API 只做身份和响应转换 |
| `bootstrap/coordination.py` 直接操作游标与效果队列 | 修改补偿策略还要修改 Celery 运输代码 | 应用层 `pending_effects` 决定到期工作；Bootstrap 只负责通知 |
| 控制入口调用完整 `_dispatch` | 每次即时唤醒都会经过容器清理、旧渠道发送和保留期维护 | 分开 `_pump_inbox`、`_dispatch_pending_runs` 与原维护入口；控制快路径不调用模型、渠道发送或容器清理 |
| `CoordinationService.launch` 混合模型协调和跨模块 Run 提交恢复 | 接单工具维护者还要理解并发槽、冻结请求和晚到绑定 | 独立 `WorkerDispatcher` 负责原生提交边界，服务先完成来源授权 |
| 通用 Repository 有无效 `active` 参数及基于 getattr 的 thread 查询 | 接口承诺了实际没有执行的过滤 | 改为事务记录视图 `RecordCollection`，领域规则明确筛选；去掉未生效参数和存储模块的便利重导出 |
| 效果状态及租约检查分散 | 容易漏检 owner/期限，或把新 generation 误作撤销当前租约 | 明确 EffectKind/EffectState，集中 `is_owned_by`、`is_dispatchable` 与事务内租约校验 |

比较过两种可行方案：A 仅在原服务中补注释和局部条件，改动较少，但仍让 HTTP、Celery 和模型协调掌握原生提交细节；B 把状态查询、到期推进收回应用用例，并单独封装 Worker 提交边界。采用 B，同时保留来源授权和接单规则在原协调模块，不为每个分支再建一层 Service/Repository。

继续保留单 scope 加密快照和短事务行锁：它与当前接单原子性、通知恢复和来源清理相配套。将其改为逐表事件存储会涉及迁移与恢复协议，不作为本轮代码整理顺带实施。16 MiB 容量限制及缺少长期负载验证的事实仍保留，不能把本次回查称为生产容量验收。

## 发现及修复

1. **输入侧失败没有及时终止 Pi。** 原 `_exchange` 只等待输出侧，输入拉取线程报撤权错误后可能一直等到模型/工具结束。新增故障注入测试先失败，改为等待双向任务任一结束，立即传播授权失败并取消另一侧；原有 Node 进程树清理继续生效。
2. **Outbox 发送前缺少完整的事务领取。** 旧快照读取和状态写入之间可能被其他 owner 接管。真实 PostgreSQL 测试先复现旧 owner 仍能发送，再把 owner、暂停、当前提问和 pending→unknown 领取放在同一事务。四个并发调用只发送一次。
3. **提交成功但返回丢失后发生幂等冲突。** 原来只冻结指令、请求键和输入 ID；新消息提高优先级后来源元数据变化。故障注入测试先得到 RunIdempotencyConflictError，现在来源元数据也随请求冻结；重试取回原 Run，新消息保留待投递。
4. **恢复未知发送需要通知对账。** unknown 不能静默结算为成功。保存发送 owner，区分同一有效尝试的重复调用与新 owner 的崩溃恢复；后者记录 DELIVERY_RESULT_UNKNOWN 并通知主会话，禁止重发。
5. **撤权清理存在派生正文遗漏。** 退回原因及被替换 Run 的孤立 provisional report 也可能含来源正文，现在一并清理，保留必要幂等标识。
6. **强化原生提交窗口的停止补偿。** 人类停止会撤销原效果租约；已创建 Run 的取消补偿仍须保存，不能先因租约失效退出而遗留永远排队的 Run。新增停止发生在原生提交与绑定之间的测试。重复绑定也不会覆盖当前报告。

## 可读性与注释

- 复杂方法补充中文用例契约：授权、范围冻结、事务提交、原生调用、绑定、报告和渠道对账的阶段及失败语义。
- 保留解释不可互换顺序的局部注释，例如发送前先记 unknown、绑定前禁止原生领取、停止后仍须取消补偿；不逐行翻译代码。
- 大量同类型 ID 的 Trigger、Mandate、Ticket、TaskInput 改为关键字构造，避免读者依靠字段顺序识别身份和来源。
- TaskRecord 不再被误述为仅 Console 使用；TaskInput 不再把 run_id 为空笼统等同为排队。
- 对外使用明确的领域方法 `ticket`、`pending_evaluation`、`record_notice`、`requeue_trigger` 和 `ContextService.authorize_actor`，不让调用方钻私有方法。
- 移除业务回调中的 object/cast 及动态导入，使用实际 Engine 类型；测试故障注入保留原生方法签名，没有用宽泛 kwargs 或忽略类型错误绕过检查。

## 验证记录

- 受影响 Context、Runs、Task、Worker 生命周期、协调和 Pi 协议回归：280 项通过；随后新增或整理部分再按调用范围复验。
- 协调与 Pi 子进程：58 项通过，包含撤权即停、请求冻结、停止补偿、未知发送及派生正文清理。
- 最后可读性整理：领域、协调服务及 Task 续接 51 项通过。
- 真实 PostgreSQL / 真实 Pi + DeepSeek：5 项通过，含发送租约、四路并发幂等、迁移与原生 Task 续接；PG 测试使用独立随机 schema，模型用例使用独立文件库，渠道为 Fake。
- Ruff、格式检查、Mypy（158 文件）通过；Alembic check 无新增迁移。
- 本轮没有修改前端及 Node 生产代码，复用上一轮已通过的检查，不声称本轮重新完成飞书实发或 UI 视觉验收。

完整维护与控制快路径拆分后，协调、Context、Task 续接和 Worker 生命周期专项回归 **256 项通过**；新增测试把容器清理及渠道发送设为必定失败，确认控制入口仍能处理 Inbox 并发出调度通知。随后 PostgreSQL 原生 Task/Outbox 与真实 Pi 两轮会话再次运行，**3 项通过**。这些数字是各轮检查的实际数量，不应相加解释为去重后的测试总数。

重启前确认本机业务库无 QUEUED/RUNNING Run、协调效果均未在运行，验证旧加密状态可被新模型读取。重启已授权的本地进程后，API live/ready 和 Web 均返回 200，控制/effects 队列恢复消费。没有运行新的迁移，也没有重发飞书验收消息。

原有真实飞书证据及容量边界见 [功能验收记录](superagent-acceptance-20261103.md)。
