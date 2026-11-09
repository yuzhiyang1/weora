---
name: weora-ui
description: "使用 Weora 已确认的 HeroUI v3 设计体系新增、修改或审查控制台页面、组件、CSS、弹窗、表单与响应式交互。涉及 apps/web 的可见界面时使用，保持 HeroUI 单一组件与主题来源；纯后端任务不触发。"
---

# Weora UI

Weora 正式界面统一使用 **HeroUI v3**。用户在 2026-10-21 对比交互预览后选择了 HeroUI：保留其圆润控件、蓝色主操作、表面层次和交互反馈，不再混用 shadcn/ui 的中性按钮、小圆角或 Radix 控件。

本 Skill 随仓库维护。代码路径均相对 Weora 根目录；先确认实际仓库，不能因聊天初始目录不同而修改 Plane。

## 开始界面工作前

1. 读取 [设计契约](references/design-contract.md) 和 [公开设计原则的项目落地](references/design-taste.md)，查找当前公共组件及一个同类成熟页面。后者用于确定信息层级、留白与操作主次，不代替业务契约。
2. 先读 `apps/web/src/components/console` 的实际 API，再扩展业务交互。该目录适配业务提交、表格列与反馈，底层全部来自 `@heroui/react`。
3. 打开相关基线截图，并在真实浏览器查看当前页面。可复用仍有效的检查，不为小改动重跑完整流程。
4. 共享设计依据为 [ADR-0013](../../../docs/adr/0013-heroui-console-ui.md) 和用户当前要求。旧 ADR-0010 的组件选择已被替代；未确认的新页面不能反过来覆盖当前基线。

## 实现约定

- 只使用 `@heroui/react` / `@heroui/styles` 作为正式 Web 的组件与主题来源。复用 `components/console` 的业务组合；没有组合时直接使用 HeroUI。不得重新加入 shadcn、Radix、cmdk、Semi、Ant Design 或 MUI 形成第二套基础控件。
- `styles/global.css` 按顺序导入 Tailwind CSS 与 HeroUI 样式。颜色、控件圆角、状态、阴影与交互优先保留 HeroUI 默认值；`styles/theme.css` 只维护业务语义别名和基础排版，不重新实现一套皮肤。
- 业务 CSS 管布局、宽度、间距、换行与滚动，不覆盖 `.button`、`.input`、`.select`、`.modal`、`.tabs` 等组件内部外观来恢复旧设计。表面背景使用 `--surface` / `--surface-secondary`，`--muted` 是次要文字色。
- 延续 `AppShell` 的导航与页面结构，图标使用 Lucide。新增普通页面不重建侧栏、顶部栏或主题系统。
- 保持浅深色成对可用，使用现有 `weora-appearance` 持久化；首次访问默认深色，已保存的浅色选择继续生效。
- Multica 仅用于公开交互参考，不复制其代码、CSS 或品牌资产。实际组件来源见 `apps/web/licenses/README.md`。
- 业务数据继续使用 feature API 和 Query。保持工作空间隔离、私有任务、固定版本、审批、恢复与凭据保护；不复制原型里的示例数据或模拟进度。

## 易错的组合边界

- `console/Button` 的提交属性仍是 `htmlType="submit"`，`type` 表示业务色调；底层 HeroUI Button 使用 `type="submit"`、`variant`、`isDisabled` 和 `onPress`。异步期间禁用重复提交及不安全的关闭入口。
- HeroUI v3 使用组合 API，不能照搬 v2 的 `ModalContent`、`selectedKeys` 等旧用法。以项目锁定版本的类型和实际组件为准。
- Checkbox 与 Switch 的 `Control` 必须放在各自 `Content` 内，才能保持标签点击与键盘操作。不要嵌套原生 label。
- 导航使用 React Router 链接，外观可用官方 `buttonVariants`；操作使用 HeroUI Button。不使用 `asChild`，不在链接内嵌套按钮。
- 复用受控 `console/Modal`，让 HeroUI 管焦点、Escape、遮罩与滚动。业务提交失败保留输入，成功与重开均恢复可用；关闭后应归还焦点。
- `console/Select` 支持搜索与多选，过滤候选项时保留全部已选值。切换项目或 Agent 后，仍由业务校验选项有效性。
- 业务布局可用原生语义标签；文件选择仍使用原生隐藏 file input。不得因此手工重写已有的按钮、输入框、选择器或页签。

## 验收与交付

根据影响运行 typecheck、lint、test、build；公共组件迁移需覆盖业务调用及错误状态，不能只检查原型。新增行为测试关注可观察结果，不断言库的私有 DOM。

```powershell
pnpm --filter @weora/web typecheck
pnpm --filter @weora/web lint
pnpm --filter @weora/web test
pnpm --filter @weora/web build
```

真实浏览器检查修改页面的浅色、深色、桌面与窄屏；公共 UI 至少检查 1440×1000 和 390×844。检查焦点、键盘、长文本、加载、空、失败、提交中与权限不足状态，宽表格在自身容器内滚动。

本次截图放在 `output/playwright/<任务>/`。共享方向变更经用户明确选择后，更新设计契约、相关 ADR 和仓库内参考截图；不能用替换截图掩盖意外偏离。本 Skill 不授予提交、推送或发布权限。
