"""通过公共 Port 行为验证内存对象存储。"""

import pytest

from weora.shared.object_storage import BytesObjectSource, ObjectKey
from weora.shared.object_storage.errors import ObjectConflictError
from weora.shared.object_storage.testing import InMemoryObjectStorage


@pytest.mark.anyio
async def test_put_then_stream_read_preserves_content_and_metadata() -> None:
    """调用方写入对象后，应能通过流读取相同内容和应用摘要。"""
    storage = InMemoryObjectStorage()
    key = ObjectKey("skills/workspace-1/demo.skill")
    source = BytesObjectSource(
        b"weora-skill",
        content_type="application/vnd.weora.skill+zip",
    )

    stored = await storage.put(key, source)
    async with await storage.read(key) as object_read:
        content = b"".join([chunk async for chunk in object_read])

    assert content == b"weora-skill"
    assert stored.key == key
    assert stored.content_length == source.content_length
    assert stored.content_type == source.content_type
    assert stored.sha256 == source.sha256


@pytest.mark.anyio
async def test_put_rejects_different_content_for_an_existing_key() -> None:
    """同一逻辑键不能被不同内容静默覆盖。"""
    storage = InMemoryObjectStorage()
    key = ObjectKey("skills/workspace-1/demo.skill")
    await storage.put(key, BytesObjectSource(b"version-one", content_type="application/zip"))

    with pytest.raises(ObjectConflictError):
        await storage.put(key, BytesObjectSource(b"version-two", content_type="application/zip"))


@pytest.mark.anyio
async def test_delete_is_idempotent_and_removes_object_metadata() -> None:
    """重复清理孤儿对象时，不存在的对象也应视为清理成功。"""
    storage = InMemoryObjectStorage()
    key = ObjectKey("skills/workspace-1/orphan.skill")
    await storage.put(key, BytesObjectSource(b"orphan", content_type="application/zip"))

    await storage.delete(key)
    await storage.delete(key)

    assert await storage.stat(key) is None


@pytest.mark.anyio
async def test_copy_preserves_content_and_requires_expected_digest() -> None:
    """发布草稿副本时必须验证来源内容仍是应用层确认的版本。"""
    storage = InMemoryObjectStorage()
    source_key = ObjectKey("skills/workspace-1/draft/demo.skill")
    target_key = ObjectKey("skills/workspace-1/versions/0.1.0/demo.skill")
    source = BytesObjectSource(b"published", content_type="application/zip")
    await storage.put(source_key, source)

    copied = await storage.copy(source_key, target_key, expected_sha256=source.sha256)
    async with await storage.read(target_key) as object_read:
        copied_content = b"".join([chunk async for chunk in object_read])

    assert copied_content == b"published"
    assert copied.key == target_key
    assert copied.sha256 == source.sha256
