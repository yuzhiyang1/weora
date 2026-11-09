"""阿里云 OSS Adapter 的边界行为测试。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import alibabacloud_oss_v2 as oss
import pytest

from weora.infrastructure.object_storage.aliyun_oss import AliyunOssObjectStorage
from weora.shared.object_storage import BytesObjectSource, ObjectKey
from weora.shared.object_storage.errors import (
    ObjectAccessDeniedError,
    ObjectIntegrityError,
    ObjectStorageTimeoutError,
    ObjectStorageUnavailableError,
)


@dataclass(slots=True)
class _StoredObject:
    content: bytes
    content_type: str
    metadata: dict[str, str]
    last_modified: datetime


class _FakeAsyncBody:
    """提供与 OSS AsyncStreamBody 等价的最小测试边界。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    async def iter_bytes(self, **_: Any) -> AsyncIterator[bytes]:
        async def chunks() -> AsyncIterator[bytes]:
            yield self.content

        return chunks()

    async def close(self) -> None:
        self.closed = True


class _FakeAliyunOssClient:
    """只模拟外部 SDK 边界，不替代 Weora 自己的 Adapter。"""

    def __init__(self) -> None:
        self.objects: dict[str, _StoredObject] = {}
        self.last_body: _FakeAsyncBody | None = None
        self.probed_bucket: str | None = None
        self.closed = False

    async def head_object(self, request: oss.HeadObjectRequest, **_: Any) -> oss.HeadObjectResult:
        stored = self.objects.get(request.key)
        if stored is None:
            raise oss.exceptions.ServiceError(
                status_code=404,
                code="NoSuchKey",
                message="not found",
                request_id="request-id",
                ec="",
                timestamp="",
                request_target="",
            )
        return oss.HeadObjectResult(
            content_length=len(stored.content),
            content_type=stored.content_type,
            metadata=stored.metadata,
            last_modified=stored.last_modified,
            etag='"fake-etag"',
        )

    async def put_object(self, request: oss.PutObjectRequest, **_: Any) -> oss.PutObjectResult:
        assert request.body is not None
        assert hasattr(request.body, "read")
        content = request.body.read()
        assert isinstance(content, bytes)
        self.objects[request.key] = _StoredObject(
            content=content,
            content_type=request.content_type or "application/octet-stream",
            metadata=dict(request.metadata or {}),
            last_modified=datetime.now(UTC),
        )
        return oss.PutObjectResult(etag='"fake-etag"')

    async def get_object(self, request: oss.GetObjectRequest, **_: Any) -> oss.GetObjectResult:
        stored = self.objects[request.key]
        self.last_body = _FakeAsyncBody(stored.content)
        return oss.GetObjectResult(
            content_length=len(stored.content),
            content_type=stored.content_type,
            metadata=stored.metadata,
            last_modified=stored.last_modified,
            etag='"fake-etag"',
            body=self.last_body,
        )

    async def delete_object(self, request: oss.DeleteObjectRequest, **_: Any) -> oss.DeleteObjectResult:
        self.objects.pop(request.key, None)
        return oss.DeleteObjectResult()

    async def copy_object(self, request: oss.CopyObjectRequest, **_: Any) -> oss.CopyObjectResult:
        source = self.objects[request.source_key or ""]
        self.objects[request.key] = _StoredObject(
            content=source.content,
            content_type=source.content_type,
            metadata=dict(source.metadata),
            last_modified=datetime.now(UTC),
        )
        return oss.CopyObjectResult(etag='"copied-etag"')

    async def get_bucket_info(
        self,
        request: oss.GetBucketInfoRequest,
        **_: Any,
    ) -> oss.GetBucketInfoResult:
        self.probed_bucket = request.bucket
        return oss.GetBucketInfoResult()

    async def close(self) -> None:
        self.closed = True


def _service_error(status_code: int, code: str) -> oss.exceptions.ServiceError:
    """构造包含 SDK 必填诊断字段的服务端错误。"""
    return oss.exceptions.ServiceError(
        status_code=status_code,
        code=code,
        message="test error",
        request_id="request-id",
        ec="",
        timestamp="",
        request_target="",
    )


class _FailingHeadClient(_FakeAliyunOssClient):
    """让 HeadObject 返回指定供应商错误。"""

    def __init__(self, error: oss.exceptions.ServiceError) -> None:
        super().__init__()
        self._error = error

    async def head_object(self, request: oss.HeadObjectRequest, **_: Any) -> oss.HeadObjectResult:
        raise self._error


class _RequestFailingHeadClient(_FakeAliyunOssClient):
    """模拟请求尚未收到服务端响应时的 SDK 失败。"""

    async def head_object(self, request: oss.HeadObjectRequest, **_: Any) -> oss.HeadObjectResult:
        raise oss.exceptions.RequestError(error=TimeoutError("timed out"))


class _OperationFailingHeadClient(_FakeAliyunOssClient):
    """复现真实 SDK 把服务端错误包装为 OperationError 的调用链。"""

    async def head_object(self, request: oss.HeadObjectRequest, **_: Any) -> oss.HeadObjectResult:
        raise oss.exceptions.OperationError(
            name="HeadObject",
            error=_service_error(404, "NoSuchKey"),
        )


class _ConcurrentSameContentPutClient(_FakeAliyunOssClient):
    """模拟另一个请求先写入相同内容后触发禁止覆盖。"""

    async def put_object(self, request: oss.PutObjectRequest, **kwargs: Any) -> oss.PutObjectResult:
        await super().put_object(request, **kwargs)
        raise _service_error(409, "FileAlreadyExists")


@pytest.mark.anyio
async def test_put_returns_verified_metadata_and_hides_physical_prefix() -> None:
    """写入成功必须经过 Head 校验，调用方始终只看到逻辑对象键。"""
    client = _FakeAliyunOssClient()
    storage = AliyunOssObjectStorage(
        client=client,
        bucket="weora-test",
        prefix="weora/test",
    )
    key = ObjectKey("skills/workspace-1/demo.skill")
    source = BytesObjectSource(b"skill-package", content_type="application/zip")

    stored = await storage.put(key, source)

    assert stored.key == key
    assert stored.sha256 == source.sha256
    assert stored.content_length == source.content_length
    assert "weora/test/skills/workspace-1/demo.skill" in client.objects


@pytest.mark.anyio
async def test_read_streams_content_and_closes_sdk_response() -> None:
    """读取完成后必须关闭 SDK Body，把连接归还连接池。"""
    client = _FakeAliyunOssClient()
    storage = AliyunOssObjectStorage(
        client=client,
        bucket="weora-test",
        prefix="weora/test",
    )
    key = ObjectKey("skills/workspace-1/demo.skill")
    await storage.put(key, BytesObjectSource(b"streamed-content", content_type="application/zip"))

    async with await storage.read(key) as object_read:
        content = b"".join([chunk async for chunk in object_read])

    assert content == b"streamed-content"
    assert client.last_body is not None
    assert client.last_body.closed


@pytest.mark.anyio
async def test_delete_is_idempotent_for_missing_oss_objects() -> None:
    """清理任务重复删除同一对象时不应制造额外失败。"""
    client = _FakeAliyunOssClient()
    storage = AliyunOssObjectStorage(
        client=client,
        bucket="weora-test",
        prefix="weora/test",
    )
    key = ObjectKey("skills/workspace-1/orphan.skill")
    await storage.put(key, BytesObjectSource(b"orphan", content_type="application/zip"))

    await storage.delete(key)
    await storage.delete(key)

    assert await storage.stat(key) is None


@pytest.mark.anyio
async def test_copy_keeps_application_digest_on_the_target_object() -> None:
    """草稿发布为版本对象后，目标必须保留同一个应用 SHA-256。"""
    client = _FakeAliyunOssClient()
    storage = AliyunOssObjectStorage(
        client=client,
        bucket="weora-test",
        prefix="weora/test",
    )
    source_key = ObjectKey("skills/workspace-1/draft/demo.skill")
    target_key = ObjectKey("skills/workspace-1/versions/0.1.0/demo.skill")
    source = BytesObjectSource(b"published", content_type="application/zip")
    await storage.put(source_key, source)

    copied = await storage.copy(source_key, target_key, expected_sha256=source.sha256)

    assert copied.key == target_key
    assert copied.sha256 == source.sha256
    assert copied.content_length == source.content_length


@pytest.mark.anyio
async def test_probe_checks_configured_bucket_and_close_releases_client() -> None:
    """Readiness 只探测既有 Bucket，应用退出时释放 SDK 连接池。"""
    client = _FakeAliyunOssClient()
    storage = AliyunOssObjectStorage(
        client=client,
        bucket="weora-test",
        prefix="weora/test",
    )

    await storage.probe()
    await storage.aclose()

    assert client.probed_bucket == "weora-test"
    assert client.closed


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (403, ObjectAccessDeniedError),
        (503, ObjectStorageUnavailableError),
    ],
)
async def test_stat_preserves_access_and_availability_error_meaning(
    status_code: int,
    expected_error: type[Exception],
) -> None:
    """权限问题和供应商故障不能被误报为对象不存在。"""
    client = _FailingHeadClient(_service_error(status_code, "TestError"))
    storage = AliyunOssObjectStorage(
        client=client,
        bucket="weora-test",
        prefix="weora/test",
    )

    with pytest.raises(expected_error):
        await storage.stat(ObjectKey("skills/workspace-1/demo.skill"))


@pytest.mark.anyio
async def test_stat_treats_sdk_wrapped_404_as_missing() -> None:
    """真实 SDK 包装后的 NoSuchKey 仍应保持 Port 的“对象不存在”语义。"""
    storage = AliyunOssObjectStorage(
        client=_OperationFailingHeadClient(),
        bucket="weora-test",
        prefix="weora/test",
    )

    assert await storage.stat(ObjectKey("skills/workspace-1/missing.skill")) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"weora-sha256": "not-a-sha256"},
    ],
)
async def test_stat_rejects_missing_or_invalid_application_digest(
    metadata: dict[str, str],
) -> None:
    """历史或外部对象摘要无效时，不能使用 ETag 冒充完整性校验。"""
    client = _FakeAliyunOssClient()
    client.objects["weora/test/skills/workspace-1/demo.skill"] = _StoredObject(
        content=b"unknown",
        content_type="application/zip",
        metadata=metadata,
        last_modified=datetime.now(UTC),
    )
    storage = AliyunOssObjectStorage(
        client=client,
        bucket="weora-test",
        prefix="weora/test",
    )

    with pytest.raises(ObjectIntegrityError):
        await storage.stat(ObjectKey("skills/workspace-1/demo.skill"))


@pytest.mark.anyio
async def test_stat_maps_transport_timeout_without_parsing_error_text() -> None:
    """网络超时应保留可观测语义，不依赖供应商异常字符串。"""
    storage = AliyunOssObjectStorage(
        client=_RequestFailingHeadClient(),
        bucket="weora-test",
        prefix="weora/test",
    )

    with pytest.raises(ObjectStorageTimeoutError):
        await storage.stat(ObjectKey("skills/workspace-1/demo.skill"))


@pytest.mark.anyio
async def test_concurrent_same_content_put_is_idempotent() -> None:
    """并发写入相同摘要时，禁止覆盖错误应收敛为同一次成功。"""
    client = _ConcurrentSameContentPutClient()
    storage = AliyunOssObjectStorage(
        client=client,
        bucket="weora-test",
        prefix="weora/test",
    )
    key = ObjectKey("skills/workspace-1/demo.skill")
    source = BytesObjectSource(b"same-content", content_type="application/zip")

    stored = await storage.put(key, source)

    assert stored.sha256 == source.sha256
