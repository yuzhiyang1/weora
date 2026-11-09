# ADR 0002：AgentVersion 与 Runtime Adapter 边界

- 状态：Accepted
- 日期：2026-09-18

## 背景

Weora 需要管理 Agent 草稿、发布版本和团队 Skill，同时计划使用 Pi Agent 作为第一种执行内核。如果把 Pi 的会话、消息或 Skill 类型直接写入 Agent 领域模型，未来升级 Pi 或接入其他 Runtime 时，会迫使控制面数据模型一起变化。

Skill 已经是可版本化资产。Agent 若只记录 `latest` 或 Skill 标识，历史运行会在 Skill 发布新版本后发生漂移，无法重放和审计。

## 决策

1. Agent 草稿只能绑定当前工作空间内已经发布的 `skill_version_id`，不能绑定 Skill 草稿或隐式 `latest`。
2. 发布 Agent 时创建不可变 `AgentVersion`，冻结当时的名称、描述、System Prompt 和有序 SkillVersion 绑定快照。
3. 绑定快照保存 SkillVersion ID、业务标识、版本号、对象存储键、包大小和摘要。后续草稿修改不能改变历史 AgentVersion。
4. 控制面通过 `PublishedSkillVersionCatalog` 调用 Skills 模块的应用层查询，不跨模块读取 Skills ORM。
5. 执行面通过 `AgentRuntimeContextAssembler` 生成 Runtime 中立输入：
   - 读取 AgentVersion 固定的 Skill 包；
   - 校验对象大小和 SHA-256；
   - 只输出 `SKILL.md` 和 `references/` 下允许的文本资源；
   - 不把脚本或二进制资产自动注入提示上下文。
6. Pi Agent 适配器后续只消费 Runtime 中立输入，不进入 Agent 领域模型或数据库表。

## 结果

- Agent 版本和 Skill 版本可以稳定审计、重放与回滚。
- Pi Agent 可以独立升级或被其他 Runtime 替换。
- Skill 包缺失或摘要不一致时，运行上下文装配整体失败，不降级使用未验证内容。
- 当前变更只完成控制面和 Runtime 输入契约；Pi Worker、模型调用、Tool Calling、RunEvent、暂停恢复与真实外部联调仍属于后续执行面工作。
