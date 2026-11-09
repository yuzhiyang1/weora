"""对象存储启动配置的行为测试。"""

import pytest
from pydantic import SecretStr, ValidationError

from tests.support.settings import IsolatedAppSettings


def test_aliyun_oss_configuration_requires_bucket_region_and_endpoint() -> None:
    """启用 OSS 时不能把不完整配置推迟到第一次业务上传才暴露。"""
    with pytest.raises(ValidationError, match="bucket"):
        IsolatedAppSettings(object_storage_provider="aliyun_oss")


def test_aliyun_oss_configuration_uses_environment_scoped_default_prefix() -> None:
    """未显式指定前缀时，测试与生产对象不能共享同一物理路径。"""
    settings = IsolatedAppSettings(
        environment="test",
        object_storage_provider="aliyun_oss",
        object_storage_bucket="weora-test",
        object_storage_region="cn-hangzhou",
        object_storage_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
    )

    assert settings.resolved_object_storage_prefix == "weora/test"


def test_aliyun_oss_configuration_rejects_partial_static_credentials() -> None:
    """AccessKey 只配置一半时不能退化为难以定位的运行期认证失败。"""
    with pytest.raises(ValidationError, match="必须同时配置"):
        IsolatedAppSettings(
            object_storage_provider="aliyun_oss",
            object_storage_bucket="weora-test",
            object_storage_region="cn-hangzhou",
            object_storage_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            object_storage_access_key_id=SecretStr("access-key-id"),
            object_storage_access_key_secret=SecretStr(""),
        )


def test_production_aliyun_oss_requires_https_endpoint() -> None:
    """生产凭据和对象正文不能通过明文 HTTP 发送。"""
    with pytest.raises(ValidationError, match="HTTPS"):
        IsolatedAppSettings(
            environment="production",
            object_storage_provider="aliyun_oss",
            object_storage_bucket="weora-production",
            object_storage_region="cn-hangzhou",
            object_storage_endpoint="http://oss-cn-hangzhou.aliyuncs.com",
        )
