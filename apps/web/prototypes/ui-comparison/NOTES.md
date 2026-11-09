# Weora UI 对比原型

问题：在相同业务场景下，独立使用 HeroUI 或 shadcn/ui，哪套视觉与交互更适合 Weora？用户尚未选择最终方案。

启动：在仓库根执行 `pnpm --dir apps/web/prototypes/ui-comparison dev`，打开 `http://localhost:5174/?variant=heroui`，可切换到 `?variant=shadcn`。单独预览入口为 `/heroui.html` 与 `/shadcn.html`，均支持 `?theme=light` 或 `?theme=dark`。

首次安装：在本目录执行 `pnpm install`。此目录是独立 pnpm workspace，锁文件和 React 19 仅服务于原型，不改正式 Web 的 React 18 与依赖。选用独立运行入口，是为了避免当前并行开发中的 AppShell/路由冲突，以及 HeroUI v3 的 React 19 要求影响正式页面。

两份页面使用相同示例任务、Agent 和项目，任务创建、筛选、审批及设置只影响内存，刷新即重置，不访问业务 API。外层比较器通过独立文档隔离 CSS；业务布局分别实现，不复用生产 `console` 的自定义控件外观。

HeroUI 采用 `@heroui/react` / `@heroui/styles` 3.2.6 的真实组件；shadcn 基础组件沿用本仓库 MIT 来源 `ba1503c64472b82256d9de95e2e62c5901a38e8b`。保留本目录 `public/licenses/` 中的来源与许可证。

官方资料：

- [HeroUI v3 快速开始](https://heroui.com/en/docs/react/getting-started/quick-start)
- [HeroUI Modal](https://heroui.com/en/docs/react/components/modal)
- [HeroUI Tabs](https://heroui.com/en/docs/react/components/tabs)
- [HeroUI Select](https://heroui.com/en/docs/react/components/select)
- [shadcn/ui Vite](https://ui.shadcn.com/docs/installation/vite)

这里比较的是两套完整的页面提案，布局差异属于设计选择，不代表任一组件库只能实现某一种布局。确认方向后再决定正式迁移；此原型不能直接视为已验证的生产实现，也不改变现有项目 Skill 或 ADR。

已验证（2026-10-21）：

- `pnpm run build` 通过 TypeScript 检查并生成三个独立 HTML 入口。
- 真实浏览器覆盖两套任务创建、审批继续、详情页签、模拟完成、搜索和设置开关；另外验证 Agent 选择与项目筛选。
- 检查 1440×1050 桌面视口与 390×844 手机视口；手机导航可展开并跳转，页面没有整体横向溢出，shadcn 表格在自身区域横向滚动。
- 比较器的 A/B 切换、主题同步、单独入口和保留主题的演示重置已检查。截图位于仓库本地 `output/playwright/weora-*.png`。

原型尚未连接登录、后端执行、真实审批与持久化；“模拟完成”仅用于观察界面状态变化。
