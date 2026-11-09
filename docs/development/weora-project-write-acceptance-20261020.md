# 云效项目审批、Java 沙箱与分支推送真实验收

后续：同日已使用独立 API 身份完成普通 MR 创建与异常恢复，见 MR 示例 验收记录（内部记录已移除）。本文“未创建 MR”保留最初分支验收时点，不再表示后续状态。

后续：同日已完成固定依赖镜像下的完整 Maven 断网构建，19 模块、现有 22 项测试通过；见 Maven 验收记录（内部记录已移除）。本文独立 Java 冒烟仍保留原验收范围，不将两次验证冒充同一个 Agent Run。

日期：2026-10-20，Asia/Shanghai。本记录承接只读验收；实现决策见 [ADR 0008](../adr/0008-selected-repository-approved-sandbox-and-branch.md)。

## 授权与边界

用户明确允许对 `example-java-service` 创建专用测试分支、推送仅含独立验收文件的提交和创建不合并的 MR；不修改 master、不合并、不部署。实际本轮完成到测试分支推送，未创建 MR：用户提供的是 SSH 密钥信息，尚无项目评审 API 身份配置，Git 身份不等于云效 API 授权。

没有切换、暂存、提交或推送 Weora 源码仓库。真实远端写入仅发生在获准的目标仓库专用分支，不修改业务文件。未检索、复制或更改用户个人 SSH 私钥。

## 本机配置与镜像

目标数据库仍为 `127.0.0.1:55432/weora`，Redis 为本机 6379/0。本轮没有新增迁移。本机 `.env` 启用项目沙箱、将模型调用超时设为 300 秒，并配置独立 Java 镜像；只重启经命令行核验的 Weora API、Worker、Beat，没有重启共享 Docker daemon。

- 镜像：`sha256:ed818f8203eb12ecf2fe572c535d133c2aadb7409ccdd538cf0727df33ba3b78`。
- 来源：管理员核验的本地 Python 基础镜像，使用 `runtimes/sandbox/java/Dockerfile` 构建。
- JDK：17.0.20.1；Maven：3.8.7。
- 原 Python/Node/浏览器沙箱镜像配置保持不变。

首次使用大基础镜像构建在元数据步骤停滞。通过 Docker 状态、资源与限定探针诊断后，仅停止本次构建进程，改用轻量本地基础镜像；第二次完成下载、安装及版本校验。没有删除其他镜像或容器。构建阶段需要网络，执行容器仍无网络、非 root、只读根文件系统和资源限制。

## 真实 Console → 模型 → 审批 → Docker → Git

- 项目：`bc8a5529-5588-53ba-b221-846bb786299f`。
- 仓库：`595fbd8a-46c7-5f04-ba64-58c688973740`。
- Agent：`995b70a0-d508-5faf-a989-88d89c289689`，云效Java沙箱验收助手 0.1.0。
- Run：`9948570f-12d7-5d5b-9687-3d39c11a3c06`，最终 `SUCCEEDED`。
- 审批计划：`d8f48a1a-4ba8-5d79-854e-99451a01004c`，`APPROVED`。
- 固定基准：`example-commit`。

通过独立验收浏览器实际选择项目、仓库、Agent 版本并提交。Pi/DeepSeek 调用 `repo.file.read` 成功读取 README（1784 字符，哈希与此前真实只读验收一致），随后 `plan.propose` 使运行进入 `WAITING_APPROVAL`；此时没有写产物或交付记录。

核对路径、三条命令、远端及指纹后，在 Console 实际点击“审阅并批准”、勾选范围确认并提交决策。只有随后模型恢复执行：

1. 新增 `weora-acceptance/WeoraSmoke.java`；之前已从固定提交确认该路径不存在，文件含中文验收注释。
2. `javac -d /tmp/weora-classes weora-acceptance/WeoraSmoke.java`：`PASSED`，exit 0。
3. `java -cp /tmp/weora-classes WeoraSmoke`：`PASSED`，exit 0，输出 `WEORA-JAVA-SMOKE-20261020`。
4. `mvn --version`：`PASSED`，exit 0，确认 JDK/Maven 版本。
5. `sandbox.diff` 通过批准路径检查，再执行 `delivery.branch.push`。

八条 Tool 调用全部成功；三条命令绑定同一最终仓库状态哈希 `c03f95ce9068cbc8d8b2c7d031a5afdd0c1739e863c1a3680fead25727a5b4ac`。平台完成门槛再检查真实写入、命令、Diff 与已确认的分支记录，不只依据模型回答。模型最终回答 1426 字符，执行容器已按所有权清理，真实 `docker ps -a` 无该 Run 容器。

## 远端独立回读

- 分支：`weora/example-run`。
- 提交：`example-commit`。
- 唯一父提交：上述固定基准。
- 唯一变更：`weora-acceptance/WeoraSmoke.java`。
- 文件 SHA-256：`f100d46b4f7713b478fa157bd70f4706c892954881043642152cb4c7c8279280`。

使用登记凭据经受限 Git 网络环境，只读查询真实远端分支并在临时裸库 fetch 后核对父提交、变更路径和验收标识。master 在交付前后及独立核对前后仍为固定基准；没有合并、部署或创建 MR。不是用模型自述或本地 Mock 代替远端证明。

## 修复与自动验证

补齐选中项目固定归档到隔离沙箱的装配桥、项目专用镜像、独立分支 Tool、持久化幂等交付与前端分支/MR 区分。修复无项目旧测试沙箱被新 prepare hook 绕过的问题，使用显式布尔返回保留原行为；失败夹具修正后重新验证，不把旧失败算作通过。

测试分组有重叠，不相加为总数：Delivery/Projects/API/Worker 大组 194 项通过；Git 发布与项目桥接/分支组 16 项通过；最新运行/工具/项目桥/分支受影响组 33 项通过；项目镜像专项 3 项通过。前端全套 61 项通过，追加分支未知状态用例后 DeliveryPanel 定向 14 项通过；TypeScript、ESLint、生产构建通过，保留 Semi 原有 `findDOMNode` 弃用告警；后端 Ruff、Mypy 通过。Fake Docker 和本地 Git 测试不冒充上面的真实外部验收。

页面刷新后仍显示成功状态、真实 Java 证据和分支交付。实际展开命令产物，正文包含验收标识，证据 ID 与后台一致。修复分支名/提交号挤在一行的可读性问题，分行展示，并明确“不是 MR/PR”；链接仅打开仓库，不冒充直达 MR/分支页面。桌面与 390px 窄屏截图通过，窄屏文档宽度为 390px，无整页横向溢出。服务重启窗口曾有两条 504，恢复后新任务与刷新正常；未把历史错误日志删除或解释为零错误全程。

## 未完成范围

本轮不是业务项目完整 Maven 构建：尚未准备业务依赖缓存、私服凭据和受控依赖下载。也没有环境选择页面、跨仓写入、SSH 真实验收或动态项目 MR 配置。MR 后续需要单独的云效 API 身份/仓库配置与真实验收，不能只换一个 Git 密钥就声称支持全部代码托管能力。

本机忽略目录 `output/playwright/weora-repositories-20261020/` 保存 Run 持久记录、`branch-readback.json`、页面截图和辅助脚本；公开文档只保留技术元数据，不复制私有 README 或凭据。
