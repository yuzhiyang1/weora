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
