# Weora CLI

面向人和 Coding Agent 的独立平台客户端，采用 Python 3.12，不导入后端业务模块。

当前仅交付本地命令发现骨架。尚无业务命令、认证、HTTP 客户端、上下文查询或可安装的配套 Skill。

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

`commands` 的 `business_capabilities` 当前是空数组。`schema` 与命令解析共用声明，提供已实现命令的 JSON Schema 输入定义；不是服务端 Tool Registry 的镜像。

## 验证与构建

```powershell
uv run --project apps/cli python -m unittest discover -s apps/cli/tests -v
uv build --project apps/cli --out-dir apps/cli/dist
```

静态检查复用后端已有开发工具，不为 CLI 运行增加依赖：

```powershell
backend/.venv/Scripts/ruff check apps/cli
backend/.venv/Scripts/ruff format --check apps/cli
backend/.venv/Scripts/mypy --config-file apps/cli/pyproject.toml apps/cli/src apps/cli/tests
```

架构与后续接入方式见 [CLI 与团队上下文接入方案](../../docs/architecture/weora-cli-and-team-context.md)。
