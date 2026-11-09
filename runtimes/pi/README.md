# Weora Pi 受控执行适配器

这是执行面 Node 子进程，不是 Web 前端或通用 CLI。精确固定 Pi Agent Core 与 pi-ai 为 1.3.0，只使用明确配置的 DeepSeek 提供方。无工具文本与平台受控工具循环共用同一入口；不加载个人会话、插件、文件或 Shell 工具。

Python Worker 通过 stdin/stdout 使用 `weora.runtime.v1` JSONL 协议：`run` 可带平台注册的 `tools`，子进程发 `tool_call`，父进程鉴权和持久化后回 `tool_result`，最终返回 `completed` 或 `failed`。模型不能指定 Actor、租约、连接凭据或执行角色。总调用最多24次，不自动重放未知写操作。

密钥不作为命令行参数，SDK 原始错误不输出；不应手工在终端输入包含密钥的 JSON。Windows会终止本适配器创建的进程树；Node进程本身不是操作系统沙箱，真正的改码/命令/浏览器运行在独立Docker适配器中。

要求 Node.js >=22.19.0。在仓库根目录执行：

```powershell
pnpm --filter @weora/pi-runtime build
pnpm --filter @weora/pi-runtime typecheck
pnpm --filter @weora/pi-runtime test
```

测试使用真实 Pi Agent 循环和脚本化提供方，不调用真实 DeepSeek。完整配置、恢复和外部调试顺序见[第一版运行手册](../../docs/development/weora-v0.1-operations.md)，Pi固定版本依据见[首轮手册](../../docs/development/pi-runtime-w01.md)。
