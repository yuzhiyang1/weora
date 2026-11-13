# ADR 0028 PostgreSQL 存储与 Skill 共享

- 状态：Proposed
- 日期：2026-11-12
- 交付设计见 [Weora Skill 共享落地设计](../product/skill-sharing-design.md)。

## 背景

Skill Registry 已按工作空间管理草稿和不可变版本，版本表保存包摘要与对象存储键，Agent 版本绑定保存相同的包定位和完整性信息。团队分发需要将发布、安装结果和使用记录连接到同一个 Skill 版本。

## 决策

1. Skill 的草稿和发布版本统一引用同一工作空间下的 PostgreSQL 包记录。包体用独立表的 `bytea` 保存，完整 ZIP 按包摘要复用。发布更新版本、草稿和审计时使用同一事务。
2. Agent 运行时通过 Skills 模块按工作空间与版本 ID 读取包，保留固定版本和摘要校验。该决策更新 [AgentVersion 与 Runtime Adapter 边界](0002-agent-version-and-runtime-adapter-boundary.md) 中的包定位方式。对象存储仍承载知识库 PDF 和通用附件。
3. CLI 复用 TeamAI 的工具目录和事件适配方案，首版覆盖其 README 的 21 个公开工具，支持用户和项目范围。会话启动同步清单，安装、更新或清理可确认归属的副本。成员可以固定版本，设备回执记录实际安装版本。
4. Hook 按可证实的已安装版本记录点名和加载。同一轮任一信号都计一次使用，服务端保留两个信号，按成员、版本与观测时间查询。
5. 历史 OSS 包先回填验哈希，再切换读写。迁移期间保留原读取路径供补齐和回退。生产切换前验证历史 Agent 运行与旧版本下载。

该决策更新 [Provider 无关的对象存储 ADR](0002-provider-neutral-object-storage.md) 中关于 Skill 包的存储选择。包体进入数据库备份和恢复范围，实施时记录典型包和上限包的容量及恢复验证。

应用 `20261112_0032` 后，先用下列命令核对历史记录，再用同一命令加 `--apply` 补齐已校验的旧包。每个工作空间单独执行；核对候选数为零并验证历史下载后，再考虑停用旧存储的 Skill 访问。降级脚本会拒绝删除仍有 PostgreSQL 独有包的表。

```powershell
python -m weora.modules.skills.infrastructure.backfill --workspace <workspace-id>
```
