# ADR 0003 研发 Agent 执行与飞书上下文边界

- 状态：Proposed
- 日期：2026-10-17
- 2026-11-03 增量：[ADR 0015](0015-task-continuations.md) 确认同 Task 多轮执行；替代显式请求始终独立建任务的旧行为，保留原权限、批准与上下文边界。
- 关联：[目标架构](../architecture/agent-platform-and-feishu-context.md)、[飞书上下文设计](../product/feishu-context-v0.1-design.md)

## 背景

Weora 需要从 Agent 与 Skill 注册进入真实执行，同时支持飞书当前讨论与历史消息。如果把飞书会话、模型决策和执行策略混合在单个 Agent 中，平台难以保证权限、恢复和证据完整性，也会被单一渠道或 Runtime 绑定。

## 提案

1. Weora 独立运行；Plane 和禅道不属于第一版依赖，未来通过 Tool Adapter 接入。
2. ConsoleAgent 与 SandboxAgent 是逻辑角色。平台拥有生命周期、状态与权限；Agent 决定调查和执行策略；工具实现具体操作；审批由人类完成并由平台强制执行。
3. 自动讨论补齐是平台模型调用前的确定性流程；主动历史查询通过 Agent 工具循环。两者共用 Context Service、FeishuReader、来源授权和快照契约。
4. 保持模块化单体、控制面与执行面独立进程。第一版先完成单 Agent Runtime Adapter 与 Tool Gateway，不以可视化工作流、完整多 Agent 协作为前置。首个 Runtime 继续沿用 Pi 方向，不替代既有 WorkflowEngine 端口。
5. AgentVersion 与 SkillVersion 不可变。动态上下文和来源证据绑定 Run，角色切换不增加权限；上下文资料不能作为覆盖平台策略的指令。
6. 飞书第一版采用企业应用身份与显式来源授权，不提供个人 OAuth 或隐式个人权限继承。来源授权、发起人、Run 与输出受众共同约束读取及交付。
7. PostgreSQL 保存权威状态与待投递记录。Redis 通知可丢失后补读；不承诺远端副作用 exactly-once。不确定的写入结果必须核对，不自动盲目重放。
8. 上下文快照有保留期，撤权和过期同时约束预览、派生结果与恢复。可追踪不等于永久保存敏感正文。
9. 借鉴 HiFox 的接入分层：渠道路由、成员身份、工具授权与通知交付分别约束。第一版仅保留任务来源与原会话回报，外部作者不因被读取而获得权限；CLI 只作为 FeishuReader 的可选实现，不改变工具契约或应用身份。个人 OAuth、扫码绑定、完整双向同步、通用凭据中心及其他后置能力不纳入第一版，见[架构中的参考设计与取舍](../architecture/agent-platform-and-feishu-context.md)。

## 影响与取舍

- 初期需要建设 Run、工具网关与来源授权，不能把飞书接入理解为只加几个 API 包装。
- 自动补齐和主动查询复用底层实现，权限与分页错误不会在两套逻辑中分别修复。
- 群聊 Run 限制跨私有来源聚合，牺牲便利性以降低泄漏风险；未来若放宽，应单独设计并审批受众规则。
- 单 Agent 循环可以先交付真实价值；完整 WorkflowEngine 集成、A2A 和其他 Runtime 按后续需求演进。
- 本提案获确认前保持 Proposed；不得据此修改既有 Accepted ADR 或宣称新能力已经上线。
