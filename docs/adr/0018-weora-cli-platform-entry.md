# ADR-0018：以独立 CLI 接入平台能力和团队上下文

- 日期：2026-11-04
- 状态：CLI 基础骨架已实施；业务能力与外部 Agent 接入尚未实施
- 范围：Weora 自身的平台入口，不替代 ADR-0005 的飞书读取适配器

## 背景

团队成员使用 Codex、Claude Code 等不同 Coding Agent。Weora 承载团队共同上下文，需要为这些客户端提供一致的能力入口，也要支持飞书触发的 Agent 调用平台能力。

## 决策

1. 在 `apps/cli` 建立独立 Python 3.12 包，通过 console script 暴露 `weora`。复用现有 uv 技术基线，初期仅用标准库 argparse，不新增 Go 工具链或命令框架依赖。
2. CLI 是接口适配层，不建立 CLI 业务领域；业务命令将通过公开平台 API 调用对应应用服务，不导入后端模块或访问数据库。
3. 外部 Coding Agent 使用 CLI 执行能力，使用配套 Skill 学习查询时机、参数和引用方式。Weora 是团队上下文权威存储，Skill 不复制整份团队知识。
4. 基础阶段提供 `version`、`commands`、`schema`、`--help`。命令执行使用统一 JSON 信封和退出码；未实现的能力不注册，不提供假成功。
5. 首个真实能力优先考虑团队上下文读取。具体检索契约、身份、授权和运行时装配在对应能力开发时确定；当前不增加通用 HTTP 透传、虚构认证或写入接口。
6. 暂不建立 CLI 插件系统、通用 SDK、MCP Server 或原生 Tool 双实现。后续出现真实使用方时复用服务端业务能力。

## 取舍

- Python 包可独立安装，当前不需要 Node 或完整后端依赖；目标机器需 Python 3.12 或由 uv 管理该版本。单文件二进制分发留待真实部署需求。
- Python CLI 不参与 pnpm 的递归检查，验证命令单独记录在模块 README。
- 当前发现列表仅反映本地内置命令。未来是否按服务端版本与授权过滤业务能力，随首个能力设计，不假定本地可见即有调用权限。

## 参考

- [larksuite/cli README](https://github.com/larksuite/cli/blob/d5209485e57a0c2205ab88959811bfefe905ce29/README.md)：命令分层、JSON Output Contract、Schema Introspection、Agent Skills。
- 参考时 main 的 commit 为 `d5209485e57a0c2205ab88959811bfefe905ce29`。仅参考公开设计说明，没有复制上游实现。
- Weora 采用任务级命令、自描述和结构化输出；暂不采用上游完整三层命令体系或扩展机制。

完整接入方案见 [CLI 与团队上下文接入方案](../architecture/weora-cli-and-team-context.md)。
