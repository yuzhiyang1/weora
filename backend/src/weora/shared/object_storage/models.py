"""对象存储调用方能够感知的稳定模型。"""

import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, Self


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """不包含 Bucket、Endpoint 和供应商信息的逻辑对象键。"""

    value: str

    def __post_init__(self) -> None:
        """阻止对象键逃逸业务模块分配的命名空间。"""
        if not self.value:
            raise ValueError("对象键不能为空")
        if len(self.value.encode("utf-8")) > 1024:
            raise ValueError("对象键不能超过 1024 字节")
        if "\\" in self.value:
            raise ValueError("对象键只能使用正斜杠分段")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.value):
            raise ValueError("对象键不能包含控制字符")

        segments = self.value.split("/")
        if "" in segments:
            raise ValueError("对象键不能包含空路径片段")
        if "." in segments:
            raise ValueError("对象键不能包含当前目录片段")
        if ".." in segments:
            raise ValueError("对象键不能包含父目录片段")

    def __str__(self) -> str:
        """在 SDK 边界转换为对象键字符串。"""
        return self.value


@dataclass(frozen=True, slots=True)
class BytesObjectSource:
    """适合小对象和测试的可重读上传源。"""

    data: bytes = field(repr=False)
    content_type: str
    filename: str | None = None
    content_length: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """在创建数据源时固定长度和应用级完整性摘要。"""
        object.__setattr__(self, "content_length", len(self.data))
        object.__setattr__(self, "sha256", hashlib.sha256(self.data).hexdigest())

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        """每次创建独立流，确保 SDK 重试不会复用已消费的游标。"""
        stream = BytesIO(self.data)
        try:
            yield stream
        finally:
            stream.close()


@dataclass(frozen=True, slots=True)
class FileObjectSource:
    """适合上传包和规范化产物的可重读文件数据源。"""

    path: Path
    content_type: str
    filename: str | None = None
    content_length: int = field(init=False)
    sha256: str = field(init=False)
    _modified_ns: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """在交给 SDK 前固定文件大小和内容摘要。"""
        if not self.path.is_file():
            raise ValueError(f"上传源不是普通文件：{self.path}")

        initial_stat = self.path.stat()
        digest = hashlib.sha256()
        with self.path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        final_stat = self.path.stat()
        if initial_stat.st_size != final_stat.st_size or initial_stat.st_mtime_ns != final_stat.st_mtime_ns:
            raise ValueError("上传源在摘要计算期间发生变化")

        object.__setattr__(self, "content_length", final_stat.st_size)
        object.__setattr__(self, "sha256", digest.hexdigest())
        object.__setattr__(self, "_modified_ns", final_stat.st_mtime_ns)

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        """每次打开新的只读句柄，供 SDK 安全重试。"""
        with self.path.open("rb") as stream:
            current_stat = os.fstat(stream.fileno())
            if current_stat.st_size != self.content_length or current_stat.st_mtime_ns != self._modified_ns:
                raise ValueError("上传源在摘要确定后发生变化")
            yield stream


class ReplayableObjectSource(Protocol):
    """允许 SDK 在瞬时错误后从头重试的上传数据源。"""

    @property
    def content_length(self) -> int: ...

    @property
    def content_type(self) -> str: ...

    @property
    def sha256(self) -> str: ...

    @property
    def filename(self) -> str | None: ...

    def open(self) -> AbstractContextManager[BinaryIO]: ...


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """不包含供应商类型的稳定对象元数据。"""

    key: ObjectKey
    content_length: int
    content_type: str
    sha256: str
    last_modified: datetime
    etag: str | None = None


class AsyncObjectBody(Protocol):
    """可迭代且必须显式关闭的对象正文。"""

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class ObjectRead(AbstractAsyncContextManager["ObjectRead"]):
    """把元数据、异步正文和关闭语义绑定为一个读取结果。"""

    metadata: ObjectMetadata
    body: AsyncObjectBody

    def __aiter__(self) -> AsyncIterator[bytes]:
        """允许调用方直接异步遍历正文块。"""
        return self.body.__aiter__()

    async def aclose(self) -> None:
        """释放底层 HTTP 响应，避免连接池泄漏。"""
        await self.body.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
