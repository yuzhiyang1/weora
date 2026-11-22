# Weora CLI

面向人和 Coding Agent 的独立平台客户端，采用 Python 3.12，不导入后端业务模块。

当前支持本地 Agent 协作。会话目录按工作空间和显式授权查询，同一套命令支持直聊和任务内联调。

## 从源码运行

在仓库根目录执行，需要已安装 uv：

```powershell
uv run --project apps/cli weora --help
uv run --project apps/cli weora version
uv run --project apps/cli weora commands
uv run --project apps/cli weora schema
uv run --project apps/cli weora schema version
```

如果需要在本机终端直接使用 `weora`，可显式安装本地包：

```powershell
uv tool install --python 3.12 ./apps/cli
```

当前尚未发布到包仓库；开发验证使用 `uv run` 即可，不要求全局安装。

## 登录 Weora

```powershell
weora login --url https://weora.example.com
weora auth status
weora logout
```

`weora login` 打开浏览器。使用 Weora 账号登录，核对终端验证码、设备和工作空间后确认。CLI 将设备凭据保存到系统钥匙串。设备登录有效期为 30 天，也可在「设置 → 本地 Agent」撤销。

无图形环境可以用 `weora auth login --url https://weora.example.com --no-wait` 取得链接和请求 ID。完成浏览器确认后，在同一台机器运行 `weora auth login --resume <请求ID>`。

## 让两个本地会话对话

1. 在 Weora 的「设置 → 本地 Agent」点「配对设备」，在开发电脑上运行页面给出的命令，按提示输入一次性配对码。设备凭据由系统钥匙串保存。
2. 安装使用中的客户端 hook，然后打开或恢复要接入的原生会话。Codex 会提示审核并信任新增的 hook。

   ```powershell
   weora collab install-hook --runtime codex --auto
   weora collab install-hook --runtime claude-code --auto
   weora collab connect
   ```

   每台设备只需安装它用到的客户端。省略 `--auto` 时，下一次用户发问时 hook 会提示领取。其他运行时可通过 `weora collab register --runtime <标识> --native-id <原生ID> --name <会话名>` 接入，并主动领取。
3. 收件方在「我的原生会话 → 直接联系权限」选择可以向这条会话发消息的成员。发件方在自己的原会话里说「用 Weora 找某位同事的会话，问它一个问题」。在协作任务里，可从任务页复制加入指令，再把 `--task <任务ID>` 加到查找和发送命令。

```powershell
weora collab sessions "林晨 订单"
weora collab send --session <自己的会话ID> --to <对方的会话ID> --body "首屏如何传游标？" --key <本次请求ID>
weora collab claim --session <自己的会话ID> --message <消息ID>
weora collab reply --session <自己的会话ID> --message <原消息ID> --body "首屏省略游标。"
```

响应里有 Weora 会话 ID 和消息 ID。重试同一次发送时复用 `--key`，回复用原问题的 `--message`。回复和单向通知领取后用 `ack` 确认。可以用 `status` 查看平台投递状态，用 `cancel` 结束待领取或待确认的已发请求。

Codex 自动接收依赖支持 `queue` 的真实可执行文件及正确的配置目录。可用 `WEORA_CODEX_BIN` 指定二进制，用 `WEORA_CODEX_APP_SERVER` 指定同一台电脑上的 App Server。多个 Codex 或 Claude Code 配置目录分别启动连接器，沿用各自的 `CODEX_HOME` 或 `CLAUDE_CONFIG_DIR`。默认将提示排到当前轮次之后；连接器加 `--resume-codex` 后，在客户端支持时尝试恢复尚未加载的会话。Claude Code 的自动接收需要该原生会话提供本机 inbox。

## 安装和同步团队 Skill

1. 工作空间管理员可在「Skills」上传包，工作空间成员均可发布草稿。成员先运行 `weora login`。在「设置 → 本地 Agent」打开已登录设备的「Skill 权限」，选择「仅同步」或「同步并统计」。
2. 进入 Git 检出目录，绑定正在使用的工具。绑定会安装启动入口并下载已发布的 Skill。Codex 首次需要在 `/hooks` 中信任 Weora 的 Hook。

   ```powershell
   weora skill bind --runtime codex
   weora skill status --runtime codex
   weora skill pull --runtime codex
   weora skill install meeting-summary@1.0.0 --runtime codex
   weora skill update meeting-summary --to 1.1.0 --runtime codex
   weora skill follow meeting-summary --runtime codex
   weora skill uninstall meeting-summary --runtime codex
   weora skill usage flush --runtime codex
   ```

3. 后续打开或恢复会话时，支持原生 Hook 的工具会在后台检查更新。宿主重新扫描目录后即可看到新版本。离线时保留已安装包；遇到同名副本或本地改动时，`pull` 返回冲突路径。

项目知识晋升为 Skill 并通过审核后，在对应 Git 检出目录运行 `weora skill bind --runtime codex --project-id <Weora项目ID>`。项目绑定同时同步共享和项目 Skill，同名时优先选择该项目的来源。若项目内多个目录提供同一个安装名，同步会报告冲突并保留当前安装。后续在同一目录运行 `weora skill pull --runtime codex` 即可。已有绑定可补充一次项目 ID。在同一 Git 根目录绑定其他工具时，CLI 会沿用已选项目。

用户范围加 `--scope user`；Hermes 和 DeepSeek Harness 使用这个范围。Trae、Trae CN 安装后须在「设置 → Hooks」启用配置。Kiro 使用 CLI V3 的 Hook 文件格式。也可在打开客户端前手动同步，或用 `--executable` 指定客户端程序：

```powershell
weora skill bind --runtime trae
weora skill launch --runtime trae --executable "C:\Path\To\Trae.exe" -- .
```

当前提供 21 个安装目标及原生事件适配。控制台「Skill 详情 → 使用统计」区分点名和确认加载，两种信号在同一轮合计一次使用。安装、下载和启动同步不计使用次数。各工具的实际采集能力取决于版本、Hook 是否启用，以及事件是否包含能确认的加载证据。

当前分发范围为已授权设备所属的工作空间。角色、项目与标签分发策略，以及成员提交候选包，见[完整设计](../../docs/product/skill-sharing-design.md)。

## 贡献和检索项目知识

使用 `weora login` 登录后，可在同一工作空间贡献和检索 Markdown 经验。省略
`--project` 时使用工作空间共享范围。项目检索同时包含该项目知识和共享知识。

```powershell
weora context contribute --file ./pagination.md --project <项目ID>
weora context search "导出游标" --project <项目ID> --session <根会话标识>
weora context recall "导出游标" --project <项目ID> --check
weora context recall "导出游标" --project <项目ID> --session <根会话标识>
weora context get <知识ID> --project <项目ID> --session <根会话标识>
weora context feedback <知识ID> --project <项目ID> --session <根会话标识> --negative
weora context sync
```

普通经验立即发布。`contribute --file <Markdown> --dry-run` 可在本机核对路径、范围、正文大小和提交状态。使用预演返回的 `operation_id` 作为正式命令的 `--key`，可固定默认地址。连接中断时，客户端为当前登录设备保存待发送请求，`sync` 使用原请求 ID 重试。提交和同步按请求加锁，另有进程正在发送时可稍后重试。待发送的普通经验可在原设备用 `recall`、`recall --check` 和 `get` 按路径查询。同路径优先返回设备上的新内容，平台已提交时自动使用正式版本。排队正文留在设备上，平台召回账本只记录实际返回的已发布条目。手工反馈也会排队，由 `sync`、会话启动或正式检索按原项目和根会话补发。反馈可用 `--dry-run` 预览。正式检索暂时无法补发时，结果通过 `feedback_pending` 提示。如果平台仍可查询，当前设备的待送反馈会参与本次排序，团队账本在补发成功时更新。
安装原生 Hook 后，召回指引会给出当前宿主的根会话 ID，可用于检索、打开正文、反馈和分享。跨会话收发消息使用协作指引中给出的 Weora 会话 ID。

同一根会话先检索再打开正文，计一次采纳。`recall` 默认返回最多五条团队知识，并附上相关性和词项覆盖。同一范围内，作者、日期、类型、领域、标题和索引词项相同的重复贡献只占一个名额，以排序最高的记录进入召回账本。
项目召回默认只查该项目。管理员显式启用继承后，预检、召回结果、相关知识和自动打开才会包含可读的工作空间共享知识。目录已通过 `recall-settings` 关联项目时，`recall` 和 `get` 自动使用该项目。可以用 `--project` 指定项目，或用 `--shared` 直接访问工作空间共享范围。

```powershell
weora context recall-settings inherit --project <项目ID>
weora context recall-scope status --project <项目ID>
weora context recall-scope enable --project <项目ID>
weora context recall-scope disable --project <项目ID>
```

设置适用于新建和已有项目。设置变更随下一次请求生效。手动 `search` 和直接 `get` 仍可按原权限查找和打开知识。设备离线时，项目召回只使用待发送的本项目经验。

`recall --check` 返回相关性、首位分数和判定阈值。相关命中还返回标题、命中和缺失词项、可用的源码出处。预检不返回正文或摘要，也不记录召回。只有本机待发稿命中或代码导航可用时，结果用 `reason` 说明依据，分数和阈值为空。`search --check` 继续返回布尔结果。`search` 也可找到只命中正文的经验和本机待提交经验。

项目和共享范围分别根据各自的反馈计算冷热权重。已授权的本机待送反馈会参与本次召回预览；补发后沿用同一计分口径。召回分数相同时，按知识原文日期排序。

代码索引会生成 `gaps/detected.md`，随当前索引版本更新。召回响应中的 `knowledge_gaps` 表示有记录的扫描缺口，`relevant` 仍按普通知识命中计算。可按主题、完整导入名、包名或项目内源码路径查看缺口。精确匹配返回 `evidence`，包含导入名、文件路径和从 1 开始的源码行号，每类缺口最多返回 10 条位置。

```powershell
weora context gaps --project <项目ID>
weora context gaps "环境变量" --project <项目ID>
weora context gaps "example_package.helpers" --project <项目ID>
```

离线导出从 `teamwiki/gaps/detected.md` 链接当前各仓库的缺口页和 `gaps/evidence.json`。源码证据与扫描快照共用版本和校验清单。已有项目索引可在下次刷新时补齐证据。

知道要读哪一类代码图谱时，可跳过关键词召回，直接打开项目已发布的文档。`g1` 查看依赖关系，`g2` 查看调用链和数据流，`g5` 查看业务场景，`g6` 查看多跳关系。

```powershell
weora context graph g2 --project <项目ID>
weora context graph g2 --project <项目ID> --codebase <代码索引ID>
```

只有一个来源时返回正文。多个来源时返回候选和 `codebase_id`，选定后重读。文档未发布或随索引更新撤下时返回 `missing`。在召回会话里可传 `--session`、`--automatic` 和 Hook 提供的 `--actor`，按常规打开规则归因。

`recall --depth context` 默认检索仓库概览、模块职责和设计文档。`--depth route` 在 `code_route` 返回当前项目中可打开的仓库概览、领域和关键词。`--depth lookup` 加入源码文件证据和有权限的依赖文件，可以省略文件扩展名。三个深度均可同时检索团队知识。

实现或重构时，`lookup` 的 `change_entry_points` 给出最多 3 个源码入口及 10 条正向直接关系。响应还可包含最多 10 个 `candidate_change_files`，并标出省略数量。每个路径带代码索引 ID，同名文件可以区分。需要核对具体调用行号时，打开对应索引的 G2 调用链文档。
使用 `weora context recall-stats --project <项目ID> --session <根会话标识>` 查看同一会话的命中、未命中、
采纳及关系使用。省略会话时列最近的本人会话，添加 `--recent` 只看最近 24 小时。
项目已发布条目覆盖同类型、同逻辑路径的共享条目。贡献时可用 `--resource-path runbooks/pagination.md`
指定逻辑路径。通过清单导入时，可填写 `source.resource_path`。已有相对文件出处也可作为默认路径。

### 选择职能和项目目录

管理员可以保存工作空间的资源目录清单。清单里的职能用于选择目录，工作空间角色继续控制管理权限。当前成员的职能与所选项目共同决定生效目录。

```powershell
weora context namespace-settings status --project <项目ID>
weora context namespace-settings apply --file ./resource-namespaces.json
weora context contribute --file ./lesson.md --project <项目ID> --namespace payments
weora context contribute --file ./lesson.md --namespace ""
```

保存时填写 `status` 返回的当前清单版本。下面的清单假设已有一个工程职能成员和一个支付项目，请替换 UUID。

```json
{
  "base_revision": 0,
  "roles": {
    "engineering": {
      "knowledge": ["engineering"],
      "docs": ["platform"],
      "skills": ["engineering"],
      "scorers": ["engineering"]
    }
  },
  "members": {
    "11111111-1111-4111-8111-111111111111": "engineering"
  },
  "projects": {
    "22222222-2222-4222-8222-222222222222": {
      "learnings": ["payments"],
      "docs": ["payments"],
      "wiki": ["github__org__payments"]
    }
  }
}
```

`knowledge` 指规则目录。未声明为专属目录的文档继续共享。成员只读取自己的生效目录。显式传入空的经验目录时发布到共享根目录。`wiki` 使用 `wiki_root` 的最后一段作为目录名。已声明但未启用的 Wiki 从路径读取、检索、关联和状态中隐藏。评分器的目录可按职能和项目启用；同名且可唯一选择的目录版本覆盖根目录版本。
已发布的规则、文档和经验会解析正文中的 Markdown 链接。例如 `docs/payments/lease.md` 中的
`[收据](receipt.md)` 和 `[收据](docs/payments/receipt.md)` 均指向 `docs/payments/receipt.md`。
当目标是同一范围内唯一可见的已发布知识时，`recall` 与 `get` 会在 `relations` 中返回双向 `REFERENCES`。

可在规则、文档和经验的 YAML 前置信息中明确声明关系，例如 `related: {REFERENCES: [docs/payments/receipt.md], CONFLICTS_WITH: rules/refund.md}`。
目标写团队根路径，也可继续使用知识 UUID。经验贡献会忽略无效项，并在 `relation_findings` 中返回提示。
用 `weora context review <知识ID> preview --revision <版本> --project <项目ID>` 查看待审核版本的校验结果，
规则和文档的结构错误会阻止发布。路径暂无可见目标时返回提示，目标发布后关系自动生效。

`weora context codebase --project <项目ID>` 会返回代码索引的 `wiki_root`。引用可以指向目录 `index.md`、概览 `overview.md`、
模块摘要 `modules/<模块>.md` 或已发布设计 `docs/<文档>.md`。可以在同一个召回会话中按团队路径打开代码证据。

```powershell
weora context get teamwiki/evidence/code/github__org__billing/modules/src.md --project <项目ID> --session <根会话标识>
```

独立发布的 Skill 使用 `skills/<slug>` 或 `skills/<slug>/SKILL.md` 作为关系目标。`weora context get` 接受这两种路径和 Skill ID，
返回 Registry 中当前发布版本的正文。`search` 可以用正文词项找到独立 Skill，`recall` 也会把正文纳入评分，
并要求至少匹配一个标题或标签词项。在同一个 `--session` 中打开实际召回的 Skill 版本会计入采纳，通过关系打开也会记录关系使用。
独立 Skill 可用 `weora context feedback <Skill ID> --session <根会话标识> --positive` 或 `--negative` 手工反馈。
离线时队列保留反馈时间和原项目，补发仍归入反馈当时的发布版本。

使用 `contribute --kind rule` 提交规则时，可在 Markdown 前置信息中填写 `paths: src/{api,worker}/**`。
项目内的规则发布后，每个展开模式唯一匹配一个授权代码来源时，规则与代码详情会显示双向关联。清单可使用 `rule_paths`。

工作空间管理员可以打开未采纳知识的评分。使用同一个根会话标识查询候选并评估明确允许交给评分 Agent 的证据。

```powershell
weora context adoption enable
weora context adoption plan --project <项目ID> --session <根会话标识>
weora context adoption score --project <项目ID> --session <根会话标识> --evidence ./evidence.md --driver codex
weora context adoption write --project <项目ID> --session <根会话标识> --verdicts ./reviewed.json
```

`score` 默认只输出判定。查看后把输出保存为 JSON 交给 `write`，或在明确要提交时使用 `score --apply`。
所选 Agent 使用自己的模型配置处理本地证据，平台只接收条目标识、正文版本及 `adopted` 或 `ignored` 判定。

会话正文可单独归档。预览会查找本机原生转录，将常见凭据和本机路径替换为占位符。
请核对预览内容和分享范围。确认码在 24 小时内有效，确认发布的是这次核对的快照。原生 Hook 已记录会话的团队 Skill 时，预览的 `skill_count` 显示采集到的安装代次数。发布时平台核对设备安装回执，冻结当时的版本和脱敏描述。已启用用量上报时，快照也会包含当时已确认的 Skill 加载记录。

```powershell
weora context share-session --runtime codex --session <原生会话ID> --project <项目ID>
weora context share-session --runtime codex --session <原生会话ID> --project <项目ID> --confirm <预览码>
weora context list-shared-sessions --project <项目ID>
weora context get-shared-session <归档ID> --project <项目ID> --revision 1
```

`--runtime claude-code` 支持 Claude Code。存在多份转录时，可在预览中用 `--transcript` 选择原生文件。
省略 `--project` 使用工作空间共享范围。

归档作者可用指定版本作为评分证据。评分只纳入快照采集前向本人展示过的知识版本，提交判定时会复核归档。早期缺少采集时间的归档需要重新预览并发布。

```powershell
weora context adoption score --project <项目ID> --session <原生会话ID> --archive <归档ID> --archive-revision 1
```

团队可以用 Markdown 定义会话评分标准。YAML 前置信息包含 `labels`（每项包含 `value`、`score`、`description`）和 `passingScore`，正文写评分依据。`tool` 可指定 Codex 或 Claude Code，`model` 可指定客户端使用的模型名。
管理员可发布到工作空间或项目。同名时，项目标准优先。能读取归档的成员可以用发布标准评分或提交人工标签。`--file` 用于试跑本地标准，`--dry-run` 用于试跑发布标准。

```powershell
weora context score --project <项目ID> --archive <归档ID> --revision 1 --file ./review-quality.md
weora context session-scorers publish --project <项目ID> --file ./review-quality.md --base-revision 0
weora context score --project <项目ID> --archive <归档ID> --revision 1 --scorer review-quality
weora context score --project <项目ID> --archive <归档ID> --revision 1 --scorer review-quality --label <标签>
weora context session-labels <归档ID> --project <项目ID> --revision 1 --history
```

需要附带评分指南、示例或参考文本时，把评分标准放在 `<名称>/SCORER.md`。在正文用
`${TEAMAI_SCORER_DIR}/guides/example.md` 引用同目录的 `guides/example.md`。试跑和发布均可将目录或其中的
`SCORER.md` 传给 `--file`。目录包含的可见 UTF-8 文件合计上限为 1 MiB。

```powershell
weora context score --project <项目ID> --archive <归档ID> --revision 1 --file ./scorers/review-quality
weora context session-scorers publish --project <项目ID> --file ./scorers/review-quality --base-revision 0
weora context session-scorers publish --file ./scorers/engineering/review-quality/SCORER.md --base-revision 0
```

CLI 可从团队树里的文件位置识别命名空间，也可通过 `--namespace engineering` 指定目录。显式空值表示根目录。同名标准若在两个生效目录内出现，会从自动和点名评分中移除。当前目录版本仍保留原来的候选、附件及版本历史。

附件随标准保存到同一个版本。只修改附件也会发布新版本。评分结果通过 `scorer_source_hash` 标识所用的目录快照。

已发布标准的点名评分会保存标签。写入绑定归档和评分标准的具体版本。人工评分与模型评分分别保留，重复提交可沿用原命令返回的 `--operation-id`。

本机会话可直接评分。`--session` 查找 Codex 或 Claude Code 根会话，宿主环境能确定唯一根会话时可以省略。两个宿主都有相同会话 ID 时用 `--runtime`，同一宿主有多份转录时用 `--transcript`。CLI 先脱敏，再将证据交给评分客户端。Weora 只接收指纹、版本关联和标签。同项目成员能读取标签，原始会话 ID、标题和正文留在设备。

```powershell
weora context score --project <项目ID> --session <原生会话ID> --scorer review-quality --dry-run
weora context score --project <项目ID> --session <原生会话ID> --scorer review-quality
weora context list-local-scores --project <项目ID>
weora context session-labels <评分证据ID> --project <项目ID> --revision 1
```

本机试跑可使用 `--file` 或 `--dry-run`，两种方式都不登记证据和标签。

评分策略设为 `ask` 时，经验分享提醒会给出评分预览入口，并请用户选择标准和确认。预览默认建议采样命中且待评分的团队标准。可用 `--scorer` 自行指定一项或多项。

```powershell
weora context scoring-settings ask --workspace
weora context scoring-settings ask --project <项目ID>
weora context score-offer --project <项目ID> --session <原生会话ID>
weora context score-offer --project <项目ID> --confirm <预览码>
weora context score-offer --project <项目ID> --skip <预览码>
```

已经归档的会话可在预览时用 `--archive <归档ID> --revision 1` 指定。预览码绑定本机、项目、证据版本和所选标准，有效期为 24 小时。预览和跳过都不会调用模型。确认前若标准更新，需要重新预览。提交失败后可沿用原确认码重试，已生成的判定会复用。

默认推荐命中采样的普通标准。启用 Skill 路由后，加载情况已确认的本人会话也会推荐尚未判定的路由问题。成员也可以用 `--scorer teamai:adoption:<hash>` 主动选择采纳问题，并重复 `--scorer` 选择普通标准。带有生成式问题的预览在提交时再次校验评分授权。兼容的混合组提交后可按返回的调用 ID 查询整组收据。

本人会话固定了团队 Skill 证据后，可点名 `teamai:routing:<skill>`，或为目录 Skill 点名 `teamai:routing:<目录>:<skill>`。内置标签是 `fit`、`idle`、`extra` 和 `missed`。加载情况未知时由成员提供人工标签。团队可以发布 `teamai-routing.md`，再通过 `session-scorers reset-routing` 恢复上级或内置模板。

```powershell
weora context score --project <项目ID> --archive <归档ID> --revision 1 --scorer teamai:routing:ops:release-check
weora context score-offer --project <项目ID> --archive <归档ID> --revision 1 --scorer teamai:routing:ops:release-check
```

管理员可按运行模式开启 Skill 路由。工作空间设置为 `ask` 时，成员可以预览并确认建议。设置为 `auto` 时，后台为同一会话成组评分，领取后沿用固定的模板和证据。每组与其它评分共用工作空间的每日额度。

```powershell
weora context scoring-settings ask --workspace --skill-routing
weora context score-offer --project <项目ID> --archive <归档ID> --revision 1
weora context scoring-settings auto --workspace --skill-routing
weora context score-drain --project <项目ID>
```

同一会话的多个标准可以合并评分。兼容组共用一份证据和一次模型调用，每项标准仍产生自己的标签。`score` 和 `score-offer` 可加 `--batch`。管理员也可以按运行模式设置团队默认值，后台领取将逐组计算日额度。

```powershell
weora context score --project <项目ID> --session <原生会话ID> --scorer accuracy --scorer evidence --batch
weora context scoring-settings auto --workspace --batch
```

一次合并调用需要所有标准使用同一会话证据，并拥有相同的首选客户端和模型。开启后台批处理后，普通评分、召回采纳和 Skill 路由可以共享一次调用及一次额度。客户端校验整组标签，后台在同一事务写入标签、采纳判定和路由收据。

本机会话和已归档会话都可以后台评分。评分标准设置 `sessions: true` 和 `samplingRate` 后，管理员开启运行模式的后台评分，成员在本机绑定项目目录。会话归档开关独立配置。

```powershell
weora context scoring-settings auto --workspace --daily-limit 10
weora context scoring-settings auto --project <项目ID>
weora context score-drain --project <项目ID>
```

工作空间管理员可将 TeamAI 的 `scorers/<目录>/scoring.yaml` 中的 `sessions`、`daily_limit`、`batch`、`recall_adoption`、`isolation` 和 `skill_routing` 保存到平台。先查询目录的 `revision`，首次保存传 `0`。项目实际使用的值可通过 `--project` 查看。

```powershell
weora context scoring-settings status --namespace delivery
weora context scoring-settings apply --namespace delivery --file scorers/delivery/scoring.yaml --base-revision 0
weora context scoring-settings status --project <项目ID>
weora context scoring-settings status --workspace
```

未声明的字段取工作空间默认设置。当前职能与项目同时启用多个目录时，每种会话模式取最严格值（`off < ask < auto`），额度取目录声明的最小值。两个活跃目录给出不同的 `batch` 值时，该范围的模型评分暂停。目录配置中的 `sessions: true` 或 `sessions: false` 可同时设置两种运行模式。活跃目录有 `recall_adoption: false` 时暂停自动采纳问题。其余范围可用 `recall_adoption: true` 为该范围开启自动采纳。活跃目录可以用 `skill_routing: true` 开启自动 Skill 路由，多个目录冲突时 `false` 优先。点名评分和成员主动提交采纳判定沿用工作空间授权。

评分隔离默认使用 `best-effort`。管理员可以用 `weora context scoring-settings auto --workspace --isolation verified` 为指定运行模式提高级别。启用目录声明自己的级别后覆盖工作空间级别，多个目录取最严格值。评分标准的 YAML 前置信息及本机环境变量 `WEORA_SCORING_ISOLATION=verified` 可以进一步收紧。当前 `verified` 接受 TeamAI 参考配置对应的 Codex CLI 0.163.0 和 Claude Code 2.1.308。当前设备缺少相符版本时后台返回 `isolation_unavailable`，模型不会启动。

上述命令默认用于交互会话。原生 Hook 记录 `Stop`，连续闲置 6 小时后可由下一次会话启动或 `score-drain` 冻结证据。无人值守模式在任务结束后调用 `score-drain`。评分设置使用 `--run-mode unattended`。

失败续做沿用原始证据及标准版本，最多领取四次。本机脱敏快照由原设备从缓存读取，后台运行时清理已过期 14 天的快照。Weora 只接收证据引用和标签。`WEORA_SCORING_DISABLED=1` 可关闭本机后台评分。每天的用量按工作空间、成员和 UTC 日期计算，点名评分也计入用量。

工作空间管理员还可运行 `weora context adoption enable`，让自动评分处理已结束会话中实际召回、尚未打开或判定的知识。已有自动评分目录绑定即可使用。每个采纳问题固定召回时看到的正文版本。采纳和普通会话评分共用每日额度。稍后分享同一会话时，同一条采纳只计一次。

管理员可以发布 `teamai-adoption.md` 覆盖内置标准。项目版本优先，随后使用工作空间版本或内置标准。模板可用 `{{ doc.id }}`、`{{ doc.path }}` 和 `{{ doc.file }}` 引用条目 ID、逻辑路径和证据文件名。正文用 `recalled/{{ doc.file }}` 引用对应的召回片段。自动评分与 `adoption score` 都使用该标准的标签分值、通过阈值和首选客户端。

```powershell
weora context session-scorers publish --file ./scorers/teamai-adoption.md --project <项目ID> --base-revision 0
weora context session-scorers reset-adoption --project <项目ID> --base-revision 1
```

重置项目版本后会继承工作空间的标准。重置工作空间版本时省略 `--project`。已领取的任务保留当时的模板版本和模型，结算保留原标签与采纳结果。

同一会话下偏好相同客户端和模型的采纳问题默认合并评分。每次最多合并 20 个问题，计一次日额度。客户端集齐并校验整组标签后提交，每条知识保留独立判定。失败后，仍待判定的问题继续使用原会话、正文和标准版本。

管理员为对应运行模式开启 `scoring-settings auto --workspace --batch` 后，使用同一份会话证据且偏好相同客户端和模型的普通标准和 Skill 路由也可加入采纳分组。已领取的混合分组续做时保留原来的标准和证据版本。

可以把 `adoption plan --all-recalled` 返回的 `teamai:adoption:<hash>` 名称传给 `score --scorer`。由同一客户端和模型处理的兼容采纳问题默认分组。开启 `--batch` 可以加入兼容的普通标准。点名支持已打开的知识，采纳账本每条仍只计一次。

```powershell
weora context score --project <项目ID> --session <原生会话ID> --scorer quality --scorer teamai:adoption:<hash> --batch --dry-run
weora context score-receipt <调用ID> --project <项目ID>
```

移除 `--dry-run` 可正式提交整组。指定 `--archive` 和 `--revision` 可以使用本人已分享的归档。工作空间的采纳开关控制是否记入采纳账本，点名调用收据保留原标签及记账结果。

会话归档策略按 `attended`（有人值守）和 `unattended`（无人值守）分别配置。
成员在项目目录选择 `ask` 查看待核对的会话，或选择 `auto` 按工作空间授权自动分享。`off` 停止该目录的策略处理，`inherit` 清除当前目录覆盖。

```powershell
weora context archive-settings ask --project <项目ID>
weora context finished-sessions

weora context archive-settings auto --workspace
weora context archive-settings auto --project <项目ID>
weora context finished-sessions --apply

weora context archive-settings auto --workspace --run-mode unattended
weora context archive-settings auto --project <项目ID> --run-mode unattended
weora context score-drain --run-mode unattended
```

交互会话从协作 Hook 记录的最后一次 `Stop` 起闲置 6 小时后可处理，恢复会话会重新计时。
已安装 Hook 的设备在下一次交互会话启动时检查此前的候选。

对已配对且已安装协作 Hook 的 Codex 或 Claude Code，`weora skill launch --unattended` 会在原生进程退出后统一收尾。命令保留原生工具的 stdout 和退出码，在 stderr 输出 Weora 汇总。归档、评分、用量分别沿用当前目录和工作空间的设置。

```powershell
weora skill launch --runtime codex --unattended -- exec "检查发布清单"
weora skill launch --runtime claude-code --unattended -- -p "检查发布清单"
```

在 CI 或外部运行器中直接启动原生工具时，为任务设置 `CI=true` 或 `WEORA_RUN_MODE=unattended`，完成后在同一项目目录运行 `weora context score-drain`。命令会查找此前捕获的无人值守根会话，无需提供会话 ID。一次处理可同时报告归档、评分和用量的结果。返回 `drain_failed` 时命令退出码为 1，重跑可继续待处理阶段。已知单个会话 ID 时仍可使用 `finished-sessions --drain <原生会话ID> --runtime codex --apply`。
启用自动归档需要管理员许可及成员目录绑定。工作空间默认值为 `attended: ask` 和 `unattended: off`。

查看 `weora context --help` 可了解版本审核和维护命令。

归档的 180 天规则和晋升的 14 天门槛，按当前发布版本的原文日期计算。贡献 Markdown 时，前置信息可写 `date: 2026-05-09`，也可以提供带时区的完整时间。日期加不加引号均可。原文日期随知识版本保存，修订时省略日期会沿用已发布版本。缺少可用日期的历史知识按首次入库时间计算。

前置信息可声明 `domain: technical`、`ops`、`support` 或 `neutral`。没有有效声明时，按完整标签、团队路径、资源类型的优先级推断。领域参与召回排序，待审稿获批后才切换当前排序。工具规则导入保留已有声明，Registry Skill 使用对应发布包里的 `SKILL.md`。

已经安装 Codex 或 Claude Code 协作 Hook 的成员，可在项目根目录启用自动召回指引。
指引默认关闭，管理员可设置工作空间默认值。成员的目录设置覆盖工作空间，`WEORA_RECALL_DISABLED=1` 优先级最高。
关闭后，Hook 停止添加引导。此前提示里的 `--automatic` 检索和打开仍返回结果，暂停自动记账和反馈补发。待发反馈留在本机，可随时用 `weora context sync` 手工补发。启用后，启动 Hook 只补发当前目录范围的反馈，自动检索只补发该次检索范围的反馈。启动 Hook 仍会处理已排队的知识贡献。

运行 `weora collab install-hook --runtime codex` 或 `weora collab install-hook --runtime claude-code` 可安装 `weora-recall` 子 Agent 和 `SubagentStart` Hook。安装器复用已有同名定义。启用召回后，主会话在修改代码、排查或设计需要团队背景时，会收到先单独委托 `weora-recall` 的指引。已有答案、能直接从当前文件确认、简单局部修改或团队知识范围外的任务可以跳过。召回角色先做只读相关性检查，再按任务选择深度。Hook 分别在检索和打开命令中传入该执行者的归因标识。子 Agent 打开自己召回的内容时不计采纳，主会话或同根会话的其他子 Agent 可以为实际打开的版本计采纳。

```powershell
weora context recall-settings enable --project <项目ID>
weora context recall-settings status
weora context recall-settings disable
weora context recall-settings inherit
weora context recall-settings enable --workspace
```

客户端完成 `TodoWrite` 时，Weora 按同一根会话每 24 小时提示一次召回。提示同时遵守当前目录的召回策略和工作空间独立开关。工作空间管理员可选择内置中文、英文或默认双语提示，也可提供静态 Markdown 文案。

```powershell
weora context plan-reminder status
weora context plan-reminder configure --language zh
weora context plan-reminder configure --message-file ./plan-reminder.md
weora context plan-reminder configure --default-message --language default
weora context plan-reminder disable
weora context plan-reminder enable
```

已安装旧版 Hook 的客户端可重新运行 `weora collab install-hook` 并指定运行时，以加入 `PostToolUse` 配置。这条提醒沿用 TeamAI 的 `TodoWrite` 触发条件。Claude Code 使用任务工具集时，可用 `CLAUDE_CODE_ENABLE_TASKS=0` 切换到 `TodoWrite`。 [Claude Code 工具参考](https://code.claude.com/docs/en/tools-reference#task-tool-availability)

分类清单导入使用 `weora context import --manifest ./knowledge-items.json`。
清单每项包含 `kind`、`title`、`body`、可选 `tags` 和 `source`（包含 `type`、`ref`）。
逐条确认后提交审核，已经整批确认时可传 `--all`。标为 `personal` 的条目在客户端跳过。

本机规则可直接导入。CLI 从受支持工具的用户规则目录发现 Markdown 和 MDC 文件，
将每份文件的前 3000 字交给所选 Agent 分类，预览团队条目并让操作者接受、改标题或跳过。
具备发布权限的操作者确认后整批发布，其他成员确认的条目进入待审核。知识出处使用工具名和相对路径。

```powershell
weora context import --from-tools --via claude --project <项目ID>
weora context import --from-tools --via codex --all
weora context import --from-tools --output ./knowledge-drafts
weora context import --resume <导入运行ID>
```

`--all` 显式接受分类后的全部团队条目。分类和人工审阅的进度逐条保存，续做沿用原项目、
审阅方式和输出位置，可用 `--via` 更换剩余条目的分类客户端。本地输出只写入 Markdown，可在未登录时使用。

Claude Code 以受限模式执行提炼。CLI 从其用户设置的 `env` 中读取静态模型网关、登录和模型选择变量。进程已指定模型提供方时，使用该进程的连接配置。用户 Hook 和凭据辅助命令保持隔离。

iWiki 导入从当前命令的环境变量 `TAI_PAT_TOKEN` 读取凭据，按页面链接读取一页，
或按数字空间 ID 遍历最多 200 页。分类、审阅、发布权限和本地输出沿用同一流程。

```powershell
weora context import --from-iwiki 12345 --project <项目ID>
weora context import --from-iwiki https://iwiki.woa.com/p/12345 --output ./knowledge-drafts
weora context import --resume <导入运行ID>
```

部分页面或子树读取失败时，命令返回失败项与已完成结果。续做保留原项目、先前的审阅决定
和已发布条目的请求标识。每批最多并行处理五页，同一页的正文与元数据同时读取。续做复用已保存的正文摘要和标题，只请求缺失的部分；完整读取后中断时可直接从本机检查点继续。

已确认的原页会提取接口路径和代码术语，与所选项目已发布的代码证据建立 `MAPS_TO` 关系。平台在知识版本发布后展示关系，目标版本更新时撤下旧关系。离线输出目录已有 teamwiki 图谱时，导入会更新该图谱并保留证据摘要。

加上 `--iwiki-dual`，普通导入之后还会归纳业务接口、外部知识源和术语表。平台把三个章节各自保存为可评审的文档版本。管理员可确认发布，普通成员提交待审版本，`--require-review` 也可让管理员本次生成的章节进入待审。已发布的普通知识在汇总失败后仍可用，使用原运行 ID 只重试剩余汇总。

```powershell
weora context import --from-iwiki 12345 --iwiki-dual --project <项目ID> --dry-run
weora context import --from-iwiki 12345 --iwiki-dual --project <项目ID> --all --require-review
weora context import --from-iwiki https://iwiki.woa.com/p/12345 --iwiki-dual --output ./knowledge-drafts --all --require-review
weora context review <导入运行ID> preview --iwiki-section business-api
weora context review <导入运行ID> accept --iwiki-section business-api
```

离线输出在 `docs/team-codebase/external-knowledge.md` 中按托管标记更新单个章节，保留手工段落。待审章节可用上述命令预览、接受，或用 `reject` 驳回。

PR/MR 导入从每个评审提炼一条经验。CLI 读取标题、描述、提交、差异与评审讨论，
预览草稿和可能重叠的知识。可以输入 `accept`、`edit 新标题` 或 `skip`。
工作空间成员确认后按普通经验发布，出处保留原链接及可取得的作者和日期。

```powershell
weora context import --from-mr https://github.com/org/repo/pull/123 --project <项目ID>
weora context import --resume <导入运行ID>
```

每批最多 25 个链接。未完成的来源会随退出错误返回，其他已确认的经验仍可发布。需要英文时传
`--language en`，跳过讨论时传 `--no-discussions`。预览只返回草稿与重叠建议。续做沿用原来的选择。
发布前比较最近 14 天的项目与共享经验及本机离线经验。重叠建议按正文关键词相似度排序，`--all`
的结果也包含匹配项。PR/MR 的本地 Markdown 保留 TeamAI 的作者、日期、标签、初始置信度和来源字段。

导入到项目时，同一仓库成功提炼的链接会合并为一次增量刷新。刷新限于已登记且已建立索引的连接，
审阅时跳过的草稿也会参与。预览和本地 Markdown 输出跳过刷新。`codebase_refresh` 返回刷新结果
和跳过原因。刷新失败时，已发布的经验仍可检索。通过 `codebase --status` 可以查看最近的触发链接
和每条链接实际采集的提交。

- GitHub 使用 `GITHUB_TOKEN`、`GH_TOKEN` 或本机 `gh` 登录。公开 PR 支持匿名读取。

### 知识库健康、团队周报与评分失败

```powershell
weora context health --project <项目ID>
weora context digest --project <项目ID>
weora context digest --project <项目ID> --failures --limit 20
weora context digest --project <项目ID> --json --scorer review-quality
```

省略项目时查看工作空间共享知识。CLI 在项目范围运行 `health` 和 `digest` 时汇总该项目及可见共享知识。加 `--project-only` 可以仅查看项目知识。周报中的会话、用量和工作流统计按所选范围计算。管理员可在知识库的「健康报告」查看覆盖率、采纳、维护候选、关系和代码来源。
`health --limit` 控制明细长度，总数仍按完整范围计算。「最近召回月份」将每个条目计入最后一次被召回的月份。
成员可用 `digest` 查看最近七天新增的经验和知识 Skill 发布，以及当前和上一周期的共享会话数量、轮次、运行模式和标题摘要。每个会话按首次共享计入一个周期。管理员还能查看同范围的团队 Skill 使用轮次、人数和排行。

`digest` 的 `interventions` 显示该范围累计上报的根会话数、中断、工具拒绝和用户纠正次数、每会话均值及运行模式。装好 Codex 或 Claude Code 的 Weora 协作 Hook 后，在本机项目目录开启上报。选择工作空间共享范围时用 `--shared`。

```powershell
weora context activity-settings enable --project <项目ID>
weora context activity-settings status
weora context publish-activity
weora context activity-settings disable
```

`weora skill pull` 完成同步后自动补发。同一根会话继续运行时上报更新的累计值，失败时可运行 `publish-activity` 重试。无人值守的统一收尾也会发送计数。上报独立于归档与评分，只包含计数、模式、会话摘要及观测时间。工作空间管理员可用 `activity-settings disable --workspace` 停止接收；设备可通过目录设置或 `WEORA_USAGE_REPORT_DISABLED=1` 停止本机采集与上报，待上报记录保留在设备。

`digest` 的 `conversation` 显示累计真人提示轮次，以及输入、输出、缓存读取和缓存写入 Token，并按运行模式拆分轮次和 Token 总数。Codex 输入量已扣除缓存，Claude Code 的重复消息块只计一次，子会话只计用量。历史设备只上报干预时，缺失的 Token 保持为空，`token_sessions` 说明有可读用量的根会话数。模式使用各根会话最新上报的记录。

`digest` 的 `session_trends` 对比最近两个 UTC 七天窗口，当前窗口含当天。成功率、平均提示轮次、平均活跃时长、缓存读取比例和纠正率归到根会话首次 Stop 所在日。恢复后出现中断、宿主错误或用户纠正时，原窗口的成功率随新快照更新。活跃时长累计相邻事件间不超过五分钟的时间。

请求成本按实际请求发生日统计，返回 `avg_request_cost_micros` 和可计价请求数。成本采用宿主记录的金额，或按随 CLI 发布的原生模型费率估算。未知费率的请求仍参与会话用量统计。费率基线有固定版本，已经记录的估算在重试时保留。宿主的提示词、请求标识、模型标识与日志正文留在本机。[OpenAI 费率](https://developers.openai.com/api/docs/pricing) · [Claude 费率](https://platform.claude.com/docs/en/about-claude/pricing)

`digest` 的 `outcomes` 按运行模式和产出类别显示已结束会话数、每会话输入加输出 Token 均值及人工干预均值。交互会话在最后一次 Stop 闲置六小时后由下一次上报结算。已记录启动点的无人值守会话在统一收尾时结算。启动前启用该目录的活动上报，以便记录完整起点。

会话可以同时归入 PR、提交和经验三行。没有可确认产出的会话归入 `none`。提交按本机 Git 身份和会话起止提交核对。PR 还须有该会话的一笔提交出现在源分支，且从本机权限读取到开放评审。经验由控制面按同项目、同作者和会话来源确认。分类一旦结算便保留当时的计数，Git 邮箱、提交哈希和评审地址留在设备。[GitHub PR 查询](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests)

`digest` 的 `workflows` 显示当前项目累计运行数、成功数、失败数和耗时。将 [工作流完成上报模板](../../examples/ci/github-knowledge-workflow-activity.yml) 放入登记资源仓库的默认分支，沿用源模板的文件名和工作流名称。它监听知识评审、改进基准及定期改进的完成事件，用可信 Runner 上预装的 CLI 和 `actions: read` 权限回读源尝试。复用这些模板的 Weora 工作空间、项目和管理员设备配置。工作空间的活动上报策略同时控制工作流上报。[GitHub 完成事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run)

同次完成事件重放只保留一份。同一运行重跑后按最新结束尝试汇总。取消计入运行数和耗时，全程跳过的运行不计入。耗时是首个作业开始到最后一个作业结束之间的时间。上报失败时可重跑上报工作流，也可使用具备 Actions 读取权限的 GitHub 凭据补发：

```powershell
weora context publish-workflow --project <项目ID> --repository owner/repo --run <运行号> --attempt <尝试序号>
```

`digest --failures` 查看最近 30 天的失败标签、按固定标准版本和证据来源划分的每周通过率，以及模型与会话作者人工标签的一致性。相同证据版本和评委的重复评分保留最新标签。无样本的周通过率为空，`--limit` 仅限制失败明细。报告只读取可见的标签和版本元数据。已安装 Weora Hook 的 Codex 和 Claude Code 在新会话启动时，可以在本机记录已登记 Git 仓库的提交和已跟踪文件变更状态。会话归档或登记本机评分证据时同步，按项目和会话固定首份记录。`git_start.status` 用于区分已记录、存在未提交变更、仓库连接不可用及缺少起点。

使用 `--scorer` 时，从当前项目仓库目录运行命令。输出的 `improvement.failures` 包含当前检出中存在干净起点的候选及同会话发布的经验，每个根会话最多一次。`improvement.refused` 列出缺少起点、启动时有已跟踪文件变更或起点提交不在当前检出的会话。`--limit` 只限制普通失败明细，回归候选读取所选标准近 30 天的全部可见标签。

管理员可登记 TeamAI `v1alpha1` 格式的套件 YAML 和以 `successCriteria` 判定的任务 YAML。套件使用已登记仓库和 `[main, pulled]` 对比矩阵。任务使用候选的 `label_id` 及 `startingCommit`，文件名用于任务的逻辑路径。

```bash
weora context benchmark suite-add --project <项目ID> --file SUITE.yaml
weora context benchmark task-add --project <项目ID> --scorer review-quality --label <标签ID> --file missed-null-check.yaml
weora context digest --project <项目ID> --json --scorer review-quality --suite benchmarks/regressions
weora context benchmark tasks --project <项目ID> --proposal <提案ID>
weora context benchmark proposal-settle --project <项目ID> --proposal <提案ID> --status merged
```

`task-add` 默认将任务加入该标准的开放提案。管理员可用 `--publish` 直接登记到默认套件，或发布提案中的任务。计划的 `covered` 列出已覆盖会话的任务路径，`hasProposal` 标示开放提案。套件已登记时，至少两个新候选，或开放提案再新增一个候选，即满足默认启动条件。

基准计划从项目已登记的资源仓库解析两个本地 Git 引用，固定两侧提交、任务起点和试验矩阵。用相同 `--key` 重试会读取原运行。省略 `--proposal` 时执行范围使用默认套件任务。

```bash
weora context benchmark run-plan --project <项目ID> --resources <资源仓库目录> --base-ref main --proposal-ref <提案引用> --proposal <提案ID> --key <操作UUID>
weora context benchmark run-inputs --project <项目ID> --run <运行ID>
weora context benchmark run-record --project <项目ID> --run <运行ID> --file verdicts.json
weora context benchmark run-report --project <项目ID> --run <运行ID>
```

执行器提交由 `trial_id`、`outcome` 组成的 JSON 数组。`passed` 和 `failed` 还需提供 `judge_tool` 和 `judge_model`，可附上 `judge_effort`。`error` 与 `unscored` 保存原因类别，不携带评判身份。报告按任务、工具和重复编号配对。两侧评判不一致时排除对应试验，缺失或排除的试验使门禁保持未完成。

可用 `benchmark run` 一次完成计划和试验，参数合并 `run-plan` 和 `run-execute` 的项目、仓库、资源引用、镜像及评判选项。用现有运行 ID 执行时，使用下面的命令。每个试验在独立 Linux 容器中写入临时检出，只载入该侧的规则、Skill 和 Claude 子 Agent。评判使用另一个容器，只接收任务、标准、答复及代码差异。结果先记录在本机，平台写入失败时可重试同一命令。`--fail-on-regression` 在已验证的回退发生时保留 JSON 报告并返回退出码 1。

首次执行会登记实际选中的镜像 ID、容器入口脚本摘要、内部网络、网关和评判身份。换机器续跑时继续使用同一组条件，平台会拒绝配置变化和不匹配的收据。已有旧格式结果的运行沿用原结算契约。

```bash
weora context benchmark run-execute --project <项目ID> --run <运行ID> --checkout <项目检出目录> --resources <资源检出目录> --image <镜像ID或摘要> --network <内部网络> --gateway http://provider-gateway:8080 --judge-tool codex --judge-model <固定模型>
```

镜像需要 Git、Python 3 和对应版本的 Codex、Claude Code。可用 `benchmark.Dockerfile` 构建，传入 `CODEX_VERSION` 和 `CLAUDE_CODE_VERSION`。运行时使用本机镜像，代码和资源挂载为只读。模型网关位于专用 Docker 内部网络，按 `/openai/v1` 和 `/anthropic` 提供模型调用，实际提供商凭据由网关持有。省略网络与网关时容器断网，可用于离线执行器自测。

管理员可从重复失败生成改进提案。生成器读取评分标准、所选失败的关联经验以及固定版本的规则、Skill、子 Agent 和团队说明。它给出可修复的共同原因、资源差异和每项失败的回归任务。`--preview` 将草稿暂存在本机供核对。后续使用同一操作 UUID 会提交该草稿，写入失败时也沿用同一 UUID 重试。

```bash
weora context self-improvement run review-quality --project <项目ID> --checkout <项目检出目录> --resources <资源检出目录> --base-ref main --image <镜像ID或摘要> --network <内部网络> --gateway http://provider-gateway:8080 --model <固定模型> --key <操作UUID>
```

CLI 在 PostgreSQL 中为同一开放提案追加修订。已生成的提案可通过下面的命令创建或续写一个 GitHub PR。CLI 通过本机 GitHub 授权写入提案专用分支，控制面通过项目登记的读取权限核对 PR 及文件内容。CLI 保留原资源检出的未提交修改。

```bash
weora context self-improvement publish --project <项目ID> --proposal <提案ID> --resources <资源检出目录>
weora context self-improvement confirm --project <项目ID> --proposal <提案ID> --benchmark <基准运行ID>
```

用 `publish` 回执中的提案提交运行完整基准矩阵，基准组使用提案固定的原始资源提交。PR 在 GitHub 合并后，`confirm` 会核对实际合并和完整基准门禁，再把任务纳入默认套件。默认分支已偏离固定基准或远端分支被其他操作者修改时，交付会暂停并显示冲突。

可将 [GitHub Actions 基准模板](../../examples/ci/github-self-improvement-benchmark.yml) 放到资源仓库的工作流目录。它在同仓库的 Weora 提案创建、更新或重新打开时运行完整矩阵，在合并后用最新通过的运行确认任务发布。

在 Actions 中重跑同一次工作流时，仍有效的基准会继续使用原运行及已结算的试验。最新尝试接手剩余试验，或直接重报已完成的检查状态。运行已到期、出现执行错误或套件矩阵发生变化时，会重新建立基准。

- 可信 Linux Runner 需要 `weora-benchmark` 标签、Weora CLI、Docker 和可读取任务起点的项目 Git 检出。资源提交由 CLI 取到临时 Git 库。
- 配置 `WEORA_URL`、`WEORA_WORKSPACE_ID`、`WEORA_PROJECT_ID`、`WEORA_BENCHMARK_CHECKOUT`、`WEORA_BENCHMARK_IMAGE`、`WEORA_BENCHMARK_NETWORK`、`WEORA_BENCHMARK_GATEWAY` 和 `WEORA_BENCHMARK_JUDGE_MODEL`，密钥使用 `WEORA_CI_DEVICE_TOKEN`。
- 在目标分支的保护规则中要求 `weora/knowledge-regression` 提交状态，并启用合并前分支须更新到目标分支。检查状态写到核验过的 PR 头提交，详细比较结果可从状态链接进入 Actions 日志。

### 定期生成改进 PR

将 [定期改进模板](../../examples/ci/github-self-improvement-schedule.yml) 和基准模板放入同一资源仓库的工作流目录。运行时间为每周一 06:00 UTC，也可从 Actions 手动运行并选择已登记的评分标准，默认是 `review-quality`。

- 复用基准任务的可信 Runner、Weora 项目、设备密钥和模型网关。定期任务另外读取 `WEORA_IMPROVEMENT_MODEL`，以及可选的 `WEORA_IMPROVEMENT_SUITE` 和 `WEORA_IMPROVEMENT_SCORER`。若未配置套件，使用 `benchmarks/regressions`。
- 使用为该资源仓库安装的 GitHub App，授予仓库内容与 PR 写权限。配置 `WEORA_IMPROVEMENT_APP_ID` 和 `WEORA_IMPROVEMENT_APP_PRIVATE_KEY`，使创建和更新 PR 时可继续触发基准模板。[GitHub 工作流触发规则](https://docs.github.com/en/actions/concepts/security/github_token)
- 候选足够且生成器给出有效修改时，CLI 把任务和差异登记为同一修订，再创建或续写 PR。候选不足时输出跳过原因。PR 经评审合并且门禁通过后，合并工作流才将任务加入默认套件。
- 交付中断时，在 Actions 重跑原工作流。CLI 用 GitHub 运行编号找回 PostgreSQL 中的原修订，继续交付或返回已交付回执。也可用 `self-improvement publish` 手动续做。默认分支变化时交付停止，待确认固定基准。手动生成流程可以给 `self-improvement run` 添加 `--publish` 直接交付本操作的修订。

### 生成质量修订草稿

选定项目后，维护涵盖该项目及可见共享知识，同路径优先取项目版本。使用 `--project-only` 可限制在项目范围内。管理员归档共享条目后，能够使用该条目的项目都会停止召回。

管理员可用 `--prune --dry-run` 查看低置信度经验。正式清理将经验归档，可用 `restore <条目ID>` 恢复发布版本。`--confidence-writeback` 将按反馈计算的分数登记到当前发布版本的快照中，首次或变动超过 0.05 时写入，可用 `get <条目ID>` 查看。两个命令均支持分页续做。

管理员可为至少被两位成员召回、累计召回至少五次且采纳至多一次的规则、文档和 Skill 生成草稿。
CLI 用当前正文和相关已采纳经验起草 Markdown。预览只列候选。正式生成时若单条失败，已生成的草稿仍可使用，
再次运行会保留已有文件和其中的人工修改。

```powershell
weora context maintenance --prune --threshold 0.15 --dry-run --project <项目ID>
weora context maintenance --prune --archive --project <项目ID>
weora context maintenance --confidence-writeback --dry-run --project <项目ID>
weora context maintenance --confidence-writeback --project <项目ID>
weora context maintenance --update-quality --dry-run --project <项目ID>
weora context maintenance --update-quality --project <项目ID> --via codex --output ./quality-drafts
weora context maintenance --submit-quality <草稿文件>
weora context review <知识ID> accept --revision <待审核版本> --project <项目ID>
```

提交前可编辑正文，保留文件顶部的基准信息。草稿保存原条目的工作空间、项目和版本。直接按草稿文件提交即可，审核时选择条目的原范围。当前发布内容在审核期间继续可见。
Skill 审核通过时在原 Registry 资产下发布新版本，保留包头、附属文件及旧版本。独立发布的 Skill 提案绑定发布版本 ID，提交结果中的 `id` 是待审核提案 ID。基准发生变化或 Registry 有独立编辑时，审核返回冲突。

### 晋升团队经验

列出满足置信度和采纳门槛的已发布经验。指定 `--project` 时列出项目经验及其可见的共享经验。`--project-only` 限制为项目经验，省略项目时只处理工作空间共享经验。晋升保留原经验的目录和文件名，手工经验使用稳定条目 ID。

```powershell
weora context promote --project <项目ID>
weora context promote <经验ID> --project <项目ID> --dry-run
weora context promote <经验ID> --project <项目ID> --via codex
weora context promote <经验ID> --project <项目ID> --category skills --via claude
```

候选列表和预演按关键词建议类别。提交时 Agent 选择类别、说明理由并改写正文；`--category skills|rules|docs` 固定类别。模型回复无法使用时可沿用建议类别或原正文，显式指定的 Agent 无法运行时会结束提交。网络中断后重试同一条命令会复用首次准备的草稿和请求标识。待审稿暂不进入检索，审核接受后规则和文档进入检索，Skill 同步发布到原项目或共享范围。驳回后可再发起晋升。

例如，`learnings/platform/retry-deploys.md` 晋升为 Skill 后，对应团队路径 `skills/platform/retry-deploys/SKILL.md`，在已绑定项目的 Runtime 中按 `retry-deploys` 安装。发布保留草稿已有的清单，缺少名称时使用文件名，缺少说明时从正文提取。历史 Skill 的后续质量修订继续使用原安装名。文件名不符合 Registry 安装规则时，Registry 使用稳定的条目标识，按原路径仍可检索。若同一范围和目录已经注册了同名 Skill，审核返回冲突，可在审核队列处理晋升草稿。

归档后，同一经验仍可将新版晋升到原 Skill 路径。待审期间该资产继续归档。审核通过后，同一资产发布新版，保留安装名、旧版本和附属文件。驳回后可再次提出晋升。原资产有独立 Registry 草稿时，发布会返回冲突并保留两份草稿。

### 分享会话经验

团队可以覆盖会话经验分享 `teamai-share-learnings` 和会话归档及评分 `teamai-share-session`。先查看工作空间生效版本，再由管理员提交 YAML。恢复默认也需要当前版本。分享经验时同时选了归档或评分，会固定会话分享子流程的生效版本。

```powershell
weora context workflow-definition status teamai-share-learnings
weora context workflow-definition apply teamai-share-learnings --file ./share-learnings.yaml --revision <当前版本>
weora context workflow-definition reset teamai-share-learnings --revision <当前版本>
```

```yaml
schemaVersion: v1alpha1
name: teamai-share-learnings
alias: share-notes
description: 确认会话中值得复用的经验
invoke: ask
steps:
  - id: learning
    prompt: 和本人核对正文、标题和目标项目。
  - id: approve
    approve: { message: 核对这些草稿后发布到项目知识库, required: true }
  - id: contribute
    needs: [learning, approve]
    uses: weora.contribute-learnings
  - id: share-session
    needs: []
    uses: weora.share-session
```

设备发起时，`invoke: ask` 返回本人确认入口，`allow` 可直接进入草拟，`deny` 只允许本人在浏览器启动。默认的可选审批可由发起设备代答。`--hitl person` 交给本人处理，`--auto-approve` 自动同意可选审批。必需审批始终在浏览器中完成。

```powershell
weora context workflow describe share-notes --project <项目ID>
weora context workflow start share-notes --runtime codex --session <原生会话ID> --project <项目ID> --hitl person
weora context workflow draft <运行ID> --file ./lesson.md --project <项目ID>
weora context workflow resume <运行ID> --project <项目ID>
```

需要由本人直接启动时给 `start` 加 `--human`，打开返回的链接。如果发起时选定会话归档或评分，使用现有会话分享和评分命令。评分带 `--operation-id <运行ID>`，再用 `workflow session-complete <运行ID> --archive <归档ID> --revision <归档版本>` 登记该会话的完成收据。

重新运行 `weora collab install-hook --runtime codex` 或 `--runtime claude-code` 可更新用户 Hook。Codex 提示有新增或修改的 Hook 时，使用 `/hooks` 核对并信任 Weora 的 `SessionStart`、`UserPromptSubmit` 和 `Stop`。
达到 15 次工具调用且满足会话提醒阈值时，每个根会话最多收到一次提醒。识别到用户中断、拒绝工具、短时间内纠正或多次工具失败时会计分。
提醒基于本地会话记录，分享流程和分享提醒都需要开启召回。用 `weora context share-reminder disable`
停用当前目录的提醒，用 `inherit` 恢复上级设置。管理员可用 `share-reminder disable --workspace` 修改工作空间默认值。
环境变量 `WEORA_SHARE_REMINDER_DISABLED=1` 可强制停用提醒。

先把可复用的经验整理成 Markdown，再核对预览返回的正文与范围。指定 `--project` 发布到项目，
省略时发布到当前工作空间的共享知识。每份草稿一个文件，`--file` 可以重复。

```powershell
weora context share-learnings --runtime codex --session <原生会话ID> --file ./lesson.md --project <项目ID>
weora context share-learnings --runtime codex --session <原生会话ID> --file ./lesson.md --project <项目ID> --confirm <预览确认码>
```

平台保存确认后的经验正文与会话引用。转录和评分分别按会话归档与评分策略授权。

### 近期合并提醒

已安装 Weora 协作 Hook 的 Codex 和 Claude Code 会在会话启动时后台检查当前检出的 GitHub
`origin`，在结果可用后的第一条提示词展示待提炼的合并记录和导入命令。已在可见知识中发布的来源
会被过滤。同一设备上，同一仓库已展示的来源在 30 天缓存期内只提示一次。默认检查最近 7 天，
最多读取 10 条。

```powershell
weora context mr-hint status
weora context mr-hint enable --project <项目ID>
weora context mr-hint disable
weora context mr-hint inherit
weora context mr-hint enable --workspace --lookback-days 14 --max-mrs 20
```

个人设置作用于当前目录及子目录，使用 `--shared` 可切换到共享知识。工作空间管理员可以调整
默认开关、查询天数与条数。优先级依次为 `WEORA_MR_HINT_DISABLED=1`、个人目录设置、
工作空间默认值。召回和近期合并提醒各有开关。

### 在 CI 中展示评审建议和发布经验

用已授权的 Weora 管理员设备身份运行任务，并将登记该仓库的项目 ID 传给 CLI。代码托管令牌由 CI 的密钥配置注入。GitHub 沿用 PR 导入的凭据配置，发布建议还需评论权限。提炼可选择 Codex 或 Claude Code。

```powershell
weora ci extract-mr --url $env:REVIEW_URL --project $env:WEORA_PROJECT_ID --mode comment --individual-comments --via codex
weora ci extract-mr --url $env:REVIEW_URL --project $env:WEORA_PROJECT_ID --mode write --individual-comments --via codex
```

默认的 `comment` 模式只展示建议。用 `write` 处理已合并的评审，或用 `both` 在合并前展示建议、在合并后展示并写入。两阶段使用同一 `--comment-marker`。自动任务通过 `--event-head` 传入事件中的完整源提交。源提交已变化，或评审已关闭、已结算、仍为草稿时，自动评论返回 `skipped`。评论身份默认取当前托管账号，GitHub Actions 自带机器人也可使用。合并任务使用不同托管账号时，通过 `--comment-author` 指定建议的原发布账号。

图谱建议对比评审的固定 Git 提交，显示最多 50 个变更文件里的新增、变更和移除节点。关系按项目仓库的索引范围比较，展示新增和移除的依赖、调用引用和接口实现，也涵盖文件未变但目标发生变化的关系。变更只涉及 `tsconfig.json`、`jsconfig.json` 或 `go.mod` 时，按代码索引规则触发预览与刷新。关系分析超时或目录不可读时仍会展示节点。无法读取基线或固定提交时，建议显示受影响的文件。

启用 `--individual-comments` 后，经验和代码索引各有独立建议。GitHub 评论的 👎 会否决对应项。无法读取表态时，合并任务保留草稿供重试。一次合并只结算一次。索引刷新失败时，重跑沿用原经验和收据。

加 `--dry-run` 预览草稿及评论，加 `--output` 导出目录内的 `learning.md`，加 `--no-discussions` 跳过评审讨论。

| 平台 | 自动触发流程 |
| --- | --- |
| GitHub | [Actions 模板](../../examples/ci/github-knowledge-review.yml) 放在目标仓库的工作流目录。PR 打开或更新时展示建议，合并后结算。配置变量 `WEORA_URL`、`WEORA_WORKSPACE_ID`、`WEORA_PROJECT_ID` 和密钥 `WEORA_CI_DEVICE_TOKEN`。 |

### 仓库代码索引

```powershell
weora context import --from-repo <项目仓库ID> --project <项目ID>
weora context import --from-repo <项目仓库ID> --project <项目ID> --incremental --max-files 300
weora context import --dir ./service --dry-run
weora context import --dir ./service --project <项目ID> --incremental
weora context import --dir ./service --output ./wiki --incremental --via codex
weora context import --from-repo https://github.com/acme/service.git --output ./wiki --incremental --skip-enrich
weora context import --from-repo https://github.com/acme/billing.git --output ./wiki --domain 订单计价 --skip-enrich
weora context import --from-repo-list ./repos.yaml --output ./wiki --concurrency 3
weora context import --from-org https://github.com/acme --output ./wiki
weora context codebase --output ./wiki --status
weora context codebase --output ./wiki --lint
weora context codebase --output ./wiki --project service --deep-enrich --dry-run
weora context codebase --output ./wiki --project service --deep-enrich --via codex
weora context codebase --project <项目ID> --repository <项目仓库ID> --deep-enrich --via codex
weora context codebase --project <项目ID> --local <本地索引ID> --deep-enrich --dry-run
weora context import --from-repo-list ./repos.yaml --project <项目ID> --concurrency 3 --incremental
weora context import --from-org https://github.com/acme --project <项目ID> --dry-run
weora context import --from-org https://github.com/acme --skip-import
weora context import --resume <导入运行ID>
weora context codebase --project <项目ID> --reconcile --dry-run
weora context codebase --project <项目ID> --reconcile
weora context codebase --output ./wiki --reconcile --dry-run
weora context codebase --output ./wiki --reconcile
weora context codebase --project <项目ID> --status
weora context codebase --project <项目ID> --repository <项目仓库ID> --lint
weora context codebase --project <项目ID> --local <本地索引ID> --lint
```

索引读取 Weora 项目里登记的仓库与固定提交。默认采集 200 个候选文件，优先采集常见入口和服务文件，
命令报告采集、复用、删除和截断情况。增量导入根据 Git blob 复用未变化文件，并检查依赖目标是否仍然存在。
索引记录声明、行号、仓库内依赖、可解析的 TypeScript 接口实现、Swift 协议遵循及 Python、JavaScript、TypeScript、Go、Swift 跨文件调用。可用
`weora context search` 按项目检索，用 `weora context get` 查看目标文件与实现或调用位置。

已启用仓库后台同步时，检索显示来源为当前、过期、缺失或未知；检索只读取已有本地快照。
索引检查会报告缺失的基线、文件清单不一致、悬空关系和采集缺口。

同一项目或离线 teamwiki 内，跨仓库关系根据未在本仓解析的 Python、JavaScript 和 TypeScript 导入，以及配置源码、独立 TOML、INI、CONF 的章节和键名或 SQL 表声明生成。结果标记两端索引版本和判断依据。存在多个候选时暂不建立关系，仓库停用或证据变化后关系也会更新。项目知识健康报告显示跨仓库关系数。重新导入时会按当前提取版本补齐旧索引。

JS/TS 调用在证据页显示调用表达式、目标声明所在的文件和调用行号。能确认目标导出时标为 `EXTRACTED`，只能确认静态导入模块时标为 `INFERRED`。支持 JSX/TSX 中的实际表达式和相对模块中的 `.js` 到 `.ts` 回退。索引使用最近的 `tsconfig.json` 或 `jsconfig.json` 解析 `paths`，读取仓库内已索引的相对继承配置，并沿再导出查找实际声明。直接依赖仍指向导入的模块。

Go 使用已索引的 `go.mod` 和包声明定位模块内调用。证据页会标出同包函数、声明类型可确定的接收者方法、导入包别名调用及静态 HTTP 路由。

Swift 从同一默认 SwiftPM 目标定位可见的顶层函数、类型和静态成员，标记为 `INFERRED`。源代码和测试目标各自解析，参数、捕获变量和可见成员同名时跳过对应推断。

`--dir` 读取当前设备目录，包括未提交的文件。预览列出待采集的相对路径。发布时发送选中的 UTF-8 源码，
平台保留哈希、结构证据、版本和审计。单文件需小于 256 KB，一次采集最多 8 MiB。
同一成员对同一项目和目录重复使用 `--incremental` 时，未变化的文件会复用提取结果，移除的文件会归档。
本地目录的后续状态以再次导入为准。Git checkout 还会记录 HEAD、相对仓库子目录和工作区状态。
对于干净的仓库根目录，平台在当前登记连接的同步缓存中核对固定提交、采集范围及 Git blob。全部一致时复用登记仓库索引，
可按返回的 `repository_id` 或 `codebase_id` 查询。未提交改动、子目录或无法验证的内容保留为独立设备快照。
`checkout.status` 指示复用、已关联、未登记、匹配歧义或连接已变化。设备绝对路径留在本机，Git 地址仅用于匹配。

`--dir --output` 无需设备配对，使用同一套提取器将可浏览的 Markdown、最近一次导入清单、各来源的独立清单和代码图谱写到 `<输出目录>/teamwiki/`。通过 `codebase --output` 读取同一目录，可用 `--project <代码库名称>` 查看某个来源。`--lint` 只读检查清单、索引及证据完整性，高严重度问题返回退出码 1。重复导入复用未变化的提取结果，并更新活动图谱与导航；原始源码留在设备目录。

代码导入默认在索引发布后运行深度生成。远端仓库先根据固定源码索引生成架构导读，附到源码概览中。本地目录导入跳过导读。模型暂时失败时源码概览仍可浏览，导入结果的 `repository_overview` 与 `failures` 会显示状态。用 `--via` 选择工具，也可设置 `WEORA_AI_TOOL` 或 `TEAMAI_AI_TOOL`。默认优先使用已安装的 Claude Code，随后尝试 Codex。`--skip-enrich` 跳过 AI 生成。预览和仅生成组织清单无需启动 Agent。指定的工具无法运行时，导入会在写入前报告错误。设备上还没有客户端时，CLI 保存代码索引并报告待补齐的设计。

仓库按固定版本的文件声明与关系生成业务领域、简介和关键词。离线目录用当前版本的领域组织仓库导航；平台项目可按领域或关键词检索。用 `--from-repo ... --domain <名称>` 或在 YAML 清单的仓库条目中填写 `domain` 可指定领域，配合 `--skip-enrich` 时也会保存。重新指定领域会保留已有的简介和关键词。分类失败时其余设计可以继续生成，重跑后可补齐画像。

批量导入会保存各仓库的索引回执。设计生成失败时，`import --resume <运行ID>` 只补失败仓库的设计，复用已提交的索引和成功文档。单目录和平台单仓也可用 `codebase --deep-enrich` 补齐。生成阶段使用该次索引回执，索引已变更时会提示按当前版本另起生成。

用 `contribute --kind doc --resource-path product/<文档名>.md` 提交产品文档并完成评审。项目成员可运行 `codebase --reconcile --dry-run` 查看文档与代码的映射、缺口、冲突和普通资源的候选关系。管理员去掉 `--dry-run`，将产品映射发布到项目图谱。若要采纳普通文档或规则的候选，在 Markdown 的 `related:` 中加入 `MAPS_TO: <代码条目ID>`，再贡献该资源的新版本并提交评审。也可用 `--maps-to <代码条目ID>` 提交同一关系。
离线调和读取 `<输出目录>/teamwiki/product/` 的产品资料，以及 `teamwiki/docs/` 和当前索引中的代码证据。预览还从输出目录下的 `docs/`、`rules/` 提出关系候选。正式执行只更新 `teamwiki/.indices/graph-index.json`。

离线 `codebase --deep-enrich` 使用导入结果的代码库名称。它先以最多 3 路并行归纳模块职责，再归类仓库。业务域、层级、职责与源码入口可在对应快照的 `_component-profiles.json` 查看。随后按组件并行生成设计文档，默认并发 5，再生成架构总览和有源码依据的跨组件场景序列图。`--max-modules` 只限制详细设计。`missing_profiles` 列出待补的模块职责，已有设计和提取证据可继续使用。依赖、调用、接口和三元组等图谱从索引直接生成。部分文档失败时命令返回退出码 1 并列出缺项，重新运行只生成当前版本缺失的文档。源码更新后，新一轮生成绑定新的证据版本，项目索引切换到新版本。用 `--via claude` 或 `--via codex` 选择客户端，默认读取 `WEORA_AI_TOOL` 或 `TEAMAI_AI_TOOL`。预览计划无需启动客户端。

在平台项目里用 `--repository` 或 `--local` 选择要分析的索引。本地快照的上传成员可以发布该快照的设计。服务端按项目授权返回固定的声明和关系，CLI 生成 Markdown 后逐篇发布。平台返回可用 `weora context get` 打开的知识条目 ID，团队成员也能按项目检索。相同证据的已发布文档可续用。源码、采集范围或提取事实变化后，旧计划会被拒绝，原设计保留为历史版本。

远端、清单和组织同样支持 `--output`。单仓与清单的 `--dry-run` 查询远端 HEAD；组织预览列出会写入白名单的仓库。增量导入时，同一提交及采集上限对应的完整证据会直接复用。其他情况浅克隆到临时裸库，读取固定 Git 对象。清单按优先级处理，生效的单库证据进入同一份图谱；批次部分失败后，用原运行 ID 续做未完成仓库。可使用 `--ssh`，也可在 YAML 为每个来源设置 `auth: ssh` 或 `auth: public`。其他 HTTPS 来源使用匹配托管平台的本机凭据或 Git 认证配置。

平台模式下，`--from-repo` 也接受与项目登记匹配的 HTTPS 或 SSH 地址。`--from-repo-list` 接受
`version: 1` 与 `repos`，每项使用 `url` 或 `repository_id`，可选 `priority`
为 `high`、`normal` 或 `low`。项目登记决定 Git 连接与凭据。清单中的 `org` 行给出跳过原因，
可通过 `--from-org` 单独展开。单个仓库失败时，其余仓库仍提交，命令返回失败详情与原运行 ID。

`--from-org` 支持 GitHub 和 GitCode，默认拉取最多 200 条并排除已归档的仓库。
用 `--include-pattern` 和 `--exclude-pattern` 按仓库全名筛选。组织发现读取本机相应托管平台凭据。
组织清单写到 `.weora/repo-whitelist.draft.yaml`，`--skip-import` 只生成清单。
`--dry-run` 预览发现与登记匹配，跳过写文件与索引。组织中未登记的仓库会列入失败项。

## 输出约定

- `--help`：stdout 输出可读帮助，退出码 0。
- 命令成功：stdout 输出一行 UTF-8 JSON，退出码 0，stderr 为空。
- 参数错误或命令不存在：stderr 输出一行 JSON，退出码 2，stdout 为空。
- 内部异常：stderr 输出脱敏 JSON，退出码 1，stdout 为空。

```json
{"ok":true,"data":{"version":"0.1.0","protocol_version":1}}
```

```json
{"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"..."}}
```

`commands` 的 `business_capabilities` 包含 `auth`、`collab` 和 `skill`。用各命令的 `--help` 查看操作和参数。原生 Hook 与 `skill launch` 使用宿主的输入输出协议。

## 验证与构建

```powershell
uv run --project apps/cli python -m unittest discover -s apps/cli/tests -v
uv build --project apps/cli --out-dir apps/cli/dist
```

静态检查复用后端已有开发工具：

```powershell
backend/.venv/Scripts/ruff check apps/cli
backend/.venv/Scripts/ruff format --check apps/cli
backend/.venv/Scripts/mypy --config-file apps/cli/pyproject.toml apps/cli/src apps/cli/tests
```

架构与后续接入方式见 [CLI 与团队上下文接入方案](../../docs/architecture/weora-cli-and-team-context.md)。

## 上传团队 Skill

登录并授权设备后，在任意目录运行以下命令。参数可以是包含 `SKILL.md` 的目录，也可以是现有的 `.zip` 或 `.skill` 包。`upload` 是 `push` 的别名。

```powershell
weora skill push ./my-skill
weora skill push ./my-skill --project-id <Weora项目ID> --namespace engineering/platform
weora context get skills/engineering/platform/my-skill/SKILL.md --project <Weora项目ID>
```

首次提交会创建待发布草稿。同一范围、同一目录下的同名包进入该 Skill 的「待审核」列表，管理员可预览文件、查看校验结果、接受为草稿或驳回。在控制台的 Skills 页面选择工作空间共享或对应项目，即可审核候选包并发布草稿。项目详情显示团队目录，下载和使用统计沿用同一项目范围。活动记录会显示实际发布人。包内 `agents/openai.yaml` 的展示名称和简介可在控制台搜索。普通新包的安装目录由 `SKILL.md` 的 `name` 决定，晋升 Skill 使用经验文件名。

编辑已安装的目录后，直接把该目录传给 `weora skill push`。CLI 根据本机安装记录提交到原项目或共享目录，审核仍在原资产进行。包未修改时返回 `unchanged`。如果团队已经发布更新，先核对新版本，再提交合并后的修改。晋升 Skill 发布后，知识库中的原条目同步使用新版本。随后运行 `weora skill pull --runtime codex`，已安装的文件和发布包完全一致时仅更新本机版本及回执。仍有未发布修改时会报告冲突并保留文件。

管理员在 Skills 页面归档晋升资产后，项目召回和分发清单会同时撤下它。下次 `pull` 清理未改动的受管副本，有本地改动的副本会保留。管理员可在 Skills 页面恢复资产，也可执行 `weora context restore <知识ID> --project <项目ID>` 恢复原发布版本。共享 Skill 恢复时省略项目参数。若原经验已有另一份待审或已发布的晋升，或原路径已被其他资产使用，先处理冲突再恢复。
