# Weora CLI

面向人和 Coding Agent 的独立平台客户端，采用 Python 3.12，不导入后端业务模块。

当前支持本地 Agent 协作。会话目录按工作空间和显式授权查询，同一套命令支持直聊和任务内联调。

## 从源码运行

在仓库根目录执行，需要已安装 uv：

```powershell
uv run --project apps/cli weora --help
uv run --project apps/cli weora version
uv run --project apps/cli weora commands
uv run --project apps/cli weora schema
uv run --project apps/cli weora schema version
```

如果需要在本机终端直接使用 `weora`，可显式安装本地包：

```powershell
uv tool install --python 3.12 ./apps/cli
```

当前尚未发布到包仓库；开发验证使用 `uv run` 即可，不要求全局安装。

## 让两个本地会话对话

1. 在 Weora 的「设置 → 本地 Agent」点「配对设备」，在开发电脑上运行页面给出的命令，按提示输入一次性配对码。设备凭据由系统钥匙串保存。
2. 安装使用中的客户端 hook，然后打开或恢复要接入的原生会话。Codex 会提示审核并信任新增的 hook。

   ```powershell
   weora collab install-hook --runtime codex --auto
   weora collab install-hook --runtime claude-code --auto
   weora collab connect
   ```

   每台设备只需安装它用到的客户端。省略 `--auto` 时，下一次用户发问时 hook 会提示领取。其他运行时可通过 `weora collab register --runtime <标识> --native-id <原生ID> --name <会话名>` 接入，并主动领取。
3. 收件方在「我的原生会话 → 直接联系权限」选择可以向这条会话发消息的成员。发件方在自己的原会话里说「用 Weora 找某位同事的会话，问它一个问题」。在协作任务里，可从任务页复制加入指令，再把 `--task <任务ID>` 加到查找和发送命令。

```powershell
weora collab sessions "林晨 订单"
weora collab send --session <自己的会话ID> --to <对方的会话ID> --body "首屏如何传游标？" --key <本次请求ID>
weora collab claim --session <自己的会话ID> --message <消息ID>
weora collab reply --session <自己的会话ID> --message <原消息ID> --body "首屏省略游标。"
```

响应里有 Weora 会话 ID 和消息 ID。重试同一次发送时复用 `--key`，回复用原问题的 `--message`。回复和单向通知领取后用 `ack` 确认。可以用 `status` 查看平台投递状态，用 `cancel` 结束待领取或待确认的已发请求。

Codex 自动接收依赖支持 `queue` 的真实可执行文件及正确的配置目录。可用 `WEORA_CODEX_BIN` 指定二进制，用 `WEORA_CODEX_APP_SERVER` 指定同一台电脑上的 App Server。多个 Codex 或 Claude Code 配置目录分别启动连接器，沿用各自的 `CODEX_HOME` 或 `CLAUDE_CONFIG_DIR`。默认将提示排到当前轮次之后；连接器加 `--resume-codex` 后，在客户端支持时尝试恢复尚未加载的会话。Claude Code 的自动接收需要该原生会话提供本机 inbox。

## 接入 OpenCode、Pi、Cursor、Kimi Code 和 DeepSeek Harness

每台电脑先配对设备。收件会话登记后，在 Weora 控制台给发件人开启该会话的直接联系权限。原生客户端必须能从 PATH 运行 `weora`。OpenCode 和 Pi 也可以在启动客户端前通过 `WEORA_CLI_EXECUTABLE` 指定本机 CLI 的绝对路径。

| 客户端 | 接入方式 | 空闲时如何收件 |
| --- | --- | --- |
| OpenCode | `weora collab install-hook --runtime opencode --auto`，重启 OpenCode 服务后打开目标会话 | 插件按原生会话 ID 投递。插件需在目标项目的服务进程中加载。 |
| Pi | `weora collab install-hook --runtime pi --auto`，重新启动或重载扩展 | 扩展向正在打开的会话提交提示。会话忙时等当前轮完成。支持扩展注册 MCP 的 Pi 会在该会话内提供 Weora 收件和回复工具。 |
| Cursor | `weora collab install-hook --runtime cursor`，重新打开 Cursor 会话 | 下次启动、调用工具或一轮结束时由 Hook 提示。已经空闲的窗口需要用户继续一轮。 |
| Kimi Code | 在目标工作目录运行 `weora collab kimi-acp --name "接口联调"`。恢复已结束的会话时加 `--session <原生ID>` | Weora 保持 `kimi acp` 进程运行，收到消息后在该 ACP 会话启动一轮。请先配置 Kimi 的模型或完成登录。 |
| DeepSeek Harness | 在目标会话的启动配置中加载下方 MCP overlay | 在该会话里要求 Agent 调用 Weora 的 `claim`。 |

OpenCode 插件支持 1.x 和 2.x 的投递接口。对已运行的 OpenCode 1.x 服务，也可用 `weora collab opencode-server --url http://127.0.0.1:4096 --native-id <ses_ID> --name "接口联调"` 连接一条指定会话。Pi 扩展、OpenCode 插件和 Kimi ACP 进程会自己订阅平台消息。它们只向客户端传递收件提示，正文由该会话领取。

DeepSeek Harness 使用 `dsh --profile <profile> --patch ./weora-overlay.yml` 加载以下 overlay。将 `--native-id` 设置为该会话的稳定本机 ID。每条独立会话使用自己的 overlay 和 ID，恢复该会话时复用原 ID。注册发生在 MCP 进程启动时。

```yaml
- insert:
    - id: mcp-weora
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: weora
        transport: stdio
        command: weora
        args: [collab, mcp, --runtime, deepseek-harness, --native-id, replace-with-local-session-id, --name, 'DeepSeek 联调']
```

MCP 提供 `sessions`、`send`、`claim`、`reply` 和 `ack`。MCP 连接由本机配置绑定到一条会话，工具调用时无法把发件或收件身份换成另一条会话。

Cursor 的本机 Shell 受权限控制。也可以在目标工作区的 `.cursor/mcp.json` 中配置 stdio MCP，`command` 设为 `weora`，`args` 设为 `["collab", "mcp", "--runtime", "cursor", "--native-id", "<目标原生会话ID>", "--name", "<会话名>"]`。在 Cursor 确认启用此 MCP 后，于对应会话里用它领取和回复。一个 MCP 配置绑定一个明确的会话。

## 输出约定

- `--help`：stdout 输出可读帮助，退出码 0。
- 命令成功：stdout 输出一行 UTF-8 JSON，退出码 0，stderr 为空。
- 参数错误或命令不存在：stderr 输出一行 JSON，退出码 2，stdout 为空。
- 内部异常：stderr 输出脱敏 JSON，退出码 1，stdout 为空。

```json
{"ok":true,"data":{"version":"0.1.0","protocol_version":1}}
```

```json
{"ok":false,"error":{"code":"INVALID_ARGUMENT","message":"..."}}
```

`commands` 的 `business_capabilities` 包含 `collab`。用 `weora collab --help` 查看各业务操作和参数。原生 hook 直接输出客户端要求的 JSON；连接器将待领取消息引用写入 stderr。

## 验证与构建

```powershell
uv run --project apps/cli python -m unittest discover -s apps/cli/tests -v
uv build --project apps/cli --out-dir apps/cli/dist
```

静态检查复用后端已有开发工具：

```powershell
backend/.venv/Scripts/ruff check apps/cli
backend/.venv/Scripts/ruff format --check apps/cli
backend/.venv/Scripts/mypy --config-file apps/cli/pyproject.toml apps/cli/src apps/cli/tests
```

架构与后续接入方式见 [CLI 与团队上下文接入方案](../../docs/architecture/weora-cli-and-team-context.md)。
