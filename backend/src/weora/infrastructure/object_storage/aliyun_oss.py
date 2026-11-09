"""阿里云 OSS Python SDK V2 适配器。"""

import re
from collections.abc import AsyncIterator, Mapping
from datetime import UTC
from typing import Any, Protocol, cast

import alibabacloud_oss_v2 as oss

from weora.shared.object_storage.errors import (
    ObjectAccessDeniedError,
    ObjectConflictError,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStorageTimeoutError,
    ObjectStorageUnavailableError,
)
from weora.shared.object_storage.models import (
    ObjectKey,
    ObjectMetadata,
    ObjectRead,
    ReplayableObjectSource,
)

_SHA256_METADATA_KEY = "weora-sha256"
_READ_BLOCK_SIZE = 64 * 1024


class AliyunOssAsyncClient(Protocol):
    """Adapter 实际使用的 OSS SDK 边界。"""

    async def put_object(
        self,
        request: oss.PutObjectRequest,
        **kwargs: Any,
    ) -> oss.PutObjectResult: ...

    async def head_object(
        self,
        request: oss.HeadObjectRequest,
        **kwargs: Any,
    ) -> oss.HeadObjectResult: ...

    async def get_object(
        self,
        request: oss.GetObjectRequest,
        **kwargs: Any,
    ) -> oss.GetObjectResult: ...

    async def delete_object(
        self,
        request: oss.DeleteObjectRequest,
        **kwargs: Any,
    ) -> oss.DeleteObjectResult: ...

    async def copy_object(
        self,
        request: oss.CopyObjectRequest,
        **kwargs: Any,
    ) -> oss.CopyObjectResult: ...

    async def get_bucket_info(
        self,
        request: oss.GetBucketInfoRequest,
        **kwargs: Any,
    ) -> oss.GetBucketInfoResult: ...

    async def close(self) -> None: ...


class _AliyunObjectBody:
    """把 OSS AsyncStreamBody 转换为 Port 的关闭与迭代语义。"""

    def __init__(self, body: oss.types.AsyncStreamBody) -> None:
        self._body = body

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        iterator = await self._body.iter_bytes(block_size=_READ_BLOCK_SIZE)
        async for chunk in iterator:
            yield chunk

    async def aclose(self) -> None:
        await self._body.close()


class AliyunOssObjectStorage:
    """把 OSS SDK 的供应商语义收敛为 Weora 对象语义。"""

    def __init__(
        self,
        *,
        client: AliyunOssAsyncClient,
        bucket: str,
        prefix: str,
    ) -> None:
        if not bucket:
            raise ValueError("OSS Bucket 不能为空")
        self._client = client
        self._bucket = bucket
        self._prefix = str(ObjectKey(prefix.strip("/")))

    async def put(
        self,
        key: ObjectKey,
        source: ReplayableObjectSource,
    ) -> ObjectMetadata:
        """写入不可变对象，并通过 HeadObject 校验长度和应用摘要。"""
        existing = await self.stat(key)
        if existing is not None:
            if existing.sha256 == source.sha256:
                return existing
            raise ObjectConflictError(key)

        try:
            with source.open() as stream:
                await self._client.put_object(
                    oss.PutObjectRequest(
                        bucket=self._bucket,
                        key=self._physical_key(key),
                        body=stream,
                        content_length=source.content_length,
                        content_type=source.content_type,
                        metadata={_SHA256_METADATA_KEY: source.sha256},
                        # 防止并发请求在检查后覆盖已经写入的内容。
                        forbid_overwrite=True,
                    )
                )
        except oss.exceptions.BaseError as exc:
            sdk_error = self._unwrap_sdk_error(exc)
            if isinstance(sdk_error, oss.exceptions.ServiceError) and sdk_error.status_code in {409, 412}:
                concurrent_object = await self.stat(key)
                if (
                    concurrent_object is not None
                    and concurrent_object.sha256 == source.sha256
                    and concurrent_object.content_length == source.content_length
                ):
                    return concurrent_object
            raise self._translate_sdk_error(exc, key) from exc

        stored = await self.stat(key)
        if stored is None or stored.content_length != source.content_length or stored.sha256 != source.sha256:
            raise ObjectIntegrityError(key)
        return stored

    async def read(self, key: ObjectKey) -> ObjectRead:
        """返回可关闭的异步正文流，不把 OSS 响应对象暴露给业务模块。"""
        try:
            result = await self._client.get_object(
                oss.GetObjectRequest(
                    bucket=self._bucket,
                    key=self._physical_key(key),
                )
            )
        except oss.exceptions.BaseError as exc:
            sdk_error = self._unwrap_sdk_error(exc)
            if isinstance(sdk_error, oss.exceptions.ServiceError) and sdk_error.status_code == 404:
                raise ObjectNotFoundError(key) from exc
            raise self._translate_sdk_error(exc, key) from exc

        if result.body is None:
            raise ObjectIntegrityError(key)
        body = cast(oss.types.AsyncStreamBody, result.body)
        return ObjectRead(
            metadata=self._to_metadata(key, result),
            body=_AliyunObjectBody(body),
        )

    async def delete(self, key: ObjectKey) -> None:
        """幂等删除对象；OSS 对不存在对象返回成功。"""
        try:
            await self._client.delete_object(
                oss.DeleteObjectRequest(
                    bucket=self._bucket,
                    key=self._physical_key(key),
                )
            )
        except oss.exceptions.BaseError as exc:
            sdk_error = self._unwrap_sdk_error(exc)
            # 兼容启用版本控制或网关返回显式 404 的部署。
            if isinstance(sdk_error, oss.exceptions.ServiceError) and sdk_error.status_code == 404:
                return
            raise self._translate_sdk_error(exc, key) from exc

    async def copy(
        self,
        source_key: ObjectKey,
        target_key: ObjectKey,
        *,
        expected_sha256: str,
    ) -> ObjectMetadata:
        """在同一 Bucket 内复制对象，并验证来源与目标的应用摘要。"""
        source = await self.stat(source_key)
        if source is None:
            raise ObjectNotFoundError(source_key)
        if source.sha256 != expected_sha256:
            raise ObjectIntegrityError(source_key)

        target = await self.stat(target_key)
        if target is not None:
            if target.sha256 == expected_sha256:
                return target
            raise ObjectConflictError(target_key)

        try:
            await self._client.copy_object(
                oss.CopyObjectRequest(
                    bucket=self._bucket,
                    key=self._physical_key(target_key),
                    source_bucket=self._bucket,
                    source_key=self._physical_key(source_key),
                    forbid_overwrite=True,
                )
            )
        except oss.exceptions.BaseError as exc:
            sdk_error = self._unwrap_sdk_error(exc)
            if isinstance(sdk_error, oss.exceptions.ServiceError) and sdk_error.status_code in {409, 412}:
                concurrent_target = await self.stat(target_key)
                if concurrent_target is not None and concurrent_target.sha256 == expected_sha256:
                    return concurrent_target
            raise self._translate_sdk_error(exc, target_key) from exc

        copied = await self.stat(target_key)
        if copied is None or copied.content_length != source.content_length or copied.sha256 != expected_sha256:
            raise ObjectIntegrityError(target_key)
        return copied

    async def probe(self) -> None:
        """只验证预创建 Bucket 的访问权限，不在应用启动时创建资源。"""
        try:
            await self._client.get_bucket_info(oss.GetBucketInfoRequest(bucket=self._bucket))
        except oss.exceptions.BaseError as exc:
            raise self._translate_sdk_error(exc, None) from exc

    async def aclose(self) -> None:
        """释放异步 SDK 持有的连接池。"""
        await self._client.close()

    async def stat(self, key: ObjectKey) -> ObjectMetadata | None:
        """查询对象元数据；只有明确的 404 才表示对象不存在。"""
        try:
            result = await self._client.head_object(
                oss.HeadObjectRequest(
                    bucket=self._bucket,
                    key=self._physical_key(key),
                )
            )
        except oss.exceptions.BaseError as exc:
            sdk_error = self._unwrap_sdk_error(exc)
            if isinstance(sdk_error, oss.exceptions.ServiceError) and sdk_error.status_code == 404:
                return None
            raise self._translate_sdk_error(exc, key) from exc

        return self._to_metadata(key, result)

    def _physical_key(self, key: ObjectKey) -> str:
        """只在 Adapter 内增加部署前缀，数据库始终保存逻辑键。"""
        return f"{self._prefix}/{key}"

    @staticmethod
    def _to_metadata(
        key: ObjectKey,
        result: oss.HeadObjectResult | oss.GetObjectResult,
    ) -> ObjectMetadata:
        """拒绝缺少应用摘要的对象，避免把 ETag 误当完整性依据。"""
        raw_metadata: Mapping[str, str] = result.metadata or {}
        metadata = {name.lower(): value for name, value in raw_metadata.items()}
        sha256 = metadata.get(_SHA256_METADATA_KEY)
        if (
            result.content_length is None
            or result.last_modified is None
            or sha256 is None
            or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None
        ):
            raise ObjectIntegrityError(key)

        last_modified = result.last_modified
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=UTC)
        return ObjectMetadata(
            key=key,
            content_length=result.content_length,
            content_type=result.content_type or "application/octet-stream",
            sha256=sha256.lower(),
            last_modified=last_modified,
            etag=result.etag,
        )

    @staticmethod
    def _unwrap_sdk_error(error: oss.exceptions.BaseError) -> Exception:
        """剥离 SDK 的操作包装层，保留真正的服务端或传输错误。"""
        current: Exception = error
        # 当前 SDK 只有一层包装；限制深度可避免异常对象意外形成循环。
        for _ in range(4):
            if not isinstance(current, oss.exceptions.OperationError):
                break
            current = current.unwrap()
        return current

    @classmethod
    def _translate_sdk_error(
        cls,
        error: oss.exceptions.BaseError,
        key: ObjectKey | None,
    ) -> ObjectStorageError:
        """兼容 SDK 的直接错误和 OperationError 包装错误。"""
        sdk_error = cls._unwrap_sdk_error(error)
        if isinstance(sdk_error, oss.exceptions.ServiceError):
            return cls._translate_service_error(sdk_error, key)
        if isinstance(sdk_error, oss.exceptions.RequestError):
            return cls._translate_request_error(sdk_error)
        if isinstance(sdk_error, TimeoutError):
            return ObjectStorageTimeoutError("OSS 请求超时")
        return ObjectStorageUnavailableError("OSS SDK 操作失败")

    @staticmethod
    def _translate_service_error(
        error: oss.exceptions.ServiceError,
        key: ObjectKey | None,
    ) -> ObjectStorageError:
        """把供应商状态码转换为调用方可稳定处理的错误。"""
        if error.status_code in {401, 403}:
            return ObjectAccessDeniedError(key)
        if error.status_code in {408, 504}:
            return ObjectStorageTimeoutError("OSS 请求超时")
        if error.status_code in {409, 412} and key is not None:
            return ObjectConflictError(key)
        return ObjectStorageUnavailableError("OSS 服务暂不可用")

    @staticmethod
    def _translate_request_error(
        error: oss.exceptions.RequestError,
    ) -> ObjectStorageError:
        """根据底层异常类型区分超时和其他传输失败。"""
        if isinstance(error.unwrap(), TimeoutError):
            return ObjectStorageTimeoutError("OSS 请求超时")
        return ObjectStorageUnavailableError("OSS 请求未能发送")
