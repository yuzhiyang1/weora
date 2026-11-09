"""对象存储 Provider 的启动期装配。"""

from collections.abc import Callable

import alibabacloud_oss_v2 as oss
import alibabacloud_oss_v2.aio as oss_aio

from weora.bootstrap.settings import AppSettings
from weora.infrastructure.object_storage.aliyun_oss import (
    AliyunOssAsyncClient,
    AliyunOssObjectStorage,
)
from weora.shared.object_storage import ObjectStoragePort
from weora.shared.object_storage.errors import ObjectStorageConfigurationError

AliyunClientFactory = Callable[[oss.config.Config], AliyunOssAsyncClient]


def create_object_storage(
    settings: AppSettings,
    *,
    aliyun_client_factory: AliyunClientFactory = oss_aio.AsyncClient,
) -> ObjectStoragePort | None:
    """根据唯一 Provider 配置创建对象存储；禁用时不提供隐式本地后备。"""
    if settings.object_storage_provider == "disabled":
        return None
    if settings.object_storage_provider != "aliyun_oss":
        raise ObjectStorageConfigurationError(f"尚未支持对象存储 Provider：{settings.object_storage_provider}")

    bucket = settings.object_storage_bucket
    region = settings.object_storage_region
    endpoint = settings.object_storage_endpoint
    if not bucket or not region or not endpoint:
        # AppSettings 正常会提前拦截，这里防止未来从其他入口直接装配。
        raise ObjectStorageConfigurationError("aliyun_oss 配置不完整")

    access_key_id = settings.object_storage_access_key_id
    access_key_secret = settings.object_storage_access_key_secret
    if access_key_id is not None and access_key_secret is not None:
        security_token = settings.object_storage_security_token
        credentials_provider: oss.credentials.CredentialsProvider = oss.credentials.StaticCredentialsProvider(
            access_key_id.get_secret_value(),
            access_key_secret.get_secret_value(),
            security_token.get_secret_value() if security_token is not None else None,
        )
    else:
        # 未提供 WEORA_* 静态凭据时，交给 SDK 从 OSS_* 环境变量读取。
        credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()

    config = oss.config.load_default()
    config.credentials_provider = credentials_provider
    config.region = region
    config.endpoint = endpoint
    config.connect_timeout = settings.object_storage_connect_timeout_seconds
    config.readwrite_timeout = settings.object_storage_read_timeout_seconds
    config.retry_max_attempts = settings.object_storage_max_attempts
    config.use_cname = settings.object_storage_use_cname
    config.use_path_style = False

    return AliyunOssObjectStorage(
        client=aliyun_client_factory(config),
        bucket=bucket,
        prefix=settings.resolved_object_storage_prefix,
    )
