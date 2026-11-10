# ADR 0026：Agent 使用官方飞书 CLI

- 日期：2026-11-10
- 状态：已实施，部署通过 `WEORA_FEISHU_AGENT_CLI_EXECUTABLE` 启用

飞书群任务使用原群已绑定的 Bot。Agent 可以通过 `lark.cli.exec` 传入原生参数，使用帮助、schema、快捷命令、领域命令和原始 API。各业务命令共享一个入口。机器人不支持的命令或资源由飞书 CLI 返回错误。原有群消息读取适配器仍由 ADR 0005 管理。

同版本官方 Skill 随后端打包。Agent 通过 `lark.cli.skill` 列出 Skill，按需读取 `SKILL.md` 和 Markdown 参考文件，再调用 CLI。上游脚本和模板保留在源码中，Skill 工具只提供文本查询。

Gateway 每次校验运行租约、工作空间、来源授权和现有 Bot 绑定。启用此入口后，获准发起该群任务的成员可以通过 Agent 使用绑定 Bot 在飞书中的实际权限。命令可能写入外部系统，工具按有副作用执行，未知结果不会重放。登录、改 CLI 配置和升级由部署管理。

部署在 Linux Worker 上使用官方源码的 `authsidecar` 构建模式。CLI 在 Bubblewrap 隔离目录内运行，只有短期代理签名，真实 Bot 令牌由进程内代理在转发到飞书时注入。输入文件和取回的文件均显式指定，并受工具调用的大小和时间预算限制。

版本与现有消息适配器对齐为 `v1.0.80`。部署构建先执行上游的 `python scripts/fetch_meta.py`，然后执行 `CGO_ENABLED=0 go build -tags authsidecar -trimpath -ldflags '-s -w -X github.com/larksuite/cli/internal/build.Version=1.0.80'`。部署环境应登记已校验摘要的绝对路径，并安装 Bubblewrap。清除该变量并重启 Worker 即可关闭 Agent 的 CLI 入口。
