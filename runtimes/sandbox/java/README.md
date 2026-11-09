# Java / Maven 沙箱

`Dockerfile` 提供 JDK 17 和 Maven；只有 `mvn --version` 成功不能证明项目构建通过。

## 准备与执行分开

1. 从项目当前授权的固定 Git 提交导出归档，沿用平台的路径、类型、大小和 SHA-256 检查。
2. 管理员使用独立联网容器和 `settings.public.xml` 准备 Maven 依赖。模板使用本项目已声明的阿里云公共 Maven 镜像，只放入待构建源码，不继承宿主的 Maven、SSH、Git 或云效 API 凭据；换源前应做可达性与速度验证，不能因下载慢就修改依赖版本。
3. 准备命令使用固定插件版本：`mvn -B -ntp -s /maven/settings.xml -Dmaven.wagon.rto=60000 -Dmaven.wagon.http.retryHandler.count=3 org.apache.maven.plugins:maven-dependency-plugin:3.8.1:go-offline`。本机验收可加 `-T4` 并行准备；外层使用 `timeout --kill-after=5s 850s` 限制整个准备进程，不能只限制 Docker 客户端而遗留后台 Maven。失败后按已核实原因重试，不能无限循环或跳过缺失依赖。
4. 依赖准备成功不代表构建成功。先断网执行一次完整构建，把明确缺失的注解处理器、测试运行器和实际传递依赖补齐，再导出缓存到专用构建上下文。源文件全部设为 root 所有、文件 `0444`、目录 `0555`；`dependencies.Dockerfile` 只用一次 COPY，继承 Java 基础镜像的非 root 用户。上下文不能包含源码与认证配置。
5. 在新的断网容器执行 `mvn -o -B -ntp -s /opt/weora/maven/settings.xml clean verify`，不跳过测试、不调用 `deploy`。生命周期动态使用的依赖若仍缺失，应回到独立准备阶段补齐，不能给执行容器打开网络。

离线缓存只读；项目源码和构建输出位于该 Run 的临时工作区。保留镜像 ID、输入提交、依赖清单、命令、退出码、测试报告和产物哈希。依赖版本变化后重新准备，不借用其他项目的缓存并宣称可复现。

构建上下文布局如下；两处 `settings.xml` 都是无凭据的 `settings.offline.xml`。`/etc/maven/settings.xml` 让普通 `mvn` 命令默认找到离线缓存，不要求 Agent 临时猜配置路径。

```text
Dockerfile
rootfs/
├── etc/maven/settings.xml
└── opt/weora/maven/
    ├── repository/
    ├── settings.xml
    └── manifest.json
```

导出 tmpfs 缓存时可在容器内用 Python 流式写出 tar，再在管理员准备目录校验路径、成员类型和大小后解包；不通过宿主目录挂载共享缓存。清单记录每个文件的大小、SHA-256、固定源码提交和基础镜像；临时下载、锁和失败标记不封入镜像。`go-offline` 不保证注解处理器、Surefire 动态 provider 或跨模块版本仲裁后的所有依赖齐全，必须以真正断网的 `clean verify` 收口。

## 私服与凭据

公共源模板不含 `<servers>`。项目有私服时，由管理员明确登记可信 HTTPS 源和单独的 Maven 只读身份；不能把 Git 密码或云效 API 令牌当作 Maven 密码。认证应只存在于可信依赖代理/准备设施中，不能进入 Agent 可访问的构建容器、镜像层、事件或日志。当前模板不代表已实现通用私服凭据管理。

`mirrorOf=*` 只约束 Maven 仓库解析，不是通用网络防火墙；因此联网准备容器不运行项目测试，也不携带私密凭据。执行容器的网络隔离由 Docker `--network none` 保证。

## 项目资源预算

部署管理员设置 `WEORA_PROJECT_SANDBOX_IMAGE` 和 JSON 格式的 `WEORA_PROJECT_SANDBOX_RESOURCES`。Java 多模块项目可使用：

```json
{"memory_mb":3072,"cpus":2,"pids":256,"timeout_seconds":600,"output_bytes":262144,"workspace_mb":1024,"temporary_mb":256}
```

预算跟随固定镜像，并在 Run 首次绑定时冻结。旧默认镜像继续使用原预算。增加 CPU、内存和时长不会授予网络、宿主目录挂载或特权；模型也没有修改这些配置的工具。

本机项目的真实结果单独记录在 `docs/development/`，不能用这里的操作步骤代替执行证据。
