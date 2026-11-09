# Weora 对象存储基建模块设计

## 文档信息

- 状态：核心实现完成，已通过真实 OSS 联调
- 日期：2026-08-21
- 对应 ADR：[`ADR-0002`](../adr/0002-provider-neutral-object-storage.md)
- 第一接入目标：阿里云 OSS
- 演进目标：AWS S3、MinIO、其他 S3 兼容对象存储

## 1. 结论

对象存储不是独立业务领域，而是多个领域共享的基础设施能力。供应商无关的稳定契约位于
`shared/object_storage`，具体供应商适配器位于 `infrastructure/object_storage`。一个小而深的异步
Port 负责隐藏供应商 SDK、Endpoint、签名、寻址方式、重试、流关闭和异常映射。

第一版采用：

```text
业务模块
   ↓
ObjectStoragePort
   ↓
ObjectStorageFactory
   ├── aliyun_oss -> AliyunOssObjectStorage（首期）
   └── S3 Compatible（后续按契约测试启用）
       ├── aws_s3
       ├── minio
       └── custom_s3
```

Provider 在应用启动时由配置选择。运行中不动态热切换，也不在单次请求中自动降级到另一个 Provider。
“配置切换”表示业务代码不变，不表示未实现的 Provider 已经可用，也不表示历史对象会自动迁移。

## 2. 变更边界

本模块负责：

- 将可重读的数据源写入私有 Bucket。
- 流式读取对象。
- 查询对象大小、内容类型、应用校验值和更新时间。
- 在同一 Provider 内复制对象。
- 幂等删除对象。
- 将供应商异常映射为稳定错误。
- 提供连接、重试、指标和追踪。
- 在应用 Readiness 中验证 Bucket 访问权限。

本模块不负责：

- 决定业务对象键。
- 管理 Skill、知识库或运行产物的生命周期。
- 创建 Bucket、修改 ACL 或配置生命周期规则。
- 自动迁移不同 Provider 之间的历史对象。
- 数据库事务。
- 病毒扫描、文件解析和业务格式校验。
- 自动公开对象或生成永久 URL。

## 3. 为什么不是“一个 OSS 工具类”

直接包装几个 SDK 方法看起来简单，但调用方仍然需要知道：

- 使用哪个 Endpoint。
- OSS 要求 virtual-hosted，MinIO 常用 path-style。
- 哪些异常应该重试。
- 如何关闭响应流。
- ETag 能否当作 MD5。
- 上传是否能安全重试。
- Provider 返回的 403、404 和 5xx 应如何解释。

这样的包装只是把 SDK 调用移到另一个文件，并没有减少调用方必须理解的信息。

`ObjectStoragePort` 的价值是让业务调用方只处理：

- 逻辑对象键。
- 可重读数据源。
- 内容长度和媒体类型。
- 应用计算的 SHA-256。
- 稳定的成功结果或错误类型。

## 4. 公共模型

### 4.1 ObjectKey

逻辑对象键是带校验的值对象：

- 使用 `/` 分段。
- 不允许前导 `/`、尾随 `/`、空段、`.`、`..`、反斜杠和控制字符。
- 不允许由用户原始文件名直接拼接。
- 最大长度 1,024 字节。
- 不包含 Bucket、Endpoint 或 Provider 名称。

业务模块负责生成自己的命名空间，例如：

```text
skills/{workspace_id}/{skill_id}/versions/{version}/{content_sha256}.skill
knowledge/{workspace_id}/{dataset_id}/sources/{document_id}/{content_sha256}
runs/{workspace_id}/{run_id}/artifacts/{artifact_id}
```

Adapter 会在物理写入前统一增加部署前缀：

```text
{configured_prefix}/{logical_key}
```

推荐前缀为 `weora/{environment}`。

### 4.2 ReplayableObjectSource

上传源必须能够重新打开，以便 SDK 在瞬时失败后安全重试：

| 字段或行为 | 含义 |
| --- | --- |
| `open()` | 每次返回从头读取的新二进制流 |
| `content_length` | 精确字节数 |
| `content_type` | 标准媒体类型 |
| `sha256` | 由应用计算的十六进制 SHA-256 |
| `filename` | 仅用于诊断和下载建议，不参与对象键 |

第一版提供两种内部实现：

- 临时文件数据源，用于 FastAPI 上传和规范化产物。
- 内存数据源，用于小对象和测试。

Port 不接受只能消费一次的裸流，避免自动重试时上传空内容或部分内容。

### 4.3 ObjectMetadata

稳定元数据包括：

- 逻辑对象键。
- 内容长度。
- 内容类型。
- 应用 SHA-256。
- 最后修改时间。
- Provider 返回的 ETag，仅用于诊断。

ETag 不能作为内容完整性依据。阿里云官方说明 OSS 与 S3 在 PUT 大小写和分片算法上都可能不同。

### 4.4 ObjectRead

读取结果同时提供：

- `ObjectMetadata`
- 异步字节流
- 显式关闭能力

API 层必须在客户端断开、读取完成或发生异常时关闭底层响应流。SDK 响应对象不能泄漏到业务模块。

## 5. ObjectStoragePort

概念接口如下：

| 操作 | 语义 |
| --- | --- |
| `put(key, source)` | 完整写入一个对象，并校验响应元数据 |
| `read(key, byte_range?)` | 返回可关闭的异步对象流 |
| `stat(key)` | 返回对象元数据；不存在返回 `None` |
| `copy(source_key, target_key, expected_sha256)` | 在同一 Provider 内复制并校验目标 |
| `delete(key)` | 幂等删除，不存在也视为成功 |
| `probe()` | 检查 Bucket 访问和基础操作权限 |

接口刻意不暴露：

- Bucket。
- Endpoint。
- Region。
- ACL。
- Storage Class。
- Multipart Upload ID。
- SDK Request 或 Response。
- OSS、AWS 或 MinIO 异常。

## 6. 适配器与装配

### 6.1 使用的设计模式

#### Ports and Adapters

Application 依赖 `ObjectStoragePort`，Infrastructure 实现它。领域模型不导入 SDK。

#### Adapter

`AliyunOssObjectStorage` 首期将统一对象语义转换为阿里云 OSS Python SDK V2 调用，并完成错误翻译。
后续的 `S3CompatibleObjectStorage` 负责已通过契约测试的 AWS S3、MinIO 和兼容服务。

#### Strategy

不同 Provider Adapter 决定 SDK、Endpoint、Region、寻址方式和认证装配，但不改变业务接口。

#### Simple Factory

启动装配函数读取配置并从显式映射中创建唯一实例。未知或尚未实现的 Provider 立即启动失败，
无需为工厂再设计类层次。

### 6.2 Provider 与 Adapter

| Provider | Adapter | 状态 | 说明 |
| --- | --- | --- | --- |
| `aliyun_oss` | `AliyunOssObjectStorage` | 首期实现 | 使用阿里云官方 Python SDK V2 |
| `aws_s3` | `S3CompatibleObjectStorage` | 后续 | 使用 `boto3`，完成真实契约测试后启用 |
| `minio` | `S3CompatibleObjectStorage` | 后续 | 使用 `boto3`，完成 Testcontainers 契约测试后启用 |
| `custom_s3` | `S3CompatibleObjectStorage` | 后续 | 必须逐服务完成真实契约测试 |

供应商能力不通过公共参数暴露给业务调用方。即使多个 Provider 共用
`S3CompatibleObjectStorage`，寻址方式和签名配置也由 Adapter 内部的 Provider 配置确定。

### 6.3 异步边界

阿里云 OSS Python SDK V2 从 `1.2.0` 开始提供原生异步客户端，首期直接使用
`alibabacloud_oss_v2.aio.AsyncClient`：

- Port 保持异步。
- 下载响应按固定块大小从 SDK 异步流读取。
- `ObjectRead` 在完成、异常和取消时显式关闭 SDK Body。
- FastAPI lifespan 关闭 Adapter，从而释放 SDK 连接池。
- SDK 没有 `py.typed` 标记，因此 Adapter 定义只包含实际调用方法的最小 Protocol。

后续 `S3CompatibleObjectStorage` 如果使用同步 `boto3`，线程池桥接只存在于该 Adapter 内，
不改变公共 Port。

## 7. 配置

### 7.1 应用配置

建议增加以下启动配置：

| 配置 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `WEORA_OBJECT_STORAGE_PROVIDER` | 是 | `disabled` | 首期只接受 `disabled/aliyun_oss`；其他值随 Adapter 启用 |
| `WEORA_OBJECT_STORAGE_BUCKET` | 启用时 | 无 | 预先创建的私有 Bucket |
| `WEORA_OBJECT_STORAGE_REGION` | OSS/AWS | 无 | Region |
| `WEORA_OBJECT_STORAGE_ENDPOINT` | OSS/MinIO/Custom | 无 | 不包含 Bucket 的 Endpoint |
| `WEORA_OBJECT_STORAGE_ACCESS_KEY_ID` | 视凭据链 | 无 | 使用静态凭据时提供 |
| `WEORA_OBJECT_STORAGE_ACCESS_KEY_SECRET` | 视凭据链 | 无 | Secret 类型，不进入日志 |
| `WEORA_OBJECT_STORAGE_SECURITY_TOKEN` | 否 | 无 | STS 临时凭据 |
| `WEORA_OBJECT_STORAGE_PREFIX` | 否 | `weora/{environment}` | 物理对象公共前缀 |
| `WEORA_OBJECT_STORAGE_USE_CNAME` | 否 | `false` | Endpoint 为已绑定 OSS 的自定义域名时启用 |
| `WEORA_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS` | 否 | `5` | 连接超时 |
| `WEORA_OBJECT_STORAGE_READ_TIMEOUT_SECONDS` | 否 | `30` | 读取超时 |
| `WEORA_OBJECT_STORAGE_MAX_ATTEMPTS` | 否 | `3` | 总尝试次数 |

`disabled` 是显式状态，用于对象存储功能尚未启用的开发阶段。任何依赖对象存储的业务模块启用后，
缺少有效配置必须在启动或 Readiness 阶段失败，不能静默写入本地磁盘。

### 7.2 阿里云 OSS 示例

```dotenv
WEORA_OBJECT_STORAGE_PROVIDER=aliyun_oss
WEORA_OBJECT_STORAGE_BUCKET=weora-private
WEORA_OBJECT_STORAGE_REGION=cn-hangzhou
WEORA_OBJECT_STORAGE_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
WEORA_OBJECT_STORAGE_PREFIX=weora/development
WEORA_OBJECT_STORAGE_USE_CNAME=false
WEORA_OBJECT_STORAGE_ACCESS_KEY_ID=
WEORA_OBJECT_STORAGE_ACCESS_KEY_SECRET=
WEORA_OBJECT_STORAGE_SECURITY_TOKEN=
```

生产环境优先使用 RAM Role 或 STS 临时凭据。静态 AccessKey 只作为明确配置的兼容路径。
如果中国内地新建 OSS 用户受默认公共 Endpoint 限制，应绑定 CNAME，并把
`WEORA_OBJECT_STORAGE_USE_CNAME` 设为 `true`。

### 7.3 切换示例

切换到 MinIO 时只改变配置：

```dotenv
WEORA_OBJECT_STORAGE_PROVIDER=minio
WEORA_OBJECT_STORAGE_BUCKET=weora-private
WEORA_OBJECT_STORAGE_REGION=us-east-1
WEORA_OBJECT_STORAGE_ENDPOINT=https://minio.example.internal
```

业务代码、逻辑对象键和数据库记录不变。

## 8. 配置切换不等于数据迁移

修改 Provider 后，新实例会到新 Bucket 读取和写入相同逻辑对象键。如果历史对象没有提前复制，
业务会得到 `ObjectNotFound`。

正确切换流程：

1. 冻结或双写不是第一版默认能力。
2. 使用独立迁移工具把旧 Provider 对象复制到新 Provider 的相同逻辑键。
3. 对每个对象核对大小和应用 SHA-256。
4. 抽样执行真实下载。
5. 暂停写入或处理增量差异。
6. 修改一个 Provider 配置并重启应用。
7. 执行 Readiness 和业务回归。
8. 保留旧 Provider 只读观察期，再按审批清理。

第一版不做运行时自动回退。自动回退会导致新旧 Provider 同时产生权威数据，增加数据分裂风险。

## 9. 完整性与幂等

- 上传前由业务模块计算 SHA-256。
- Adapter 将 SHA-256 写入对象自定义元数据。
- 上传成功后使用 `HeadObject` 读取大小和元数据进行校验。
- 不使用 ETag 代替 SHA-256。
- 内容寻址对象只能写入同内容；同键不同 SHA-256 返回冲突。
- `delete` 幂等。
- `copy` 在目标存在且 SHA-256 一致时直接成功，不一致时返回冲突。
- 重试只使用可重读数据源。

## 10. 错误模型

Port 只抛出稳定错误：

| 错误 | 含义 | 是否建议调用方重试 |
| --- | --- | --- |
| `ObjectNotFoundError` | 对象不存在 | 否 |
| `ObjectConflictError` | 同键存在不同内容 | 否 |
| `ObjectAccessDeniedError` | 凭据或策略拒绝 | 否 |
| `ObjectIntegrityError` | 大小或 SHA-256 不一致 | 否，触发告警 |
| `ObjectStorageTimeoutError` | 连接或读取超时 | 由 Adapter 有界重试后返回 |
| `ObjectStorageUnavailableError` | 服务端或网络暂时不可用 | 由 Adapter有界重试后返回 |
| `ObjectStorageConfigurationError` | 启动配置不完整或矛盾 | 否，启动失败 |

调用方不解析字符串、HTTP 状态码或 SDK Error Code。

## 11. 安全

- Bucket 私有并启用 Block Public Access。
- 应用不接受 ACL 参数，不能通过业务请求把对象设为公开。
- 使用最小权限 RAM Policy，只允许指定 Bucket 和前缀。
- 凭据使用 Secret 类型，禁止出现在日志、异常详情和 API 响应中。
- 生产环境优先使用 RAM Role 或 STS。
- 服务端加密由 Bucket 默认策略统一配置，业务调用方不能关闭。
- 对象键不包含邮箱、凭据或用户原始文件名。
- 下载由业务 API 先完成权限检查，再调用对象存储。
- Provider 的响应正文和请求签名不进入日志。

第一阶段所需权限范围：

- 读取 Bucket 状态。
- 对指定前缀执行 Get、Head、Put、Copy 和 Delete。

应用不需要创建、删除 Bucket 或修改 Bucket ACL。

## 12. 超时与重试

- 使用 SDK Standard Retry。
- 总尝试次数默认 3 次。
- 连接超时默认 5 秒，读取超时默认 30 秒。
- 只重试超时、连接失败、限流和可识别的服务端瞬时错误。
- 权限、参数、对象不存在和完整性错误不重试。
- 不使用 Adaptive Retry 作为默认值，避免一个共享客户端的限流影响其他工作空间。
- 应用层不得在 Adapter 重试之外再套无界重试。

## 13. 可观测性

每次操作产生结构化日志和 OpenTelemetry Span：

- `object_storage.provider`
- `object_storage.operation`
- `object_storage.key_namespace`
- `object_storage.content_length`
- `object_storage.attempts`
- `object_storage.result`
- `object_storage.duration_ms`

日志不记录完整对象键，只记录命名空间和对象键哈希，避免暴露业务路径。

指标至少包括：

- 请求数和错误率。
- P50/P95/P99 延迟。
- 上传和下载字节数。
- 重试次数。
- 活跃下载流数量。
- 活跃 SDK 连接和连接池等待时间。

## 14. 健康检查

- Liveness 不依赖对象存储，避免供应商故障导致进程重启风暴。
- Readiness 在 Provider 启用时执行 `HeadBucket` 或等价只读探测。
- Readiness 缓存短时间结果，避免每次探针都调用云 API。
- 对象存储不可用时，依赖它的业务接口快速失败并返回稳定错误。

Bucket 权限不足和 Bucket 不存在视为配置错误，而不是暂时可恢复错误。

## 15. 测试设计

### 15.1 Port 契约测试

同一套契约测试必须验证所有 Adapter：

- Put 后 Stat 和 Read 内容一致。
- SHA-256 与大小一致。
- 不存在对象的 Stat 返回 `None`。
- Delete 幂等。
- Copy 保持内容和元数据。
- 同键不同内容产生冲突。
- 流在完成、异常和取消时关闭。
- Provider 错误映射稳定。

### 15.2 快速测试

- In-memory Fake 只用于 Application 测试。
- Stub SDK 响应验证错误映射、virtual/path 寻址配置和重试边界。
- 配置模型验证每个 Provider 的必填项和安全默认值。

### 15.3 真实集成

第一阶段使用独立阿里云 OSS 测试 Bucket 执行凭据控制的集成测试：

- 真实 Put/Get/Head/Copy/Delete。
- virtual-hosted 请求。
- RAM 最小权限。
- 超时、404 和 403 映射。
- 自定义 SHA-256 元数据。
- 进程重启后对象仍可读取。

真实 OSS 测试没有凭据时应明确 Skip，不能用 Mock 通过冒充真实联调。

未来接入 AWS S3 或 MinIO 时，必须复用同一套 Port 契约测试。

### 15.1 真实联调记录

2026-08-21 在阿里云深圳地域的私有 Bucket 完成真实契约测试：

- Bucket 探测成功。
- Put、Head、流式 Get、Copy、Delete 全部通过。
- SHA-256、正文长度和复制结果校验通过。
- 测试使用唯一逻辑键，结束后确认来源与副本对象均已删除。
- 联调发现 SDK 会用 `OperationError` 包装 `ServiceError`，Adapter 已统一解包并保留 404、
  权限、超时和服务不可用语义。

## 16. 模块边界

建议的职责布局：

```text
shared/object_storage/
├── models          # ObjectKey、数据源、元数据和读取流
├── port            # ObjectStoragePort
└── errors          # 稳定错误

infrastructure/object_storage/
├── aliyun_oss      # 首期原生 OSS Adapter
└── s3_compatible   # 后续 boto3 Adapter

bootstrap/
└── object_storage  # 启动装配
```

该目录是概念布局，不要求为每个名字创建独立文件。实现时以减少导航和保持职责清晰为准。

业务模块只依赖 `shared/object_storage` 中的契约，不能导入 `infrastructure`。只有 Bootstrap
负责选择并装配具体供应商适配器。

## 17. 第一阶段交付范围

1. 配置模型和启动时校验。
2. `ObjectStoragePort`、稳定模型与错误。
3. `AliyunOssObjectStorage`。
4. `aliyun_oss` 启动装配。
5. In-memory Fake。
6. 单元、契约和真实 OSS 集成测试。
7. Readiness、日志和基础指标。
8. 开发与生产配置文档。

暂不实现：

- MinIO 实例和 Docker Compose 服务。
- AWS S3 真实联调。
- Provider 数据迁移工具。
- 签名 URL。
- 分片上传。
- Bucket 自动创建。
- 动态多 Provider 路由。

## 18. 验收标准

- 使用 `aliyun_oss` 配置可以通过真实 OSS 契约测试。
- 未来启用 MinIO 或 AWS S3 Adapter 时，业务模块无需修改。
- SDK 类型、Bucket、Endpoint、ACL 和 ETag 不出现在业务接口。
- 对象完整性只由应用 SHA-256 判断。
- 非法或缺失配置在启动或 Readiness 阶段明确失败。
- 私有对象不能匿名读取。
- 所有响应流在成功、异常和取消时正确关闭。
- 没有真实 OSS 凭据时，报告“未联调”，不能声称云端接入完成。

## 19. 官方参考

- [Dify 对象存储接口与工厂，commit `0aefcfa`](https://github.com/langgenius/dify/blob/0aefcfa37375a9c1d801e4bfbc7bb38d1534afb8/api/extensions/ext_storage.py)
- [Dify 阿里云 OSS Adapter，commit `0aefcfa`](https://github.com/langgenius/dify/blob/0aefcfa37375a9c1d801e4bfbc7bb38d1534afb8/api/extensions/storage/aliyun_oss_storage.py)
- [Coze Studio 对象存储接口与工厂，commit `fefb05f`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/infra/storage/impl/storage.go)
- [Coze Studio S3、MinIO、TOS Adapter，commit `fefb05f`](https://github.com/coze-dev/coze-studio/tree/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/infra/storage/impl)
- [阿里云 OSS S3 API 兼容范围与差异](https://www.alibabacloud.com/help/en/oss/developer-reference/compatibility-with-amazon-s3)
- [阿里云 OSS Python SDK V2](https://www.alibabacloud.com/help/en/oss/developer-reference/python-sdk-v1-to-v2-migration-guide)
- [阿里云 OSS RAM 最小权限](https://www.alibabacloud.com/help/en/oss/user-guide/ram-policy/)
- [阿里云 OSS 权限与 Block Public Access](https://www.alibabacloud.com/help/en/oss/user-guide/permissions-and-access-control-overview)
- [AWS S3 PutObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/bucket/put_object.html)
- [AWS SDK Standard Retry](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html)
