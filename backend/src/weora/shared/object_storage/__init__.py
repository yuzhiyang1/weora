"""供应商无关的对象存储公共契约。"""

from weora.shared.object_storage.models import (
    BytesObjectSource,
    FileObjectSource,
    ObjectKey,
    ObjectMetadata,
    ObjectRead,
    ReplayableObjectSource,
)
from weora.shared.object_storage.port import ObjectStoragePort

__all__ = [
    "BytesObjectSource",
    "FileObjectSource",
    "ObjectKey",
    "ObjectMetadata",
    "ObjectRead",
    "ObjectStoragePort",
    "ReplayableObjectSource",
]
