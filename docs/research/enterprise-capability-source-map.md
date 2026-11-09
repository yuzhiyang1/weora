# Weora 企业能力源码地图

## 文档信息

- 状态：调研资料
- 更新日期：2026-07-30
- Dify 快照：[`7b33622cdd0c628b7cced6fcf85251720d2a9686`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686)
- Coze Studio 快照：[`fefb05ff27be1da939612fbf9faf5db62583b8ae`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae)

## 1. 文档用途

这份文档用于定位 Dify 和 Coze Studio 中值得研究的企业 Agent 平台能力，帮助 Weora 后续进行领域设计和开发路线规划。

需要明确以下边界：

1. 这是源码调研地图，不是 Weora 的需求清单或开发路线。
2. 链接固定到具体 commit，避免上游目录变化导致参考内容漂移。
3. 参考重点是领域模型、接口边界、状态流转和异常处理，不直接照搬代码。
4. Weora 的产品需求、架构设计和验收标准应形成独立文档。
5. 复用任何源码前，需要单独确认对应项目及文件的许可证要求。

## 2. 企业能力源码地图

| 企业能力 | Dify 参考位置 | Coze Studio 参考位置 | Weora 主要参考方向 |
| --- | --- | --- | --- |
| 用户、空间、权限 | [`account.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/models/account.py)<br>[`workspace`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/controllers/console/workspace)<br>[`access-control`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/web/service/access-control) | [`user`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/user)<br>[`permission`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/permission)<br>[`openauth`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/openauth) | 只参考领域模型和权限边界，结合 Weora 的租户模型自行实现 |
| 模型网关 | [`model_runtime.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/plugin/impl/model_runtime.py)<br>[`model_runtime_factory.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/plugin/impl/model_runtime_factory.py) | [`modelbuilder`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/bizpkg/llm/modelbuilder)<br>[`modelmgr`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/application/modelmgr) | 优先参考 Dify 的统一接口和 Runtime Factory 设计 |
| Agent Runtime | [`core/agent`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/agent)<br>[`base_agent_runner.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/agent/base_agent_runner.py) | [`agentflow`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/agent/singleagent/internal/agentflow)<br>[`agentrun`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/conversation/agentrun) | 对照两边的运行生命周期、事件模型和会话关系 |
| RAG 知识库 | [`core/rag`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/rag)<br>[`retrieval_service.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/rag/datasource/retrieval_service.py)<br>[`dataset.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/models/dataset.py) | [`domain/knowledge`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/knowledge)<br>[`retrieve.go`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/knowledge/service/retrieve.go)<br>[`infra/document`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/infra/document) | 参考 Dify 的功能完整度和 Coze Studio 的领域分层 |
| Tool Calling | [`tool_engine.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/tools/tool_engine.py)<br>[`core/tools`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/tools) | [`plugin/service/tool`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/plugin/service/tool) | 优先参考 Dify 的 Tool Runtime、参数转换和调用流程 |
| MCP | [`core/mcp`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/mcp)<br>[`mcp_tool`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/tools/mcp_tool) | [`invocation_mcp.go`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/plugin/service/tool/invocation_mcp.go) | 对照协议适配、工具注册、调用和错误转换 |
| 工作流后端 | [`workflow_entry.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/workflow/workflow_entry.py)<br>[`workflow/nodes`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/workflow/nodes)<br>[`workflow_service.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/services/workflow_service.py) | [`workflow/interface.go`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/workflow/interface.go)<br>[`workflow/internal`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/workflow/internal) | 参考 Dify 的 Python 执行流程和 Coze Studio 的 DDD 边界 |
| 草稿、版本、发布 | [`models/workflow.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/models/workflow.py)<br>[`publish_workflow`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/services/workflow_service.py) | [`singleagent/publish.go`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/application/singleagent/publish.go)<br>[`agent DAL`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/agent/singleagent/internal/dal) | 两边都需要研究，重点关注不可变版本和运行实例绑定 |
| 运行轨迹、可观测 | [`core/ops`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/ops)<br>[`observability.py`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/core/app/workflow/layers/observability.py)<br>[`trace service`](https://github.com/langgenius/dify/blob/7b33622cdd0c628b7cced6fcf85251720d2a9686/api/services/workflow_node_execution_trace_service.py) | [`execute history`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/domain/workflow/internal/repo/execute_history_store.go)<br>[`前端 Trace`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/frontend/packages/workflow/test-run-next/trace) | 参考 Dify 后端事件和 Coze Studio 前端 Trace 交互 |
| 工作流画布 | [`Dify workflow UI`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/web/app/components/workflow) | [`workflow/playground`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/frontend/packages/workflow/playground)<br>[`workflow/nodes`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/frontend/packages/workflow/nodes) | 主要参考 Coze Studio 的画布组织、节点交互和调试体验 |
| Agent Studio | [`develop`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/web/app/components/develop) | [`agent-ide`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/frontend/packages/agent-ide)<br>[`project-ide`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/frontend/packages/project-ide) | 主要参考 Coze Studio 的 IDE 信息架构和交互方式 |
| 部署 | [`Dify docker`](https://github.com/langgenius/dify/tree/7b33622cdd0c628b7cced6fcf85251720d2a9686/docker) | [`Coze docker`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/docker)<br>[`Helm`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/helm/charts/opencoze) | 对照两边的服务拓扑、基础设施依赖和部署方式 |

## 3. 建议的源码阅读方法

每项能力都按照同一组问题阅读，避免陷入逐文件抄代码：

1. 这项能力解决什么业务问题？
2. 核心领域对象及其关系是什么？
3. 应用层、领域层和基础设施层如何划分？
4. 对外接口和内部扩展接口分别是什么？
5. 正常状态、失败状态和恢复状态如何流转？
6. 如何处理权限、租户隔离、幂等、并发和审计？
7. 哪些设计适合 Weora，哪些只是上游历史包袱？

建议每完成一项调研，就在独立文档中记录：

```text
能力目标
领域模型
关键状态机
接口边界
异常与恢复
安全与权限
上游设计对比
Weora 采用决策
暂不采用的内容及原因
```

## 4. 后续文档入口

后续进行功能路线设计时，建议新增以下文档，而不是直接修改这份源码地图：

```text
docs/
├── product/
│   ├── product-scope.md
│   └── capability-boundaries.md
├── architecture/
│   ├── system-context.md
│   ├── domain-model.md
│   └── decisions/
├── planning/
│   └── development-roadmap.md
└── research/
    └── enterprise-capability-source-map.md
```

这样可以将“上游项目事实”“Weora 产品决策”和“实际开发顺序”相互隔离，避免源码实现反过来替代产品设计。
