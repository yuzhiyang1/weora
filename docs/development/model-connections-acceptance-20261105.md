# 多模型配置实施与验收（2026-11-05）

设置页“模型与密钥”已改为列表。支持添加/编辑多个模型和 Key、同模型的备用 Key、设为主要、逐条连接检测和移除备用项。主要项置顶并明确标示，Key 不回填。厂商与模型可搜索选择，目录来自实际锁定 SDK，避免只增加下拉选项却仍只能执行 DeepSeek。

## 用户操作

1. 点击“添加模型配置”，填写名称，选择厂商及模型，输入 Key。
2. 可勾选“保存后设为主要配置”，或保存后在列表点击“设为主要”。额度不足时可手动切换备用项。
3. 编辑留空 Key 会保留原值；换厂商必须填新 Key。移除当前主要项前需先切换；移除备用项不改写历史快照。
4. 运行预算单独保存，修改主要项不会丢失预算草稿。其他管理员改过预算时保留本地输入并返回版本冲突。

**生效范围：新任务和新主 Agent 会话。** 已有任务续接、新 Run 的重试和已有主会话继续沿用其固定版本；本轮未提供运行中热换 Key。工具权限、命令执行和人工审批继续使用原机制。若协调范围明确覆盖模型，切换厂商后需核对覆盖模型的兼容性。

支持的预设：DeepSeek、OpenAI、Anthropic、Google Gemini、Moonshot/Kimi 中国区。每条配置保存一组厂商、模型及 Key，目录上限 100 条。没有用厂商余额、免费额度或演示连通状态冒充实际检测结果。

## 后端与执行链路

- 新增模型目录服务、版本化公共 DTO、预设目录及厂商探测适配。复用既有 SQL/CAS 与加密端口，不增加技术栈或依赖。
- `GET/POST /settings/model-connections`：目录读取与新增；`PUT /settings/model-connections/{id}`：编辑；`POST .../{id}/primary|check|remove`：切换、检测和移除；`PUT /settings/model-budget`：保存预算。均在现有团队路径下，写操作校验 CSRF 与管理员角色。
- 原单模型接口保留兼容投影，新写入统一进入 `kind=models`。原设置首次写入时兼容转换，旧配置行不改写，无额外数据库迁移。
- 模型提供方随配置冻结并传入 Python/Pi。后台只取当前 Run/Session 固定版本中的选中 Key，切换、轮换和移除均不改变旧配置引用。
- 每条 Key 独立加密，公开查询不解密，轮换一条不需要解密其他项；坏 Key 不阻挡其他配置轮换。
- Pi 只注册预设厂商；没有任意 URL、插件或个人登录态接入。目录模型以当前 `pi-ai@1.3.0` 为准，保留旧 DeepSeek 别名。

实现决策见 [ADR 0019](../adr/0019-model-connection-catalog.md)。探测的单模型接口与别名行为核对了 [Claude 官方文档](https://platform.claude.com/docs/en/api/models/retrieve) 和 [Google 官方模型接口](https://aistudio.google.com/api-reference/models)。SDK 提供方注册、模型元数据及基础地址直接核对本地锁定包源码。

## 自动验证

- Web：全量 110 项通过；TypeScript、ESLint、生产构建通过。
- 后端：设置、运行执行、Worker 生命周期、协调及实际 Pi 子进程相关回归 162 项通过；最终设置模块 23 项复验通过。Ruff 与源码及新增设置测试的 Mypy 通过（174 个文件）。
- Pi：16 项通过，含真实 Agent 工具循环、持续会话、预设与 SDK 全目录一致性、未知提供方/URL 拒绝、旧模型 ID 保留；类型、Lint、构建通过。
- 覆盖多 Key、跨厂商切换、轮换、移除、历史版本、权限/CSRF、版本冲突、原单配置兼容、失效密钥轮换，以及各厂商正确的官方主机与认证头。实际构建的 Pi 子进程对五家厂商均验证未知模型在联网前拒绝。

前端仍有既有 React Aria 浮层 `act` 与 React Router 升级提示；Pi 测试有 Node MockTimers 实验性提示，均未导致失败。未把脚本响应或协议测试称为真实模型联调。

## 本地浏览器

在独立 SQLite 验收库启动 FastAPI 与 Rsbuild，未启动执行 Worker 或飞书接收器。使用明确命名的“验收”配置及无效测试 Key，未向厂商发起真实认证或生成请求。实际完成：

- 同一 DeepSeek 模型保存两条 Key，再新增 OpenAI 配置。
- 从主 Key 切换到备用 Key，再切换厂商；刷新后主要选择保留。
- 编辑名称时 Key 输入为空，保存元数据不会清除原 Key。
- 预算草稿保留到主要项切换后仍可保存，最后恢复原预算。
- 深浅色、1440×1000 和 390×844 检查；整页无横向溢出，750px 的表格在自身 287px 容器内滚动；新增弹窗使用共享 HeroUI 控件。

截图和日志位于 `output/playwright/model-connections-20261105/`，主要图为 `final-desktop-light.png`、`final-desktop-dark.png`；`layout-checks.json` 记录布局检查。`frontend-tests.log`、`frontend-build.log`、`backend-tests.log`、`backend-final-model-tests.log` 记录自动检查。

本地入口：<http://localhost:5173/settings/models>。保留服务供复核，未提交或推送 Git。部署需同时构建 Pi 运行时并更新后端和 Web，以加载本次协议中的预设提供方字段。
