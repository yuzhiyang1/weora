# 仓库连接、自动克隆与多仓只读验收

- 日期：2026-10-20。
- 范围：[ADR 0007](../adr/0007-selected-repository-readonly-snapshots.md)。不含多仓改码、推送、评审创建或部署。

## 当前结论

独立浏览器实际选择两个仓库 → PostgreSQL/Redis/Celery → 固定提交浅 fetch → Pi/DeepSeek 调用两个 README 读取工具 → 页面显示完整回答，已经通过。这里的远端是本机隔离 HTTPS Git 服务，不冒充云效目标通过。

用户指定的云效 `example-java-service` 登记在「云效只读接入验收」项目：`bc8a5529-5588-53ba-b221-846bb786299f`。早期因缺凭据未验收；用户在 Console 填写 Git 认证信息后，于同日完成真实云效校验、固定提交拉取和 README 模型链路，见下方追加记录。

## 使用方式

1. 在 Console 项目详情中编辑仓库，填写 Git 地址、默认分支、Git 认证用户名和 **Git 密码或访问令牌**。不强制 PAT，不在地址内嵌密码。
2. 保存后点「校验连接」。成功表示默认分支可读，不代表已读文件或允许执行/推送。
3. 在「运行 → 发起任务」选择项目及本次需要的多个仓库，选择已发布 Agent 版本，要求读取 README。
4. 详情分别查看固定资料、只读快照准备状态、Agent 回答和工具事实。实际读取以 `repo.file.read` 的仓库 ID、revision 与文件内容为准。

原字段 `credential_token` 为兼容保留，但承载 Git 密码或令牌；后端使用 HTTP Basic 的密码位，不校验令牌格式。表单只写、密码遮挡，重新打开不回填；普通资料保存不覆盖旧秘密，明确勾选才清除，修改地址清除旧秘密。

## 真实 Git 和模型证据

测试服务使用 `git http-backend`、自签专用 CA、HTTPS Basic 认证与独立裸仓，不使用网络 Mock。仅管理员放行 `https://127.0.0.1:19443` 并配置该 CA，保持 TLS 校验和禁止重定向；没有修改个人 Git/SSH/代理配置。

测试项目 `ba04e5d8-c3ef-5fbf-af5e-7b2fd2959a34`，名称「Git 多仓只读验收-20261020」。三个独立仓库中只选择前两个：

| 仓库 | 仓库 ID | 固定提交 | 文件标识 |
| --- | --- | --- | --- |
| 验收前端 | `8a249e0e-6354-522a-95e7-69e29101f344` | `22a583cfa60f3360e857bcf13414162b18e3c855` | `WEORA-README-WEB-20` |
| 验收后端 | `a2376c3b-6f05-5388-a104-9085ed6ac3ff` | `487a1087d4091484a901e833e4315ca75b85fcfa` | `WEORA-README-API-20` |
| 未选仓库 | `e181529f-dab8-5909-a39b-bc9251fb20d5` | 不物化到本次 Run | `WEORA-NEVER-READ-20` 不进入本次证据 |

成功 Run：**`07589580-8628-5d5a-9cda-fd10e209a7f6`**，Agent「多仓只读验收助手」0.1.0，冻结项目 v5。执行配置为 `pi-agent-core@1.3.0 / deepseek / deepseek-flash`，输出上限 2048 tokens、总时限 120 秒。

持久记录为 `SUCCEEDED`、序号 3；两个仓库 `READY`，只有两条 `repo.file.read`，均成功，参数显式包含不同 repository_id。工具与回答均包含两个真实 README 标识及固定提交。前端职责为 React/TypeScript 订单界面与退款错误提示，后端为 Python/FastAPI 及退款幂等键。模型指出 README 简短的证据缺口，没有宣称执行测试或读取其他代码。

工具返回的文件 SHA-256：前端 `a3c5bee997c30c18410042541bac4d86dd431bd83ac9bd32b35410924cf2ed5f`；后端 `ad5805c22b2b523ad254cff31476a887baef7b6821977391bf39653fa7ea5836`。

之前 Run `16ef973d-b6df-5d0b-8760-62915e3554bd` 已完成两个读取，但旧 768-token 配置导致 `MODEL_TRUNCATED`，保留失败事实。调整本机配置并重启 API/Worker/Beat 后重新发起上面的新 Run，没有改写旧结果；之后重输前端凭据推进连接版本，旧任务按撤权规则不再继续访问。

## 页面与修复

内置浏览器桥报告不可用，本次使用独立 Chrome/Playwright CLI，不声称操作用户原有 IAB 标签。

真实点击连接校验、密码写入和重新打开不回填均验证。运行刷新后仍保留同一回答和两条工具事实，展开真实结果可见 README、提交和 SHA-256；390px 窄屏无整页横向溢出。修复只读快照状态将 Semi Tag 放入 `p` 造成的非法 HTML 嵌套，改为独立 `div`；刷新后的独立验收页没有该嵌套错误，没有屏蔽 Semi 原有 `findDOMNode` 弃用告警。

忽略目录 `output/playwright/weora-repositories-20261020/` 保存本机夹具和验收证据，包括 `connection-verified.png`、`password-form.png`、`task-selection.png`、`readme-answer-desktop.png`、`readme-tool-evidence.png`、`readme-answer-mobile.png` 和成功/失败 Run 的 JSON。辅助 Cookie 仅临时用于验收，完成后删除；配置备份及测试证书私钥也不作为公开证据。

## 数据库与检查

本机目标先确认是 `127.0.0.1:55432/weora`。备份后由 0013 升级至 **`20261020_0014`**：

- 备份：`volumes/postgres-local/backups/weora-before-0014-20261020-105524.dump`，176448 字节。
- SHA-256：`1764b211bed637f48cbe15783003f3cad2a6e348806e8e44f9fc543b981d4e9a`。
- 仓库新增用户名、连接版本及连接校验元数据；新增 `project_run_repositories` 保存固定提交与准备状态。
- 独立临时 SQLite 升/降级和 ORM 一致性通过；真实 PostgreSQL `alembic check` 返回无新增操作。未对真实库做破坏性降级。

自动测试分组有重叠，不相加成“总测试数”：

- 项目/Git/运行/工具/迁移第一组 60 项通过；运行查询/恢复/Worker 生命周期另一组 28 项通过。
- 准备阶段心跳与取消、项目读取 API 三项通过，证明长时间准备不丢租约、取消后不进入模型。
- Git 安全测试最终 15 项通过，包括 Git 密码/令牌及特殊字符、无参数泄露、固定 IP、私网拒绝、固定 SHA、敏感/越界路径、符号链接、大小/二进制拒绝及取消清理。
- 前端最终全套 **60 项通过**（10 个文件）；TypeScript、ESLint、生产构建通过。
- 后端 Ruff、Mypy（src 与 projects 测试共 132 文件）通过。

开发期间曾出现测试资源并发启动超时及新增面板缺少 QueryClient 的真实失败；补齐测试 Provider、限制 2 workers 后全套通过。未将修复前失败结果算作通过。

## 保留边界

真实云效只读已在追加记录通过；SSH 和私有 GitLab 仍待各自环境验收。需要平台接受的 Git 认证信息，不能推断网页登录密码通用。缓存当前为逐克隆大小过程监测，不是硬隔离磁盘配额；全局配额、持久缓存保留/孤儿回收尚未实现。项目写操作仍阻断，不隐式使用旧 W08 默认仓库。

本次没有切换分支、暂存、提交、推送或创建远端评审，没有向飞书发送测试消息。迁移、`.env` 与服务重启只作用于上述本机测试环境。

## 追加：云效真实仓库只读通过（2026-10-20 11:35，Asia/Shanghai）

用户自行在 Console 保存凭据后，实际点击「校验连接」初次失败。通过受限探针将原始报文仅归类、不输出或落盘，确认无 `.git` 地址返回跨站 HTTP 302；同一仓库带 `.git` 的地址返回 HTTP 200 及 `application/x-git-upload-pack-advertisement`，Git `ls-remote` 可读。根因是网页地址与 Git 克隆地址不同，不是密码错误；未跟随跨站跳转，也没有关闭 TLS 或借用个人 Git 登录态。

修复仅对云效官方 HTTPS 主机规范 `.git` 后缀（含重复/末尾斜杠处理），保持通用 Git/GitLab 地址不变。保存、响应与后台读边界一致，旧记录无需用户再次填写密码；等价 URL 不推进连接版本或清除秘密，并在聚合锁内拒绝旧网页 URL 与新克隆 URL 重复登记。真实换地址仍清除旧凭据。前端补充克隆地址说明。

重启本机 API、Worker、Beat 后，真实页面连接校验成功；随后在 Console 选择云效项目、`example-java-service` 和已发布只读 Agent 0.1.0 并提交：

- 成功 Run：`d96ffe3a-8a29-50ea-b5e1-6aefcce47077`，状态 `SUCCEEDED`。
- 仓库 ID：`595fbd8a-46c7-5f04-ba64-58c688973740`；默认分支 `master`。
- 固定提交：`example-commit`；准备状态 `READY`。
- 一条 `repo.file.read` 成功，路径 `README.md`，返回 1784 字符；未请求其他文件或任何写工具。
- README SHA-256：`e0338e89b1f068759011e8990a96df91ab7562f801ee679c175f87ef8f6257b2`。
- Pi/DeepSeek 返回 1066 字符的完整回答；固定提交和工具事实由持久记录核对，页面刷新后仍可见。

证据：`cloud-connection-verified.png`、`cloud-readme-answer.png`、`run-d96ffe3a-8a29-50ea-b5e1-6aefcce47077.json`，仅保留在本机验收目录。该目录新增忽略规则，防止私有 README、认证状态或测试配置进入 Git；公开文档只记录技术元数据，不复制私有正文或凭据。

回归：先观察到 URL/真实调用测试失败，再修复；旧地址密码与版本保留/规范化判重测试亦先失败后通过。最终 API 与 Git 定向 **23 项通过**，前端项目 **7 项通过**，Ruff、Mypy（133 文件）、TypeScript、ESLint 通过。较早大组测试有一条夹具漏传用户名导致的失败，补齐后单测及上述定向组通过，不把旧失败输出改写成整组通过。

结论仅覆盖当前授权云效仓库的 HTTPS 只读链路；W10 推送、MR/PR、审批交付和跨仓改码不因此视为完成。没有创建提交、切换分支或修改远端状态。
