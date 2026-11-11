# Pi 与 OMP 原生编码工具

此派生镜像把官方 SDK 安装在受管沙箱内。Pi 使用 `@earendil-works/pi-coding-agent@1.3.0`，OMP 使用 `@oh-my-pi/pi-coding-agent@18.16.0` 和 Bun 1.4.4。镜像构建时装好依赖，运行时沿用 Weora 沙箱的断网、非 root、只读根文件系统和资源限制。

| 运行时 | 沙箱内原生工具 |
| --- | --- |
| Pi | `read`、`ls`、`find`、`grep`、`edit`、`write`、`bash` |
| OMP | `read`、`glob`、`grep`、`edit`、`write`、`bash`、`ast_grep`、`ast_edit` |

仓库调研任务可直接使用 Weora 的仓库检索工具。原生工具作用于已准备的任务沙箱，适合读取改动后的文件、做结构化搜索、精确编辑和执行测试。OMP 的 LSP、浏览器、联网、子 Agent 与记忆工具各有额外服务和权限，需要单独接入。

## 构建和启用

选用已验证的 Debian 系 Weora 沙箱镜像，以及 Node 22.19 或更新版本的官方镜像。Node/npm 从官方镜像复制，Python、Git 和项目构建依赖继承自沙箱基础镜像。下面的变量需指向管理员核验的镜像引用。

```powershell
docker build --build-arg "SANDBOX_IMAGE=$verifiedSandboxImage" --build-arg "NODE_IMAGE=$verifiedNodeImage" -t weora-sandbox:native-tools runtimes/sandbox/coding-tools
docker image inspect weora-sandbox:native-tools --format '{{.Id}}'
docker run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev --env HOME=/tmp weora-sandbox:native-tools node /opt/weora-coding-tools/smoke.mjs
```

将完整镜像 ID 配置到对应沙箱的 `WEORA_SANDBOX_IMAGE` 或 `WEORA_PROJECT_SANDBOX_IMAGE`，再设置 `WEORA_SANDBOX_NATIVE_TOOLS_ENABLED=true`。沙箱镜像没有执行器时，调用会失败并记录实际退出状态。

## 审批和调用

在既有 `plan.propose` 的 `commands` 中登记原生调用的完整 argv，并包含 `command.run`。例如 Pi 读取沙箱内的 README：

```json
["/usr/local/bin/weora-native-tool", "pi", "read", "{\"path\":\"README.md\"}"]
```

Pi 运行只获得 `sandbox.pi`，OMP 运行只获得 `sandbox.omp`。审批后传入 `tool` 和与计划完全相同的 `arguments_json`，运行时由平台根据本轮运行配置核对。编辑和写入还需要 `file.write`，并且只能操作批准的路径。参数沿用上游工具，Pi 的 `edit` 使用 `path` 与 `edits: [{oldText, newText}]`，OMP 的 `edit` 使用 `path`、`old_string`、`new_string`。OMP 的 `ast_edit` 使用 `paths` 与 `ops: [{pat, out}]`。原生查找和编辑均保留审计记录，验证命令仍需在修改后运行。
