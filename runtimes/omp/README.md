# Oh My Pi Worker

首版固定 OMP Agent Core 18.16.0，通过 Weora JSONL 接入。运行需要 Bun 1.4.4 和主要 DeepSeek 模型连接。工具由 Weora 网关执行。OMP CLI 的个人配置、原生文件和 Shell 工具、LSP 以及自动发现均不加载。

先在仓库根目录安装 Pi 协议依赖，再安装 OMP 依赖。

```powershell
pnpm install --frozen-lockfile
bun install --cwd runtimes/omp --frozen-lockfile --ignore-scripts
```

在 API 和 Worker 的部署环境配置 `WEORA_OMP_BUN_EXECUTABLE`（建议绝对路径）和 `WEORA_OMP_RUNTIME_ENTRY`（本目录下 `src/index.ts` 的绝对路径）。两端都需要能读取该入口和依赖。团队管理员可在「设置 → Agent 执行内核」为新 Worker 运行选择 Oh My Pi。切回 Pi 会影响后续新运行，已创建的运行和续跑继续使用快照中的内核。

使用本机指定的 Bun 执行审批、取消和恢复验收。

```powershell
cd backend
$env:WEORA_TEST_OMP_BUN = "C:/path/to/bun.exe"
uv run pytest tests/infrastructure/test_omp_runtime.py
```

验收使用真实 OMP 进程和执行循环，模型响应由固定脚本提供。
