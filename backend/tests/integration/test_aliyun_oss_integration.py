"""使用真实阿里云 OSS Bucket 验证公共 Port 契约。"""

import os
from contextlib import suppress
from uuid import uuid4

import pytest

from weora.bootstrap.object_storage import create_object_storage
from weora.bootstrap.settings import AppSettings
from weora.shared.object_storage import BytesObjectSource, ObjectKey

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("WEORA_RUN_ALIYUN_OSS_INTEGRATION") != "1",
        reason="未显式启用阿里云 OSS 真实联调",
    ),
]


@pytest.mark.anyio
async def test_aliyun_oss_contract_against_real_bucket() -> None:
    """真实执行 Put、Head、Get、Copy、Delete，并在结束时清理测试对象。"""
    settings = AppSettings()
    assert settings.object_storage_provider == "aliyun_oss"
    storage = create_object_storage(settings)
    assert storage is not None

    run_id = uuid4().hex
    source_key = ObjectKey(f"integration/{run_id}/source.txt")
    copied_key = ObjectKey(f"integration/{run_id}/copied.txt")
    source = BytesObjectSource(
        f"weora-object-storage-{run_id}".encode(),
        content_type="text/plain; charset=utf-8",
        filename="source.txt",
    )

    try:
        await storage.probe()
        stored = await storage.put(source_key, source)
        assert stored.sha256 == source.sha256
        assert stored.content_length == source.content_length

        async with await storage.read(source_key) as object_read:
            downloaded = b"".join([chunk async for chunk in object_read])
        assert downloaded == source.data

        copied = await storage.copy(
            source_key,
            copied_key,
            expected_sha256=source.sha256,
        )
        assert copied.sha256 == source.sha256
        assert copied.content_length == source.content_length

        await storage.delete(copied_key)
        await storage.delete(source_key)
        assert await storage.stat(source_key) is None
        assert await storage.stat(copied_key) is None
    finally:
        # 主流程已经验证删除；异常清理不能掩盖最先发生的联调错误。
        with suppress(Exception):
            await storage.delete(copied_key)
        with suppress(Exception):
            await storage.delete(source_key)
        await storage.aclose()
