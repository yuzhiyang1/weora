"""短时凭据代理创建提交、受控分支与草稿 PR，不把 token 放入沙箱或模型。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

import aiohttp

from weora.modules.delivery.application.ports import RemoteResult
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.docker import DockerSandbox


@dataclass(frozen=True)
class TokenLease:
    value: str = field(repr=False)
    expires_at: datetime


class ShortLivedTokens(Protocol):
    async def issue(self, remote_id: str) -> TokenLease:
        """由应用安装身份签发最多 15 分钟的短时凭据，不读取开发者个人登录态。"""
        ...


@dataclass(frozen=True)
class GitHubRemote:
    repository: str
    target_branches: tuple[str, ...]


class UnconfiguredPullRequests:
    async def create(self, run_id: UUID, scope: PlanScope, business_key: str, title: str, body: str) -> RemoteResult:
        raise DeliveryError("PR_NOT_CONFIGURED", "尚未配置测试远端和短时凭据代理")

    async def reconcile(self, scope: PlanScope, business_key: str) -> RemoteResult | None:
        raise DeliveryError("PR_NOT_CONFIGURED", "尚未配置远端对账能力")


class GitHubDraftPullRequests:
    """只支持管理员登记的 github.com 仓库；不接受任意 API 主机或 URL。"""

    def __init__(self, remotes: Mapping[str, GitHubRemote], tokens: ShortLivedTokens, sandbox: DockerSandbox) -> None:
        self.remotes = remotes
        self.tokens = tokens
        self.sandbox = sandbox

    def _remote(self, scope: PlanScope) -> GitHubRemote:
        remote = self.remotes.get(scope.remote_id or "")
        if remote is None or scope.target_branch not in remote.target_branches:
            raise DeliveryError("REMOTE_DENIED", "远端或目标分支未登记")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote.repository):
            raise DeliveryError("PR_NOT_CONFIGURED", "远端仓库配置格式错误")
        return remote

    async def _request(
        self, scope: PlanScope, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object:
        remote = self._remote(scope)
        lease = await self.tokens.issue(scope.remote_id or "")
        now = datetime.now(UTC)
        if not lease.value or lease.expires_at <= now or lease.expires_at > now + timedelta(minutes=15):
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "远端凭据不是有效的短时授权")
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20),
                    trust_env=False,
                    headers={
                        "Authorization": f"Bearer {lease.value}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                ) as session,
                session.request(
                    method,
                    f"https://api.github.com/repos/{remote.repository}/{path}",
                    json=payload,
                    allow_redirects=False,
                ) as response,
            ):
                if response.status >= 500:
                    raise TimeoutError("REMOTE_UNKNOWN")
                if response.status >= 300:
                    raise DeliveryError("REMOTE_REJECTED", "远端拒绝该授权操作，请检查仓库及分支权限")
                if response.content_length and response.content_length > 2 * 1024 * 1024:
                    raise DeliveryError("REMOTE_PROTOCOL", "远端响应超过限制")
                data = await response.content.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise DeliveryError("REMOTE_PROTOCOL", "远端响应超过限制")
                import json

                return cast(object, json.loads(data))
        except aiohttp.ClientError:
            raise ConnectionError("REMOTE_UNKNOWN") from None
        except ValueError:
            raise DeliveryError("REMOTE_PROTOCOL", "远端返回了无效 JSON") from None

    @staticmethod
    def _object(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise DeliveryError("REMOTE_PROTOCOL", "远端响应结构错误")
        return cast(dict[str, object], value)

    @staticmethod
    def _sha(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise DeliveryError("REMOTE_PROTOCOL", "远端没有返回合法提交标识")
        return value

    async def create(self, run_id: UUID, scope: PlanScope, business_key: str, title: str, body: str) -> RemoteResult:
        self._remote(scope)
        changes = await self.sandbox.export_changes(run_id, scope)
        if not changes:
            raise DeliveryError("EMPTY_DIFF", "没有改动，不创建空 PR")
        commit = self._object(await self._request(scope, "GET", f"git/commits/{scope.base_revision}"))
        tree = self._object(commit.get("tree"))
        base_tree = self._sha(tree.get("sha"))
        entries: list[dict[str, object]] = []
        for change in changes:
            sha: str | None = None
            if change["content"] is not None:
                blob = self._object(
                    await self._request(
                        scope,
                        "POST",
                        "git/blobs",
                        {
                            "content": change["content"],
                            "encoding": "base64",
                        },
                    )
                )
                sha = self._sha(blob.get("sha"))
            entries.append({"path": change["path"], "mode": "100644", "type": "blob", "sha": sha})
        created_tree = self._object(
            await self._request(
                scope,
                "POST",
                "git/trees",
                {
                    "base_tree": base_tree,
                    "tree": entries,
                },
            )
        )
        marker = f"<!-- weora-delivery:{business_key} -->"
        created_commit = self._object(
            await self._request(
                scope,
                "POST",
                "git/commits",
                {
                    "message": title + "\n\n" + marker,
                    "tree": self._sha(created_tree.get("sha")),
                    "parents": [scope.base_revision],
                },
            )
        )
        branch = f"weora/{business_key[:24]}"
        await self._request(
            scope,
            "POST",
            "git/refs",
            {
                "ref": f"refs/heads/{branch}",
                "sha": self._sha(created_commit.get("sha")),
            },
        )
        pr = self._object(
            await self._request(
                scope,
                "POST",
                "pulls",
                {
                    "title": title,
                    "head": branch,
                    "base": scope.target_branch,
                    "body": body + "\n\n" + marker,
                    "draft": True,
                    "maintainer_can_modify": False,
                },
            )
        )
        return replace(
            self._result(pr, scope), branch_name=branch, branch_revision=self._sha(created_commit.get("sha"))
        )

    def _result(self, pr: dict[str, object], scope: PlanScope) -> RemoteResult:
        remote = self._remote(scope)
        number, url = pr.get("number"), pr.get("html_url")
        if not isinstance(number, int) or not isinstance(url, str):
            raise DeliveryError("REMOTE_PROTOCOL", "远端没有返回可核验的 PR 标识")
        if url != f"https://github.com/{remote.repository}/pull/{number}":
            raise DeliveryError("REMOTE_PROTOCOL", "远端 PR 链接与登记仓库不匹配")
        return RemoteResult(str(number), url)

    async def reconcile(self, scope: PlanScope, business_key: str) -> RemoteResult | None:
        remote = self._remote(scope)
        branch = f"weora/{business_key[:24]}"
        owner = remote.repository.split("/")[0]
        data = await self._request(scope, "GET", f"pulls?state=all&head={owner}:{branch}&per_page=100")
        if not isinstance(data, list):
            raise DeliveryError("REMOTE_PROTOCOL", "远端对账结构错误")
        marker = f"<!-- weora-delivery:{business_key} -->"
        for item in data:
            pr = self._object(item)
            base = self._object(pr.get("base"))
            head = self._object(pr.get("head"))
            if (
                marker in str(pr.get("body", ""))
                and base.get("ref") == scope.target_branch
                and head.get("ref") == branch
            ):
                return self._result(pr, scope)
        return None
