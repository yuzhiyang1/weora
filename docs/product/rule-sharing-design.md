# Weora Rule 共享设计

状态：设计稿 · 2026-11-25。以 TeamAI CLI `1104f3058d268e6a00926e1f6d0bd7c724dbe0a6` 的实际行为为兼容基线。Weora 用 PostgreSQL 和工作空间 API 提供团队仓库的读写能力。Rule 的资源模型、用户命令、作用域、发布语义和工具投递均沿用该基线。 [架构决策草案](../adr/0031-postgres-rule-sharing.md)

## 1. 产品入口与行为对照

Rule 是 TeamAI 资源体系中和 Skill、Doc、Learning 并列的资源类型。控制台入口沿用 **Resources → Rules**，展示规则名、命名空间、说明和当前工作区的交付结果。未交付原因沿用命名空间、标签和同名覆盖三类。角色与项目的选择沿用统一分发配置，当前检出通过 `projects set` 选择项目。`recall` 可以检索 Rule，使用的仍是该工作区实际收到的规则集合。

| 环节 | TeamAI CLI | Weora |
| --- | --- | --- |
| 编写 | `rules/<name>.md` 或 `rules/<name>/RULE.md`，可放在命名空间下 | 保留相同的逻辑路径、Markdown 和文件树。源文件编辑走团队资源存储接口 |
| 普通提交 | `teamai push` 扫描本地改动，推送分支，创建或复用 PR/MR | `weora push` 沿用扫描和选项，API 保存候选分支与合并请求，由工作空间的团队资源权限承接 Git 权限 |
| 生效 | 合入默认分支后成为可同步版本。导入命令另有直接写入路径 | 事务推进 PostgreSQL 默认引用。普通合并和授权直写分别对应上游入口 |
| 选择 | CLI 根据共享根目录、角色和项目 `knowledge` 命名空间、个人标签订阅解析规则 | API 提供已授权的快照和作用域事实，CLI 使用同样的过滤和覆盖逻辑生成有效集合 |
| 分发与加载 | `teamai pull` 和宿主启动钩子协调更新，工具按原生方式加载 | `weora pull` 和同类钩子协调更新，继续走工具原生路径 |
| 查询与诊断 | `list rules`、`status`、`doctor`、`recall`、Resources → Rules | 沿用入口及各自的数据范围。`list rules` 列出团队源，`status` 比较本地和团队源，`doctor` 检查实际交付，资源页展示当前工作区的选择结果 |

[TeamAI 共享指南](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/docs/guide/zh-CN/sharing.md#rules) · [资源页实现](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/dashboard/client.ts#L167) · [分发指南](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/docs/guide/zh-CN/distribution.md)

## 2. 规则格式与作用域

### 规则原文

单文件规则用 `.md`，目录型规则用 `RULE.md` 加可选的 `RULE.<lang>.md` 和辅助文件。`rules/backend/api-style.md` 与 `rules/backend/api-style/RULE.md` 代表同一个逻辑键 `backend/api-style`，只能选一种形式。首段目录是命名空间，文件正文对所有工具使用同一份中立格式。`paths` 接受 TeamAI 支持的文件通配模式，`related` 描述规则与文档、经验、代码知识的关系。

```text
rules/
  code-review.md
  backend/
    code-review.md
    java-vo/
      RULE.md
      RULE.en.md
      references/example.md
  frontend/
    code-review.md
```

目录型规则以 `RULE.md` 的 frontmatter 为准。语言文件可替换正文，缺少对应语言文件时读取基础正文。`${TEAMAI_RULE_DIR}` 由 CLI 替换成工具实际可访问的辅助目录。配置路径使用相对路径和正斜杠，服务端与 CLI 均校验路径逃逸、符号链接、同名及大小写冲突，导入和导出保留原文与目录结构。 [TeamAI 单文件解析](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/resources/team-rule.ts) · [目录型规则](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/resources/rule-folders.ts)

### 规则的选择顺序

工作空间访问权限承接 TeamAI 的团队仓库读写权限。CLI 读取同一修订中的 `rules/`、`teamai.yaml`、`manifest/roles.yaml`、`manifest/projects.yaml`、`manifest/members.yaml` 和 `tags.yaml`，按 TeamAI 的顺序计算规则。角色、项目与标签控制本次投递范围。成员自行选择的项目保存在当前检出的 TeamAI 兼容配置里，团队声明与成员分配随团队仓库修订一起同步。

1. **确定安装范围。** 用户范围绑定一个团队源和工具配置实例。项目范围由当前检出的团队安装和 `projects set` 选中的活动项目确定，支持同一检出激活多个项目。会话按工作目录定位有效配置。父目录管理多个项目检出时，父范围对同一团队源的子检出分别求值，再按完整规则路径取并集。子检出会话继续使用自己的有效配置。
2. **确定活动命名空间。** 使用成员主要角色、附加角色和活动项目声明的 `knowledge` 命名空间并集。工作空间权限角色与知识分发角色分别使用，选择知识分发角色不改变 Owner、Admin 等平台权限。管理员设置了成员角色分配或项目分区时，成员没有符合的角色和项目就只接收共享根规则。沿用 TeamAI 的无过滤模式时，所有规则按照各自路径下发。工作流显式指定 TeamAI 式执行角色时，规则按该角色和活动项目求值，跳过成员个人标签过滤，项目和工作空间授权继续生效。
3. **应用标签。** 先处理命名空间，再处理标签。未订阅标签时不按标签过滤。订阅后，未标标签规则和至少匹配一个订阅标签的规则保留。标签只能筛选已进入候选集的规则。
4. **解析覆盖。** 在有命名空间过滤的范围里，通过筛选的 `rules/<namespace>/<name>.md` 或等价目录可以覆盖 `rules/<name>.md`。两个活动命名空间的同名规则按各自完整路径同时交付，深层路径保留原样。某个命名空间规则被标签排除后，不再覆盖根规则。工具扁平化路径产生实体冲突时才进入工具侧冲突处理。
5. **合入平台默认内容。** 保留 `rules/teamai-recall/` 的逻辑名称、默认文件、团队覆盖、语言变体、有效性检查和关闭逻辑。团队根目录同名目录可覆盖允许替换的文件，其他文件由 CLI 内置版本补齐。组合无效时回退内置版本并给出原因。内置指令指向 Weora 的对应 `recall` 入口，检索结果遵循 TeamAI 的规则、文档、经验和代码知识的访问范围。

同一设备的用户级范围和项目级范围各自求值。项目可以显式继承用户范围的自动同步和规则检索。Codex 原生读取用户级 `AGENTS.md`，项目钩子只附加该项目范围的结果，两者可同时生效。`paths` 控制工具如何表达文件适用范围，项目绑定控制哪些会话会收到规则。

[TeamAI 命名空间解析](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/resource-namespaces.ts#L141) · [筛选、覆盖与工作区并集](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/resources/desired.ts#L566) · [内置 Recall Rule](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/rules/teamai-recall/README.zh-CN.md)

### 一个项目例子

团队发布共享 `rules/code-review.md` 和 `rules/backend/code-review.md`，在 `manifest/projects.yaml` 给 `weora` 项目声明 `knowledge: [backend]`。成员在一个检出执行 `weora projects set weora` 后，新会话收到后端版。另一个未激活 `backend` 的检出仍收到共享版。若后端版打了 `java` 标签，而该成员只订阅 `python`，该成员仍收到共享版。若 `backend` 和 `frontend` 的同名规则都通过过滤，项目会话收到两条命名空间规则。

## 3. 从提案到 PostgreSQL 发布

### 操作流程

1. **扫描变更。** `push` 读取 TeamAI 会扫描的工具规则目录，按完整逻辑键、投递记录与原始内容判断新增和修改。原生转换格式仅将已有团队规则的正文写回，保留上游 frontmatter。目录型 Rule 从团队源编辑。新规则通过 `--shared`、`--project` 或 `--role` 指定落点，省略时沿用候选命名空间及交互选择规则。记录本机已发布规则的目标路径，后续修改继续使用同一落点。
2. **保存分支和合并请求。** 普通 `push` 按 TeamAI 的选择、分组和重试方式生成或复用候选分支与请求。PostgreSQL 保存候选引用、变更路径、基线修订和请求状态。`teamai.yaml` 的 `reviewers` 对应请求的评审人，合并权限对应上游 Git 提供方对默认分支的写入权限。`review --branch` 读取候选分支并沿用 Rule 的结构、格式和关系校验。合并按请求的基线和候选内容检查冲突，再以一个事务推进默认引用。遇到重试、分支已写但请求未建或基线已变化时，沿用上游的可重试结果与冲突提示。
3. **导入和删除保留原入口。** `import --from-tools` 对候选逐条分类为 Rule、Doc、Learning，按 TeamAI 工作流确认、编辑或跳过。`--all` 和 `--output` 沿用原语义。接受的导入通过团队源的授权直写路径提交，直写失败保留错误。`remove rules <names...>` 清理发起端可确认归属的副本、记录删除标记并提交团队删除分支。其他成员在默认引用合入后同步清理。
4. **固定和恢复跟随。** `pull --team-ref <ref>` 保留候选分支、固定标签、固定修订三种读法，并使用默认分支名解除固定。分支会跟随新修订。固定期间暂停原基线会拒绝的写操作，作用域及模式限制同 TeamAI。团队共享规则回退通过向默认引用提交恢复历史内容的变更完成。

[TeamAI 扫描与回写](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/resources/rules.ts#L79) · [普通提交与请求复用](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/push.ts#L1006) · [导入直接发布](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/import-local.ts#L279) · [删除实现](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/remove.ts#L296) · [固定版本的适用条件](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/team-ref.ts#L16)

### PostgreSQL 团队存储

| Git 概念 | PostgreSQL 映射 |
| --- | --- |
| 团队仓库与默认分支 | 工作空间内的团队源记录与默认引用。团队源是分发与权限边界 |
| commit 与 tree | 不可变修订、父修订、作者、时间，以及 `revision + path → blob` 的文件树。`rules/` 和同批配置文件通过同一修订读取 |
| blob | 原始文件字节及摘要。目录由其文件路径组成，未修改文件可复用既有 blob |
| 候选分支与标签 | 可移动的候选引用和不可变标签引用。沿用分支、标签、修订 ID 三种固定语义 |
| PR/MR | 团队源的通用合并请求记录，含基线引用、候选引用、评审人、状态和合并后的修订 |

存储接口提供引用解析、按修订读文件树、更新候选引用、提交或查询合并请求、合并至默认引用及授权直写。Rule 继续以完整资源路径标识，版本来自团队源修订。所有存储关联带 `workspace_id`，读写分别验证团队源权限。更新引用校验预期修订，内容和引用同事务提交，客户端复用现有设备身份及本地缓存。 [Weora 设备身份实现](../../backend/src/weora/modules/collaboration/infrastructure/agent_sessions.py#L99)

## 4. CLI 与工具加载

### 同步、安装与清理

- 使用通用入口 `weora init`、`weora push`、`weora pull`、`weora list rules`、`weora status`、`weora doctor`、`weora remove rules` 和 `weora uninstall`。分发选择沿用 `roles set`、`projects set`、`tags subscribe`。安装默认作用域为 `workspace`，`--scope user` 与 `--inherit-user-scope` 保留原语义。 [TeamAI CLI 命令定义](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/index.ts#L200)
- 同一仓库的 linked worktree 共享团队安装配置，各检出分别投递和记录归属。self 模式的业务提交携带 PostgreSQL 团队源标识、不可变修订引用与启动配置。发布时先写入 PostgreSQL 修订，再随业务提交发布引用，保持资源随检出切换及克隆后引导的行为。新 worktree 仍先走 Git checkout hook，缺失时由会话或工具钩子补齐。
- `pull` 先刷新本地团队源快照，再按当前作用域解析和写入。同步保留团队源版本、本地归属与已投递摘要，同一物理目标加锁。修订相同时沿用上游快速路径和修复条件。辅助文件先准备，再写目标规则。项目和用户辅助目录维持各自的交付位置，实际位置由 Weora 的本地数据根和上游工具路径规则解析。
- 清理沿用 TeamAI 的归属、摘要与旧版格式判定。手工修改的副本、个人文件和其他来源管理的文件按原冲突行为保留。内联指令文件更新标记内区块。已保留的目录型 Rule 仍可读其辅助文件。清理、切换和卸载使用同一套归属规则。团队源刷新失败时记录失败，启动钩子继续按已取得的本地团队源版本处理。

### 工具通道

工具描述表按用户与项目范围定义原生路径、格式、可用事件、辅助目录读取权限及验证证据。兼容范围以 TeamAI 固定版本的 Rule 支持表为准。每个工具继续使用自己的规则格式和启用条件，现有 Weora Skill 工具表只提供可复用的工具识别、配置路径和 Hook 合并能力。

| 通道 | 用户范围 | 项目范围 |
| --- | --- | --- |
| 原生规则文件 | Claude Code 使用 `.claude/rules`，Cursor 使用 `.cursor/rules` 并渲染 `.mdc`，Copilot 使用 `instructions` | Claude Code、Cursor、Copilot 等使用各自的项目规则目录。OpenCode 同步维护其 `instructions` 引用 |
| 指令文件、会话钩子或扩展 | Codex、Pi、Hermes、OpenClaw 等按支持表合并各自的用户指令文件。Cursor 对其原生路径无法覆盖的工作区沿用 TeamAI 的钩子补充行为 | Codex、ZCode、DeepSeek Harness、Muse 使用会话钩子，Pi 使用扩展。Hermes、OpenClaw 按上游矩阵只有用户范围 |

`paths` 由目标工具解释。支持原生 glob 的格式使用原生字段。Codex 等内联通道剥离 frontmatter，在正文前加 `Applies to files matching: …`，整段进入上下文。团队 Rule 描述模型应遵循的约定，命令权限仍由宿主和平台授权策略管理。上游只证明文件写入或读取未知的条目保留相应证据等级，Weora 真实会话验收再更新状态。 [TeamAI 工具支持矩阵](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/docs/guide/zh-CN/tool-support.md) · [规则格式实现](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/resources/rule-format.ts)

### Codex 交付时序

1. **用户范围。** `pull` 写入 `$CODEX_HOME/AGENTS.md` 的管理区块，默认目录为 `~/.codex`。保留区块外文字。发现宿主会优先读取非空 `AGENTS.override.md` 时，`doctor` 报告用户指令被遮蔽。用户文件与项目绑定分别管理。
2. **项目范围。** `pull` 把 `SessionStart` 和 `SubagentStart` 合并到 `$CODEX_HOME/hooks.json`。采用业务仓库自引导时，项目入口写在 `.codex/hooks.json`，由宿主按项目信任及 worktree 规则发现。两种入口都使用 `additionalContextLimit: 0` 避免宿主沿默认钩子上限截断。合并时保留原有钩子并检查宿主信任。活动项目规则由启动钩子返回，项目自带的 `AGENTS.md` 按 Codex 原生规则继续加载。
3. **会话启动。** 钩子根据 `cwd` 解析项目绑定，从上一份完整本地快照取规则，返回 `hookSpecificOutput.additionalContext`，其中 `hookEventName` 与当前事件一致。`SessionStart` 在新会话、清空和压缩时附加规则，`resume` 沿用历史而跳过重复注入。`SubagentStart` 同样按其项目范围附加规则。当前会话的前台注入和后台刷新分开执行，刚发布的规则可先手动 `pull` 再打开新会话验证。
4. **验证交付。** `doctor` 对照上游分别检查规则所在位置和启动钩子，例如两个启动事件的 `additionalContextLimit: 0`、信任状态及用户级文件的覆盖。在真实 Codex 会话中使用唯一测试规则，分别验证主会话与子会话是否取得正文。

[TeamAI Codex 钩子](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/hook-handlers.ts#L1119) · [钩子参数](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/builtin-hooks.ts#L625) · [Codex 钩子协议](https://learn.chatgpt.com/docs/hooks#sessionstart) · [Weora 的现有钩子合并入口](../../apps/cli/src/weora_cli/collab_native.py#L38)

## 5. 模块边界与验收

- 团队源存储适配层负责 PostgreSQL 文件树、引用、合并请求和权限，复用 Weora 工作空间身份与设备认证。资源路径和团队清单都从同一修订读取。
- CLI 保留 TeamAI 的资源处理器边界。`rules` 处理器接入通用扫描、发布、同步、卸载和诊断。规则解析、工具适配及投递归属继续在 CLI。资源页复用相同的规则选择结果。
- `recall` 对当前范围有效的 Rule 建索引。`related` 的目标和反向关系按同一范围判断，保留 TeamAI 的资源路径和关系类型。 [TeamAI Recall 与资源关系](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/docs/guide/zh-CN/knowledge.md#资源之间的关系)

| 验收面 | 可复核的通过条件 |
| --- | --- |
| TeamAI 行为对照 | 用相同团队文件树、CLI 参数、成员角色、项目和工具配置分别跑 TeamAI 与 Weora。对比命令结果、活动规则、未交付原因、生成的工具正文、钩子事件和清理结果。对照 `pull-rules-role-filter`、`pull-namespace-override`、`rule-folders`、`rule-render-contracts`、`codex-hook-rules` |
| Git 与 PostgreSQL 存储对照 | 集成测试分别验证候选分支未合入、合入后生效、重复提交复用、授权直写、移除、固定分支与固定标签、合并冲突、事务失败和工作空间隔离 |
| 本机同步与清理 | 实际 CLI 验证首次与重复同步、变更后收敛、缓存损坏、安装中断、个人修改、卸载、多个工具共享物理路径及多 worktree 隔离 |
| Codex 实机 | 从发布一条唯一正文开始，覆盖默认与自定义用户目录、项目与用户两条渠道、项目外会话、子会话、恢复、压缩和信任状态。保存工具版本、有效规则、目标文件或钩子输出和真实会话结果 |
| 其他工具 | 按上游矩阵逐端记录文件落盘、工具实际读取、钩子或扩展是否可用。路径映射和自动测试分别记录，真实读取有独立证据 |

验收以固定提交的 TeamAI Rule 工具矩阵和可观察行为为准。双方允许的对照差异是命令品牌前缀、团队源存储标识、认证和存储相关本机路径。其他行为差异应在实现中修正。

### 基线核对说明

TeamAI 文档描述了 `sharing.rules.enforced`，当前参考提交可查到 schema、初始化默认值及说明，源码中未发现它被规则选择、删除或注入逻辑读取。以参考提交的运行行为为验收口径，该字段随团队源配置保留。 [TeamAI 配置声明](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/types.ts#L128) · [TeamAI 删除入口](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/remove.ts)
