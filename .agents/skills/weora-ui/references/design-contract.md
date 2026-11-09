# Weora HeroUI 设计契约

用户在 2026-10-21 体验两套交互预览后，明确选择完全使用 HeroUI。正式基线为 ADR-0013；旧 shadcn 重构提交 `0fc6eda` 仅保留历史参考，不再约束新界面圆角、组件或主题。

## 视觉与来源

- 正式组件：`@heroui/react` / `@heroui/styles` 3.2.6，React 19，Tailwind CSS 4；具体安装版本以 `apps/web/package.json` 与根锁文件为准。
- 保留默认主题：深色背景与分层表面，浅色灰底白卡片，蓝色主操作，柔和状态 Chip，圆润 Button / Input / Card 与原生焦点、按压反馈。
- 公共表单使用官方 `secondary` 输入与选择器变体，保证深色卡片上的空字段仍可辨认；不通过自写边框或背景另做皮肤。
- 控件尺寸与圆角使用官方变体，不用旧的 4/6/9/12px 标尺覆盖。中文普通内容优先 13～16px；辅助信息可更小，但不得牺牲关键操作的可读性。
- 2026-10-21 按用户要求纳入 [公开 Design Taste 的项目落地标准](design-taste.md)：正文和字段标签以 14px、辅助说明以 12px 为基线；工作台优先任务信息，操作区域保持清楚的主次，不以缩字或营销装饰替代布局。
- 图标使用 Lucide，字体使用系统栈。业务内容和布局由 Weora 自己设计，不搬用 Pro 模板或受限制的成品代码。
- 首次访问默认深色，保留用户已有浅色选择。Web 与采用 React DOM 的桌面端可复用这些控件；原生 App 继续遵守 ADR-0001 的领域与平台边界。

## 参考截图

以下文件随 Skill 保存；2026-10-21 按公开 Design Taste 方向优化后更新为正式页面，工作台取消宣传区，优先展示真实任务。内容来自隔离验收数据，不能当作业务默认值。

| 文件 | 观察重点 |
| --- | --- |
| [工作台浅色](screenshots/overview-light.png) | 默认浅色表面、蓝色主操作、卡片与导航层次 |
| [工作台深色](screenshots/overview-dark.png) | 深色背景、表面分层、文本及按钮对比度 |
| [工作台窄屏](screenshots/overview-mobile.png) | 可用的折叠菜单、双列指标、内容与表格内滚动 |
| [创建 Agent](screenshots/agent-editor.png) | HeroUI Modal、输入、标签、复选、提交态 |
| [运行详情](screenshots/run-detail.png) | HeroUI Tabs、Chip、详情表面和业务操作 |

## 组件、样式与成熟页面

| 场景 | 公共实现 | 业务对照 |
| --- | --- | --- |
| 按钮、输入、搜索选择、复选 | `components/console/controls.tsx` | `features/runs/CreateRunDialog.tsx` |
| 弹窗、卡片、状态、页签 | `components/console/surfaces.tsx` | `features/agents/AgentEditorDialog.tsx`、`features/runs/RunDetailPage.tsx` |
| 类型化数据表格 | `components/console/data-table.tsx` | `features/agents/AgentsPage.tsx`、`features/projects/ProjectsPage.tsx` |
| 导航、主题、快速查找 | `app/AppShell.tsx` | `features/overview/OverviewPage.tsx` |
| 上传及错误恢复 | `features/skills/SkillPackageDialog.tsx` | `features/skills/SkillsPage.tsx` 与对应测试 |

表内目录前缀均为 `apps/web/src/`。业务组合只适配数据、提交与权限约定，所有基础交互组件均来自 HeroUI。已有组件没有需要的能力时，再查锁定版本的官方组件及类型；不要恢复 `components/ui` 下的 shadcn 源码。

| 样式文件 | 维护范围 |
| --- | --- |
| `styles/global.css` | Tailwind → HeroUI → 业务布局的导入顺序 |
| `styles/theme.css` | 系统字体、正文与旧业务语义别名；不覆盖 HeroUI 默认配色 |
| `styles/workspace.css` | 侧栏、上下文、折叠与窄屏导航布局 |
| `styles/controls.css` | 业务组合、表单、上传和内容滚动布局 |
| `styles/pages.css`、`styles/dashboard.css` | 列表、详情、工作台的信息布局 |
| feature CSS | 局部业务补充，避免组件内部皮肤覆盖 |

HeroUI 的 `--muted` 是次要文字，`--surface-secondary` 才是表面底色；使用 `--accent` 与 `--accent-foreground` 表达主操作。`--card`、`--brand`、`--weora-*` 等旧业务名只能映射到 HeroUI 令牌。没有新的共享设计决策时，不在 feature 定义品牌色或主题。

## 业务与可访问性

- 加载、空与失败分开；已加载页数不冒充全部统计，错误不能变成成功。
- 字段与图标按钮有名称；隐藏导航不可聚焦，展开后限制焦点并支持 Escape。
- 提交失败保留输入，提交中禁止双击与不安全关闭；成功后可以重新打开。
- 选择器过滤后保留多选值；不能绕过 `TableColumn<T>` 的字段类型约束。
- 长 ID、JSON、日志和宽表格在内容区内换行或滚动，不撑宽整页。
- 业务权限、固定版本、审批和恢复规则保持原有契约。

来源与许可证见 `apps/web/licenses/README.md`。工程绘图仍使用根 AGENTS 第 13 节的独立约定。
