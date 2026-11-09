"""Application 测试使用的内存对象存储实现。"""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from weora.shared.object_storage.errors import (
    ObjectConflictError,
    ObjectIntegrityError,
    ObjectNotFoundError,
)
from weora.shared.object_storage.models import (
    ObjectKey,
    ObjectMetadata,
    ObjectRead,
    ReplayableObjectSource,
)


@dataclass(slots=True)
class _MemoryObject:
    """内存实现保存的正文与公共元数据。"""

    content: bytes
    metadata: ObjectMetadata


class _MemoryObjectBody:
    """模拟一次可关闭的异步下载流。"""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if not self._closed and self._content:
            yield self._content

    async def aclose(self) -> None:
        self._closed = True


class InMemoryObjectStorage:
    """只用于 Application 测试，不作为开发环境持久化后备。"""

    def __init__(self) -> None:
        self._objects: dict[ObjectKey, _MemoryObject] = {}

    async def put(
        self,
        key: ObjectKey,
        source: ReplayableObjectSource,
    ) -> ObjectMetadata:
        """完整消费可重读数据源并保存稳定元数据。"""
        existing = self._objects.get(key)
        if existing is not None:
            if existing.metadata.sha256 == source.sha256:
                return existing.metadata
            raise ObjectConflictError(key)

        with source.open() as stream:
            content = stream.read()
        if len(content) != source.content_length or hashlib.sha256(content).hexdigest() != source.sha256:
            raise ObjectIntegrityError(key)
        metadata = ObjectMetadata(
            key=key,
            content_length=len(content),
            content_type=source.content_type,
            sha256=source.sha256,
            last_modified=datetime.now(UTC),
            etag=source.sha256,
        )
        self._objects[key] = _MemoryObject(content=content, metadata=metadata)
        return metadata

    async def read(self, key: ObjectKey) -> ObjectRead:
        """以异步流返回对象，保持与云端 Adapter 一致的消费方式。"""
        stored = self._objects.get(key)
        if stored is None:
            raise ObjectNotFoundError(key)
        return ObjectRead(
            metadata=stored.metadata,
            body=_MemoryObjectBody(stored.content),
        )

    async def stat(self, key: ObjectKey) -> ObjectMetadata | None:
        """返回稳定元数据；对象不存在不是基础设施异常。"""
        stored = self._objects.get(key)
        return stored.metadata if stored is not None else None

    async def delete(self, key: ObjectKey) -> None:
        """幂等删除对象，便于数据库失败后的尽力清理。"""
        self._objects.pop(key, None)

    async def copy(
        self,
        source_key: ObjectKey,
        target_key: ObjectKey,
        *,
        expected_sha256: str,
    ) -> ObjectMetadata:
        """复制经过摘要确认的对象，并保持目标键不可变。"""
        source = self._objects.get(source_key)
        if source is None:
            raise ObjectNotFoundError(source_key)
        if source.metadata.sha256 != expected_sha256:
            raise ObjectIntegrityError(source_key)

        existing = self._objects.get(target_key)
        if existing is not None:
            if existing.metadata.sha256 == expected_sha256:
                return existing.metadata
            raise ObjectConflictError(target_key)

        metadata = ObjectMetadata(
            key=target_key,
            content_length=source.metadata.content_length,
            content_type=source.metadata.content_type,
            sha256=source.metadata.sha256,
            last_modified=datetime.now(UTC),
            etag=source.metadata.etag,
        )
        self._objects[target_key] = _MemoryObject(
            content=source.content,
            metadata=metadata,
        )
        return metadata

    async def probe(self) -> None:
        """内存 Fake 始终可用。"""

    async def aclose(self) -> None:
        """内存 Fake 不持有外部连接。"""
