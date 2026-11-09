"""只验证普通 MR HTTP 协议；不将替身测试当作真实云效验收。"""

import json
from dataclasses import replace

import aiohttp
import pytest

from tests.delivery.test_code_hosting import connection
from tests.delivery.test_codeup import Credentials, FakeHttp, review
from tests.delivery.test_service import scope
from weora.modules.delivery.application.code_hosting import PublishedBranch
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.codeup_existing_review import CodeupExistingReviews


class Existing(CodeupExistingReviews):
    rows: list[dict[str, object]] = []

    async def _page(self, connection, page):
        return self.rows, None


def inputs():
    target = connection()
    selected = replace(
        scope(remote=True),
        actions=("review.create",),
        commands=(),
        source_delivery_id="00000000-0000-0000-0000-000000000001",
        remote_binding=target.fingerprint,
    )
    return target, selected, PublishedBranch("weora/" + "d" * 24, "b" * 40)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [201, 302, 403, 503])
async def test_single_post_is_bounded_and_never_merges(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    calls = []

    class Http(FakeHttp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.status = status

        def post(self, url, **kwargs):
            calls.append(url)
            assert url.endswith("/repositories/123/changeRequests")
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"] == {"x-yunxiao-token": "test-only-token"}
            body = kwargs["json"]
            assert body["sourceBranch"] == "weora/" + "d" * 24 and body["targetBranch"] == "main"
            assert body["triggerAIReviewRun"] is False
            assert "workInProgress" not in body and "merge" not in url
            return self

        async def read(self, count):
            if status != 201:
                return b"secret-not-for-output"
            return json.dumps(review() | {"status": "UNDER_REVIEW", "workInProgress": False}).encode()

    monkeypatch.setattr(aiohttp, "ClientSession", Http)
    adapter = Existing(Credentials())
    if status == 201:
        result = await adapter.create_existing(*inputs(), "d" * 64, "测试", "不合并")
        assert result.remote_id == "42"
    else:
        with pytest.raises((DeliveryError, ConnectionError)) as error:
            await adapter.create_existing(*inputs(), "d" * 64, "测试", "不合并")
        assert "secret-not-for-output" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.anyio
async def test_existing_business_key_is_read_back_without_post() -> None:
    adapter = Existing(Credentials())
    adapter.rows = [review() | {"status": "UNDER_REVIEW", "workInProgress": False}]
    result = await adapter.create_existing(*inputs(), "d" * 64, "测试", "不合并")
    assert result.remote_id == "42"


@pytest.mark.parametrize(
    "changes",
    [
        {"webUrl": "https://evil.test/change/42"},
        {"status": "MERGED"},
        {"localId": True},
    ],
)
def test_unsafe_or_merged_result_cannot_be_reported_as_open_mr(changes) -> None:
    target, selected, branch = inputs()
    with pytest.raises(DeliveryError):
        CodeupExistingReviews._result(
            target, selected, branch, "d" * 64, review() | {"status": "UNDER_REVIEW"} | changes
        )


@pytest.mark.anyio
async def test_real_terminal_page_shape_reaches_single_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实云效末页 x-next-page 回填当前页；仅在总页数和总数同时佐证时接受。"""
    calls = []

    class Http(FakeHttp):
        mode = "get"

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.headers = {"x-next-page": "1", "x-page": "1", "x-total-pages": "1", "x-total": "1"}

        def post(self, url, **kwargs):
            calls.append(url)
            self.mode, self.status = "post", 201
            return self

        async def read(self, count):
            if self.mode == "get":
                return json.dumps([review() | {"sourceBranch": "unrelated", "state": "UNDER_REVIEW"}]).encode()
            return json.dumps(review() | {"status": "UNDER_REVIEW"}).encode()

    monkeypatch.setattr(aiohttp, "ClientSession", Http)
    result = await CodeupExistingReviews(Credentials()).create_existing(*inputs(), "d" * 64, "测试", "不合并")
    assert result.remote_id == "42" and len(calls) == 1


def test_real_codeup_list_uses_repo_web_url_and_verified_detail_url() -> None:
    target, selected, branch = inputs()
    row = review() | {
        "state": "TO_BE_MERGED",
        "workInProgress": False,
        "webUrl": target.web_url,
        "detailUrl": target.web_url + "/change/42",
    }
    result = CodeupExistingReviews._result(target, selected, branch, "d" * 64, row)
    assert result and result.url == target.web_url + "/change/42"
