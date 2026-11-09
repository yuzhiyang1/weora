# 业务模块

业务代码会按能力进入该目录。第一阶段计划包含：

- `identity`：用户、团队、空间与权限；
- `model_gateway`：模型供应商、凭据、路由与用量；
- `agents`：Agent 定义、版本与运行；
- `skills`：团队 Skill 的注册、版本和分发；
- `workflows`：工作流定义、发布与执行；
- `knowledge`：数据集、文档、索引与检索；
- `collaboration`：人和 Agent 的项目群聊；
- `observability`：运行轨迹、指标与审计。

每个模块在真实需求出现时再按 `domain`、`application`、`infrastructure` 和 `api` 分层。不是每个模块都必须拥有完整四层：没有领域规则的简单查询不需要为了形式增加抽象。
