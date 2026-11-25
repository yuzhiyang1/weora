# ADR 0031 用 PostgreSQL 承载团队 Rule 发布

- 状态：Proposed
- 日期：2026-11-25
- 功能设计见 [Weora Rule 共享设计](../product/rule-sharing-design.md)。

## 背景

TeamAI CLI 把规则正文、作用域配置及发布历史保存在 Git 中。CLI 通过统一的资源命令处理 `rules`，在成员侧解析交付范围，经工具原生规则文件、指令文件或会话钩子交给模型。Weora 复用工作空间身份和设备凭据，替换 TeamAI 的 Git 团队源。 [TeamAI Rule 实现基线](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/resources/desired.ts) · [Weora Skill 共享决策](0028-postgres-skill-sharing.md)

## 决策

1. TeamAI CLI `1104f3058d268e6a00926e1f6d0bd7c724dbe0a6` 是 Rule 行为基线。保留资源格式、统一命令、项目与用户作用域、命名空间、标签、覆盖优先级及工具投递方式。
2. PostgreSQL 以团队源为单位保存文件树、不可变修订、分支及标签引用、合并请求。规则和分发配置在同一修订内读取。普通提交沿用候选分支和合并时机，直写导入沿用独立入口。更新文件树和引用在同一事务提交。
3. 工作空间 API 承接 Git 团队仓库的访问权限。CLI 读取和缓存授权的修订，继续负责规则选择、渲染、落盘、注入和清理。Codex 用户范围走 `AGENTS.md`，项目范围走前台启动钩子。
4. `rules` 作为通用资源类型接入 `push`、`pull`、`list rules`、`remove rules`、`recall` 和诊断。控制台沿用 Resources → Rules 的职责。规则用完整资源路径定位，项目通过 `knowledge` 命名空间选择，版本来自团队源修订。

## 影响

PostgreSQL 从同一修订恢复团队文件树，CLI 按检出配置得到和 TeamAI 一致的规则结果。本机原生路径、更新时序与归属清理依据由工具适配器保持。对照验收以相同输入下的规则集合、命令结果、原生输出及清理行为为准。
