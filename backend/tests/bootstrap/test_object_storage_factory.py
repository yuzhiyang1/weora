"""对象存储启动工厂的行为测试。"""

from typing import cast

import alibabacloud_oss_v2 as oss
from pydantic import SecretStr

from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.object_storage import create_object_storage
from weora.infrastructure.object_storage.aliyun_oss import (
    AliyunOssAsyncClient,
    AliyunOssObjectStorage,
)
from weora.shared.object_storage.testing import InMemoryObjectStorage


def test_factory_keeps_object_storage_explicitly_disabled() -> None:
    """禁用状态不能静默回退到本地磁盘并制造伪持久化。"""
    settings = IsolatedAppSettings(object_storage_provider="disabled")

    assert create_object_storage(settings) is None


def test_factory_uses_provider_config_without_exposing_it_to_callers() -> None:
    """调用方只使用 Port，Region、Endpoint 和重试参数由启动工厂装配。"""
    captured_config: oss.config.Config | None = None
    storage_backend = InMemoryObjectStorage()

    def build_client(config: oss.config.Config) -> AliyunOssAsyncClient:
        nonlocal captured_config
        captured_config = config
        return cast(AliyunOssAsyncClient, storage_backend)

    settings = IsolatedAppSettings(
        environment="test",
        object_storage_provider="aliyun_oss",
        object_storage_bucket="weora-test",
        object_storage_region="cn-hangzhou",
        object_storage_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        object_storage_access_key_id=SecretStr("access-key-id"),
        object_storage_access_key_secret=SecretStr("access-key-secret"),
        object_storage_connect_timeout_seconds=7,
        object_storage_read_timeout_seconds=31,
        object_storage_max_attempts=4,
    )

    storage = create_object_storage(settings, aliyun_client_factory=build_client)

    assert storage is not None
    assert captured_config is not None
    assert captured_config.region == "cn-hangzhou"
    assert captured_config.endpoint == "https://oss-cn-hangzhou.aliyuncs.com"
    assert captured_config.connect_timeout == 7
    assert captured_config.readwrite_timeout == 31
    assert captured_config.retry_max_attempts == 4
    assert isinstance(storage, AliyunOssObjectStorage)
