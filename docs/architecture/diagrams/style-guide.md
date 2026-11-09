# Weora 图表配色与字体

- 状态：配色、离线字体方案与整体观感已通过用户确认
- 日期：2026-10-17
- 来源：`apps/web/src/styles/global.css` 的现有 Weora 设计令牌
- Font source：`system`，无网络请求

此文件是 Weora 绘图 Profile 的仓库内可读摘要。完整 Profile 保存于 `%USERPROFILE%/.diagram-design/profiles/weora.md`，项目根目录的 `.diagram-design` 通过 `profile: weora` 选择它。后续 Weora 架构图默认沿用本方案，采用上游无阴影、正交连线、少量焦点与独立图例的绘图规则；不修改共享安装目录或其他项目的风格。

## 语义颜色

| Role | 使用值 | 来源与用途 |
| --- | --- | --- |
| paper | `#f7f8fa` | 现有 `--weora-canvas`，画布与遮罩 |
| paper-2 | `#eef0f3` | 派生输入背景，不单独承担信息含义 |
| ink | `#1c1f23` | 现有 `--weora-ink`，正文与主边框 |
| ink-strong | `#111111` | 预留高对比文本，不用于额外强调 |
| muted | `#646a73` | 现有 `--weora-muted`，次级文字与默认连线 |
| soft | `#646a73` | 小标签，保持与次级文字一致的可读性 |
| rule | `#e5e6eb` | 现有 `--weora-line`，装饰分隔线 |
| rule-solid | `#d4d7dd` | 图例分隔线，不承担关系语义 |
| accent | `#005fd7` | 现有 `--weora-accent-dark`，小字与线条使用较深蓝以满足对比要求 |
| accent-tint | `#e8f3ff` | 现有 `--weora-accent-soft`，焦点背景 |
| link | `#005fd7` | 外部读取关系，除颜色外同时标注方向与文字 |

每图最多 2 个焦点组件。平台、Agent、Tool 和人类通过 P/A/T/H 标签区分，不用四种颜色编码角色。

## Typography

| Role | 字体栈 | 大小与说明 |
| --- | --- | --- |
| title | Instrument Serif、Noto Serif、Songti SC、SimSun、serif | HTML 标题 28px；离线时使用已安装的衬线字体 |
| node-name | Geist、Aptos、Segoe UI Variable、PingFang SC、Microsoft YaHei、sans-serif | 节点名称 12px/600，中文不缩小至 12px 以下 |
| sublabel | Geist Mono、Consolas、Liberation Mono、monospace | 技术标识 9px；中文解释另用 12px 无衬线字体 |
| eyebrow | 同 sublabel | 图中 P/A/T/H 等技术类型标签 9px |
| arrow-label | 同 node-name | 中文关系说明 12px，遮罩与连线保持间隔 |
| callout | 同 title | 本组不使用独立书法式或斜体标注 |

不加载或安装上述字体。优先使用机器已存在的字体，缺失时按栈回退；不将字体声明当成实际已加载的证明。SVG 内明确写出颜色与字体栈，避免部分离线阅读器不解析 CSS 变量而丢失样式。

## 几何与布局

总览默认画布 1280×720，主要坐标与尺寸按 4px 网格对齐。节点圆角 6px，转折半径 8px；默认线宽 1.2px。图例位于底部，关系标签有独立背景，组件遮罩在连线之后绘制。HTML 使用局部横向滚动，SVG 最小宽度等于 viewBox 宽度；打印时释放最小宽度限制。其他图型可按内容调整画布，复杂内容优先拆图，不缩小中文文字硬塞。

## Profile 复用与迁移

- 名称：`Weora`；slug：`weora`；确认日期：2026-10-17。
- 每次绘图先读取项目根目录 `.diagram-design`，再直接读取用户目录中的同名完整 Profile，不覆盖 diagram-design 安装目录的 `references/style-guide.md`。
- 新机器需复制完整 `weora.md` 到该机器的用户 Profile 库；仓库内的项目标记不会自动同步用户目录。Profile 缺失时应明确提示，不得静默退回上游橙色默认主题。
- `default.md` 是上游固定版本的原始恢复快照，不是 Weora 的备用配色，不覆盖或重写它。
- 变更风格时同步维护完整 Profile 与本摘要，重新验证对比度和渲染结果；未明确要求时不启用深色、终端或其他配色。
