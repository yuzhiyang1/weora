# Weora 离线架构图

当前架构图用于说明代码中的任务入口、调度与执行边界。职责总览和两张飞书图记录[第一版研发 Agent 架构](../agent-platform-and-feishu-context.md)的设计。每张图采用独立、静态的 HTML 与内联 SVG，使用离线系统字体。

## 图表入口

| 图表 | 说明 | 离线源文件 | 文档图片 |
| --- | --- | --- | --- |
| 当前架构与路线图 | 双语总览，包含控制面、持久化调度、Pi 与可选的 Oh My Pi、工具网关，以及四项计划中的能力 | [HTML](weora-current-architecture.html) | [SVG](weora-current-architecture.svg) |
| 职责总览 | 请求、调查、审批、工具与交付的责任分界 | [HTML](weora-overview.html) | [SVG](weora-overview.svg) |
| 飞书上下文 | 自动补齐与主动查询共用服务及证据快照 | [HTML](feishu-context.html) | [SVG](feishu-context.svg) |
| 接入与授权 | 渠道、成员、来源和输出受众分别约束 | [HTML](feishu-boundaries.html) | [SVG](feishu-boundaries.svg) |
| 工作空间邀请 ER 图 | 用户、空间、成员、邀请和会话的关系及键约束 | [HTML](workspace-invites-er.html) | [SVG](workspace-invites-er.svg) |

HTML 是绘图源文件，SVG 由源文件导出。修改时先编辑 HTML，再运行上游 `skills/diagram-design/scripts/export_svg.py <HTML 路径> --system-fonts`，不要分别维护两套几何与文字。HTML 可直接离线打开；在窄屏中局部横向滚动，打印样式允许整体缩放。SVG 可用于 Markdown 内嵌。

## 绘图规范

- 工具：[cathrynlavery/diagram-design](https://github.com/cathrynlavery/diagram-design/tree/550fd66edff058bc73056c3416b76d69cfe865ba)。本次固定 commit 为 `550fd66edff058bc73056c3416b76d69cfe865ba`，Skill 元数据版本为 `2.6`。
- 参考：`assets/template.html`、`references/type-architecture.md`、`primitives-core.md`、`output-spec.md` 与 Mermaid 导入规范；几何与内容为 Weora 独立绘制。
- 类型：Architecture。这里主要表达组件调用与约束关系，不采用消息时序、部署拓扑或状态机的布局。
- 输出：HTML 与 SVG；`doc-wide` 画布为 1280×720，`balanced` 细节、工程读者。每张不超过 9 个主要节点、12 条关系、2 个蓝色焦点。
- 风格：[Weora 图表配色与字体](style-guide.md)，已保存为命名 Profile `weora`，沿用用户确认的蓝白配色、离线字体和整体观感。仅通过 Weora 项目标记选用，不修改安装目录或其他项目的绘图风格。
- 许可证：模板规范采用 MIT，保留[上游声明](LICENSE.diagram-design.txt)。上游仅提供绘制方法，不代表其认可 Weora 架构。

## Profile 自动选择

项目根目录 `.diagram-design` 的内容为 `profile: weora`。以后生成 Weora 架构图时，直接读取 `%USERPROFILE%/.diagram-design/profiles/weora.md` 中的完整样式规范，不必重新选择风格。

完整 Profile 位于用户目录，不随 Git 仓库自动迁移；换机器时需复制该文件。缺失时明确报告，不静默切换到其他 Profile。仓库内的[样式摘要](style-guide.md)用于阅读和核对，不能代替完整 Profile 的结构检查。

## 内容合并与保真记录

原总体 Mermaid 逻辑图有 19 个节点和 26 条关系，超出单张总览预算。新的总览压缩为 9 个节点、12 条关系，并用上下文与授权细节图补足重点；原逻辑图保留在架构文档的折叠区域。

| 原内容 | 新图的处理方式 |
| --- | --- |
| 飞书、Console、Inbox 与 Run 接入 | 总览以请求入口、Task/Run 控制合并表达；持久接收、确认时机保留在产品说明的调用顺序中 |
| 自动装配、Context Service、来源数据与上下文工具 | 分离为飞书上下文细节图，保留自动与主动读取的不同触发时机 |
| 计划固化与人工审批 | 计划固化归平台 Run 控制，连接至人工审批；拒绝或补充的分支保留在正文状态规则中 |
| Worker、Runtime Adapter 与 SandboxAgent | 总览突出执行角色，进程和 Runtime 边界保留在架构第 4 节；不表示角色自身绕过平台调度 |
| 研发工具、隔离环境、产物与回写 | 合并为研发工具与沙箱、可见性检查与交付；授权的草稿 PR、交付证据与 Outbox 细节仍以开发计划为准 |
| 取消、失败、恢复、预算与存储实现 | 不在总览铺开，保留在原逻辑参考、正文和验收条件中，不视为删除需求 |

## 验证记录

2026-11-06 当前架构图通过 `self_check.py --offline`，并用 `export_svg.py --system-fonts` 导出。已核对九个节点、八条连线、标签遮罩与 SVG 导出一致性，并在 Chrome 检查桌面渲染和窄屏局部滚动。字体使用系统回退，打印样式未实测。

2026-10-17 已对三个 HTML 执行固定版本上游脚本：

- `skills/diagram-design/scripts/self_check.py --offline`：全部通过，检查可访问 SVG、单文件安全与边界。
- `scripts/verify-geometry.py`：全部通过，0 个发现；另人工检查箭头交叉并加入桥形跳线。
- `skills/diagram-design/scripts/export_svg.py --system-fonts`：成功导出三个 SVG，保留命名空间、标题与描述。
- 使用环境已有的 Sharp 进行纯本地 SVG 栅格预览，检查中文、连线和文字位置；预览文件位于 `output/diagram-review-20261017/`，不作为绘图源文件。

2026-10-17 用户已确认当前输出风格，并要求保存为 Weora 专用 Profile。浏览器本地文件预览受当前环境策略限制，未执行浏览器字体解析、窄屏滚动及打印实测，用户观感确认不替代这些技术验证。不同机器未安装相同字体时允许按声明的系统字体栈回退。
