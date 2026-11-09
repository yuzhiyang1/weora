"""业务模块依赖的对象存储 Port。"""

from typing import Protocol

from weora.shared.object_storage.models import (
    ObjectKey,
    ObjectMetadata,
    ObjectRead,
    ReplayableObjectSource,
)


class ObjectStoragePort(Protocol):
    """隐藏供应商 SDK、Bucket、签名和响应流实现。"""

    async def put(
        self,
        key: ObjectKey,
        source: ReplayableObjectSource,
    ) -> ObjectMetadata: ...

    async def read(self, key: ObjectKey) -> ObjectRead: ...

    async def stat(self, key: ObjectKey) -> ObjectMetadata | None: ...

    async def copy(
        self,
        source_key: ObjectKey,
        target_key: ObjectKey,
        *,
        expected_sha256: str,
    ) -> ObjectMetadata: ...

    async def delete(self, key: ObjectKey) -> None: ...

    async def probe(self) -> None: ...

    async def aclose(self) -> None: ...
