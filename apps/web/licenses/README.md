# 前端组件来源

正式 Web 统一使用 HeroUI v3：

- `@heroui/react` / `@heroui/styles` 3.2.6；来源为 [HeroUI 官方仓库](https://github.com/heroui-inc/heroui)，通过 npm 包安装。
- HeroUI 的 React Aria 依赖及项目直接使用的 `react-aria-components` 沿用 Adobe 的 Apache-2.0 声明。
- HeroUI 与 React Aria 许可证保存于本目录，随构建复制到 `/licenses/`。
- Markdown 展示使用 `react-markdown` 10.1.0 与 `remark-gfm` 4.0.1，均为 MIT；许可证原文保存在本目录并随构建分发。
- `src/components/console` 是 Weora 自有业务组合，只适配数据、受控提交和反馈；基础组件来自 HeroUI。
- 正式代码已移除本地 shadcn 基础组件，以及 Radix、cmdk、class-variance-authority 的直接依赖。旧 shadcn MIT 文件仅记录历史来源；独立对比原型仍自行保留其来源与许可证。
- Multica 仅参考公开任务、详情、筛选及创建交互，不导入其源码、CSS 或品牌资产。不使用 HeroUI Pro 模板。

业务请求、权限、版本冻结、仓库选择与审批规则由 Weora 自己实现。架构选择见 ADR-0013。

版本核对：HeroUI v3.2.6 随包 LICENSE 及该 tag 为 Apache-2.0，虽然 package.json 的 license 字段仍写 MIT，本项目保留实际 LICENSE 原文（heroui-LICENSE.txt），不依据元数据改写许可。固定来源：https://github.com/heroui-inc/heroui/blob/v3.2.6/LICENSE。
