# 虚拟电脑本地部署

Windows 宿主使用 WSL2 Ubuntu。以下命令从 WSL 中的 Weora 仓库根目录执行。需要 Docker Engine、Docker Compose 2.24.4 或更新版本、Git 和 Python 3。构建时只使用固定的 OpenBot 源码版本，两个镜像内均携带上游 MIT 许可。

```bash
bash runtimes/openbot/build.sh
```

Docker Hub 直连超时时，可加 `WEORA_OPENBOT_DOCKERHUB_MIRROR=mirror.gcr.io` 使用公共镜像缓存。若 Playwright 的下载跳转超时，可从 [Chrome for Testing 官方存储](https://chrome-for-testing-public.storage.googleapis.com/151.0.7922.34/linux64/chrome-linux64.zip) 下载 Linux x64 归档，并以 `WEORA_OPENBOT_CHROMIUM_ARCHIVE=/绝对路径/chrome-linux64.zip bash runtimes/openbot/build.sh` 构建。配方先校验归档，再安装锁定版本的系统依赖。

在 Compose 使用的私有环境文件中配置 `WEORA_COMPUTER_SUPERVISOR_TOKEN` 和 `WEORA_COMPUTER_MASTER_TOKEN`。分别生成两个不同的随机值，例如各执行一次 `openssl rand -hex 32`。沿用现有部署的数据库密码和初始化流程。开启电脑功能时 Compose 禁用开发身份绕过，只在本机开放控制台端口 3000。

```bash
docker compose -f compose.yaml -f compose.computers.yaml build backend frontend
docker compose -f compose.yaml -f compose.computers.yaml up -d --wait postgres
docker compose -f compose.yaml -f compose.computers.yaml run --rm backend /app/.venv/bin/alembic upgrade head
docker compose -f compose.yaml -f compose.computers.yaml up -d
```

打开一个私有控制台 Run 的「虚拟电脑」页签，依次验证启动、截图、获取控制权、点击桌面、交还和停止。Agent 调用浏览器工具遇到人工接管时会在 Run 的执行时限内等待，单次等待最多 45 秒。同一 Task 的后续 Run 复用该任务的工作文件和浏览器配置。桌面帧以一秒左右的轮询更新，当前支持基础英文键盘、左键和右键，完整桌面的控制权在 OpenBot 服务端检查。

要让 Agent 操作浏览器，运行现有 Weora Worker 和调度器。API 与 Worker 需要同样的电脑环境配置，并加入 `weora-computer-control` 和 `weora-computers` 网络。对已部署的 Worker 容器，可分别执行 `docker network connect <网络名> <Worker 容器名>`。首次导航可以按需创建电脑。人工停止后需要手动启动。工具使用注册在 `WEORA_COMPUTER_BROWSER_ORIGINS` 中的精确来源，例如 `["http://computer-test:8080"]`。把无凭据的测试站点容器加入 `weora-computers` 网络并赋予相同的 DNS 名称。该网络默认是 Docker 内部网络。电脑容器的 Shell、破坏性重置和文件写入 API 已禁用。

## 飞书验收话题

通过现有 Feishu Context 路由及 Super-agent 配置绑定专用话题和 Worker。将返回的协调话题 ID 明确登记在 API 和 Worker 的 `WEORA_COMPUTER_FEISHU_SCOPES` 中，例如 `{"<64 位协调话题 ID>":"observe"}`。其他飞书话题和状态查询运行没有电脑权限。观察模式仅允许登记站点的 GET/HEAD，禁止浏览器点击和填表，并使用独立浏览器配置卷。`browser.evidence` 返回最近业务请求的状态及截图编号，截图在 Run 的「审批与交付」产物中查看。需要测试账号交互时单独配置 `test` 模式，使用测试环境和已授权人员。

测试模式支持在控制台接管桌面完成登录，交还后 Agent 先重新读取快照。电脑服务会从快照移除密码和带自动填充标记的一次性验证码字段值，并保留普通输入值。快照也提供普通文本元素的定位引用，支持操作没有按钮语义的业务入口。

Windows 上直接运行 Python API 和 Worker 时，Supervisor 可绑定到 `127.0.0.1`，由它为电脑分配回环端口。Weora 验证电脑名和回环端口后连接，适合本机联调。Compose 默认继续使用 Docker 私网服务名。

电脑按用户和 Task 隔离，同一任务续接的 Run 复用页面与登录态，每次操作仍校验当前 Run 的租约。旧版按 Run 创建的配置不自动迁移到新电脑，避免把调试登录态作为自主验收的起点。

测试账号可通过 `POST /api/v1/workspaces/{workspace_id}/computer-credentials` 登记，包含精确 `origin`、已开放 test 模式的 `scope_id`、`label` 及只写的 `username`、`password`。仅当前用户可使用，一天后失效；`DELETE /computer-credentials/{id}` 撤销。密码必须通过受保护配置请求提交，不写入飞书或模型指令。Worker 使用 `browser.credentials` 和 `browser.fill_credential`，没有凭据或遇到验证码时调用 `browser.request_human` 并报告 blocked。交还后在原话题回复继续，由 Weora 恢复任务。

停止电脑会保留卷。Supervisor 动态创建的容器和卷需要分别管理，Compose 停止时先在 Weora 面板停止对应电脑。更新镜像后验证启动与接管，迁移或清理卷前按用户和 Task 确认归属。
