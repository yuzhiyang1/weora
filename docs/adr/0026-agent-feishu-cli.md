# ADR 0026：Agent 使用官方飞书 CLI

- 日期：2026-11-10
- 状态：已实施，部署通过 `WEORA_FEISHU_AGENT_CLI_EXECUTABLE` 启用

飞书群任务使用原群已绑定的 Bot。Agent 可以通过 `lark.cli.exec` 传入原生参数，使用帮助、schema 和通过只读 OpenAPI 执行的业务命令。机器人不支持的命令或资源由飞书 CLI 返回错误。原有群消息读取适配器仍由 ADR 0005 管理。

同版本官方 Skill 随后端打包。Agent 通过 `lark.cli.skill` 列出 Skill，按需读取 `SKILL.md` 和 Markdown 参考文件，再调用 CLI。上游脚本和模板保留在源码中，Skill 工具只提供文本查询。

Gateway 每次校验运行租约、工作空间、来源授权和现有 Bot 绑定。请求代理仅为飞书 OpenAPI 的 GET 和 HEAD 注入 Bot 令牌。写操作、使用 POST 的查询和 MCP 端点均不经此入口转发。写操作需要使用能对具体动作和资源进行人工审批的入口。修复前的调用可能产生写入，因此 Gateway 保留对既有回执和恢复角色的限制。登录、改 CLI 配置和升级由部署管理。

部署在 Linux Worker 上使用官方源码的 `authsidecar` 构建模式。CLI 在 Bubblewrap 隔离目录内运行，只有短期代理签名，真实 Bot 令牌由进程内代理在转发到飞书时注入。输入文件和取回的文件均显式指定，并受工具调用的大小和时间预算限制。

版本与现有消息适配器对齐为 `v1.0.80`。部署构建先执行上游的 `python scripts/fetch_meta.py`，然后执行 `CGO_ENABLED=0 go build -tags authsidecar -trimpath -ldflags '-s -w -X github.com/larksuite/cli/internal/build.Version=1.0.80'`。部署环境应登记已校验摘要的绝对路径，并安装 Bubblewrap。清除该变量并重启 Worker 即可关闭 Agent 的 CLI 入口。
