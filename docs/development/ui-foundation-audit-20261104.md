# Figma 基础组件与现行 UI 规范复核

日期：2026-11-04。

## 结论

**未通过。现有 Figma 基础组件库不能视为已与 Weora UI 规范完整对齐，也不能直接作为开发时覆盖 HeroUI 样式的依据。** 上一轮设置页中部分按钮的实例替换，只修正了局部问题，没有完成基础库验收。

当前正式基线仍是 ADR-0013、项目 `weora-ui` Skill、HeroUI 3.2.6 与 `apps/web/src/components/console` 的实际组合。复核没有改变这项产品决策，也没有修改应用代码。

## 检查方法与范围

- 读取当前安装的 `@heroui/styles`，没有套用 HeroUI v2 或网上其他版本。
- 通过 Figma 网页原生导出深、浅两个基础画板的 SVG 和 PNG；SVG 保留文字与图层名称，逐项检查画板中的按钮、状态标签、输入、导航、页签和侧栏。
- 原库存为每种主题 16 个主组件，上一轮另加一个 Dark/Button/Outline。导出另出现一个 Outline 实例；不要把实例重复计为新的主组件。
- 对照仓库内已确认的创建 Agent、运行详情基线截图。本轮没有重新运行应用，所以这些截图属于已保存的正式基线，不冒充本轮实时截图。
- Figma MCP 当前有额度限制，检查使用正常网页操作和原生导出完成，没有用隐藏接口读取或修改文件。

## 发现

| 范围 | 当前 Figma | 现行实现 / 规范 | 结论 |
| --- | --- | --- | --- |
| 深浅 Primary 与新增 Outline | 已校正为 36px 高；Primary 的主色、前景色正确；Outline 的描边色正确 | 桌面 md 按钮高 36px、圆角 24px、横向 padding 16px | 这些基础外观已修正；字体与完整状态仍未验收 |
| 深浅 Secondary / Danger / Disabled / Busy | 仍有 40px 高、12px 圆角 | 桌面 md 高 36px、rounded-3xl 为 24px；窄屏 md 高 40px，不能把两种上下文混为一谈 | 不对齐，不能将当前组件称为桌面默认按钮 |
| 深浅 Secondary | 使用 surface-secondary 灰底，前景为普通 foreground | `button--secondary` 使用 default 背景和 accent-soft-foreground；console 默认按钮其实映射 outline | 变体语义与用色都不对齐 |
| 深浅 Busy | 完整不透明的主按钮，仅显示提交文字，没有 Spinner | `controls.tsx` 的 loading 同时启用 isDisabled、aria-busy，并渲染 Spinner；禁用透明度是 0.5 | 未表达当前业务组合的真实提交状态 |
| 十个 Tag | 各状态统一灰底，文字直接使用 accent / success / warning / danger 原色，文字为 Regular | `console/Tag` 固定 sm + soft；背景和前景应分别来自对应 soft 令牌，字重 medium | 状态视觉系统不对齐，不是只调整圆角即可 |
| 四个 Input | 使用 surface-secondary：深色 #232325、浅色 #EFEFF0；横向留白 16px | `console/Input` 默认 secondary；实际背景来自 default：深色约 #27272A、浅色约 #EBEBEC；px-3 为 12px | 背景令牌及横向留白不对齐。Invalid 还需按官方状态 ring 核验 |
| 四个 Tab | 选中项蓝字；深色选中底色是 surface；标签高度与默认样式有偏差 | 当前 console/Tabs 使用默认 variant；选中前景为 segment-foreground，指示器为 segment，标签高 32px；不是蓝字独立按钮 | 选中状态和容器组合不对齐 |
| 两个 Sidebar | 宽度 224px | 当前 theme.css 桌面默认宽度 232px；其他断点另有明确规则 | 与当前桌面 AppShell 基线不一致 |
| 字体 | Noto Sans SC 回退 | 正式 Web 使用 Segoe UI Variable / Segoe UI / PingFang SC / Microsoft YaHei 系统栈 | 已知差异，不能声称字体完全一致 |
| 控件覆盖 | 当前库只有上述有限种类，其他设置内容大量为文字和矢量分组 | 设置页还需要 Select、Switch、Checkbox、Modal、Card 等真实组合及焦点、错误、禁用、提交状态 | 可复用组件和状态库存不完整 |

基础语义色本身不是全部错误：背景、surface、accent、foreground 等主要颜色与源码一致。主要问题是缺少 default、soft、segment 等令牌后，组件错误地复用了 surface-secondary 或原始状态色。

## 源码依据

- `.agents/skills/weora-ui/references/design-contract.md`：HeroUI 是唯一组件和主题来源。
- `apps/web/src/components/console/controls.tsx`：Button 变体映射、loading 组合、Input 默认 secondary。
- `apps/web/src/components/console/surfaces.tsx`：Tag 的 sm/soft 和 Tabs 的组合。
- `apps/web/src/styles/theme.css`：系统字体、桌面默认侧栏宽度。
- `apps/web/node_modules/@heroui/styles/dist/components/button.css`：高度、圆角、变体和状态。
- 同目录的 `input.css`、`chip.css`、`tabs.css` 及 `dist/themes/default/variables.css`：输入、状态标签、页签与语义令牌。

## 证据文件

均在 `output/figma-console-20261104/`：

- `foundation-audit-before-dark.svg` / `.png`
- `foundation-audit-before-light.svg` / `.png`
- `foundation-audit-before.json`：从两份原生 SVG 提取的图层、颜色、字重和字体，包含那个额外的 Outline 实例，不能直接当主组件数量。
- `foundation-audit-inspector.png`：本轮网页属性检查截图。

这些文件是复核证据，不是修正后的设计交付。除了导出配置，本轮没有修改 Figma 组件外观；上述不一致仍然存在。

## 校准顺序

1. 先补齐并绑定真实 default、soft、segment、focus 等语义令牌；保留已经正确的颜色，不重建品牌皮肤。
2. 在原主组件上校正变体、尺寸、字重、输入内边距与提交状态，保留有效 ID 和实例引用；完善缺失的基础控件及状态。
3. 再检查两套主题和既有设置页实例。矢量分组不会随主组件自动修正，必须单独替换或同步，不能只改组件库便宣布整稿完成。
4. 导出并比对正式基线，验证受影响的原型入口后，才将状态改为通过。


## 后续校准结果（2026-11-05）

前文记录的是当时的检查结果。现有基础组件偏差及相关页面现已修正，详见 [本轮校准与验收记录](ui-foundation-repair-20261105.md)。字体回退、部分矢量控件及完整状态库存仍按新记录明确保留边界；不得将本次结果表述为整个设计系统已完全一致。
