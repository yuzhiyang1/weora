# W02 真实只读工具链路验收

- 日期：2026-10-19。
- 结论：Console → Pi → DeepSeek → Tool Gateway → 文件读取 → 模型续推理 → 持久化与页面展示已通过；这不是容器隔离或整版发布验收。
- 本轮未使用飞书凭据，没有调用飞书、写业务仓库、创建 PR 或进行 Git 提交。

## 1. 环境与配置

复用本机独立 PostgreSQL `127.0.0.1:55432/weora`，迁移 head 为 `20261017_0011`；Redis `6379`、Celery Worker/Beat、API `8000`、Web `5173`。未更改原系统 PostgreSQL 实例。

运行使用 Pi Agent Core 1.3.0、`deepseek / deepseek-flash`。每次模型响应的输出上限从 128 调整为 256 tokens，供应商自动重试保持关闭；不是取消运行预算。本轮仅从页面新建两项模型任务，不因刷新重发模型请求。未记录供应商账单金额或精确 HTTP 请求次数，不能据此声称实际费用。

本机忽略的 `backend/.env` 增加：

```dotenv
WEORA_READONLY_REPOSITORY_PATH=D:/projects/weora/volumes/w02-readonly-repository
WEORA_READONLY_REPOSITORY_WORKSPACE_ID=9241cacd-7363-5d92-a481-7f7f7b867d4d
```

该目录仅有不含凭据的 README 测试资料；没有把整个 Weora 工作树开放给模型。路径、工作空间与调用主体由平台注入，模型只提交相对文件名。测试文件中的验收标识为 `W02-README-7F3A9C`，只放在文件中，没有放进 Agent 系统提示词或任务正文。

本轮没有构建 Git 远端或执行 Git 操作；此目录是 W02 文件读取夹具，不冒充 W08 的完整 Git 工作区。

## 2. 正向真实链路

- 工作空间：`9241cacd-7363-5d92-a481-7f7f7b867d4d`。
- Agent：`845b70ce-262d-5bcb-abfc-289ab6e34e64`，名称 `W02 Readonly Repository Agent`，固定发布版本 `0.1.0`。原无工具验收 Agent 未改动。
- Run：`0aa5b3ad-ab9e-5a8a-82ce-d75f85b8206a`。
- 页面任务正文：`请调用 repo.file.read 读取 README.md，只回答文件中的验收标识和项目用途。不要猜测。`
- 调用 ID：`call_00_hIyz3J63nuhPVheCrYZm5008`。
- 平台工具名：`repo.file.read`；参数：`{"path":"README.md"}`；结果 `ok=true`、状态 `SUCCEEDED`。
- 回答准确包含验收标识和 README 中的项目用途。

独立 Chrome 完成真实登录、选择 Agent 与固定版本、提交任务、等待回答、展开参数与事实结果。桌面宽度 1440、窄屏宽度 390 均无页面横向溢出。刷新页面以及重启 API/Worker 后仍能查询同一回答和调用记录。

数据库只读复核确认：Run 为 `SUCCEEDED`，事件序号为 3，fencing token 为 1；仅一条 ToolInvocation，预算 `used=1`。数据库、工具 API 与页面事实一致。

## 3. 拒绝链路与修复

检查发现旧实现仅按原始大小写拦截敏感文件名。Windows 上 `.ENV`、`.env ` 可读到同一份 `.env`。复现只使用 pytest 临时目录中的 `DUMMY_NOT_A_SECRET=fixture`，没有读取真实密钥文件。

修复位于 `backend/src/weora/modules/tools/infrastructure/repository.py`：

- 在打开文件前拒绝尾随空格、句点的路径分量。
- 敏感文件与 `.git` 拦截使用大小写折叠，各执行平台统一保守策略。
- 保留相对路径约束、链接/重解析点检查、已打开文件句柄验证、UTF-8 与 64 KiB 上限。

9 个文件名别名回归在修复前全部未满足拒绝契约，其中 `.ENV` 和尾随空格确实返回假凭据正文；修复后通过。补充不存在文件、非 UTF-8、超大文件及 Gateway 将拒绝持久化为 FAILED 的测试。

重启 API/Worker 加载修复后，从页面提交第二个真实 Run：`7e5ace2b-621a-582b-845c-c0fdb5904d2f`。

| 模型实际请求 | 平台事实 | 调用状态 |
| --- | --- | --- |
| `.ENV` | `REPOSITORY_PATH_DENIED`，无正文 | `FAILED` |
| `../outside.md` | `REPOSITORY_PATH_DENIED`，无正文 | `FAILED` |

模型如实回答两次拒绝，没有重试；数据库确认两条调用、预算 `used=2`。页面以红色“调用失败”展示各自错误，参数和事实结果均可展开。Run 为 `SUCCEEDED` 只表示“完成了报告拒绝结果的任务”，不代表文件读取成功。

## 4. 自动回归

- 工具 Gateway、Pi 子进程协议与运行生命周期的受影响 40 项测试通过。
- Web 全量 41 项测试通过，其中新增 3 项工具面板回归：成功参数/结果展示与纯文本防注入、路径拒绝、未知结果不伪造成功或自动重试。
- Web TypeScript、ESLint 与生产构建通过。
- 后端 Ruff、Mypy（157 个源/测试文件）通过。
- 后端全量非外部测试：262 passed、4 deselected，125.41 秒。4 项外部集成测试本轮未执行，不计为通过；其中已有 PostgreSQL 约束回归保留上一轮证据。本轮另有上文真实 PostgreSQL/Redis/模型/浏览器链路的现场复核，不将 Fake 测试称为真实模型验收。

可复验命令（backend 目录，使用新的临时目录避免本机历史 ACL 问题）：

```powershell
$taskTestBase = Join-Path ([IO.Path]::GetTempPath()) ('weora-w02-' + [guid]::NewGuid().ToString('N'))
.venv-v01/Scripts/python.exe -X utf8 -m pytest -m 'not integration' --basetemp "$taskTestBase" -o addopts='' -q
.venv-v01/Scripts/ruff.exe check src tests
.venv-v01/Scripts/python.exe -X utf8 -m mypy src tests
```

## 5. 截图与限制

本机截图位于忽略目录 `output/playwright/weora-w02-20261019/`：

- `04-readme-answer-and-tool-desktop.png`：持久回答与展开后的工具事实。
- `02-readme-tool-mobile-before.png`：窄屏成功结果；文件名中的 before 指修复路径规则前的捕获，不表示有前端样式变更。
- `03-denied-tools-mobile.png`：拒绝结果和错误代码。

内嵌浏览器桥仍不可用，未绕过其限制；本轮操作的是独立隔离 Chrome，不是用户当前标签页。未登录时 `/auth/me` 返回 401、未启用上下文时两个上下文接口返回 503 属于明确的未配置状态，页面显示说明，没有把它们计为工具链路失败。

**部署安全项仍未完成**：本机工具采用只读打开文件和平台路径校验，但没有验证 OS 级容器只读挂载。当前没有 Docker；不会以普通宿主目录或文件只读属性冒充容器隔离。W02 计划中的“只读挂载与部署级链接逃逸验收”保留待办，不应在接入真实敏感仓库前跳过。

下一步仍为 W03 飞书连接与单群授权。本轮提供的测试应用信息未复制到模型输入、测试仓库或验收文档，后续按 W03 的凭据托管流程配置，不扩大第一版范围。
