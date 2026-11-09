# ADR-0002：Provider 无关的对象存储端口

- 状态：已接受（阿里云 OSS 真实联调已完成）
- 日期：2026-08-21

## 背景

Weora 的 Skill 包、知识库原文件、运行产物和协作附件都需要对象存储。第一阶段计划使用阿里云
OSS，但未来可能切换到 AWS S3、MinIO 或其他兼容 S3 API 的云厂商。

如果业务模块直接依赖 OSS SDK、Bucket、Endpoint、ACL、签名方式和异常类型，后续切换供应商时，
所有调用方都需要修改。反过来，如果为了“兼容所有对象存储”一次性暴露全部厂商能力，公共接口
会被不稳定的供应商差异污染。

阿里云官方文档说明，OSS 支持的是 Amazon S3 API 的一个子集。当前 Weora 需要的
`PutObject`、`GetObject`、`HeadObject`、`DeleteObject` 和 `PutObjectCopy` 都在兼容范围内，
但 OSS 只接受 virtual-hosted 请求，ACL 和 ETag 语义也与 S3 存在差异。

## 决策驱动因素

必须塑造设计：

- 业务模块只能依赖稳定的对象语义，不能依赖厂商 SDK。
- 第一阶段必须真实接入阿里云 OSS。
- 切换供应商时，业务代码和数据库对象键不能跟着变化。
- 私有 Bucket、完整性校验、超时、错误映射和可观测性必须一致。
- 已有对象不会因为修改配置自动迁移，切换前必须完成数据迁移与校验。

应该隐藏或提供安全默认值：

- Endpoint、签名、寻址风格、连接池、重试和 SDK 异常。
- OSS、AWS S3 和 MinIO 的差异化客户端配置。
- ETag 大小写和分片上传算法差异。

真实环境验证结论：

- 阿里云 OSS Python SDK V2 已在深圳地域完成完整契约测试。
- 本地开发使用公网 Endpoint 已验证；生产网络仍需根据部署位置选择公网或同地域内网 Endpoint。

## 上游参考事实

本决策核查了以下固定版本源码，只采用架构经验，不复制实现：

| 项目 | Commit 与位置 | 实现事实 | Weora 结论 |
| --- | --- | --- | --- |
| Dify | [`0aefcfa` `BaseStorage`](https://github.com/langgenius/dify/blob/0aefcfa37375a9c1d801e4bfbc7bb38d1534afb8/api/extensions/storage/base_storage.py)、[`Storage` 工厂](https://github.com/langgenius/dify/blob/0aefcfa37375a9c1d801e4bfbc7bb38d1534afb8/api/extensions/ext_storage.py) | 使用统一接口和配置工厂选择实现 | 采用 Port 和启动期工厂 |
| Dify | [`AliyunOssStorage`](https://github.com/langgenius/dify/blob/0aefcfa37375a9c1d801e4bfbc7bb38d1534afb8/api/extensions/storage/aliyun_oss_storage.py)、[`AwsS3Storage`](https://github.com/langgenius/dify/blob/0aefcfa37375a9c1d801e4bfbc7bb38d1534afb8/api/extensions/storage/aws_s3_storage.py) | OSS 使用 `oss2`，AWS S3 使用 `boto3`，没有强行共用一个 Adapter | 阿里云首期使用原生 SDK；供应商切换由工厂完成 |
| Coze Studio | [`Storage`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/infra/storage/storage.go)、[`New` 工厂](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/infra/storage/impl/storage.go) | 统一接口后按配置选择 S3、MinIO 或 TOS | 采用显式 Provider 配置，不让业务层选择 SDK |
| Coze Studio | [`S3`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/infra/storage/impl/s3/s3.go)、[`MinIO`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/infra/storage/impl/minio/minio.go)、[`TOS`](https://github.com/coze-dev/coze-studio/blob/fefb05ff27be1da939612fbf9faf5db62583b8ae/backend/infra/storage/impl/tos/tos.go) | 三种实现分别适配原生 SDK 和分页、标签、签名差异 | 供应商差异必须留在 Adapter 内，并用同一契约测试约束 |

这些项目证明“统一接口 + 配置工厂 + Provider Adapter”已经是可行路径，但它们不是可直接照抄的
Weora 标准：

- Dify 的 AWS `exists` 会把所有异常都当成不存在，无法区分 404、403 和服务故障。
- Dify 与 Coze 的部分读取接口会一次性把对象读入内存，不适合未知大小的 Skill 包和知识库原文件。
- Coze 的 MinIO 分页实现实际返回全部对象，与接口的分页语义并不完全一致。
- 两个项目都有启动时自动创建 Bucket 的实现，这会扩大应用凭据权限。
- 当前看到的 Adapter 测试更偏 Mock 或上层消费测试，不能代替逐 Provider 的真实契约测试。

## 候选方案

### 方案 A：单个 S3 兼容 Adapter 覆盖全部供应商

使用 `boto3` 和 Provider Profile 同时访问 OSS、AWS S3、MinIO 及其他 S3 兼容服务。

优点：

- 当前基础操作位于 OSS 声明兼容的 S3 API 子集内。
- 一个 SDK 和一套实现即可覆盖多个供应商。

代价：

- OSS 只兼容 S3 API 子集，签名、寻址、ACL、错误和 ETag 仍有差异。
- 第一阶段的真实目标是 OSS，却会优先承担一层兼容协议风险。
- 上游成熟项目没有采用“一个 S3 Adapter 覆盖 OSS 和所有供应商”的实现。

### 方案 B：每个供应商都使用独立原生 Adapter

分别实现 OSS、AWS S3 和 MinIO Adapter。

优点：

- 能完整使用供应商能力。
- 签名、重试和协议细节由各供应商官方 SDK 处理。

代价：

- 从第一阶段开始就维护多套依赖和近似实现。
- AWS S3、MinIO 和其他兼容服务可能产生不必要的重复 Adapter。
- 相同业务语义容易在多个 Adapter 中产生细微差异。

### 方案 C：统一 Port，按已验证兼容性复用 Adapter

定义 Provider 无关的 `ObjectStoragePort`。第一阶段只实现
`AliyunOssObjectStorage`，使用阿里云官方 Python SDK V2。后续为 AWS S3、MinIO 和
其他兼容服务增加 `S3CompatibleObjectStorage`；只有契约测试证明无法兼容时，才增加独立
Provider Adapter。

优点：

- 首期真实目标使用供应商官方 SDK，不承担额外兼容风险。
- 业务调用方式和对象键始终不变，供应商仍由一个配置项切换。
- 已确认兼容的供应商共享实现，不为每个厂商机械复制代码。
- 供应商特有签名、错误、分页和流行为都能封装在 Adapter 内。

代价：

- 需要为每个实际启用的 Adapter 运行同一套契约测试。
- 将来同时启用多种 SDK 时，需要维护各自依赖和错误翻译。

## 决策

采用方案 C。

供应商无关的 Port、模型和稳定错误位于 `shared/object_storage`；阿里云 OSS 等供应商实现位于
`infrastructure/object_storage`；启动期工厂位于 `bootstrap`。因此 `object_storage` 是技术能力名，
不是业务领域名，业务模块也不会依赖具体基础设施实现。

第一阶段使用阿里云官方 Python SDK V2 的原生异步客户端构建
`AliyunOssObjectStorage`。SDK 从 `1.2.0` 开始提供 `aio.AsyncClient`，因此 FastAPI
调用链保持原生异步，不增加线程池桥接。

`WEORA_OBJECT_STORAGE_PROVIDER` 只负责选择已经安装并通过契约测试的 Adapter：

```text
aliyun_oss -> AliyunOssObjectStorage
aws_s3     -> S3CompatibleObjectStorage
minio      -> S3CompatibleObjectStorage
custom_s3  -> S3CompatibleObjectStorage
```

第一阶段实际可用值只有 `disabled` 和 `aliyun_oss`。其他值在对应 Adapter 完成前必须启动失败，
不能展示成虚假的“已支持”。未来完成 Adapter 后，切换只修改配置，不修改业务代码。

公共 Port 第一阶段只提供：

- 写入对象。
- 读取对象流。
- 查询对象元数据。
- 复制对象。
- 幂等删除对象。
- 检查 Bucket 可访问性。

不在公共 Port 中提供：

- Bucket 创建和管理。
- 公共 ACL。
- 生命周期规则。
- 厂商图片、视频处理。
- 厂商事件通知。
- 归档恢复。
- 对象列举和批量迁移。
- 签名 URL。

这些能力出现真实调用方后，再单独设计管理端口或供应商扩展，不能提前污染业务端口。

## 影响

### 正向影响

- Skills、Knowledge、Runs 等模块只理解逻辑对象键、流、大小和 SHA-256。
- Adapter 和启动工厂统一隐藏 OSS、AWS S3 和 MinIO 的客户端差异。
- 应用自己的 SHA-256 是完整性依据，不依赖 ETag。
- 新增 Provider 时只需通过同一契约测试，不需要修改业务模块。

### 负向影响

- 阿里云 OSS Python SDK V2 当前未发布 `py.typed` 标记，需要由 Adapter 自己维护最小类型边界。
- 后续启用 S3 Adapter 时会增加 `boto3` 依赖。
- OSS 的原生扩展能力仍不能越过当前公共 Port 进入业务层。
- Provider 配置切换只改变读写目标，不会复制历史对象。

### 运维要求

- Bucket 由部署系统预先创建，应用启动时不创建 Bucket。
- Bucket 必须私有并启用 Block Public Access。
- 使用 RAM Role、STS 或最小权限 RAM 用户，禁止使用主账号 AccessKey。
- 切换 Provider 前，用独立迁移工具按相同逻辑对象键复制并校验全部对象；迁移完成后再修改配置。

## 演进条件

- AWS S3、MinIO 或其他兼容服务进入交付范围时，先实现并运行
  `S3CompatibleObjectStorage` 契约测试。
- 只有真实契约测试证明某个供应商无法满足公共 Port 语义时，才增加独立 Adapter。
- 厂商图片处理、归档恢复等能力如果成为真实需求，应设计独立扩展端口，不能扩张基础 Port。

## 官方依据

- [阿里云 OSS 与 Amazon S3 的兼容范围和差异](https://www.alibabacloud.com/help/en/oss/developer-reference/compatibility-with-amazon-s3)
- [阿里云 OSS Python SDK V2 迁移说明](https://www.alibabacloud.com/help/en/oss/developer-reference/python-sdk-v1-to-v2-migration-guide)
- [AWS SDK 重试行为](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html)
- [AWS S3 PutObject 完整性校验](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/bucket/put_object.html)
