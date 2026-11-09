# ADR-0013：统一采用 HeroUI 控制台组件

- 状态：已接受
- 日期：2026-10-21
- 替代：ADR-0010 的基础组件、主题与 React 版本选择

## 背景

用户体验了 HeroUI 与 shadcn/ui 两份真实组件交互预览，明确选择完全采用 HeroUI，并同步项目 UI Skill。继续保留原来的中性按钮、小圆角与旧控件 CSS，会使正式页面偏离已选择的效果。

## 决策

1. 正式 Web 只使用 `@heroui/react` / `@heroui/styles` 3.2.6，使用 HeroUI 默认主题、组件变体和交互。移除本地 shadcn 基础组件及 Radix、cmdk 等直接依赖。
2. 按 HeroUI v3 的运行要求升级 React / React DOM 至 19，配套升级类型。保留 React Router、TanStack Query、Rsbuild 与现有前后端契约。
3. `components/console` 继续适配业务提交、受控弹窗、可搜索多选、类型化表格与反馈。该层不维护另一套皮肤；没有业务包装需求时直接组合 HeroUI。
4. Tailwind CSS 在 HeroUI 前导入。共享样式仅保留布局、排版及旧业务令牌别名，移除对基础控件背景、圆角、内部 DOM 的旧覆盖。首次访问默认深色，保留已有外观选择。
5. 认证、工作空间、私有任务、版本冻结、审批、事件续读、恢复及凭据保护不随视觉迁移改变。原型数据不进入产品。
6. 项目 Skill、设计契约与正式页面截图作为后续 UI 开发基线。原型保留为设计对比历史，独立依赖和 CSS 不进入正式产物。
7. 根据用户随后确认的方向，吸收 [Design Taste 公开说明](https://heroui.pro/docs/react/getting-started/design-taste) 的十类设计方向，以项目 Skill 的 `references/design-taste.md` 保存 Weora 的具体规则。正文、说明和标题保持可读层级，工作页面优先呈现任务信息。该规则由公开原则整理，不依赖 Pro 组件、模板或未获取的完整 Skill。

## 跨端边界

延续 ADR-0001：Web 与采用 React DOM 的桌面客户端可复用界面与令牌；原生 App 优先共享数据与领域行为，按触控场景选择原生组件。本次迁移不创建空桌面或移动应用。

## 来源与验证

- HeroUI 官方要求：[React 19+ / Tailwind CSS 4](https://heroui.com/en/docs/react/getting-started/quick-start)。
- 来源及许可证随 `apps/web/licenses/` 分发。Multica 仅作公开交互参考。
- 执行类型、lint、业务测试和正式构建；通过真实浏览器检查表单、多选、弹窗锁定、焦点恢复、主题和响应式。
- 验证结果记录于 `docs/development/weora-heroui-migration-acceptance-20261021.md`。
