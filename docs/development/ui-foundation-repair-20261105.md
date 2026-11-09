# Figma 基础组件校准与页面同步结果

日期：2026-11-05。范围：修复现有基础组件的已知偏差，并同步相关设计稿；没有修改应用代码。

## 结果

现有 34 个主组件的本轮校准已经完成，深浅色各 17 个。当前 28 个设置与流程画板已同步相关控件。此次验收针对下列明确列出的尺寸、用色、字重、布局和原型路径，不代表整个设计系统已完成全部控件及交互状态覆盖。

Figma 文件：<https://example.test/redacted>。

## 源码基线

- 项目 `.agents/skills/weora-ui/SKILL.md` 与其 design-contract、design-taste 参考。
- `apps/web/src/components/console/controls.tsx`、`surfaces.tsx`。
- `apps/web/src/styles/theme.css`、`controls.css`、`workspace.css`。
- 当前安装的 HeroUI 3.2.6：button、input、chip、tabs 与 spinner 的实际实现及主题变量。
- 仓库已确认的 Agent 编辑、运行详情截图用于视觉对照；本轮没有重新运行应用，不将旧截图描述为本轮实时应用截图。

## 主组件修正

| 项目 | 本轮结果 |
| --- | --- |
| Button | 桌面默认高 36、圆角 24、左右 padding 16；Secondary 使用 default / accent-soft-foreground；Outline 透明描边。 |
| Disabled / Busy | 使用主按钮前景色、50% 透明度；Busy 加入源码对应的 16px Spinner 静态图形。 |
| Tag | sm + soft；高 20、左右 padding 4、上下 padding 0；12/20 Medium；状态背景与前景分开使用 soft 令牌。 |
| Input | 高 36、圆角 12、左右 padding 12；default 背景；14/20 Regular；Invalid 为未聚焦状态的 1px 外侧 danger 描边。 |
| Tabs | 单个标签高 32、圆角 24；选中为 segment / foreground。页面组合包含 default 背景、4px 内边距和 40px 容器。 |
| Sidebar | 桌面默认宽度由 224 改为 232。 |
| 令牌和文字 | 补齐两种主题的 default、segment、soft 等 22 对原始/语义变量，补 space/0；共享 Label 14/20 Medium、ControlText 14/20 Regular、TagLabel 12/20 Medium。 |
| 库清理 | 新增 Light/Button/Outline 主组件；删除额外的临时 Outline 实例，保留主组件及有效引用。 |

没有将所有背景统一替换为同一种灰色，也没有将 outline 与 secondary 混为一类。原本正确的 background、surface、accent 等继续保留。

## 页面同步

- 94 处单行字段统一为 36px 高、12px 水平内边距；多行说明字段保留原有高度。
- 52 处范围和状态标签调整为 20px 高、柔和背景，并绑定共享 TagLabel 样式。
- 12 处飞书页签热点调整组合外观，绑定共享 Label 样式；保留原热点外层 ID。
- 保留的 10 个矢量操作按钮检查并同步文字样式；工作台侧栏操作改为透明描边。它们仍是可编辑矢量，不冒充原生实例。
- 原有按钮和输入框实例继承主组件修改；历史页面只复查实例继承，不重新设计其旧业务流程。
- 28 个当前页面的根 Frame ID 均保留；156 处侧栏文字坐标与修复前导出一致。
- Figma 画板中的评审说明已经同步更新；根节点仍为 36:6。

## 原型与验收

- 补齐授权页返回应用页的深浅色连线。
- 32 个主题切换入口覆盖全部 28 个当前画板，其中 30 个为本轮补齐。
- 从已经验证的侧栏入口复用 130 个导航热点，分 12 批检查目标；当前栏目及对应弹窗背景的高亮分组没有替换。
- 实际点击验证了设置入口、返回工作台、两种主题的飞书页签往返、主题切换、新增空间弹窗打开及取消、六类侧栏导航。
- 验收记录包含 20 次成功跳转；此前发现的缺失连线和未命中热点的点击保留在 attempts 中，均有后续成功复测，未解决项为 0。
- 原生 SVG 结构校验：94/94 单行字段、52/52 标签、12/12 页签文字样式、28/28 根 Frame ID、156/156 侧栏文字位置通过。
- 76 个应用源文件与本轮设计工作前的基线哈希一致。

原生导出期间发现过 SVG 命名空间导致标签未显示、SVG 导入忽略 Medium 字重的问题。已修正兼容性，并将标签和页签绑定到 Figma 共享文字样式；最终导出重新验证通过。

## 明确边界

- Figma 网页字体列表无法使用 Segoe UI、Microsoft YaHei、PingFang，当前仍使用 Noto Sans SC 回退；不能声称字体已与 Web 系统栈完全一致。
- 页面中的部分控件仍为可编辑矢量，已校准本轮核对的外观，尚未全部实例化。现有库也不是 Select、Switch、Checkbox、Modal、Card 及 hover/focus 等所有类型与状态的完整组件库存。
- 表单填写、保存、连接探测等仍为设计演示，不是可调用后端的应用功能。本轮没有运行应用构建或业务自动化测试。
- 当前 Figma 是设计真值。旧 `settings-v2.cjs` 和旧 SVG 保留历史生成结构，不得整页重导覆盖现有实例及原型连接。

## 证据

位于 `output/figma-console-20261104/`：

- `foundation-after-dark.svg/.png`、`foundation-after-light.svg/.png`：最终基础库原生导出。
- `settings-after-svg/`、`settings-after-png/`：28 页与评审说明的最终导出。
- `dependent-verification.json`、`final-verification.json`：结构与源码哈希核验结果。
- `prototype-after-verification.json`：真实点击与复测记录。
- `theme-prototype-repair.json`、`navigation-prototype-repair.json`：连线修复明细。
- `prototype-after-light.png`：实际浏览器原型截图，保留完整页面上下文。
- `legacy-after-svg/`：历史页面中的实例继承复查。
