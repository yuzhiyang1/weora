# HeroUI 正式前端迁移验收（2026-10-21）

用户在对比预览后明确选择完全采用 HeroUI，并要求同步 UI Skill。本次在 Weora 当前分支工作区实施，没有修改 Plane，也没有提交或推送。

## 完成内容

- 正式 Web 使用 HeroUI 3.2.6，React / React DOM 19.3.1，配套 React 19 类型。
- 按钮、输入组、文本域、可搜索单选/多选、复选、弹窗、卡片、状态、加载、进度、页签、数据表格、工作空间浮层与快速查找均使用 HeroUI。
- 移除 13 个本地 shadcn 基础组件及 Radix、cmdk、class-variance-authority、tw-animate-css 的直接依赖。对比原型仍独立保存，不进入正式构建。
- 共享主题映射到 HeroUI 原生颜色、表面和圆角；去除旧控件皮肤。表单使用官方 secondary 变体，保证空字段在深色表面上也能辨认。
- 保留现有业务 API、权限、私有任务、固定版本、提交锁定、错误恢复及持久化外观选择。首次访问默认深色。
- 更新 AGENTS、项目 weora-ui Skill、设计契约、ADR-0013、许可来源说明与五张正式页面参考截图。

## 自动验证

- `pnpm --filter @weora/web typecheck`：通过。
- `pnpm --filter @weora/web lint`：通过。
- `pnpm --filter @weora/web test`：87 / 87 通过，包含当前工作区已有的新项目配置与渠道测试。
- `pnpm --filter @weora/web build`：通过，许可证随 dist/licenses 分发。
- Skill 的 `quick_validate.py`：通过（Windows 使用 Python UTF-8 模式）。

测试覆盖密码与 ref、重复提交、弹窗 Escape 和焦点恢复、忙碌关闭锁定、搜索多选保留、审批显式确认、项目切换清仓、幂等重试、权限与正文渲染等行为。角色断言随 HeroUI 的真实语义调整，没有通过伪造旧 aria 属性适配测试。

## 真实浏览器

使用独立本地工作空间“HeroUI 界面验收”，通过正式页面注册、创建 Agent、保存草稿、发布 1.0.0、选择固定 Agent 与版本、搜索选项并提交短文本任务。

真实运行 `31f619e8-7a95-52cc-a479-0e70798e5b1e` 已成功，返回“HeroUI 界面验收通过。”，事实记录包含 queued / started / succeeded。只验证了一次无工具短文本运行，不把它视为全部后端或代码交付能力的验收。

桌面 1440×1000 与窄屏 390×844 检查了工作台、Agent、运行详情、飞书上下文与代码设置。主题切换后刷新保持浅色；窄屏导航展开、Shift+Tab 焦点限制、Escape 关闭与焦点恢复通过，页面没有整体横向溢出。Ctrl+K 查找并跳转到飞书上下文通过。

浏览器发现并修正了旧页签 flex-wrap 导致逐项换行、状态 Badge 脱离容器、输入组未使用对应 Input 子组件等适配问题。登录前 auth/me 的 401 是预期未登录检查，认证后的检查没有新增 JavaScript 错误。

截图位于 `output/playwright/heroui-migration/`；五张基线已同步到 `.agents/skills/weora-ui/references/screenshots/`。

## 范围与后续

本次未更改后端接口，保留工作区已有的项目配置、渠道与执行层并行改动。此次没有创建 Git 提交或推送。对比原型与历史 ADR 只用于追溯；后续正式 UI 开发以 HeroUI Skill 与 ADR-0013 为准。

HeroUI v3.2.6 的 npm 元数据写 MIT，但其随包及固定 tag 的 LICENSE 为 Apache-2.0；保留实际许可原文于 heroui-LICENSE.txt，并记录来源，未据元数据改写许可证。
