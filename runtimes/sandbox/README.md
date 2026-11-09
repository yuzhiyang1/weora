# Weora 隔离执行镜像

此目录包含正式 W08/W09 镜像配方和独立的本地验收夹具。当前 Windows 主机已在 WSL Ubuntu 24.04 安装 Docker 29.1.3，仅使用本机 Unix socket。2026-10-19 已解决官方浏览器下载，完整镜像及真实容器断言/截图通过本机验收，详见[最新W09记录](../../docs/development/weora-w09-browser-acceptance-20261019.md)。这不是生产发布、供应链漏洞扫描或所有部署场景的安全认证；不要将早期离线夹具或中间层与该完整镜像混淆。

`Dockerfile` 预装 Python 3.12、Git、Node、pytest 9.1.1 与 Playwright 1.64.1/Chromium。Playwright 与浏览器按[官方自建 Docker 镜像方式](https://playwright.dev/docs/docker)同时安装；仅安装平台实际使用的 headless shell，不承诺 headed 支持。驱动绝对路径固定在 `/opt/weora-browser/node_modules/playwright`，不从工作仓库加载。版本是在本次实现时明确固定的，不表示后续永远安全或始终最新。

构建前准备可信的 Debian bookworm 基础镜像引用：Python 3.12-slim-bookworm 和 Node 22.22.0-bookworm-slim，**两者均核验完整 digest**。2026-10-19 从官方 ECR Public Docker Library 获取并校验 manifest、config 和每个层后导入本地：

| 基础镜像 | 本地 inspect 确认的不可变 ID |
| --- | --- |
| Python 3.12-slim-bookworm | `sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d` |
| Node 22.22.0-bookworm-slim | `sha256:7cc56ef285a8568121537d17b05e72128f01b89c54607b51acf084a50ef483f3` |

公开来源为 `public.ecr.aws/docker/library/python` 和 `public.ecr.aws/docker/library/node`，不是第三方重新打包的镜像。已下载镜像不代表最终浏览器镜像构建通过。

```powershell
# 先设置 $taskPythonImage/$taskNodeImage 为已核验的完整镜像@sha256引用，再执行。
docker build -f runtimes/sandbox/Dockerfile --build-arg "PYTHON_IMAGE=$taskPythonImage" --build-arg "NODE_IMAGE=$taskNodeImage" -t weora-sandbox:v0.1 runtimes/sandbox
```

推送到自己的测试 Registry 后取得最终镜像的仓库 digest，配置 `WEORA_SANDBOX_IMAGE`。也可使用 `docker image inspect` 确认的完整本地 `sha256:<64位小写十六进制>` 镜像 ID；适配器同时接受 `仓库@sha256:...`，但拒绝 tag、latest 和短 ID。不要把 config blob digest 直接当作镜像 ID；Docker 29/containerd 的实际标识以 inspect 为准。测试项目需要额外依赖时在受审的派生镜像预装；不要给运行时临时开放网络安装包，也不要向镜像复制团队源码或密钥。

运行时由平台强制非 root、只读根、network none、tmpfs、无宿主目录或 Docker Socket、资源上限和所有权标签。不能为了启动浏览器添加 `SYS_ADMIN`、`--privileged` 或宿主网络；启动失败应保留真实失败事实，检查镜像和资源配置。镜像 Node 子进程与浏览器均位于容器，平台持有 Docker CLI 控制权但不会把 daemon Socket 提供给容器。

浏览器请求限于指定 localhost 端口，所有页面、弹窗、重定向和资源请求共用策略。WebSocket 与下载尝试会被拒绝且不能报告成功；因此验收目标应使用不依赖 HMR 的服务。截图期间才发生的越界请求也会影响最终判定，404 页面即使命中文本也不算通过。

真实Chromium发现Playwright route不能拦截重定向链后续URL，平台因此使用容器内部、仅转发已批准origin的回环守卫，逐跳拒绝其他端口与CONNECT；取消Chromium隐式loopback绕过。该守卫不是本机v2rayN，不提供任意外网代理，同源重定向仍由浏览器真实处理。浏览器工具的`artifact_id`是验证记录，`screenshot_artifact_id`才是独立截图引用；不把截图Base64交给文本Runtime。

先在小型离线仓库验证读写、测试失败/成功、符号链接拒绝、输出/资源超限、网络拒绝、取消和孤儿环境清理。浏览器验收另验证服务端口、越界跳转、断言和真实截图；完整步骤见[运行手册](../../docs/development/weora-v0.1-operations.md)。

## 有界构建与可恢复缓存

`fetch_registry_image.py` 只允许明确选定的官方 ECR Python/Node 仓库及完整 manifest digest，使用 HTTPS 分段下载，校验后生成 OCI 导入包。用系统 `timeout` 限制整个获取阶段；成功分片保留，不能无限重试或改用未知镜像源。

`fetch_apt_cache.py` 在已有正式构建中间层中读取经签名验证的 APT 索引，将公开包通过主机 HTTPS 并发下载，再逐包核对索引 SHA256；`build-cache/apt/` 中的二进制已忽略，不进入源码提交。Dockerfile 仅在构建阶段复制缓存，暂存 slim 镜像的自动清理 hook 以避免 `apt-get update` 擦掉缓存，安装后恢复 hook，最终显式 `apt-get clean`。不复制宿主运行文件、不挂载运行时目录、不传递凭据。

浏览器系统依赖层最多 900 秒，官方 headless shell 安装最多 600 秒，失败应保留日志和已完成层，不修改运行时安全边界。2026-10-19 已核验 102 个 Debian 包，共 96,294,808 字节；清单保存在本机 `volumes/sandbox-build-20261019/apt/verified.json`。这只是供应链来源与完整性记录，不等于已通过生产安全扫描或位级可复现构建。

`browser_acceptance.py` 必须在完整镜像就绪后运行，直接使用产品 DockerSandbox 和浏览器适配器，验证成功/断言失败截图、外链/迟到外链/跨端口跳转/WebSocket 拒绝、404 与未就绪服务。所有页面来自脚本生成的无凭据夹具，不接触真实项目；证据保存到明确传入的 `output/playwright/` 子目录。Node 接口替身单测只验证脚本分支，不冒充真实 Chromium 验收。

### 2026-10-19 较早失败尝试（保留历史）

- 正式配方中间层 `sha256:0dee97ebeefa43c19e0dbb6ac98d3d0bca63d3b294a73818c3d52bb48fd639b9` 已包含 Python 3.12.15、Git 2.39.5、pytest 9.1.1、Node 22.22.0、Playwright 1.64.1 及全部浏览器系统依赖；尚无 Chromium，最终 USER/清理层也未执行，不得配置为已验收沙箱镜像。
- 官方目标为 Chrome Headless Shell 156.0.8078.4 / revision 1248。`cdn.playwright.dev` 跳转至 `storage.googleapis.com` 后，IPv4 连接超时、IPv6 网络不可达；安装阶段约 31.54 秒后退出码 1，早于 600 秒硬上限。未改用第三方源、未追加无界重试，也没有生成成功/失败截图。
- 最终构建日志：`volumes/sandbox-build-20261019/formal-build-cached.log`；事实与摘要校验值：同目录 `build-facts.json`；实际软件版本：`intermediate-versions.log`。该次构建约 206 秒，浏览器前的缓存/依赖阶段约 174 秒。失败构建容器已按本次所有权标签核对后回收，镜像及公开下载缓存保留以便恢复；没有全局 prune。

官方下载地址恢复可达后，复用上述固定基础镜像和缓存重新执行同一构建命令。构建必须完成最终非 root 用户与清理层；再以 `docker image inspect` 取得最终完整 ID，运行：

```powershell
backend/.venv-v01/Scripts/python.exe runtimes/sandbox/browser_acceptance.py --image $taskFinalImage --output D:/projects/weora/output/playwright/weora-w09-container-20261019
backend/.venv-v01/Scripts/python.exe runtimes/sandbox/acceptance.py --wsl --browser --image $taskFinalImage --output D:/projects/weora/volumes/sandbox-local/formal-acceptance-20261019
```

上述失败时继续使用原先W08核心夹具；后续完整构建与真实验收通过后才切换配置，未启用缺少浏览器的中间镜像。

### 当前完整镜像与浏览器缓存

完整ID：`sha256:417c6aa5950dd52b14e846bb756400eff27f2501946345e9d4859001fc8232a7`，用户`10001:10001`，实际Chromium156.0.8078.4/revision1248，Playwright1.64.1。镜像和平台仍只提供headless。

官方Google Storage的标准路径在本机不稳定，但同一公开对象的[虚拟主机端点](https://docs.cloud.google.com/storage/docs/request-endpoints)可达。Chromium通过HTTPS取得并以官方对象MD5核对，两个ZIP再做完整性检查、记录SHA256。下载来源与摘要见最新W09记录，不使用未知重打包源；SHA记录不冒充上游签名。

可选`build-cache/browser/manifest.json`包含`chromium-headless-shell`与`ffmpeg`，每项指定固定文件名、revision、size、sha256，Chromium另有browser_version；来源URL、官方对象摘要可附在账本中。`install_browser_cache.py`按镜像当前`playwright-core/browsers.json`逐项核验后，仅在构建容器回环临时提供这两个文件给官方安装器，安装后删除辅助脚本与缓存。缺少清单时走官方在线安装，存在清单但内容不符则停止；不能靠失败后回退网络掩盖缓存损坏。二进制和账本均忽略，保留`.gitignore`占位。

真实容器事实见`output/playwright/weora-w09-container-accepted-20261019/facts.json`，包括成功/失败截图、同源/越界多跳重定向、HTTPS/WebSocket、404、无服务端口及禁端口探针0次命中。正式镜像核心隔离/回收验收见`volumes/sandbox-local/formal-accepted-20261019/facts.json`。本地`.env`现使用完整ID与浏览器端口3000；原W08夹具保留用于回滚，运行时没有下载代理、宿主挂载或Docker Socket。

## 本地核心验收夹具

`Dockerfile.acceptance` 是只含 Python/Git/pytest 的轻量联网构建配方。网络不可用时，`build_offline_fixture.py` 可从本机已安装的明确系统文件制作 `Dockerfile.offline-acceptance` 的输入：只选择 Python 3.12、Git、标准库、Git 模板及 `ldd` 列出的动态库，测试依赖只复制虚拟环境内的纯 Python pytest 包。它不复制 `/home`、`/root`、现有 `/etc` 配置或业务项目；最小账户映射由脚本重新生成。`provenance.json` 记录系统包版本、每个常规文件及最终 rootfs 的 SHA256。

离线镜像不含 Node/Playwright，也没有完整系统运维能力，只用于本机 W08 核心隔离验收。它不是正式镜像的离线分发方式，更不代表供应链扫描或浏览器验收已经通过。

`acceptance.py` 接受明确的不可变镜像 ID，生成无凭据的最小 Bug 仓库，验证测试失败→修复→测试通过→Diff/导出，以及隔离配置、断网、符号链接拒绝、输出/超时/取消回收和孤儿清理。浏览器仅在显式 `--browser` 时检查；未启用会记录 `NOT_RUN`。输出目录保留 `input.tar`、`facts.json`、`changes.diff`，不会使用真实业务仓库。

Windows 通过管理员配置的结构化 CLI 前缀使用 WSL，不开放 daemon TCP：

```text
WEORA_SANDBOX_CLI_EXECUTABLE=wsl.exe
WEORA_SANDBOX_CLI_ARGS=["-d","Ubuntu","-u","root","--exec","docker","--host","unix:///var/run/docker.sock","--config","/var/lib/weora/docker-cli"]
```

必须使用 `--exec`（或 `-e`）；WSL 的 `--` 只终止选项解析，仍会调用默认 shell，可能改变模型命令参数甚至造成 shell 注入。适配器会拒绝缺少直接 exec 的 WSL 前缀。固定参数不能由模型或任务覆盖。独立 CLI 配置目录应由管理员创建为空目录，避免继承已有 Docker 登录态和 context。所有验收容器均采用随机 Run ID 和专用部署标签；清理只针对已核对所有权的容器，不使用全局 prune。
