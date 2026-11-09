# W09 正式镜像与真实浏览器验收（2026-10-19）

结论：**W09本机headless真实验收通过**，包含正式完整镜像、模型/工具/人工审批/Console证据闭环。接续此前网络阻塞记录（内部记录已移除）。本轮只处理本机测试环境、无凭据夹具与W09；没有向飞书发送新消息，没有远端提交、推送、PR或部署，也没有修改个人代理节点、订阅或系统代理。

## 1. Chromium 下载与正式构建

- 用户启动的是`E:/v2rayn`，实际为`127.0.0.1:10808`混合HTTP/SOCKS端口；旧目录中的10809不能代表当前配置。原`storage.googleapis.com`地址仍出现TLS超时/连接重置，Windows客户端另出现吊销查询离线，不能只凭端口监听断言代理全链路正常。
- 根据[Google Cloud官方端点说明](https://docs.cloud.google.com/storage/docs/request-endpoints)，使用同一公开bucket/object的虚拟主机HTTPS地址：`https://chrome-for-testing-public.storage.googleapis.com/156.0.8078.4/linux64/chrome-headless-shell-linux64.zip`。该地址在本机代理及直连探测均返回200；最后7,296,163字节经206断点续传完成，没有第三方重打包源。
- Chromium文件125,797,539字节，MD5为`743dac7baf99eaa72e221335ea966f07`，与官方对象响应一致；SHA256为`c3e50596aef52c58f8fda3c6b3411c33d34c88b177b3ed1c2243359fd3b8ba20`。FFmpeg 1013来自官方Playwright分发，2,302,701字节，SHA256为`ee986434d158f74fb5d9c5ff1092026ceedb63f324fe90b5be89460926b7b679`。两个ZIP完整性检查通过。下载保留TLS证书链和主机名校验，未使用`--insecure`；Windows离线吊销查询采用best-effort。这些摘要是传输/缓存完整性证据，不冒充上游签名或供应链安全扫描。
- 新增`install_browser_cache.py`：缓存缺失时仍用官方在线安装；存在缓存清单时核对当前Playwright的revision、浏览器版本、文件名、大小及SHA256，任何不符立即停止，不静默降级。仅构建期间以容器回环固定文件服务向原版Playwright安装器提供这两个文件，不修改驱动、不自行伪造安装完成标记；安装后删除缓存与辅助脚本。
- 原102个APT包继续按签名索引核对并复用。没有将宿主Python、Git、浏览器用户资料、业务源码或凭据复制到正式镜像。

最终镜像ID：`sha256:417c6aa5950dd52b14e846bb756400eff27f2501946345e9d4859001fc8232a7`，最终用户`10001:10001`。Python3.12.15、Git2.39.5、pytest9.1.1、Node22.22.0、Playwright1.64.1、实际启动的Chromium156.0.8078.4；仅headless，不承诺headed。

本地构建日志`volumes/sandbox-build-20261019/formal-build-browser-cache.log`的SHA256：`7986a136be8742f4c93386b2e91fe4b30a003d3b63c4a828c411c3f4b6898b3a`。配方SHA256：`756505f870eed1b013d4aa1ab7e1643645ded13176da56b597207bf4cd821b73`。原失败日志不覆盖，二进制缓存与来源账本均在忽略目录。

## 2. 真实 Chromium 发现并修复的问题

第一轮真实成功/失败截图已得到，但外部及其他端口的302被错误归类为SERVICE_NOT_READY。替身单测没有模拟真实重定向行为，不能替代Chromium。[Playwright路由规则](https://playwright.dev/docs/api/class-browsercontext#browser-context-route)只拦截重定向链首个URL，因此仅检查route和最终page.url不足以防止中间访问。

修复保留原路由限制，并增加容器内部回环转发器：每个真实HTTP目标均校验唯一批准origin，拒绝CONNECT及其他端口；显式取消Chromium隐式loopback直连例外。同源302仍由真实浏览器正常处理，不改写成伪造页面。它是容器内目标访问策略，不是可出网代理，不使用本机v2rayN，也不放宽network none、只读根、cap-drop或资源上限。

新增Node分支回归先复现SERVICE_NOT_READY而非URL_DENIED，再修复通过。真实容器验收覆盖：

| 场景 | 真实结果 |
| --- | --- |
| 页面文本命中、同源重定向 | PASSED并生成PNG |
| 文本不命中 | ASSERTION_FAILED并生成PNG |
| 即时/迟到外链、跨端口/外部重定向、多跳重定向、HTTPS CONNECT、WebSocket | URL_DENIED |
| 404、无服务端口 | SERVICE_NOT_READY，不冒充通过 |
| 绝对URL、协议相对地址、反斜杠路径 | BROWSER_URL_DENIED |
| 被禁止端口上真实监听的探针 | 收到0次请求，证明拒绝发生在越界访问前 |

最终证据目录：`output/playwright/weora-w09-container-accepted-20261019/`，`facts.json`的`failures=[]`、`blocked_port_hits=0`，并记录实际浏览器版本。首轮错误目录保留；增强探针时曾将“无服务端口”与探针同时设为3001，夹具冲突得到1次合法访问，已改用3002并重验，未误归因于产品漏洞。

同一正式镜像另跑`acceptance.py --wsl --browser`，测试失败→修复→测试成功→Diff/导出、断网、根目录写拒绝、符号链接拒绝、输出上限、超时、取消、孤儿容器回收全部通过。证据：`volumes/sandbox-local/formal-accepted-20261019/facts.json`。

## 3. 模型、审批与 Console 真实链路

保留第一轮Run`76540b32-d093-5280-a3fd-0d77eb45330f`及其0.1.0版本；其执行成功，但回答把browser记录ID误当截图ID。没有回写旧回答，而是补齐内部`BrowserEvidence`配对契约及工具返回字段：`artifact_id`是browser验证记录，`screenshot_artifact_id`才是本次真实截图，另有`screenshot_status`。无图时明确为null，永不返回图像Base64给文本Runtime。

新回归先复现缺少独立截图引用，再验证两种产物ID不同、同属当前计划、确有持久记录，工具结果不含图像正文。另发布测试Agent 0.1.1，不改其他Agent或飞书路由。

最终Run：`94903a3a-b5fa-5256-84ad-29c6ce151ff9`，计划`4ad8f925-591c-5d23-b26b-3a694bae0bd7`。页面实际选择发布版本并提交，审阅后勾选范围确认、填写说明并审批。只允许`index.html`、固定启动/就绪命令、browser.verify及Diff；不包含真实主仓库修改、Git提交、远端或部署。`a`×40只是本地夹具固定revision标识，不是远端commit。

两轮提案前模型均尝试读取当前只读目录下不存在的index.html，真实返回REPOSITORY_FILE_UNAVAILABLE，失败记录保留；没有因此扩大只读根或假装读到Docker快照。后续写入和完成事实来自受审的容器夹具。

最终Run状态SUCCEEDED，事件序号6。真实链路为页面提交→模型提案→页面审批→容器写文件→固定命令退出0/服务就绪→负例ASSERTION_FAILED→正例PASSED→非空Diff→完成并回收。命令证据与最终Diff均绑定仓库摘要`9848eb35f7cd3957f7fb30c50a156d639eeb8d0c67010e6241aa5f8e0198f969`，没有沿用修改前的测试。

| 断言 | browser验证记录 | 独立screenshot产物 |
| --- | --- | --- |
| 预期失败 | `44889797-d103-5355-9df7-a2161f9f1710` | `166423fe-7f5c-5478-900c-165632a52d62` |
| 成功 | `2c8880dd-92d7-59eb-9c2a-f55a6483ec6a` | `9199cb1b-8bed-573a-b69e-8cea74a7f097` |

逐一核对工具结果、持久产物与最终回答，0.1.1已正确区分上述ID；文本Runtime明确未看过图像。两张PNG均为同一真实页面，断言目标不同，因此相同图像哈希不代表状态相同。页面列表分别保留“断言失败”和“验证通过”，点击预览显示对应证据ID及状态，图片解码尺寸1280×720。未配置、服务未就绪、越界拒绝、截图失败与超时有独立中文状态，不混作成功。

真实测试浏览器恢复同一Smoke账号的短期会话后，实际点击提交/审批/查看证据；登录表单本身复用此前已验收结果，不冒称本轮重新手输密码。会话载入文件已删除。1440×1000桌面检查、390×844窄屏及刷新后重新打开截图均通过：页面文档宽375≤视口390，图片宽297，无横向溢出。截图位于`output/playwright/weora-full-20261019/`的`21-w09-final-result.png`、`23-w09-failed-screenshot.png`、`25-w09-mobile-reload.png`、`26-w09-evidence-reference.png`。主Agent已视觉检查桌面结果、截图与窄屏，不只检查DOM。

验收后`docker ps -a`为空，API ready与数据库正常；没有全局prune，镜像及公开缓存保留以备复验。

## 4. 自动检查与边界

- 交付模块及文本恢复媒体回归105项通过；新增缓存8项、转发器拒绝与截图引用回归均包含其中。Mypy26个相关文件通过，Ruff与修改文件格式检查通过。
- 执行API与媒体恢复专项16项通过；全后端Mypy163个文件与Ruff通过。此前353项非integration全后端保留原验证时点，未修改的其他模块复用该证据，不写成本轮全量重跑。
- 前端先新增证据ID断言，针对用例真实失败，随后补齐预览元数据；完整45项在限定2个worker后通过。默认并发首次44通过、1个既有Agent发布页测试在懒加载等待阶段超时，保留首次失败；没有放宽断言或增加全局等待时间。补齐4种浏览器状态中文标签后，最终完整49项通过（59.80秒），TypeScript、ESLint与生产构建通过。复验命令为`pnpm --filter @weora/web exec vitest run --maxWorkers=2 --minWorkers=1`，没有修改项目默认并发设置。
- 初次新缓存单测因系统pytest临时目录ACL失败，改用仓库内独立`--basetemp`后8项通过，没有修改系统ACL；一次从仓库根目录运行Mypy无法定位项目类型，回到backend标准工作目录后通过，没有忽略类型错误。
- 本地`.env`已切换到完整不可变镜像并启用3000浏览器端口，保留原W08夹具及可回滚镜像；没有把代理配置或个人凭据带入执行容器。服务重启前确认无RUNNING/QUEUED任务。
- 本轮不等于整版发布验收、生产部署、供应链漏洞扫描、headed支持、云效/华为云远端适配或图片OCR完成。
