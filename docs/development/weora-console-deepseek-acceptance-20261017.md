# Console → DeepSeek 本地联调记录

- 日期：2026-10-17。
- 范围：本地 PostgreSQL、Redis、Pi 和 DeepSeek；不接飞书、沙箱或远端仓库。
- 结论：**Web 同源代理 → Console 认证/任务 API → PostgreSQL Outbox → Beat → Redis → Worker → Pi → DeepSeek → 持久化结果的真实链路已通过。** 此后通过独立 Chrome 完成登录、页面发起与回答展示，见[真实页面验收记录](weora-console-browser-acceptance-20261017.md)；下文保留本轮接口联调的原验证范围。

## 已确认的事实

| 检查项 | 结果与边界 |
| --- | --- |
| PostgreSQL | 经用户确认另建独立 PostgreSQL 17 实例，只监听 `127.0.0.1:55432`；专用库及普通账号均为 `weora`，不具备超级用户、建库或建角色权限。当前迁移 `20261017_0011`，`alembic check` 无结构差异。原 `5432` 服务及数据没有修改。 |
| Redis | 原先没有 Redis 监听；使用已有 Ubuntu WSL，将 Ubuntu 官方软件包解包到 `volumes/redis-local/`，没有安装系统服务。启动后 Windows 后端通过 `127.0.0.1:6379/0` 成功 PING，并通过带 TTL 的唯一临时键写入、读取、删除验证；启用 AOF，仅绑定回环地址和 protected mode。 |
| 模型配置 | Key 仅保存在被 Git 忽略的 `backend/.env`；供应商 `/models` 返回 HTTP 200，包含 `deepseek-flash` 与 `deepseek-v4-pro`。 |
| 模型名称 | 使用 `deepseek-flash`。官方说明 `deepseek-v4-flash` 是仍接受的旧别名，请求由当前 Flash 模型承接；Pi 1.3.0 的目录使用当前名称，不做未知模型自动回退。 |
| 真实模型调用 | 首次独立 Pi 连通性请求约 1.48 秒；随后经 Console 的同源代理接口创建 Run，约 3.56 秒从创建到成功落库，结果为 `WEORA_CONSOLE_OK`。两轮共发出 2 次生成请求，均未调用工具。 |
| 费用边界 | 本地输出上限 128 token、请求超时 60 秒；Pi 配置 `maxRetries: 0`。未查询账单，不能声称精确消费金额。输出上限不是账户总消费上限，完成联调后应按实际任务调整。 |
| 自动检查 | Pi / Web 构建复用前轮已通过结果，未改前端代码。后端 249 项非外部集成测试通过；新增 2 项真实 PostgreSQL 迁移回归通过；最终再验 SQLite 迁移与 PostgreSQL 回归合计 4 项通过。mypy 157 文件及变更文件 Ruff 检查通过。 |
| 页面验收 | 官方浏览器验收连接返回 `privileged native pipe bridge is not available; browser-client is not trusted`。没有改用其他浏览器入口绕过限制，尚无页面点击或截图验收证据。 |

模型名称依据：[DeepSeek 官方模型说明](https://api-docs.deepseek.com/quick_start/pricing/)。

## 当前本地配置

后端 `.env` 已配置 Pi 入口、明确模型名、输出限制、超时和 Redis Broker，并将数据库连接切换到用户批准的 `55432` 独立实例。已关闭开发密码绕过，实际验证错误密码返回 401、正确密码登录成功。没有更改原有 OSS 配置；测试 Agent 不绑定 Skill，因此本轮不依赖 OSS。配置加载相对于工作目录，必须在 `backend` 目录启动 API、Worker 和 Beat。本文不记录密钥或带密码的 URL。

本机凭据位置均已被 Git 忽略：`backend/.env` 保存应用普通账号和 DeepSeek 配置；`.postgres-admin.local` 仅保存新实例的管理账号；`.console-smoke.local` 保存测试 Console 账号。管理员不供 API/Worker 使用。一次性 `.postgres-init-password.local` 已清理，初始化凭据仍可从管理配置恢复。

独立实例数据目录：`volumes/postgres-local/data/`。迁移前备份：`volumes/postgres-local/backups/weora-before-migration-20261017-175543.dump`；0011 前含测试任务的备份：`weora-before-0011-20261017-175936.dump`。备份没有做还原演练，不能称为已验证恢复。

重启新实例（先确认 `55432` 未被其他进程占用，不使用原实例的数据目录）：

```powershell
pg_ctl --pgdata=D:/projects/weora/volumes/postgres-local/data --log=D:/projects/weora/volumes/postgres-local/postgres.log --options="-h 127.0.0.1 -p 55432" --wait start
```

Redis 使用 Ubuntu 更新源的 `redis-server/redis-tools` 包 `5:7.0.15-1ubuntu0.24.04.5`，配套库同样解包到项目目录，没有改 Ubuntu 系统包。第一次包索引过期导致 404，现已使用项目专用索引更新并下载成功。运行目录、数据、日志和软件包都位于 Git 忽略的 `volumes/redis-local/`。

后续启动 Redis（仅适用于本机已完成解包的目录；先检查端口占用，避免覆盖其他实例）：

```powershell
wsl -d Ubuntu -- env LD_LIBRARY_PATH=/mnt/d/workspac/weora/volumes/redis-local/root/usr/lib/x86_64-linux-gnu /mnt/d/workspac/weora/volumes/redis-local/root/usr/bin/redis-server --bind 127.0.0.1 --protected-mode yes --port 6379 --daemonize yes --appendonly yes --dir /mnt/d/workspac/weora/volumes/redis-local/data --pidfile /mnt/d/workspac/weora/volumes/redis-local/data/redis.pid --logfile /mnt/d/workspac/weora/volumes/redis-local/data/redis.log
```

本次结束保留独立 PostgreSQL、Redis、API `127.0.0.1:8000`、Worker、Beat 和 Web `127.0.0.1:5173`。Web 代理明确指向 `http://127.0.0.1:8000`，避免 Windows 上 `localhost` 的 IPv6 解析差异。重启电脑或关闭 WSL 后需要重新启动对应服务；本次没有注册新的系统服务。

## 原阻点与处理

开发者确认原实例尚未创建 `weora` 库和账号，且不记得原管理员密码。因此，先前的认证失败不能归因为账号密码填错。用户批准另建独立实例后，使用本机已安装的 PostgreSQL 程序初始化新目录，随机生成管理与应用凭据，以 SCRAM 认证运行；未尝试找回、重置或绕过原管理员密码。

在真实 PostgreSQL 上执行 `alembic check`，发现工具调用把相同列同时声明为主键和唯一约束。PostgreSQL 合并声明时可能把显式唯一约束名用于主键；最初的按名称删除方案在迁移后主键检查中被发现有误，已立即恢复，当时工具表为 0 行，无工具数据丢失。最终 0011 按约束类型处理：若名称属于主键则重命名；若是独立唯一约束才删除，始终保留复合主键。不改写旧迁移。

新增真实 PostgreSQL 回归在独有临时 schema 中覆盖上述两种约束形态、已有行保留、升级/回退/再升级和重复调用不新增行；成功后删除自有 schema，异常时事务回滚。SQLite 升降级及 ORM 一致性同时验证。独立测试库实际回退/再升级 0011 后，主键与任务结果均保留，`alembic check` 无差异。

## Console 接口验收证据

- 测试工作空间：`9241cacd-7363-5d92-a481-7f7f7b867d4d`。
- Agent：`7b439f4c-628f-52a6-8f74-4363b7b25bbd`，发布版本 `0.1.0`，不绑定 Skill。
- Run：`03b0ee21-bb8d-59c8-ba13-1bcaa63ecfbf`。
- 明确模型快照：`pi-agent-core@1.3.0 / deepseek / deepseek-flash`，128 token，60 秒。
- 状态：`QUEUED → RUNNING → SUCCEEDED`；结果 `WEORA_CONSOLE_OK`；独立 SQL 连接确认结果已提交，事件序号 1–3，fencing token 为 1。
- 通过：重复提交相同幂等键返回同一 Run；不同指令复用该键返回 409；SSE 交付真实事件并终止；按序号补读无遗漏；新会话登录后反复读取仍为相同结果；匿名访问返回 401。
- 通过：将同一成功 Run 再次投递真实 Redis 队列，Worker 不重新领取，Run 事件仍为 3、fencing token 仍为 1。
- 数据库迁移和上述只读检查后没有追加新的生成请求。没有执行真实模型失败/超时/取消测试，也未验证工具调用、飞书或沙箱。

接口验收入口：Run 详情（链接已脱敏）。测试账号 `weora-smoke@example.com`，密码在本机 `.console-smoke.local`，不要提交到 Git。本轮 HTTP 验证不代替页面验收；后续独立 Chrome 的实际页面提交 Run 与结果见[页面验收记录](weora-console-browser-acceptance-20261017.md)。

此次没有暂存、提交、推送、修改原 `5432` 数据库或发送飞书消息；仅修改批准的新实例和本地配置。下一步仍按[运行手册](weora-v0.1-operations.md)分项调试工具和飞书能力，不扩大第一版范围。
