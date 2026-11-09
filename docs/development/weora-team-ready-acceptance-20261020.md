# 代码设置与普通研发链路验收

日期：2026-10-20。源码基线 `0fc6eda`，保留已有 UI 重构，不修改远端分支。环境为本机 `127.0.0.1:55432/weora`、Redis、真实 DeepSeek 与 Docker；没有合并、部署或业务仓库写入。

## 代码设置

- 数据库变更前确认全局任务和 Worker 空闲，完成本机数据库备份；迁移到 `20261020_0015`，`alembic check` 无额外差异。
- 真实浏览器完成创建项目、从代码设置入口选择归属项目并添加仓库、填写 Git 认证、选择构建 Profile 和命令建议、校验 Git 可读。
- API 连接用明确的无效合成令牌验证只写保存，未拿它请求外部云效；重新打开令牌不回填，随后停用该测试连接。既有真实云效身份与业务项目配置未修改。
- 桌面 1440px、窄屏 390px、浅色/深色截图均人工查看；390px 时 document scrollWidth 为 390，无页面横向溢出。
- 修复真实浏览器表单 `pattern` 在现代正则 `/v` 模式下的无效字符转义，以及代码设置页多余的“详情”面包屑。
- Codex 内嵌浏览器连接因 native pipe bridge 不可用未能接管，改用独立 Chrome 验收；不宣称内嵌浏览器已验证。

截图（本机忽略目录，不包含凭据）：

- [桌面浅色](../../output/playwright/weora-code-settings-20261020/code-desktop-final.png)
- [窄屏浅色](../../output/playwright/weora-code-settings-20261020/code-mobile.png)
- [桌面深色](../../output/playwright/weora-code-settings-20261020/code-desktop-dark.png)
- [窄屏深色](../../output/playwright/weora-code-settings-20261020/code-mobile-dark.png)

## 普通研发 Agent

项目：`763d5666-df01-59eb-a6d9-002ac893d8d3`，仓库 `0a841e9c-a52e-5449-b88d-4b102f58f7f8`，仅指向本机隔离 HTTPS Git 服务 `ordinary.git`。

Agent：`f25027d6-a8d8-56d2-ab19-98b3f19a810c / 0.1.0`（团队研发助手）。系统提示只描述研发流程和治理边界，没有预设仓库文件名、精确命令或修复答案。用户任务是“订单刚好 50 元仍收 6 元，规则是满 50 元免运费”，由模型自主读取 README、源码与测试并提案。

首次运行 `f1112cd6-6ff2-5b14-9edc-1b77a357fd63` 只读定位成功，但 `plan.propose` 缺少 `actions` 枚举，模型猜测 Tool 名称导致 `INVALID_SCOPE`。没有审批、写文件或执行命令；虽然文本运行状态为 SUCCEEDED，不计作修复完成。新增 schema 回归先复现缺少枚举，再从领域常量导出权限名及映射说明，没有增加动作权限。

修复后运行：46e98a54-e1b0-5efa-a1a2-08d8c9018803（链接已脱敏）。从真实 Console 发起并在页面审阅、勾选确认、记录审批；最终状态 SUCCEEDED。

| 顺序 | 实际证据 | 结果 |
| --- | --- | --- |
| 读码与提案 | 3 次 `repo.file.read`；基准 `example-commit` | 自主识别 `> 5000` 的边界缺陷 |
| 审批 | 仅 `shipping.py`、`tests/test_shipping.py`；动作 `file.write / command.run / git.diff` | 未授权任何远端操作 |
| 先补测试 | `ec2be8e5-f730-575e-9d78-55b45cffb8b7` | 新增 `shipping_fee(5000) == 0` |
| 失败复现 | `07601d17-7721-5f09-b30f-c08fe344069c` | 同一 unittest argv；退出码 1，`600 != 0` |
| 修复源码 | `ac826661-02e8-5095-b6fc-fb886ee5eef1` | `>` 改为 `>=` |
| 回归通过 | `316c3cdf-c08c-5463-ab05-7b5ac752c3ce` | 同一 argv；退出码 0，3 项全部通过 |
| Diff | `8fbd7b19-00e2-5aaa-a199-005481b70a35` | 仅两条批准路径，测试和 Diff 的仓库摘要一致 |

精确 argv：`["python3","-m","unittest","discover","-s","tests","-v"]`。

最终仓库摘要：`bbfa3c07821d2cb4562a0ab1885e3b64f300322850e102162ce0f1d0a5eaf4f3`。完成验证再次保存 Diff，因此有两条相同的 Diff 证据，不能误称执行了两轮修复。交付列表为空；隔离 bare repo 仍只有 main 且保持原始 revision，确认未提交、推送或建 MR。

质量限制：模型额外添加了非必要的 unittest `__main__` 入口，并在最终报告承认不够最小。未越过批准路径，但不能把这次验证提升为“严格最小补丁质量已验证”。没有事后手动改写补丁或模型证据。

前端工具页现已同时显示调用状态和“命令未通过 · 退出码 1 / 命令通过 · 退出码 0”，展开真实参数、结果以及 Diff 预览均已实测。[工具调用截图](../../output/playwright/weora-code-settings-20261020/ordinary-tools.png)。

## 飞书自动入口：未通过

真实消息 `om_example`，18:20:15 发出；历史读取确认同群、真人、文本、@ 当前机器人、发起人已绑定。供应商事件及 Inbox 均没有这条消息，未人工补收或构造重放。

18:48 的当前实例仍有连接成功状态、近期 PONG、0 业务帧。界面已能区分过期进程心跳、当前网络连接和入站记录；输入该消息 ID 只报告“最近本地记录未找到”，不推断供应商投递原因。没有修改原应用订阅或密钥，没有创建新应用，没有要求用户继续重复发消息。自动入口不能据此记为通过。

## 自动检查与安全

- 前端最终全量：83/83；typecheck、ESLint、生产 build 通过。
- 后端代码源、接收、MR 映射等相关组 66 项通过；计划 schema 与交付服务组 31 项通过。单独重跑代码源及新缺依赖失败关闭用例通过，不将重跑计数累加冒充覆盖数。
- Ruff 通过，mypy 覆盖 134 个源文件无错误；真实 PostgreSQL Alembic 元数据检查通过。官方 SDK 测试存在上游 protobuf UTC 弃用警告，不影响通过结果。
- 当前修改候选源码执行 Gitleaks 脱敏扫描未发现泄漏；`.env`、本机账号文件及验收产物仍由 Git 忽略。没有把磁盘中的真实令牌自动导入新配置，也没有将令牌交给模型。
- 本机原始 API 证据、日志和截图位于忽略的 `output/playwright/weora-code-settings-20261020/`；不是生产级原始构建日志保留能力。数据库备份保留用于恢复；验收浏览器临时登录态文件完成后清理。
- 本轮未 `git add`、commit 或 push；用户原有 UI 重构提交保持不变。
