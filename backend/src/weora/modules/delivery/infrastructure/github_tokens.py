"""GitHub App 凭据代理：私钥和安装令牌只在控制面内存中使用。"""

import asyncio
import base64
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.github import GitHubRemote, TokenLease


def _base64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class GitHubAppTokens:
    """供应商安装令牌通常有效一小时，本地仅复用十五分钟，不宣称缩短供应商期限。

    令牌只代理 HTTP 调用，不下放给 Runtime 或容器；轮换/关闭时尽力撤销供应商令牌。
    进程异常退出仍依赖供应商自身到期，不能宣称已经远端吊销。
    """

    def __init__(
        self, app_id: str, installation_id: int, private_key_pem: str, remotes: Mapping[str, GitHubRemote]
    ) -> None:
        if not re.fullmatch(r"[0-9]{1,30}", app_id) or installation_id <= 0:
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "GitHub App 身份配置无效")
        try:
            key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        except (ValueError, TypeError):
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "GitHub App 私钥配置无效") from None
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "GitHub App 必须使用至少 2048 位 RSA 私钥")
        self.app_id = app_id
        self.installation_id = installation_id
        self._private_key = key
        self._remotes = remotes
        self._leases: dict[str, TokenLease] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _jwt(self) -> str:
        now = int(datetime.now(UTC).timestamp())
        header = _base64(b'{"alg":"RS256","typ":"JWT"}')
        payload = _base64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode())
        message = f"{header}.{payload}".encode()
        signature = self._private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_base64(signature)}"

    async def issue(self, remote_id: str) -> TokenLease:
        """原生代码交付沿用原权限，不把协调器的新读取权限混入已批准的写令牌。"""
        return await self._issue(remote_id, "delivery", {"contents": "write", "pull_requests": "write"})

    async def observe(self, remote_id: str) -> TokenLease:
        """来源观察单独使用只读令牌；App 未获这些权限时明确失败，不回退到其他凭据。"""
        return await self._issue(
            remote_id,
            "observe",
            {"contents": "read", "pull_requests": "read", "issues": "read", "checks": "read", "statuses": "read"},
        )

    async def comments(self, remote_id: str) -> TokenLease:
        """Outbox 评论只需讨论写权，不携带 contents 写权，也不改变代码计划审批。"""
        return await self._issue(remote_id, "comments", {"issues": "write", "pull_requests": "write"})

    async def _issue(self, remote_id: str, purpose: str, permissions: dict[str, str]) -> TokenLease:
        remote = self._remotes.get(remote_id)
        if remote is None or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote.repository):
            raise DeliveryError("REMOTE_DENIED", "没有登记该远端的 GitHub App 授权")
        cache_key = remote_id + ":" + purpose
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            now = datetime.now(UTC)
            current = self._leases.get(cache_key)
            if current and current.expires_at > now + timedelta(seconds=30):
                return current
            if current:
                await self._revoke(current)
                self._leases.pop(cache_key, None)
            try:
                async with (
                    aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=15),
                        trust_env=False,
                        headers={
                            "Authorization": f"Bearer {self._jwt()}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                    ) as session,
                    session.post(
                        f"https://api.github.com/app/installations/{self.installation_id}/access_tokens",
                        json={
                            "repositories": [remote.repository.split("/")[1]],
                            "permissions": permissions,
                        },
                        allow_redirects=False,
                    ) as response,
                ):
                    if response.status != 201:
                        raise DeliveryError("CREDENTIAL_UNAVAILABLE", "GitHub App 不能签发登记仓库的安装令牌")
                    raw = await response.content.read(65537)
                    if len(raw) > 65536:
                        raise ValueError
                    data = json.loads(raw)
                    token = data["token"]
                    remote_expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
                    if not isinstance(token, str) or not token or remote_expiry <= now or remote_expiry.tzinfo is None:
                        raise ValueError
                    lease = TokenLease(token, min(remote_expiry, now + timedelta(minutes=15)))
                    self._leases[cache_key] = lease
                    return lease
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
                raise DeliveryError("CREDENTIAL_UNAVAILABLE", "GitHub App 安装令牌暂不可用") from None

    @staticmethod
    async def _revoke(lease: TokenLease) -> bool:
        """仅返回确认是否吊销；不输出供应商错误正文或令牌。"""
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                    trust_env=False,
                    headers={"Authorization": f"Bearer {lease.value}", "Accept": "application/vnd.github+json"},
                ) as session,
                session.delete("https://api.github.com/installation/token", allow_redirects=False) as response,
            ):
                return response.status == 204
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def aclose(self) -> bool:
        results = await asyncio.gather(*(self._revoke(lease) for lease in self._leases.values()))
        self._leases.clear()
        return all(results)
