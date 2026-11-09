"""以 HTTP 边界替身证明短时凭据和提交/分支/草稿 PR 序列，不执行远端写。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import aiohttp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.delivery.test_service import scope
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.docker import DockerSandbox
from weora.modules.delivery.infrastructure.github import GitHubDraftPullRequests, GitHubRemote, TokenLease
from weora.modules.delivery.infrastructure.github_tokens import GitHubAppTokens


class FakeHttp:
    """只替代 aiohttp.ClientSession 的最小接口；所有调用内容留在测试进程。"""

    created = 0
    revoked = 0
    permissions: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        self.status = 201
        self.content = self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        pass

    def post(self, url: str, **kwargs: object) -> Self:
        assert url == "https://api.github.com/app/installations/456/access_tokens"
        assert kwargs["allow_redirects"] is False
        type(self).created += 1
        type(self).permissions = kwargs["json"]  # type: ignore[assignment]
        return self

    def delete(self, url: str, **kwargs: object) -> Self:
        assert url == "https://api.github.com/installation/token"
        self.status = 204
        type(self).revoked += 1
        return self

    async def read(self, count: int) -> bytes:
        return json.dumps(
            {"token": "ephemeral-test-token", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
        ).encode()


@pytest.mark.anyio
async def test_installation_tokens_are_narrow_cached_and_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp.created, FakeHttp.revoked = 0, 0
    monkeypatch.setattr(aiohttp, "ClientSession", FakeHttp)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    tokens = GitHubAppTokens("123", 456, pem, {"test-remote": GitHubRemote("example/test", ("main",))})
    leases = await asyncio.gather(*(tokens.issue("test-remote") for _ in range(6)))
    assert FakeHttp.created == 1 and len({lease.value for lease in leases}) == 1
    assert leases[0].expires_at <= datetime.now(UTC) + timedelta(minutes=15)
    assert FakeHttp.permissions == {
        "repositories": ["test"],
        "permissions": {"contents": "write", "pull_requests": "write"},
    }
    assert await tokens.aclose() and FakeHttp.revoked == 1
    with pytest.raises(DeliveryError):
        await tokens.issue("unregistered-remote")


@pytest.mark.anyio
async def test_source_tokens_are_cached_separately_and_do_not_expand_code_write_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeHttp.created, FakeHttp.revoked = 0, 0
    monkeypatch.setattr(aiohttp, "ClientSession", FakeHttp)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    tokens = GitHubAppTokens("123", 456, pem, {"test-remote": GitHubRemote("example/test", ("main",))})
    await asyncio.gather(*(tokens.observe("test-remote") for _ in range(6)))
    assert FakeHttp.created == 1
    observe_permissions = FakeHttp.permissions["permissions"]
    assert isinstance(observe_permissions, dict)
    assert set(observe_permissions.values()) == {"read"}
    assert {"checks", "statuses", "issues"} <= set(observe_permissions)
    await tokens.comments("test-remote")
    comment_permissions = FakeHttp.permissions["permissions"]
    assert isinstance(comment_permissions, dict)
    assert FakeHttp.created == 2 and "contents" not in comment_permissions
    await tokens.issue("test-remote")
    assert FakeHttp.created == 3 and FakeHttp.permissions["permissions"] == {
        "contents": "write",
        "pull_requests": "write",
    }
    assert await tokens.aclose() and FakeHttp.revoked == 3


class NoTokens:
    async def issue(self, remote_id: str) -> TokenLease:
        raise AssertionError("HTTP 已被边界替身替代，不应签发真实凭据")


class ExportSandbox(DockerSandbox):
    async def export_changes(
        self, run_id: UUID, scope: PlanScope, *, include_modes: bool = False
    ) -> list[dict[str, str | None]]:
        change: dict[str, str | None] = {"path": "src/a.py", "content": "Zml4ZWQ="}
        if include_modes:
            change["mode"] = "100644"
        return [change, {"path": "tests/old.py", "content": None}]


class CaptureGitHub(GitHubDraftPullRequests):
    def __init__(self) -> None:
        super().__init__({"test-remote": GitHubRemote("example/test", ("main",))}, NoTokens(), ExportSandbox())
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.lookup: object = []

    async def _request(
        self, scope: PlanScope, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object:
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("pulls?"):
            return self.lookup
        if method == "GET":
            return {"tree": {"sha": "b" * 40}}
        if path == "pulls":
            return {"number": 42, "html_url": "https://github.com/example/test/pull/42"}
        return {"sha": "c" * 40}


@pytest.mark.anyio
async def test_create_uses_git_database_proxy_and_always_draft() -> None:
    remote = CaptureGitHub()
    result = await remote.create(uuid4(), scope(remote=True), "d" * 64, "Fix login", "Tests passed")
    assert result.remote_id == "42"
    assert [path for method, path, payload in remote.calls] == [
        "git/commits/" + "a" * 40,
        "git/blobs",
        "git/trees",
        "git/commits",
        "git/refs",
        "pulls",
    ]
    tree = remote.calls[2][2]
    assert tree and tree["tree"] == [
        {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "c" * 40},
        {"path": "tests/old.py", "mode": "100644", "type": "blob", "sha": None},
    ]
    pr = remote.calls[-1][2]
    assert pr and pr["draft"] is True and pr["base"] == "main"
    assert pr["head"] == "weora/" + "d" * 24
    assert "merge" not in str(remote.calls)


@pytest.mark.anyio
async def test_remote_reconcile_only_gets_and_matches_exact_business_marker() -> None:
    remote = CaptureGitHub()
    remote.lookup = [
        {
            "number": 42,
            "html_url": "https://github.com/example/test/pull/42",
            "body": "<!-- weora-delivery:" + "d" * 64 + " -->",
            "base": {"ref": "main"},
            "head": {"ref": "weora/" + "d" * 24},
        }
    ]
    found = await remote.reconcile(scope(remote=True), "d" * 64)
    assert found and found.remote_id == "42"
    assert all(method == "GET" for method, path, payload in remote.calls)
    assert await remote.reconcile(scope(remote=True), "e" * 64) is None
