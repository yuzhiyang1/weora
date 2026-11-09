# 项目与多仓库配置：实现和真实验收

- 日期：2026-10-20。
- 范围：[ADR 0006](../adr/0006-projects-and-selected-repository-context.md)。本轮实现独立 Weora 功能，不接入 Plane。

## 使用入口

登录 Console 后进入「项目」，创建项目、填写业务说明/架构/研发约定，在详情添加多个仓库。Git 地址支持 HTTPS 和 SSH；托管平台可登记通用 Git、云效、GitLab 或 GitHub。令牌可选、只写保存。

进入「运行 → 发起任务」，选择项目及本次涉及的多个仓库，再选择已发布 Agent 版本并提交。未选择项目仍可普通咨询；已选择项目但没有选仓库不能提交。运行详情展示「本次项目上下文」和固定资料版本。

界面明确区分“仓库已登记”与“代码已经准备”：本轮是配置及资料上下文能力，尚不提供这些动态仓库的自动克隆、读取代码或跨仓执行。

## 数据库

先确认操作对象为本机 `127.0.0.1:55432/weora`，不是共享/生产数据库。迁移前原版本 `20261019_0012`；备份后升级至 `20261020_0013`。

- 备份：`volumes/postgres-local/backups/weora-before-0013-20261020-101454.dump`。
- 大小：164916 字节。
- SHA-256：`652b43d25ef92a5393268333b81bbef21ec1337d3454f9cc3a9ba126cd06eac2`。
- 新表：`projects`、`project_repositories`；新增 `collaboration_tasks.project_context`，历史任务保持空值。
- 独立临时库完成 head 升级、回退、再升级及 ORM 元数据一致性验证。真实库未执行破坏性降级。回滚有新任务后应先备份并停写，再评估应用与数据兼容，不能直接丢弃资料。

## 真实浏览器与模型证据

内置浏览器桥不可用，因此采用独立 Chrome/Playwright CLI，不声称验收了用户原有内置标签页。

本机测试账号：Weora Smoke；项目：**项目多仓验收-20261020**，ID `4f689931-a77b-57d9-9c48-78e3a39fdf2d`。

从页面创建三个标记仓库：订单前端、订单后端、未选审计仓库。地址均为 `git.example.test` 示例域，没有进行网络连接。使用明确的无效测试令牌验证密文存储，未使用真实仓库凭据。

真实 Console 提交 Run **`ea1cf434-aa12-57de-8eec-de6aaec8b9c5`**，仅选择前端与后端。经 PostgreSQL → Redis/Celery → Pi → DeepSeek，状态为 `SUCCEEDED`，模型配置快照为 `deepseek / deepseek-flash`。

回答正确包含 `PROJECT-CONTEXT-20`、订单前端职责和 React/TypeScript/Semi、订单后端职责和 Python/FastAPI/PostgreSQL，并明确“未读取任何代码”。未选仓库的独立标识 `EXCLUDED-REPO-20` 没有进入任务/API/模型材料或回答；测试令牌也不在响应或模型材料内。本 Run 无工具调用，不将模型建议当作执行证据。

数据库查回确认令牌为可认证加密串。页面修改项目后，原 Run 仍为 v4，后续标识 `EDIT-AFTER-RUN-20` 不在旧快照或 Runtime 材料中。当前配置版本会随停用/恢复/归档推进，不回写原任务。

原任务输出 SHA-256：`9f62e2ce5a2a50c72d2bf24446a0c3144e0d0f019d3e614003606e29d5cd5885`。

收紧模型返回前的项目鉴权后，重启 Worker，再次经真实队列提交 Run **`d2fe8817-1741-5d32-9899-0d789a5dccfa`**，冻结 v9。状态同为 `SUCCEEDED`，回答包含业务标识、两个选中仓库并明确代码未读取，工具调用数为 0。对应 `final-run.png` 与 `final-run.json`，不是复用旧进程或 Mock 的结果。

## 权限与页面验收

- 停用本次选中的前端仓库，API 返回 404，已打开的任务页面持续复验后清除正文，显示“访问权限已失效”。恢复后 API 返回 200、原快照可读。
- 页面归档项目后，“添加仓库”消失、仓库编辑禁用，并显示关联任务访问阻断；恢复项目正常。
- 390px 窄屏无整页横向溢出；1440px 桌面与已有 Agent 页面比较，复用相同 Semi、公共表单、卡片、按钮和导航样式。
- 验收中修复项目列表/空详情查询键冲突、重复 Git 地址自动 flush 导致的 500、编辑详情对象夹带只读字段、中屏导航逐字折行和资料卡多余最小高度。没有屏蔽原有 Semi `findDOMNode` 告警。

证据位于忽略目录 `output/playwright/weora-projects-20261020/`：`project-final.png`、`project-mobile-final.png`、`reference-agents.png`、`task-selection.png`、`task-result.png`、`revoked-run.png`、`evidence.json`。辅助认证文件只临时使用，不作为可提交的证据。

## 自动检查

本轮区分自动测试、真实数据库、真实模型和真实浏览器，不把 Mock 等同真实 Git 联调：

- 项目测试覆盖：选择集合/跨项目/重复 ID、快照冻结/幂等冲突、CSRF、跨工作空间、同团队他人只读、普通成员自助创建、归档/停用、令牌保留/清除/脱敏、URL 校验、默认执行仓库防回退、模型期间归档后的成功提交拒绝。
- 运行/执行/恢复/工具/Worker 生命周期受影响回归：56 项通过；后续 Worker 增强另重跑生命周期及新增撤权场景。
- Web 全部：58 项通过（10 个测试文件）；TypeScript、ESLint、生产构建通过。
- 后端项目测试 17 项、Worker 生命周期 2 项、迁移 2 项均通过；Ruff、Mypy（130 个源及项目测试文件）通过。真实 PostgreSQL `alembic check` 返回 `No new upgrade operations detected`。

Windows 默认 pytest 临时目录存在历史 ACL 问题，本轮使用唯一 `output/pytest-projects-*` 目录；没有删除或放宽原临时目录权限。

## 仍未验收

真实云效/GitLab 凭据、仓库连通性、克隆/固定 revision、多仓隔离物化、跨仓改码与逐仓授权、远端提交/推送/MR，均未在本轮验证或开放。新项目任务会拒绝借用旧默认测试仓库，不将这部分标为已完成。
