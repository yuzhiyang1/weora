"""云效只读适配契约：分页、原连接、业务键、草稿和地址都必须匹配。"""

from dataclasses import replace
from types import TracebackType
from typing import Self

import aiohttp
import pytest

from tests.delivery.test_code_hosting import connection
from tests.delivery.test_service import scope
from weora.modules.delivery.application.code_hosting import RepositoryConnection
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.codeup import CodeupReviews
from weora.modules.delivery.infrastructure.git import CredentialMaterial


class Credentials:
    def get(self, reference: str) -> CredentialMaterial:
        assert reference == "test-credential-v1"
        return CredentialMaterial(api_token="test-only-token")


class CaptureCodeup(CodeupReviews):
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        super().__init__(Credentials())
        self.pages = pages
        self.reads: list[int] = []

    async def _page(self, connection: RepositoryConnection, page: int) -> tuple[list[dict[str, object]], int | None]:
        self.reads.append(page)
        return self.pages[page - 1], page + 1 if page < len(self.pages) else None


def review() -> dict[str, object]:
    return {
        "sourceBranch": "weora/" + "d" * 24,
        "targetBranch": "main",
        "sourceProjectId": 123,
        "targetProjectId": 123,
        "localId": 42,
        "description": "<!-- weora-delivery:" + "d" * 64 + " -->",
        "workInProgress": True,
        "webUrl": "https://git.example.test/team/project/change/42",
    }


@pytest.mark.anyio
async def test_readback_follows_pages_and_never_creates_formal_review() -> None:
    target = connection()
    adapter = CaptureCodeup([[], [review()]])
    found = await adapter.reconcile(target, replace(scope(remote=True), remote_binding=target.fingerprint), "d" * 64)
    assert found and found.remote_id == "42" and adapter.reads == [1, 2]
    assert adapter.capabilities.draft is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field,value",
    [
        ("sourceBranch", "other"),
        ("targetBranch", "master"),
        ("sourceProjectId", 999),
        ("targetProjectId", 999),
        ("description", "<!-- weora-delivery:other -->"),
        ("workInProgress", False),
    ],
)
async def test_mismatched_review_is_not_accepted(field: str, value: object) -> None:
    target = connection()
    adapter = CaptureCodeup([[review() | {field: value}]])
    assert (
        await adapter.reconcile(target, replace(scope(remote=True), remote_binding=target.fingerprint), "d" * 64)
        is None
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "rows,code",
    [
        ([review(), review()], "REMOTE_AMBIGUOUS"),
        ([review() | {"webUrl": "https://evil.example/review/42"}], "REMOTE_PROTOCOL"),
        ([review() | {"localId": True}], "REMOTE_PROTOCOL"),
    ],
)
async def test_unsafe_or_ambiguous_remote_evidence_is_rejected(rows: list[dict[str, object]], code: str) -> None:
    target = connection()
    with pytest.raises(DeliveryError) as error:
        await CaptureCodeup([rows]).reconcile(
            target, replace(scope(remote=True), remote_binding=target.fingerprint), "d" * 64
        )
    assert error.value.code == code


@pytest.mark.anyio
async def test_pagination_exhaustion_is_not_absence() -> None:
    target, adapter = connection(), CaptureCodeup([[] for _ in range(6)])
    with pytest.raises(DeliveryError) as error:
        await adapter.reconcile(target, replace(scope(remote=True), remote_binding=target.fingerprint), "d" * 64)
    assert error.value.code == "RECONCILIATION_INCOMPLETE" and adapter.reads == [1, 2, 3, 4, 5]


class FakeHttp:
    """真实请求装配边界替身，只允许 GET，检查令牌、路径和禁止重定向。"""

    calls: list[str] = []

    def __init__(self, **kwargs: object) -> None:
        assert kwargs["trust_env"] is False
        self.status, self.content, self.headers = 200, self, {"x-next-page": ""}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    def get(self, url: str, **kwargs: object) -> Self:
        self.calls.append(url)
        assert kwargs["headers"] == {"x-yunxiao-token": "test-only-token"}
        assert kwargs["allow_redirects"] is False
        assert kwargs["params"] == {"projectIds": "123", "page": "1", "perPage": "100"}
        return self

    async def read(self, count: int) -> bytes:
        return b"[]"


@pytest.mark.anyio
@pytest.mark.parametrize("organization", ["organization", ""])
async def test_http_uses_official_central_and_region_paths(monkeypatch: pytest.MonkeyPatch, organization: str) -> None:
    monkeypatch.setattr(aiohttp, "ClientSession", FakeHttp)
    target = replace(connection(), organization=organization)
    assert await CodeupReviews(Credentials())._page(target, 1) == ([], None)
    expected = "/organizations/organization" if organization else ""
    assert FakeHttp.calls[-1] == "https://api.example.test/oapi/v1/codeup" + expected + "/changeRequests"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,body,headers,code",
    [
        (302, b"secret-error-content", {}, "REMOTE_REJECTED"),
        (403, b"secret-error-content", {}, "REMOTE_REJECTED"),
        (200, b"not-json", {}, "REMOTE_PROTOCOL"),
        (200, b"{}", {}, "REMOTE_PROTOCOL"),
        (200, b"[]", {"x-next-page": "https://evil.example/page"}, "REMOTE_PROTOCOL"),
        (200, b"[]", {"x-next-page": "1"}, "REMOTE_PROTOCOL"),
        (200, b"[]", {"x-next-page": "1", "x-page": "1", "x-total-pages": "2", "x-total": "101"}, "REMOTE_PROTOCOL"),
        (200, b"[]", {"x-next-page": "1", "x-page": "1", "x-total-pages": "1", "x-total": "1"}, "REMOTE_PROTOCOL"),
        (200, b"x" * (2 * 1024 * 1024 + 1), {}, "REMOTE_PROTOCOL"),
        (200, b"[" + b",".join([b"{}"] * 100) + b"]", {}, "RECONCILIATION_INCOMPLETE"),
    ],
    ids=[
        "redirect",
        "denied",
        "invalid-json",
        "invalid-object",
        "next-url",
        "repeated-page",
        "repeated-nonfinal-page",
        "inconsistent-final-count",
        "oversized",
        "missing-page",
    ],
)
async def test_http_refuses_redirects_bad_protocol_and_incomplete_pages(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    headers: dict[str, str],
    code: str,
) -> None:
    class ResponseHttp(FakeHttp):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.status, self.headers = status, headers

        async def read(self, count: int) -> bytes:
            return body

    monkeypatch.setattr(aiohttp, "ClientSession", ResponseHttp)
    with pytest.raises(DeliveryError) as error:
        await CodeupReviews(Credentials())._page(connection(), 1)
    assert error.value.code == code
    assert "secret-error-content" not in str(error.value)


@pytest.mark.anyio
async def test_http_server_failure_is_unknown_not_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingHttp(FakeHttp):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.status = 503

    monkeypatch.setattr(aiohttp, "ClientSession", FailingHttp)
    with pytest.raises(ConnectionError, match="REMOTE_UNKNOWN"):
        await CodeupReviews(Credentials())._page(connection(), 1)
