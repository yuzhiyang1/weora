"""固定大陆飞书 OpenAPI 应用身份；不接受模型 URL、个人 token 或任意 HTTP 代理。"""

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote

import aiohttp
from cryptography.fernet import Fernet, InvalidToken

from weora.modules.context.domain import ContextError, Json, Message, MessagePage, SecretCipher
from weora.modules.context.message_content import parse_content
from weora.modules.context.replies import reply_content


class FernetSecrets:
    """主密钥由部署安全配置注入；数据库只保存可认证的加密串。"""

    def __init__(self, key: str) -> None:
        self.cipher = Fernet(key.encode("ascii"))

    def encrypt(self, plaintext: str) -> str:
        return self.cipher.encrypt(plaintext.encode()).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self.cipher.decrypt(ciphertext.encode("ascii")).decode()
        except (InvalidToken, UnicodeError):
            raise ContextError("CREDENTIALS_UNAVAILABLE", "连接凭据无法解密，请管理员重新配置") from None


class FeishuHttpReader:
    """短超时、固定主机、禁止重定向；缓存仅包含内存 token，不缓存消息正文。"""

    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, session: aiohttp.ClientSession, cipher: SecretCipher) -> None:
        self.session, self.cipher = session, cipher
        self.tokens: dict[str, tuple[str, float]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def _json(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: Json | None = None,
        params: dict[str, str] | None = None,
        data: aiohttp.FormData | None = None,
        timeout_seconds: float = 2.5,
    ) -> Json:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with self.session.request(
                method,
                self.BASE + path,
                headers=headers,
                json=body,
                data=data,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                allow_redirects=False,
            ) as response:
                if response.status == 429:
                    raise ContextError("RATE_LIMITED", "飞书接口限流，请稍后重试")
                if 300 <= response.status < 400 or response.status >= 500:
                    raise ContextError("PROVIDER_UNAVAILABLE", "飞书服务暂不可用")
                # 先限制实际流字节，不信任 Content-Length；错误内容从不直接记录。
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > 2000000:
                        raise ContextError("PROVIDER_UNAVAILABLE", "飞书响应超出大小限制")
                payload = cast(Json, json.loads(chunks))
                if not isinstance(payload, dict):
                    raise ValueError
                code = payload.get("code")
                if code in {99991663, 99991664, 99991668}:
                    raise ContextError("TOKEN_EXPIRED", "应用访问凭证已失效")
                if code in {99991672, 99991679, 230002, 230006, 230013, 230073, 231203}:
                    raise ContextError("PERMISSION_DENIED", "应用缺少权限、未入群或话题不可见")
                if type(code) is not int or code != 0 or response.status >= 400:
                    raise ContextError("PROVIDER_UNAVAILABLE", "飞书拒绝请求，请检查应用配置与权限")
                return payload
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError):
            raise ContextError("PROVIDER_UNAVAILABLE", "飞书响应不可用") from None

    async def _token(self, connection: Json, *, force: bool = False, rejected: str | None = None) -> str:
        key = str(connection["id"]) + ":" + str(connection["version"])
        async with self.locks.setdefault(key, asyncio.Lock()):
            cached = self.tokens.get(key)
            if cached and cached[1] > time.monotonic() and (not force or cached[0] != rejected):
                return cached[0]
            raw = await self._json(
                "POST",
                "/auth/v3/tenant_access_token/internal",
                body={
                    "app_id": connection["app_id"],
                    "app_secret": self.cipher.decrypt(str(connection["secret"])),
                },
            )
            token, expire = raw.get("tenant_access_token"), raw.get("expire")
            if not isinstance(token, str) or not isinstance(expire, int) or expire <= 30:
                raise ContextError("CREDENTIALS_UNAVAILABLE", "应用访问凭证获取失败")
            self.tokens[key] = token, time.monotonic() + expire - 30
            return token

    async def _get(self, connection: Json, path: str, params: dict[str, str] | None = None) -> Json:
        token = await self._token(connection)
        try:
            return await self._json("GET", path, token=token, params=params)
        except ContextError as error:
            if error.code != "TOKEN_EXPIRED":
                raise
            token = await self._token(connection, force=True, rejected=token)
            return await self._json("GET", path, token=token, params=params)

    @staticmethod
    def _id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ContextError("INVALID_PARAMETERS", "消息或话题标识无效")
        return quote(value, safe="")

    async def probe(self, connection: Json, chat_id: str | None = None) -> Json:
        await self._token(connection)
        result: Json = {"connection_status": "ready"}
        bot_info = await self._get(connection, "/bot/v3/info")
        bot = self._object(bot_info.get("bot"))
        if not bot.get("open_id"):
            raise ContextError("PROVIDER_UNAVAILABLE", "无法确认应用机器人身份")
        result["bot_open_id"] = str(bot["open_id"])
        if chat_id:
            # 群信息与历史读取分别探测，收到事件不证明这两项能力。
            try:
                membership = await self._get(connection, f"/im/v1/chats/{self._id(chat_id)}/members/is_in_chat")
                data = self._object(membership.get("data"))
                result["bot_membership"] = "verified" if data.get("is_in_chat") is True else "not_in_group"
            except ContextError as error:
                result["bot_membership"] = "unavailable"
                result["membership_error"] = error.code
            try:
                now = datetime.now(UTC)
                await self.messages(connection, chat_id=chat_id, start=now, end=now, page_token=None)
                result["history_status"] = "readable"
            except ContextError as error:
                result["history_status"] = "unavailable"
                result["history_error"] = error.code
        return result

    @staticmethod
    def _object(value: object) -> Json:
        if not isinstance(value, dict):
            raise ContextError("PROVIDER_UNAVAILABLE", "飞书响应结构无效")
        return cast(Json, value)

    @staticmethod
    def _message(raw: Json) -> Message:
        try:
            content_type = str(raw.get("msg_type", "unknown"))
            body = raw.get("body")
            parsed = parse_content(
                content_type,
                body.get("content") if isinstance(body, dict) else None,
                deleted=raw.get("deleted") is True,
            )
            if parsed.status == "recalled":
                content_type = "deleted"
            sender = FeishuHttpReader._object(raw.get("sender", {}))
            raw_mentions = raw.get("mentions", [])
            return Message(
                id=str(raw["message_id"]),
                chat_id=str(raw["chat_id"]),
                thread_id=str(raw["thread_id"]) if raw.get("thread_id") else None,
                author_id=str(sender.get("id", "unknown")),
                created_at=datetime.fromtimestamp(int(str(raw["create_time"])) / 1000, UTC),
                text=parsed.text,
                content_type=content_type,
                parse_status=parsed.status,
                parse_notes=tuple(parsed.notes),
                resources=tuple(parsed.resources),
                root_id=str(raw["root_id"]) if raw.get("root_id") else None,
                parent_id=str(raw["parent_id"]) if raw.get("parent_id") else None,
                mentions=tuple(item for item in raw_mentions if isinstance(item, dict))
                if isinstance(raw_mentions, list)
                else (),
                human=sender.get("sender_type") == "user",
            )
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ContextError("PROVIDER_UNAVAILABLE", "飞书消息结构无效") from None

    async def messages(
        self,
        connection: Json,
        *,
        chat_id: str,
        start: datetime,
        end: datetime,
        page_token: str | None,
        thread_id: str | None = None,
        descending: bool = False,
    ) -> MessagePage:
        params = {
            "container_id_type": "thread" if thread_id else "chat",
            "container_id": thread_id or chat_id,
            "sort_type": "ByCreateTimeDesc" if descending else "ByCreateTimeAsc",
            "page_size": "50",
        }
        if not thread_id:
            params.update(start_time=str(int(start.timestamp())), end_time=str(int(end.timestamp()) + 1))
        if page_token:
            params["page_token"] = page_token
        raw = await self._get(connection, "/im/v1/messages", params)
        data = self._object(raw.get("data"))
        raw_items = data.get("items", [])
        # 缺失或错误类型的分页标识不能当成“没有下一页”，否则会伪造完整覆盖。
        if not isinstance(data.get("has_more"), bool) or not isinstance(raw_items, list) or len(raw_items) > 50:
            raise ContextError("PROVIDER_UNAVAILABLE", "飞书消息页结构无效")
        items = tuple(self._message(self._object(item)) for item in raw_items)
        next_token: str | None = None
        if data.get("has_more") is True:
            token = data.get("page_token")
            if not isinstance(token, str) or not token or len(token) > 4096:
                raise ContextError("PROVIDER_UNAVAILABLE", "飞书返回分页未完成但缺少有效游标")
            next_token = token
        return MessagePage(items, next_token)

    async def message(self, connection: Json, message_id: str) -> Message:
        raw = await self._get(connection, f"/im/v1/messages/{self._id(message_id)}")
        data = self._object(raw.get("data"))
        items = data.get("items", [])
        if not isinstance(items, list) or len(items) != 1:
            raise ContextError("PROVIDER_UNAVAILABLE", "引用消息不存在或不可见")
        return self._message(self._object(items[0]))

    async def upload_image(self, connection: Json, png: bytes) -> str:
        """只上传平台提供的有界 PNG，不接受模型指定的路径或下载地址。"""
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 1_000_000:
            raise ContextError("INVALID_IMAGE", "截图内容不可用")
        token = await self._token(connection)
        for attempt in range(2):
            # multipart 流不能重用；只有供应商明确拒绝旧 token 时刷新一次。
            form = aiohttp.FormData()
            form.add_field("image_type", "message")
            form.add_field("image", png, filename="screenshot.png", content_type="image/png")
            try:
                raw = await self._json("POST", "/im/v1/images", token=token, data=form, timeout_seconds=15)
            except ContextError as error:
                if error.code != "TOKEN_EXPIRED" or attempt:
                    raise
                token = await self._token(connection, force=True, rejected=token)
                continue
            key = self._object(raw.get("data")).get("image_key")
            if not isinstance(key, str) or not re.fullmatch(r"img_[A-Za-z0-9_.-]{1,500}", key):
                raise ContextError("PROVIDER_UNAVAILABLE", "飞书未返回有效图片标识")
            return key
        raise ContextError("TOKEN_EXPIRED", "应用访问凭证已失效")

    async def reply(
        self, connection: Json, message_id: str, text: str, business_key: str, *, image_keys: tuple[str, ...] = ()
    ) -> str:
        # uuid 在飞书支持的幂等窗口内避免重复；超时仍由 Outbox 标为未知，禁止盲重试。
        if len(image_keys) > 2 or any(not re.fullmatch(r"img_[A-Za-z0-9_.-]{1,500}", key) for key in image_keys):
            raise ContextError("INVALID_IMAGE", "回复最多附两张平台截图")
        token = await self._token(connection)
        raw = await self._json(
            "POST",
            f"/im/v1/messages/{self._id(message_id)}/reply",
            token=token,
            body={
                "msg_type": "post",
                "content": json.dumps(reply_content(text, image_keys), ensure_ascii=False),
                "uuid": business_key,
                "reply_in_thread": True,
            },
        )
        data = self._object(raw.get("data"))
        if not data.get("message_id"):
            raise ContextError("DELIVERY_UNKNOWN", "发送结果待确认")
        return str(data["message_id"])
