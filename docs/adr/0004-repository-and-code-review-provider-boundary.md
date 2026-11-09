# ADR 0004 Git 操作与代码托管平台解耦

- 状态：Accepted。
- 日期：2026-10-19。
- 决策依据：用户明确要求支持当前云效与未来自建 GitLab，并批准按可替换接入层实施。

## 决策

平台保持身份、审批、持久业务键与未知结果恢复的所有权。Agent 使用既有 Tool 契约，不提交平台类型、URL、凭据或连接指纹。

1. `GitPublisher` 负责从固定基准发布批准的改动；`CodeReviewProvider` 负责供应商评审能力与只读核对。`RepositoryDelivery` 组合两者，不把 Git 操作误认为托管平台 API。
2. `RepositoryRegistry` 按工作空间和登记远端选策略，不做整个部署的供应商单选，不把 `if codeup/gitlab` 扩散到业务服务。注册表启动后不可变，新增插件在装配处注册。
3. `RepositoryConnection` 记录明确的 provider、实例地址、仓库、目标分支、连接版本及凭据引用。提案时平台将连接指纹写入 `PlanScope.remote_binding`，参与审批摘要，存入现有 JSON scope，不新增数据库列。
4. 执行和 UNKNOWN 核对均只使用原指纹对应的连接。连接迁移、仓库/地址/凭据引用变化后拒绝改用同名新远端。原连接不可用时保留未核对状态；新写入需要新计划与重新批准。
5. 不具备草稿能力时，在 Git 发布前返回 `CAPABILITY_UNSUPPORTED`，不偷偷创建正式评审，不靠标题加 WIP 冒充草稿，不改变用户当前仅草稿的授权范围。
6. 凭据由后端显式引用解析。HTTPS 禁止 URL 内嵌认证，SSH 使用专用密钥和 pinned known_hosts；不继承个人 credential helper、SSH agent、Git 配置或代理，不向模型、沙箱或普通 API 输出秘密。

## 当前实现范围

- 通用 CLI Git 发布器使用临时裸库、固定 revision、plumbing 命令写入索引/提交，不 checkout、不执行 hooks/过滤器、不修改用户工作目录。只能向业务键对应的新分支推送，空期望 lease 拒绝覆盖；推送后只读核对远端 ref。
- 云效首个适配器实现新版 OAPI 的中心版/Region 版只读分页核对；创建接口未文档化草稿参数，因此创建保持禁用。这表示适配器尚未验证该能力，不断言云效产品不存在任何草稿功能。
- 原 GitHub App 适配器作为兼容策略进入注册表，保留既有 Git Database API 发布实现和回归，不冒充已经迁移到通用 Git CLI。
- GitLab 本轮没有空实现，也没有宣称已联调。用测试策略替换验证编排层不依赖云效；将来新增真实 Adapter、能力声明、装配与契约测试。
- 首版执行节点仍沿用原单工作空间、单沙箱快照部署约束；多连接登记不是多租户执行节点已经全部验收，也不新增仓库管理前端、自动克隆归档或任意 Git Tool。

## 兼容与取舍

未绑定历史计划保持原 scope 摘要，旧记录不回写。启用注册表后，这些旧计划不能自动获得当前远端权限；如需继续远端写入，重新提案并审批。UNKNOWN 的旧计划不能仅为恢复而重绑新平台。

注册表保存部署级连接而不是新增动态配置后台，降低第一版实现范围；后续如增加管理界面，仍必须遵循版本冻结、租户隔离和凭据引用契约。此决策不承诺远端 exactly-once，也不修改 `git.commit`、`git.push`、`pr.create` 分别批准的要求。

## 官方协议来源

- [云效创建合并请求](https://help.aliyun.com/zh/yunxiao/developer-reference/createchangerequest-create-merge-request)：本轮核对的新版请求字段没有草稿参数。
- [云效查询合并请求列表](https://help.aliyun.com/zh/yunxiao/developer-reference/listchangerequests-query-the-list-of-merge-requests)：认证头、中心版/Region 路径、分页响应头及评审字段。

远端真实账号、项目权限与版本差异必须另做验收，不能以协议替身测试代替。
