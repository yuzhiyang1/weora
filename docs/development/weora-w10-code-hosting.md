# W10 可替换代码托管接入

2026-10-20 13:43 后续：用户提供 API 文件引用后，普通 MR-only 独立审批、真实云效 MR 示例（链接已脱敏）、异常只读核对与模型恢复通过，未合并；见 MR 验收记录（内部记录已移除）。普通 MR 使用独立 `review.create` 动作与 Tool，不放宽原 `delivery.pr.create` 的草稿能力要求。下方“MR 未完成”保留当时切片范围，最新状态以本条为准。

2026-10-20 后续：选中项目仓库的真实 Console 审批、Java 沙箱和云效专用测试分支推送已验证，master 未变；见 [项目写入验收](weora-project-write-acceptance-20261020.md) 和 [ADR 0008](../adr/0008-selected-repository-approved-sandbox-and-branch.md)。动态项目 MR API 配置与真实创建仍未完成，W10 不整体勾选。下方“没有真实推送”是 10-19 原验收范围，不再代表最新分支能力。

- 日期：2026-10-19。
- 决策：[ADR 0004](../adr/0004-repository-and-code-review-provider-boundary.md)。
- 状态：通用发布/连接冻结/云效只读适配已实施；没有使用真实云效凭据，没有远端推送或创建评审。云效草稿创建仍禁用，W10 不整体勾选。

## 职责与入口

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| `DeliveryService` | 人工审批、精确作用域、Run 授权、业务键、UNKNOWN 状态 | 选择云效/GitLab API |
| `RepositoryRegistry` | 工作空间内远端路由、平台注入连接指纹、执行/核对时校验 | 从模型接收 URL/凭据 |
| `RepositoryDelivery` | 检查草稿能力，组合 Git 发布与平台评审 | 不支持时降级正式评审 |
| `CliGitPublisher` | 固定基准、批准路径、临时裸库、专用分支、远端 ref 核对 | checkout、执行仓库代码、覆盖目标分支 |
| `CodeupReviews` | 云效授权头、中心版/Region 路径、分页和原评审核对 | 隐式创建正式评审或重推 |

现有 Tool 名不变；GitLab 的策略位置已经通过替身契约验证，不表示真实 GitLab Adapter 已实现。原 GitHub 实现保留兼容，尚未改用通用 CLI 发布器。

## 管理员配置

只在本机安全配置中填写，不在聊天或运行指令里粘贴凭据，不输出整个 Settings 或 `.env`。配置错误不回显原始输入。

`WEORA_HOSTING_CONNECTIONS` 是 JSON 数组。一个连接示例（地址、UUID、仓库 ID 均需换成指定测试目标）：

```json
[
  {
    "workspace_id": "00000000-0000-0000-0000-000000000001",
    "remote_id": "team-codeup",
    "version": "v1",
    "provider": "codeup",
    "repository_id": "test-repository",
    "platform_repository": "123456",
    "clone_url": "https://codeup.example.test/team/test-repository.git",
    "api_origin": "https://api.example.test",
    "web_url": "https://codeup.example.test/team/test-repository",
    "credential_ref": "team-codeup-v1",
    "target_branches": ["main"],
    "organization": "organization-id"
  }
]
```

- `repository_id` 必须对应平台登记的沙箱快照，不因填写 URL 就授权任意仓库。
- `platform_repository` 第一版使用云效数字代码库 ID，不支持用逗号扩成多库查询。
- `api_origin` 是无路径、尾部斜杠、用户信息、查询参数的 HTTPS origin；从指定云效实例的官方接入点取得，不能猜域名。`web_url` 是原仓库的固定页面地址。
- 中心版填写组织 ID，Region 版 `organization` 留空。HTTP 不重定向，不使用环境代理。
- 可以登记多个连接，甚至不同工作空间相同的 remote_id；同一工作空间重复 ID 拒绝启动。首版沙箱仍受原执行工作空间/快照限制。

`WEORA_HOSTING_CREDENTIALS` 是“引用→秘密 JSON 字符串”的映射，通过配置密钥注入；示意结构：

```json
{
  "team-codeup-v1": "{\"username\":\"专用测试账号\",\"password\":\"仅本机填写\",\"api_token\":\"仅本机填写\"}"
}
```

API PAT 与 Git 密码可以不同，不宣称静态 PAT 是短时凭据。引用不在映射中时明确失败，不读取用户已有登录态。轮换身份或迁移连接时使用新 version/credential_ref，重新提案和批准；同一身份的秘密轮换也需确认仓库授权仍匹配。

SSH 克隆地址使用 `ssh://git@固定主机/路径.git`；秘密结构中指定现存绝对路径 `ssh_key_path`、`known_hosts_path`，并另配 `api_token`。不使用个人默认密钥、不关闭 host key 校验。SSH 认证和 HTTPS 认证配置边界均有测试，但尚未以真实远端验证。

## 启用与恢复

1. 提供指定的非生产测试仓库、接入点、组织/仓库 ID 和本机凭据引用；只读检查登记信息。
2. 配置更改后按正常维护流程重启 API/Worker，使审批与执行看到同一不可变注册表。不要只更新一个进程后执行旧批准。
3. 先验只读连通与权限；云效当前 `draft=false`，`delivery.pr.create` 会在发布前拒绝，既不会推分支，也不会创建正式评审。
4. 后续验证可用的草稿协议，或由用户另行批准调整交付范围后，再实施真实写入验收；不能因为“提供了凭证”推断可以正式评审或合并。
5. UNKNOWN 只读核对原业务键、源/目标仓库、分支、草稿状态和固定页面 URL；分页预算不足、歧义或原连接变化都保留待核对，禁止盲目重发。

## 验证与限制

新增测试覆盖策略替换、跨工作空间/仓库/分支拒绝、旧批准不漂移、旧摘要兼容、指纹持久化、API 拒绝伪造绑定、草稿不支持时零 Git 副作用、云效两种路径和分页/歧义/URL 检查，以及真实 Git 临时裸库的内容、删除、文件位、固定父提交、分支保护、超时/输出/清理边界。

真实 Git 测试仅在测试传输替身中把固定 URL 映射至临时本地裸库并开放 file 协议；生产实现仅允许 HTTPS/SSH。不会修改用户仓库或访问外部云效/GitLab。

本机默认 pytest 临时根目录存在既有 ACL 拒绝，验收使用 `output/` 下全新 UUID 的独立 `--basetemp`，没有删除或修改旧临时根目录权限。自动验证结果见当前进度文档，不能把测试替身称为真实供应商联调。

本轮受影响交付/配置回归 156 项通过（126.24 秒），将未关闭资源告警作为失败；追加装配及进程边界 12 项通过（20.64 秒）。此前复现的 Windows 超时后管道 transport 遗留已修复，并补“不读大 stdin 的子进程也能按时终止”测试；最后一次进程边界严格复测 11 项通过（6.33 秒），追加云效 HTTP 边界 22 项通过（0.46 秒）。这些集合有重叠，不相加。最终 Mypy 178 文件、Ruff 通过。前端未改，不重复声称执行此前页面验收。

全后端非 integration 扩展回归 451 项通过，26 项外部集成未运行，耗时 614.42 秒；JUnit 在忽略目录 `output/w10-code-hosting-tests.xml`。该进程在管道修复前已启动并加载旧实现，保留两条 Windows 管道回收告警，不能称为最终代码的无告警全量验证；修复后的受影响进程路径已用严格告警条件单独复测。该轮其余告警是 SDK protobuf 的时间弃用及既有 Alembic 配置弃用，未屏蔽。后加入的装配/HTTP 用例在上述追加验证中执行，不冒充已包含在这 451 项中。
