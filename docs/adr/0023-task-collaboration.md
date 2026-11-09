# ADR 0023 任务协作与跨成员执行

- 状态：Accepted。
- 日期：2026-11-09。
- 承接：[ADR 0015 同 Task 的持久消息与新 Run 续接](0015-task-continuations.md)。

## 决策

Task 原始发起人和渠道受众继续用于原始材料及回传鉴权。控制台共享由发起人显式发布标题、摘要和参与者，读权限限定在该工作空间的获邀成员。参与者读取协作动态和活跃状态，Run 详情使用独立权限。指派给成员后，该成员可接手、指派 Agent 或发起交接。成员继续协调自己指派给 Agent 的工作，发起人可通过成员名单撤销该权限。

Run 通过 Task 和工作空间关联，发起人记录为本轮实际启动的成员。共享执行冻结发布的摘要及该成员重新取得的项目快照。私有正文、历史会话和渠道回传不进入共享执行。本轮仍走现有发布版本、执行租约和计划审批。

Task 沿用 Multica 的内置状态键，主表保存 `status`、`assignee_type` 和 `assignee_id`。状态和负责人可独立设置。更换或移除负责人立即生效，可在同一操作中请求停止上一轮。分配给 Agent 可以选择暂不开始或分配并开始，待规划任务要先移出待规划。

共享指派使用任务行锁、任务修订号和已有的单活跃 Run 唯一索引。分配并开始时，Run、待派发记录、负责人和审计同事务写入。已有活跃 Run 时可以先指派，新 Run 等旧 Run 进入终态后启动。显式指派后，旧私有续接入口拒绝后续输入，防止原发起人的排队输入抢占工作。

任务验收沿用审核中状态和任务动态。负责人提交交付说明，任务发起人可通过或退回修改。提交验收要求当前运行和交接已结束。运行成功后，任务仍由协作成员更新状态。

「待我处理」按 Task 聚合待审批、待验收、指派和退回通知，已读与归档状态分别保存。通知只保存来源引用，查询时再次校验任务和运行权限。执行审批继续由本轮 Run 发起人通过已有审批入口处理，授权范围、计划版本和执行租约沿用现有规则。

每组由最新通知决定未读状态。归档和恢复由服务端在一个事务中更新收件人的整组记录。归档查询先选出每组的最新代表，并排除仍有未归档通知的任务，再按通知时间和 ID 进行游标分页。Run 通知通过既有 Run 与 Task 关联参与分组。

## 实施与回退

迁移 `20261109_0021` 新增协作成员与审计数据。迁移 `20261109_0022` 将旧负责人和接手标记回填为主表字段，已有待交接记录由原调度机制结清。两次迁移均保留原始渠道线程和 TaskInput 的发起人边界。

迁移 `20261109_0023` 新增 `inbox_items`，回填审核中的共享任务和仍待审批的最新计划。验收沿用原任务状态及动态表。验收动作及通知同事务保存，审批通知与版本化计划同事务保存。

迁移 `20261109_0024` 新增按工作空间和收件人隔离的 `inbox_revisions`。通知写入和版本推进同事务提交。SSE 只发送版本变化，客户端收到后重读列表，断线时使用定期 HTTP 查询补偿。

依据为 Multica `2ae2f0800` 的任务表、状态和指派行为。[任务表](https://github.com/multica-ai/multica/blob/2ae2f08005f05956ec5c8f99a3d7dc0681c84925/server/migrations/001_init.up.sql#L52) · [指派规则](https://github.com/multica-ai/multica/blob/2ae2f08005f05956ec5c8f99a3d7dc0681c84925/apps/docs/content/docs/assigning-issues.zh.mdx) · [Figma Task 协作](https://www.figma.com/design/K5VJU1uMXW397Frkq79zph?node-id=2-2)

验收状态和收件箱交互也按该版本核对。[验收流程](https://github.com/multica-ai/multica/blob/2ae2f08005f05956ec5c8f99a3d7dc0681c84925/apps/docs/content/docs/workflows.zh.mdx) · [收件箱](https://github.com/multica-ai/multica/blob/2ae2f08005f05956ec5c8f99a3d7dc0681c84925/apps/docs/content/docs/inbox.zh.mdx) · [Figma 待我处理](https://www.figma.com/design/K5VJU1uMXW397Frkq79zph?node-id=10-2)
