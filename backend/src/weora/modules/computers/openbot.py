"""OpenBot 的 HTTP 边界。电脑地址和凭据只在服务端派生。"""

import hashlib
import hmac
import json
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp


class ComputerError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class OpenBot:
    def __init__(self, url: str, supervisor_token: str, master: str, namespace: str) -> None:
        self.url, self.supervisor_token, self.master, self.namespace = (
            url.rstrip("/"),
            supervisor_token,
            master,
            namespace,
        )
        self.loopback = urlsplit(url).hostname == "127.0.0.1"

    @staticmethod
    def computer_id(workspace_id: UUID, user_id: UUID, task_id: UUID, *, observe: bool = False) -> str:
        # 同一用户的同一 Task 续接复用浏览器；新任务不继承其他任务的登录态。
        scope = workspace_id.bytes + user_id.bytes + b"task-v1" + task_id.bytes
        # 只读浏览器与可人工操作的浏览器使用不同配置卷，避免继承登录态。
        return "run-" + hashlib.sha256(scope + (b"observe" if observe else b"")).hexdigest()[:32]

    async def _request(
        self, url: str, token: str, body: dict[str, object] | None = None, *, deadline_seconds: int = 12
    ) -> dict[str, object]:
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=deadline_seconds)) as client,
                client.request(
                    "GET" if body is None else "POST",
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    allow_redirects=False,
                ) as response,
            ):
                if response.status == 409:
                    raise ComputerError(409, "COMPUTER_CONTROL_CONFLICT", "电脑控制权或页面快照已变化，请刷新后重试")
                if response.status != 200:
                    raise ComputerError(502, "COMPUTER_UPSTREAM_FAILED", "电脑服务未完成请求")
                buffer = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    buffer.extend(chunk)
                    if len(buffer) > 2_000_000:
                        raise ComputerError(502, "COMPUTER_PROTOCOL", "电脑服务响应超出限制")
        except (aiohttp.ClientError, TimeoutError):
            raise ComputerError(503, "COMPUTER_UNAVAILABLE", "电脑服务未就绪或请求超时") from None

        try:
            payload = json.loads(buffer)
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            return cast(dict[str, object], payload)
        except ValueError:
            raise ComputerError(502, "COMPUTER_PROTOCOL", "电脑服务响应无效") from None

    async def supervisor(self, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        return await self._request(
            self.url + path, self.supervisor_token, body, deadline_seconds=80 if body is not None else 5
        )

    async def call(
        self, computer_id: str, path: str, body: dict[str, object] | None = None, *, deadline_seconds: int = 12
    ) -> dict[str, object]:
        # WSL 宿主模式只接受 Supervisor 为目标容器登记的回环端口。URL 始终由平台构造。
        host = f"{self.namespace}-computer-{computer_id}:4100"
        if self.loopback:
            item = await self._computer(computer_id)
            port = item.get("port") if item else None
            if (
                not item
                or item.get("container") != f"{self.namespace}-computer-{computer_id}"
                or (type(port) is not int or not 1024 <= port <= 65535)
            ):
                raise ComputerError(503, "COMPUTER_UNAVAILABLE", "电脑回环端口未就绪")
            host = f"127.0.0.1:{port}"
        token = hmac.digest(self.master.encode(), f"weora-computer:{computer_id}".encode(), "sha256").hex()
        return await self._request(
            f"http://{host}{path}",
            token,
            body,
            deadline_seconds=deadline_seconds,
        )

    async def _computer(self, computer_id: str) -> dict[str, object] | None:
        computers = (await self.supervisor("/computers")).get("computers")
        if not isinstance(computers, list):
            raise ComputerError(502, "COMPUTER_PROTOCOL", "电脑状态响应无效")
        for item in computers:
            if isinstance(item, dict) and item.get("botId") == computer_id:
                return cast(dict[str, object], item)
        return None

    async def state(self, computer_id: str) -> str | None:
        item = await self._computer(computer_id)
        if item is None:
            return None
        state = item.get("status")
        if not isinstance(state, str):
            raise ComputerError(502, "COMPUTER_PROTOCOL", "电脑状态响应无效")
        return state

    async def control(self, computer_id: str, action: str) -> None:
        current = await self.call(computer_id, "/control")
        if current.get("holder") == ("human" if action == "take" else "bot") and not current.get("transitioning"):
            return
        request = current.get("request")
        if action == "take" and (not isinstance(request, dict) or request.get("status") not in {"waiting", "taken"}):
            current = await self.call(computer_id, "/control/request", {"reason": "Weora 用户请求接管桌面"})
            request = current.get("request")
        if not isinstance(request, dict) or not isinstance(request.get("id"), str):
            raise ComputerError(409, "COMPUTER_CONTROL_CONFLICT", "没有可以切换的接管请求")
        await self.call(
            computer_id,
            f"/control/{action}",
            {"requestId": request["id"]},
            deadline_seconds=35 if action == "take" else 12,
        )
