# Weora Skill 共享落地设计

当前代码已实现 PostgreSQL 包存储、工作空间分发、21 个工具安装入口、启动同步、结构化用量及成员候选包审核。角色、项目和标签策略仍是设计目标。TeamAI 未验证的 Trae 和 Kiro 事件按现行官方契约补齐；工具矩阵中的源码与文档证据不等于 Weora 实机验收。

状态：设计稿 · 2026-11-12。Skill 共享首版覆盖 TeamAI README 的 21 个公开工具。成员完成工作空间绑定后，CLI 将适用的团队 Skill 安装到工具原生目录。工具启动新会话时自动同步，成员也可以固定版本、手动更新或回退。开启使用统计后，控制台可按成员、版本和日期查询。

相关决策见 [ADR 0028](../adr/0028-postgres-skill-sharing.md)。沿用 [Skill Registry v0.1](skill-registry-v0.1-design.md) 的资产、校验和发布流程。

## 1. 从现有实现接入

| 位置 | 当前行为 | 本次接入 |
| --- | --- | --- |
| [Skills 模型与服务](../../backend/src/weora/modules/skills/application/service.py) | PostgreSQL 保存版本和文件清单，发布时把草稿包复制到 OSS，下载按版本校验对象大小与摘要 | 包体迁到 PostgreSQL，复用包校验、版本、权限和活动记录 |
| [Agent 版本绑定](../../backend/src/weora/modules/agents/infrastructure/skill_catalog.py) 与 [运行时装配](../../backend/src/weora/modules/agents/application/runtime.py) | 绑定包含 `skill_version_id`、存储键、大小和摘要 | 通过 Skills 应用层按版本 ID 读取包，继续核对原快照的大小和摘要 |
| [设备鉴权](../../backend/src/weora/modules/collaboration/infrastructure/agent_sessions.py) 与 [CLI 客户端](../../apps/cli/src/weora_cli/collab_client.py) | 一次性配对码、设备 Bearer、系统钥匙串，设备接口复查工作空间成员身份 | 共用设备身份和配对，按操作授予 Skill 权限 |
| [原生 Hook 安装](../../apps/cli/src/weora_cli/collab_native.py) | 合并 Codex 的 `hooks.json`，已有会话协作 Handler | 复用配置合并和运行时实例识别，增加独立的 Skill 采集 Handler |

TeamAI CLI 源码固定在 `1104f3058d268e6a00926e1f6d0bd7c724dbe0a6`。分发、同步和采集沿用它的做法：

- 安装后记录路径、来源与摘要，更新前核对副本归属。 [TeamAI 目标目录解析](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/resources/skills.ts#L234)
- 会话启动时后台同步，近期拉取结果可复用。按成员角色、项目和订阅规则计算应交付的 Skill 集合。 [TeamAI 启动 Hook](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/hook-handlers.ts#L121) · [TeamAI 分发规则](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/docs/guide/zh-CN/distribution.md#角色和项目)
- 在提示词提交和工具调用结束时采集信号，以会话轮次去重。 [TeamAI Hook 计数](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/skill-use.ts#L538)
- 先在本机持久化，失败时保留并重试。TeamAI 将成员累计值写入 Git 的 `teamai-reports`。Weora 用 PostgreSQL 保存带时间和版本的轮次记录，查询时再聚合。 [TeamAI 上报](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/pull.ts#L4150) · [TeamAI 看板](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/dashboard/improvement.ts#L193)

### 源码复用范围

以 TeamAI 的工具表、默认写入路径、Hook 适配、事件解析及对应测试样本为移植来源。路径配置转为 Weora 的工具描述，Python CLI 复用这些配置和行为。宿主本身要求 TypeScript 或 JavaScript 的插件、扩展和 Hook 桥接可直接移植相应片段，调用统一的 Weora CLI 入口。安装状态、鉴权、发布和统计使用 Weora 自身的模型。

TeamAI CLI 仓库采用 MIT 许可证。复制或改写源码时保留上游版权与许可声明，记录上游 commit、文件和用途，并在随 CLI 分发的第三方声明中收录对应许可。 [TeamAI LICENSE](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/LICENSE) · [工具描述](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/tools.ts#L508) · [默认写入路径](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/src/types.ts#L475)

### 首版工具矩阵

下表的路径是 TeamAI 的默认写入候选，项目路径相对检出根，用户路径还要应用工具的环境变量和配置实例。Codex 另支持经原生识别验证的项目 `.agents/skills`。同步入口和读取路径要按本机工具版本探测。最后一列是 TeamAI 的统计证据标记，`v` 为实机，`s` 为源码，`t` 为 TeamAI 侧代码，`+` 为上游工具有能力但 TeamAI 尚未实现，`?` 为未知。 [TeamAI 产品矩阵](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/README.zh-CN.md#产品概览) · [详细工具支持表](https://github.com/Tencent/teamai-cli/blob/1104f3058d268e6a00926e1f6d0bd7c724dbe0a6/docs/guide/zh-CN/tool-support.md)

| 工具 | CLI ID | 项目写入候选 | 用户写入候选 | 会话入口 | TeamAI 统计证据 |
| --- | --- | --- | --- | --- | --- |
| Claude Code | `claude` | `.claude/skills` | `~/.claude/skills` | settings.json | `v` |
| Codex CLI | `codex` | `.codex/skills` | `~/.codex/skills`、`~/.agents/skills` | hooks.json | `t` |
| Cursor | `cursor` | `.cursor/skills` | `~/.cursor/skills` | hooks.json | `v` |
| CodeBuddy | `codebuddy` | `.codebuddy/skills` | `~/.codebuddy/skills` | settings.json | `v` |
| WorkBuddy | `workbuddy` | `.workbuddy/skills` | ~/.workbuddy/skills 或实际配置根 | settings.json，核对实际加载 | `?` |
| GitHub Copilot CLI | `copilot` | `.github/skills` | `~/.copilot/skills` | hooks/teamai.json | `t` |
| OpenCode | `opencode` | `.opencode/skills` | `~/.config/opencode/skills` | 插件 | `t` |
| Pi Coding Agent | `pi` | `.pi/skills` | `~/.pi/agent/skills` | 扩展 | `t` |
| Oh My Pi | `omp` | `.omp/skills` | `~/.omp/agent/skills` | 扩展，按 Profile 定位 | `t` |
| OpenClaw | `openclaw` | `.openclaw/skills` | `~/.openclaw/skills` | 内部 Hook，区分 Gateway 与会话事件 | `+` |
| Hermes | `hermes` | `.hermes/skills` | `~/.hermes/skills` | 插件，按 Profile 定位 | `+` |
| DeepSeek Harness | `dsh` | `.dsh/skills` | `~/.dsh/skills` | 原生 Hook | `t` |
| Qoder | `qoder` | `.qoder/skills` | `~/.qoder/skills` | settings.json | `s` |
| Qoder CN | `qoder-cn` | `.qoder/skills` | `~/.qoder-cn/skills` | settings.json，区分 CN 配置根 | `s` |
| Kiro | `kiro` | `.kiro/skills` | `~/.kiro/skills` | 原生 Hook 适配 | `?` |
| Trae | `trae` | `.trae/skills` | `~/.trae/skills` | 原生 `hooks.json`，沿用 TeamAI 国际版目录候选，待实机 | `?` |
| Trae CN | `trae-cn` | `.trae/skills` | `~/.trae-cn/skills` | [原生 hooks.json](https://docs.trae.cn/ide_hook-configuration-reference)，须在宿主启用 | `?` |
| ZCode | `zcode` | `.zcode/skills` | `~/.zcode/skills` | CLI config.json | `s` |
| JoyCode | `joycode` | `.joycode/skills` | `~/.joycode/skills` | hooks.json | `t` |
| Muse Code | `muse` | `.agents/skills` | `~/.config/muse/skills` | 用户 settings.json、项目 hooks.json | `v` |
| Antigravity | `agy` | `.agents/skills` | `~/.gemini/config/skills` | 原生 Hook 适配 | `v` |

本机状态分别展示文件已写入、原生 Skill 已识别、会话同步入口可用、使用采集可用四项结果。已验证的原生 Hook 或扩展优先。宿主缺少可验证的会话入口时，通过 Weora 启动命令先同步再启动工具，并把可用入口显示给用户。仅有写入路径的工具需要用真实会话确认加载后，才算通过分发验收。统计入口或轮次不可验证时，展示该工具的采集缺口。

## 2. PostgreSQL 存储与发布

| 数据 | 设计 |
| --- | --- |
| `skill_packages` | 新增独立包表。字段为 `id`、`workspace_id`、`package_sha256`、`size_bytes`、`archive_bytes bytea`、`created_at`。同工作空间按包摘要复用，唯一键为 `(workspace_id, package_sha256)`。写入前验摘要，大小字段与实际字节长度一致。包内容不可修改 |
| `skill_drafts` | 草稿引用 `package_id`，并保存供发布校验的草稿修订号。替换草稿时换引用 |
| `skill_versions` | 继续保留现有版本 UUID、版本号、文件清单、兼容性、两个摘要和发布时间。增加 `package_id`，发布后冻结引用。用带工作空间的外键约束草稿、版本与包归属 |
| `skill_events` | 沿用发布、下载等审计，新增安装操作审计。统计查询从使用记录读取 |

`package_sha256` 校验 ZIP 字节，`content_sha256` 校验规范化文件内容。CLI 下载校验 ZIP 摘要，安装前后按清单核对文件。完整 ZIP 仍包含 `SKILL.md`、`references/`、`scripts/` 和其他资源。知识库 PDF 和通用附件继续走对象存储。

上传复用 [SkillPackageInspector](../../backend/src/weora/modules/skills/domain/package.py)，沿用现有 50 MiB 上传、200 MiB 解压和 5,000 条目上限。包表的实际归档大小和并发内存预算要用规范化 ZIP 实测。列表、解析版本和统计查询只读元数据。下载按精确版本读取单个包，对大包读取设置并发预算，释放数据库连接后发送响应，避免慢下载长期占用数据库连接。

发布使用同一数据库事务。锁定 Skill 行，核对待发布的草稿修订与可发布状态，插入版本、更新发布指针、删除所发布的草稿并写审计。草稿替换和归档使用相同锁顺序。事务重试时通过版本唯一键保证只有一个结果，草稿已换版时返回冲突，由用户重新确认发布的内容。

### 兼容已有 OSS 包

1. 增加包表和可空包引用。按草稿、版本的原始 OSS 键读取历史包，核对现有包大小和 SHA-256 后回填。按包摘要幂等写入，关联草稿时比较其原键与修订，避免回填写到后来替换的草稿上。对缺失、损坏的包记录失败，修复后可重跑。
2. 兼容期读取优先查 PostgreSQL，只有尚未回填的记录继续走原 OSS 键。Agent 的新旧绑定都通过 Skills 应用层按 `workspace_id + skill_version_id` 读取，仍校验快照的大小和摘要。已发布 Agent 的版本 ID 和原快照保持原样。
3. 在兼容程序全部部署后短暂冻结 Skill 写入，完成最后一轮对账，再切换所有写入到 PostgreSQL。切换前处理旧 `storage_key` 字段的非空约束，新绑定使用版本 UUID 和摘要，旧绑定保留快照。确认 API 下载和历史 Agent 运行的字节一致后，结束 OSS 回退读取。若已产生 PostgreSQL 独有的新版本，回退到旧程序前须将这些包导回原读取路径并核验摘要，再切换写入。OSS 旧副本保留至回退窗口结束。

## 3. CLI 安装和更新

项目范围使用明确绑定的 Git 检出，同一设备可以使用多个独立检出。用户范围面向该工具的个人配置实例，CLI 要求明确选定工作空间。项目启动同步先检查当前检出的绑定，同时按本机配置处理已绑定的用户范围。以下以 Codex CLI 为例，其他工具使用上表的 CLI ID。

```powershell
weora skill bind --runtime codex --project .
weora skill pull --runtime codex --project .
weora skill push ./release-check
weora skill install release-check@1.2.0 --runtime codex --project .
weora skill status --runtime codex --project .
weora skill follow release-check --runtime codex --project .
weora skill update release-check --to 1.2.0 --runtime codex --project .
weora skill uninstall release-check --runtime codex --project .
weora skill install-hook --runtime codex --project .
weora skill usage flush --project .
```

需要通过 Weora 启动的工具使用 `weora skill launch --runtime <id> --project . -- <宿主参数>`。CLI 用已探测的宿主入口启动，透传参数与退出状态。同步遇到网络失败时继续用当前已安装版本启动。

浏览器确认 CLI 登录时为新设备开启当前工作空间的 Skill 同步和使用统计，设备所有者可关闭统计。首次 `bind` 绑定并安装 Skill，同时配置后续启动同步的 Hook。用户范围通过 `--scope user` 指定。项目范围使用当前 worktree 根，Codex 项目优先使用验证过的 `.agents/skills/<slug>/`。凭据按绑定的服务和工作空间从钥匙串读取。对未跟踪的项目目标写入精确的本地 Git 排除规则，避免误提交。 [OpenAI Docs · Codex Skill 目录](https://developers.openai.com/codex/skills#where-codex-loads-local-skills)

| 动作 | 行为 |
| --- | --- |
| 同步 | 会话启动与手动 `pull` 共用同步逻辑。同步清单中新增的 Skill 自动安装，已有且跟随团队的副本切换到清单指定版本，已退订或撤回分发的副本按归属检查后移除 |
| 手动安装 | `install slug` 跟随团队版本，`install slug@version` 固定已发布版本。每次下载都先解析到版本 UUID。已安装且摘要一致时复用当前副本 |
| 更新与回退 | `update slug` 主动检查该 Skill 的团队发布目标，固定版本时提示当前固定值。 `update slug --to <version>` 切换并固定指定版本，支持回退。 `follow slug` 恢复跟随团队目标并同步 |
| 卸载 | 仅清理登记在当前安装记录中、摘要仍一致的目录，并记录本地排除项。后续主动安装可以恢复订阅 |
| 归档 | 归档 Skill 停止向设备分发，下一次完整同步移除仍由 Weora 管理且未改动的副本。历史版本仍供原 Agent 绑定和授权的 Web 下载读取，统计仍可按原版本查询 |

服务端根据成员、工作空间、所绑定项目和分发策略生成完整清单。沿用 TeamAI 的团队共享、角色与项目 namespace、可选标签订阅。工作空间可把已发布 Skill 共享给全体成员，也可按已登记的角色或项目限定范围。标签订阅只扩展用户在有权接收的集合中主动选择的 Skill。同一 Skill 的多个适用策略指向不同目标版本时，在清单中报告冲突并保留本地版本。现有 Registry 保留工作空间内唯一 slug，多个独立 Skill 分别使用唯一 slug。

成员可通过 `weora skill push <目录>` 提交 Skill。新标识符直接创建草稿；已有同名 Skill 时进入候选评审，由 Owner 或 Admin 决定是否接受为当前草稿，接受时校验草稿修订。当前工作空间的成员均可发布通过校验的草稿，发布版本和活动记录均保存实际发布人。

原生会话启动通过已安装的 Hook 或扩展调用统一同步入口，例如 Codex 和 Claude Code 的 `SessionStart`。可后台执行的工具使用后台同步，当前会话在宿主重新发现 Skill 后使用新版本。近期清单查询可以合并或复用，同一物理安装目标加锁。版本相同直接跳过下载，遇到本地改动保留副本并提示。离线、拉取失败或清单不完整时保留当前副本，下次会话或手动 `pull` 重试。只有成功取得完整清单后才执行移除。服务端确认撤回分发后，固定版本也受相同的授权范围约束。用户可以通过状态命令查看当前版本、同步入口、最近检查结果和冲突。

安装前扫描目标与该工具在当前范围可辨认的其他同名候选。已跟踪目录、个人副本、另一工作空间管理的同名目录或本地修改都返回冲突，展示需要用户处理的路径。多个工具解析到同一实际目录时，同一工作空间的同一版本只交付一份，并记录可读取它的工具，所有引用释放后才清理。若从子目录运行时出现同名候选，Hook 按该轮实际工作目录判断归属。通过确定文件路径确认的读取可归因，单凭名字点名且有歧义时跳过。

### 落盘、恢复和安装回执

1. 通过下载接口取精确版本，写入同卷暂存区。验证长度、ZIP 摘要和文件清单。解包校验根目录、路径穿越、链接、大小与条目数。Windows 额外拒绝重解析点、保留文件名、大小写折叠后的路径冲突、替代数据流和尾部空格或点。安装过程只写文件。
2. 在本机用规范化目标路径加锁。保存本次操作的 UUID 和待切换记录，再检查旧目录来源和实际文件摘要。旧目录改名到备份，暂存目录改名到目标。两个改名之间可能中断，因此记录每一步的恢复所需信息。下次运行优先根据目标、暂存和备份的摘要完成提交或恢复旧目录。状态不明时保留现场并报告冲突。
3. 在本机状态库提交安装成功记录及待上报回执，然后清理备份。每次版本切换生成新的 `installation_id`，卸载关闭当前代次。回执包含 `operation_id`、稳定的 `slot_id`、前一代与新一代标识、可读取的工具与配置实例以及版本 UUID。服务端按设备与操作 UUID 幂等，同一目标按前一代校验顺序，避免延迟的旧回执覆盖当前版本。
4. 状态与上报队列保存在 `~/.weora/skills/` 的 SQLite 文件，凭据仍存系统钥匙串。CLI 可分别展示本地安装结果和服务端回执状态。网络失败时保留回执和已安装的版本。相同设备凭据下恢复上传，服务端确认后移除待上传项。

## 4. Hook 采集与版本归因

| 信号 | 识别方式 | 统计用途 |
| --- | --- | --- |
| 提示词提交 | 适配器识别该工具的显式 Skill 语法，例如 Codex 的 `$release-check`、Pi 的 `/skill:release-check`。候选唯一且匹配受管安装时记录 | 点名次数 |
| 工具调用结束或原生 Skill 加载事件 | 对验证过的事件和输出形状，识别成功加载的 Skill。可从实际读取路径、已验证的原生 Skill 工具或宿主扩展取得证据 | 确认加载次数 |
| 会话启动 | 检查绑定并触发同步，更新采集配置和待上报状态 | 同步及采集状态 |

Codex 文档定义了两个轮次 Hook 的 `session_id`、`turn_id` 和工具输入输出。Bash 退出失败时也可能触发 `PostToolUse`，所以只看到命令参数中的路径、目录列表、截断结果或失败结果时，记录不到确认加载。以真实会话样本确认 Windows 和 macOS 的输出形状，再把对应解析器列入支持范围。 [OpenAI Docs · UserPromptSubmit](https://developers.openai.com/codex/hooks#userpromptsubmit) · [OpenAI Docs · PostToolUse](https://developers.openai.com/codex/hooks#posttooluse)

- **按文件定位版本。** 以实际读取路径匹配本机安装记录，核对 `SKILL.md` 内容、受管文件清单和安装代次。用户修改文件、同名来源无法区分、当前轮次与安装切换重叠且证据无法定位实际版本时，标记无法归因并跳过版本统计。用户先点名后加载时，同一版本的两个信号可以落在同一条记录。
- **按会话轮次去重。** 使用 `workspace_id + device_id + runtime + profile_id + session_key + turn_key + skill_version_id`，其中设备绑定唯一成员。客户端生成稳定的本机 HMAC 标识替代原生会话和轮次 ID。采用 TeamAI 的归并方式。同一轮先点名再读取三次，使用总数只加一次，点名和加载各记录一个时间。有宿主轮次 ID 时直接使用。只有提示词事件可可靠标记新轮次且能与工具事件关联的工具，才允许像 TeamAI 一样在本机生成轮次标识。无法稳定关联时展示采集缺口。
- **本机写入后异步上报。** Hook 只做有界解析、归因和本机队列写入，保持宿主正常运行。安装 Hook 复用宿主配置合并，采集按已绑定且开启统计的安装范围归属。已有连接器在线时批量提交，手动 `usage flush` 可补传。回执先于依赖它的使用记录提交。服务端每项返回接受、重复、可重试或永久拒绝，本机按结果重试。原始提示词、工具输出、会话正文和本机路径只在现场解析，事件只发送必要的标识、类型和时间。
- **管理采集权限。** 工作空间开启统计并授权设备后，CLI 明确安装 Hook，Codex 由用户审核信任。关闭本地采集即停止新事件。服务端撤权后拒绝新上传，离线队列在下次连接收到永久拒绝时清理对应条目。设备或成员失效时也停止包读取和上报。已经分发到本机的文件由该设备管理。

适配器记录宿主工具版本和已验证的事件类型。按上表 21 个工具逐端采样，并在状态与报告界面展示采集覆盖。设备未启用采集或该工具无法证明读取时，报告展示覆盖缺口。已有 Hook 的合并、宿主信任确认和不同安装路径均纳入验收。

## 5. 服务端接口与统计

保留现有控制台 Skill API。设备接口前缀为 `/api/v1/workspaces/{workspace_id}/skill-distribution`。每次请求检查设备凭据、活动成员身份和 Skill 授权。浏览器确认 CLI 登录时授予同步权限。手工配对的设备仍由成员在控制台开通同步。设备凭据限定服务源和工作空间。

| 方法与路径 | 请求与结果 | 权限 |
| --- | --- | --- |
| `GET /manifest?runtime=...&scope=...&project_id=...` | 返回完整分发清单和修订标识。每项包含 Skill UUID、slug、目标版本 UUID、摘要和兼容性。项目范围需要已绑定项目，用户范围按成员策略解析。支持条件请求，完整清单用于对账和清理 | `skills:read` |
| `GET /resolve?slug=...&version=...&runtime=...` | 返回有权访问的精确版本 UUID、大小、包摘要、文件清单及兼容性。省略版本时解析当前分发目标 | `skills:read` |
| `GET /versions/{version_id}/package` | 下载固定 ZIP，返回 `Content-Length` 和 `X-Content-SHA256`。复查权限、归档和版本所属工作空间 | `skills:read` |
| `POST /contributions` | 以 multipart 提交完整 Skill 包。新标识符返回待发布草稿，同名包返回待审核提案 ID。控制台可查看校验结果、接受为草稿或驳回 | 当前工作空间的活动成员及已授权 Skill 的设备 |
| `POST /installation-receipts` | 接收安装、更新、回退和卸载回执，返回确认状态。相同操作重复发送返回原确认 | `skills:installations:write` |
| `POST /usage-events/batch` | 接收点名或加载证据，每项包含安装代次、运行时、会话轮次标识、信号类型和观测时间。按项确认 | `skills:usage:write` 且工作空间启用统计 |

`user_id`、`device_id` 和 `workspace_id` 取自服务端验证过的设备及成员关系。统计归属的 `skill_version_id` 取自该设备的已确认安装代次。对跨空间、版本与安装代次不匹配、设备无权的事件返回拒绝。安装回执声明设备端已经完成的操作，服务端用它登记代次。历史代次已经被替换时，只接收发生在该代次有效期间的离线补报。发生时间用设备观测值，接收时间另存供排查。超出可配置时钟偏差或保留范围的事件返回可识别的错误。批量提交可能部分成功，已确认项不再补传。

| 表 | 核心字段和约束 |
| --- | --- |
| `skill_installations` | `installation_id`、`workspace_id`、`device_id`、`user_id`、`slot_id`、安装范围、可读取该代次的工具与配置实例、`skill_version_id`、`activated_at`、`retired_at`。每个物理目标只有一个有效代次，版本与设备的归属受工作空间约束。共享目录中的使用按实际触发事件的工具统计 |
| `skill_installation_receipts` | `operation_id`、`device_id`、`workspace_id`、`slot_id`、动作、前后代次、发生时间和处理结果。设备与操作 UUID 唯一，回执、代次变化和活动审计同事务提交 |
| `skill_usage_turns` | 工作空间、设备、成员、运行时、配置实例、会话轮次标识、版本 UUID、`mentioned_at`、`loaded_at` 和服务端接收时间。按轮次唯一键做幂等合并，两个信号分别保留最早的观测时间。版本外键带工作空间。数据库只存可归因的使用事件 |

在现有 Skill 路由下增加 `GET /api/v1/workspaces/{workspace_id}/skills/{skill_id}/usage`。必传 `from`、`to` 为带时区的 ISO 8601 时间，`timezone` 为分日使用的 IANA 时区。可选 `user_id`、`version_id` 及 `group_by=day|user|version`，默认按天。返回查询范围的 `totals` 和当前页的 `groups`，每组包含分组键、使用次数、点名次数、确认加载次数、使用人数和最近使用时间。控制台沿用成员登录态。Owner/Admin 看团队和成员维度，普通成员看自己的数据。所有查询先限定当前工作空间和 Skill，版本筛选须属于该 Skill。时间范围和分页上限由接口配置，按工作空间、版本和发生时间建立查询索引。

| 指标 | 计算口径 |
| --- | --- |
| 使用次数 | 沿用 TeamAI 的轮次口径。有点名或确认加载即算一次，按两个信号中最早的有效时间归入 `[from, to)` |
| 确认加载次数 | `loaded_at` 落在 `[from, to)` 的版本轮次记录数 |
| 点名次数 | `mentioned_at` 落在同一时间范围内的版本轮次记录数。与加载有重叠，两列各自展示 |
| 使用人数 | 同一查询范围内按使用次数口径去重 `user_id`。跨天、跨版本的合计重新去重 |
| 最近使用时间 | 同一过滤范围内最后一次计入使用的轮次时间 |
| 安装情况 | 从安装代次查当前设备、用户和版本，独立于使用统计 |

时间存 UTC，天粒度用请求中的 IANA 时区计算。以东八区为例，界面查 11 月 12 日时先把本地 `[11-12 00:00, 11-13 00:00)` 转成 UTC。小李在一轮中点名并读取 v1.2.0 两次，小王下一轮读取 v1.3.0 一次。筛选当天得到使用 2 次、加载 2 次、人数 2 人、点名 1 次，两个版本各使用 1 次。小王如果只点名，使用仍为 2 次，加载降为 1 次。离线事件按观测日期归入原日期。

## 6. 开发顺序与验收

| 阶段 | 实施与验收 |
| --- | --- |
| 1. 包体迁移 | 先迁 PostgreSQL 包表、事务发布和历史读取。真实 PostgreSQL 验证并发发布、迁移重跑、损坏包、跨空间读、旧 Agent 版本及回退演练。用典型 Markdown 包和接近上限的包记录下载并发、备份和恢复开销 |
| 2. 21 个工具的安装与同步 | Owner 发布两个版本，普通成员在每个工具完成首次同步、会话启动同步和原生识别，记录工具版本、目录及 Hook、扩展或 Weora 启动入口。共用流程覆盖固定版本、回退、归档、成员提案、本地改动、同名冲突、共享目录、并发、中断及离线 |
| 3. 统计闭环 | 在各工具用真实会话采集事件并记录覆盖范围。覆盖点名、成功读取、失败读取、同轮重复、跨轮、跨用户、版本切换、断网补传、设备撤销和跨时区零点。验收以事件记录、接口结果及控制台的日期、用户、版本数字三者一致为准 |

21 个工具共用同一套包、同步协议和统计口径。发布前逐端记录「写入、原生识别、启动同步、使用采集」的证据和缺口。启动入口需给出原生 Hook、扩展或 Weora 启动命令的验证结果。
