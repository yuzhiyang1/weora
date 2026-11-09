"""自助配置的 MR 仓库数字 ID 与 Git 目标必须在 POST 前通过只读映射。"""

import json
from typing import Self

import aiohttp
import pytest

from tests.delivery.test_code_hosting import connection
from tests.delivery.test_codeup import Credentials, FakeHttp
from weora.modules.delivery.domain.models import ReviewNotSent
from weora.modules.delivery.infrastructure.codeup_existing_review import CodeupExistingReviews


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["ok", "wrong_id", "wrong_clone", "wrong_web", "redirect", "bad_json", "oversize"])
async def test_repository_mapping_uses_one_bounded_get_never_post(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    target = connection()
    calls: list[str] = []

    class Http(FakeHttp):
        def get(self, url: str, **kwargs: object) -> Self:
            calls.append(url)
            assert url.endswith("/repositories/123")
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"] == {"x-yunxiao-token": "test-only-token"}
            if mode == "redirect":
                self.status = 302
            return self

        async def read(self, count: int) -> bytes:
            assert count == 512 * 1024 + 1
            if mode == "bad_json":
                return b"not-json-sensitive-body"
            if mode == "oversize":
                return b"a" * count
            data = {"id": 123, "httpUrlToRepo": target.clone_url, "webUrl": target.web_url}
            if mode == "wrong_id":
                data["id"] = 456
            if mode == "wrong_clone":
                data["httpUrlToRepo"] = "https://git.example.test/other.git"
            if mode == "wrong_web":
                data["webUrl"] = "https://git.example.test/other"
            return json.dumps(data).encode()

    monkeypatch.setattr(aiohttp, "ClientSession", Http)
    adapter = CodeupExistingReviews(Credentials())
    if mode == "ok":
        await adapter.verify_repository(target)
    else:
        with pytest.raises(ReviewNotSent) as failed:
            await adapter.verify_repository(target)
        assert "sensitive-body" not in str(failed.value)
    assert len(calls) == 1
