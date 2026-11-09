# 本地开发

Weora 采用单仓库结构，后端和客户端可以分别启动、测试和构建。日常开发不要求先安装 Docker。

客户端采用 pnpm workspace：

```text
apps/web/       # 当前 Web 控制台
apps/desktop/   # 未来桌面端
apps/mobile/    # 未来移动端
packages/       # 经真实需求验证的跨端共享包
```

## 环境要求

- Python 3.12
- uv 0.7 或更高版本
- Node.js 20 或更高版本
- pnpm 10
- 若启用 Pi 执行面，Node.js 需 >=22.19.0；配置和 Worker/Beat 启动见 [Pi 运行手册](pi-runtime-w01.md)。
- 可选：Docker 与 Docker Compose

## 启动后端

```powershell
cd backend
Copy-Item .env.example .env
uv sync
uv run uvicorn weora.main:app --reload --host 127.0.0.1 --port 8000
```

启动后可以访问：

- OpenAPI：<http://localhost:8000/docs>
- 存活检查：<http://localhost:8000/api/v1/health/live>
- 就绪检查：<http://localhost:8000/api/v1/health/ready>

首次启动或模型变更后执行数据库迁移：

```powershell
uv run alembic upgrade head
```

Weora 默认校验密码。仅在本地开发需要临时绕过时，可以设置 `WEORA_DEVELOPMENT_AUTH_BYPASS_ENABLED=true`，让已有账户使用任意非空密码登录。该开关只在 `WEORA_ENVIRONMENT=development` 时生效，测试和生产环境始终执行正式密码校验。

## 阿里云 OSS 联调

对象存储默认是 `disabled`，不会静默回退到本地目录。复制
[`backend/.env.example`](../../backend/.env.example) 后，填写以下配置：

```dotenv
WEORA_OBJECT_STORAGE_PROVIDER=aliyun_oss
WEORA_OBJECT_STORAGE_BUCKET=weora-test
WEORA_OBJECT_STORAGE_REGION=cn-hangzhou
WEORA_OBJECT_STORAGE_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
WEORA_OBJECT_STORAGE_PREFIX=weora/development
WEORA_OBJECT_STORAGE_USE_CNAME=false
WEORA_OBJECT_STORAGE_ACCESS_KEY_ID=
WEORA_OBJECT_STORAGE_ACCESS_KEY_SECRET=
WEORA_OBJECT_STORAGE_SECURITY_TOKEN=
```

凭据必须来自最小权限 RAM 用户或 STS，不要使用主账号 AccessKey。若不填写 `WEORA_*`
凭据，SDK 会尝试读取 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` 和可选的
`OSS_SESSION_TOKEN`。

在 `backend` 目录执行：

```powershell
$env:WEORA_RUN_ALIYUN_OSS_INTEGRATION = "1"
uv run pytest tests/integration/test_aliyun_oss_integration.py -q
Remove-Item Env:WEORA_RUN_ALIYUN_OSS_INTEGRATION
```

测试会在配置的前缀下创建唯一临时对象，验证上传、Head、流式下载、复制和幂等删除，并在结束时清理。
测试开关必须在执行命令的进程环境中临时设置；没有把
`WEORA_RUN_ALIYUN_OSS_INTEGRATION` 设为 `1` 时，该测试会明确跳过。不要把开关长期写入 `.env`，
避免日常执行完整测试时意外访问真实 OSS。

## 启动 Web 控制台

打开另一个终端：

```powershell
Copy-Item apps/web/.env.example apps/web/.env
pnpm install
pnpm dev:web
```

访问 <http://localhost:5173>。开发服务器会把 `/api` 请求代理到本地后端。

## 运行质量检查

后端：

```powershell
cd backend
uv run pytest
uv run ruff check src tests
uv run mypy
```

所有客户端：

```powershell
pnpm test
pnpm typecheck
pnpm lint
pnpm build
```

## 容器化启动

安装 Docker 后，可在仓库根目录执行：

先在当前终端设置 `WEORA_POSTGRES_PASSWORD` 环境变量。该值只用于本地 PostgreSQL，不要写入仓库。

```powershell
docker compose up --build
```

容器入口为：

- 产品界面：<http://localhost:3000>
- 后端接口：<http://localhost:8000>

PostgreSQL 保存用户、工作空间、成员关系和认证会话等权威状态。Redis 已进入开发拓扑，
现由独立 Celery Worker/Beat 用于运行通知，不作为权威业务状态来源。
当前 Compose 不自动启动 Pi、Worker、Beat、飞书 Receiver 或隔离沙箱；第一版完整启动与调试见[运行手册](weora-v0.1-operations.md)。
