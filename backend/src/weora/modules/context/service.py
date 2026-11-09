"""平台确定性上下文：授权、稳定游标、共享预算与有期限的证据快照。"""

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from weora.modules.context.domain import (
    ContextAccess,
    ContextActor,
    ContextError,
    ContextRunScope,
    ContextStore,
    FeishuReader,
    Json,
    Message,
    SecretCipher,
)


def utc(value: datetime) -> datetime:
    """拒绝没有时区的调用，避免把工作空间本地时间误当 UTC。"""
    if value.tzinfo is None:
        raise ContextError("INVALID_PARAMETERS", "时间必须包含时区")
    return value.astimezone(UTC)


def timestamp(value: object) -> datetime:
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def channel_run_ids(record: Json) -> set[str]:
    """一个协调任务可以有多轮 Run；正文到期必须覆盖全部派生副本。"""
    values = record.get("coordinated_run_ids", [])
    return ({str(record["run_id"])} if record.get("run_id") else set()) | (
        set(map(str, values)) if isinstance(values, list) else set()
    )


def _public(record: Json) -> Json:
    return {key: value for key, value in record.items() if key not in {"secret", "verification_token"}}


class ContextService:
    def __init__(
        self,
        store: ContextStore,
        reader: FeishuReader,
        cipher: SecretCipher,
        cursor_key: bytes,
        access: ContextAccess,
        *,
        retention_days: int = 7,
    ) -> None:
        if len(cursor_key) < 32:
            raise ValueError("上下文游标签名密钥至少需要 32 字节")
        if not 1 <= retention_days <= 7:
            raise ValueError("第一版正文保留期只允许 1 至 7 天")
        self.store, self.reader, self.cipher, self.access = store, reader, cipher, access
        self.cursor_key, self.retention_days = cursor_key, retention_days
        self.run_authorizer: Callable[[ContextRunScope], Awaitable[None]] | None = None

    async def authorize_actor(self, actor: ContextActor, *, manage: bool = False) -> None:
        """验证当前来源访问者的成员资格；管理操作另外要求工作空间管理员权限。"""
        if not await self.access.is_active(actor):
            raise ContextError("PERMISSION_DENIED", "成员已停用或不属于当前工作空间")
        if manage and not await self.access.can_manage(actor):
            raise ContextError("PERMISSION_DENIED", "只有工作空间管理员可修改上下文配置")

    async def _scope(self, scope: ContextRunScope) -> None:
        await self.authorize_actor(scope.actor)
        if not await self.access.can_access_run(scope):
            raise ContextError("PERMISSION_DENIED", "无权访问该运行")
        if scope.audience == "group":
            # 入站请求和出站正文也是本 Run 的上下文副本；TTL 到期先拒绝访问，
            # 不依赖 Beat 已完成物理清理，也不能被较晚创建的快照延长有效期。
            now = datetime.now(UTC)
            for kind in ("inbox", "delivery", "external"):
                for item in await self.store.list(scope.actor.workspace_id, kind):
                    if str(scope.run_id) in channel_run_ids(item) and timestamp(item["expires_at"]) <= now:
                        raise ContextError("CONTEXT_EXPIRED", "渠道上下文正文已过期")

    async def save_connection(
        self,
        actor: ContextActor,
        *,
        app_id: str,
        name: str,
        secret: str | None = None,
        connection_id: UUID | None = None,
        enabled: bool = True,
        verification_token: str | None = None,
    ) -> Json:
        await self.authorize_actor(actor, manage=True)
        if not app_id.startswith("cli_") or len(app_id) > 128 or not name.strip():
            raise ContextError("INVALID_PARAMETERS", "请填写飞书自建应用标识与连接名称")
        key = str(connection_id or uuid4())
        if connection_id and await self.store.get(actor.workspace_id, "connection", key) is None:
            raise ContextError("NOT_FOUND", "连接不存在")

        def change(old: Json | None) -> Json:
            if old is None and not secret:
                raise ContextError("INVALID_PARAMETERS", "首次配置必须提供应用密钥")
            if old and old["app_id"] != app_id:
                raise ContextError("INVALID_PARAMETERS", "连接应用身份不可修改，请新建连接")
            result: Json = dict(old or {})
            result.update(
                id=key,
                workspace_id=str(actor.workspace_id),
                app_id=app_id,
                name=name.strip(),
                enabled=enabled,
                version=int(str(result.get("version", 0))) + 1,
            )
            if secret:
                result["secret"] = self.cipher.encrypt(secret)
                result["connection_status"] = "unverified"
            if verification_token:
                result["verification_token"] = self.cipher.encrypt(verification_token)
            result.setdefault("connection_status", "unverified")
            result.setdefault("event_status", "unverified")
            return result

        return _public(await self.store.change(actor.workspace_id, "connection", key, change))

    async def connections(self, actor: ContextActor) -> list[Json]:
        await self.authorize_actor(actor, manage=True)
        return [_public(item) for item in await self.store.list(actor.workspace_id, "connection")]

    async def _connection(self, actor: ContextActor, connection_id: str) -> Json:
        record = await self.store.get(actor.workspace_id, "connection", connection_id)
        if not record or record.get("enabled") is not True:
            raise ContextError("SOURCE_REVOKED", "飞书连接不存在或已禁用")
        return record

    async def probe(self, actor: ContextActor, connection_id: UUID, source_id: UUID | None = None) -> Json:
        await self.authorize_actor(actor, manage=True)
        connection = await self._connection(actor, str(connection_id))
        source = await self.store.get(actor.workspace_id, "source", str(source_id)) if source_id else None
        if source_id and (not source or source["connection_id"] != str(connection_id)):
            raise ContextError("NOT_FOUND", "来源不存在")
        result = await self.reader.probe(connection, str(source["chat_id"]) if source else None)
        if source:
            await self.store.change(
                actor.workspace_id,
                "connection",
                str(connection_id),
                lambda old: dict(
                    old or {},
                    **{
                        key: value
                        for key, value in result.items()
                        if key
                        in {
                            "connection_status",
                            "bot_open_id",
                        }
                    },
                ),
            )
        kind, key = ("source", str(source_id)) if source else ("connection", str(connection_id))
        await self.store.change(actor.workspace_id, kind, key, lambda old: dict(old or {}, **result))
        return result

    async def save_source(
        self,
        actor: ContextActor,
        *,
        connection_id: UUID,
        chat_id: str,
        name: str,
        source_id: UUID | None = None,
        enabled: bool = True,
    ) -> Json:
        await self.authorize_actor(actor, manage=True)
        await self._connection(actor, str(connection_id))
        if not chat_id.startswith("oc_") or len(chat_id) > 128 or not name.strip():
            raise ContextError("INVALID_PARAMETERS", "请填写群 ID 和来源名称")
        existing = await self.store.list(actor.workspace_id, "source")
        for item in existing:
            if (
                item["connection_id"] == str(connection_id)
                and item["chat_id"] == chat_id
                and (source_id is None or str(source_id) != item["id"])
            ):
                raise ContextError("CONFLICT", "此群已登记为来源")
        # 自然来源键确定 ID，两个管理员并发登记同一群也只会命中同一行。
        key = str(source_id or uuid5(NAMESPACE_URL, f"weora:{actor.workspace_id}:{connection_id}:{chat_id}"))

        def change(old: Json | None) -> Json:
            if source_id and old is None:
                raise ContextError("NOT_FOUND", "来源不存在")
            if old and source_id is None:
                raise ContextError("CONFLICT", "此群已登记为来源")
            if old and (old["connection_id"] != str(connection_id) or old["chat_id"] != chat_id):
                raise ContextError("INVALID_PARAMETERS", "来源连接与群不可修改")
            return dict(
                old or {},
                id=key,
                connection_id=str(connection_id),
                chat_id=chat_id,
                name=name.strip(),
                enabled=enabled,
                version=int(str((old or {}).get("version", 0))) + 1,
                history_status=(old or {}).get("history_status", "unverified"),
                bot_membership=(old or {}).get("bot_membership", "unverified"),
            )

        return await self.store.change(actor.workspace_id, "source", key, change)

    async def set_grant(self, actor: ContextActor, source_id: UUID, user_id: UUID, enabled: bool) -> Json:
        await self.authorize_actor(actor, manage=True)
        if not await self.access.is_active(ContextActor(user_id, actor.workspace_id)):
            raise ContextError("PERMISSION_DENIED", "读者必须是当前工作空间的有效成员")
        if not await self.store.get(actor.workspace_id, "source", str(source_id)):
            raise ContextError("NOT_FOUND", "来源不存在")
        key = f"{source_id}:{user_id}"
        return await self.store.change(
            actor.workspace_id,
            "grant",
            key,
            lambda old: {
                "source_id": str(source_id),
                "user_id": str(user_id),
                "enabled": enabled,
                "version": int(str((old or {}).get("version", 0))) + 1,
            },
        )

    async def grants(self, actor: ContextActor) -> list[Json]:
        await self.authorize_actor(actor, manage=True)
        return await self.store.list(actor.workspace_id, "grant")

    async def bind_identity(
        self,
        actor: ContextActor,
        connection_id: UUID,
        open_id: str,
        user_id: UUID,
        enabled: bool = True,
    ) -> Json:
        await self.authorize_actor(actor, manage=True)
        await self._connection(actor, str(connection_id))
        if not open_id.startswith("ou_") or not await self.access.is_active(ContextActor(user_id, actor.workspace_id)):
            raise ContextError("INVALID_PARAMETERS", "只能关联明确的 open_id 与有效成员")
        # 显式管理员绑定；显示名从来不参与匹配，也不隐式授予来源读取权限。
        return await self.store.change(
            actor.workspace_id,
            "binding",
            f"{connection_id}:{open_id}",
            lambda _: {
                "connection_id": str(connection_id),
                "open_id": open_id,
                "user_id": str(user_id),
                "enabled": enabled,
            },
        )

    async def bindings(self, actor: ContextActor) -> list[Json]:
        await self.authorize_actor(actor, manage=True)
        return await self.store.list(actor.workspace_id, "binding")

    async def save_collection(self, actor: ContextActor, name: str, source_ids: list[UUID]) -> Json:
        await self.authorize_actor(actor, manage=True)
        for source_id in source_ids:
            if not await self.store.get(actor.workspace_id, "source", str(source_id)):
                raise ContextError("NOT_FOUND", "集合包含不存在的来源")
        if not name.strip():
            raise ContextError("INVALID_PARAMETERS", "集合名称不能为空")
        key = str(uuid4())
        return await self.store.change(
            actor.workspace_id,
            "collection",
            key,
            lambda _: {
                "id": key,
                "name": name.strip(),
                "source_ids": [str(item) for item in source_ids],
            },
        )

    async def collections(self, actor: ContextActor) -> list[Json]:
        await self.authorize_actor(actor)
        allowed = {str(item["id"]) for item in await self.sources(actor)}
        result = []
        for item in await self.store.list(actor.workspace_id, "collection"):
            ids = [str(value) for value in cast(list[object], item["source_ids"]) if str(value) in allowed]
            if ids:
                result.append(dict(item, source_ids=ids))
        return result

    async def _authorize(self, scope: ContextRunScope, source_id: UUID) -> tuple[Json, Json, str]:
        await self._scope(scope)
        if scope.audience == "group" and scope.source_id != source_id:
            raise ContextError("PERMISSION_DENIED", "群聊运行只能读取其原始群来源")
        source = await self.store.get(scope.actor.workspace_id, "source", str(source_id))
        if not source or source.get("enabled") is not True:
            raise ContextError("SOURCE_REVOKED", "来源不存在或已撤销")
        grant = await self.store.get(scope.actor.workspace_id, "grant", f"{source_id}:{scope.actor.user_id}")
        if not grant or grant.get("enabled") is not True:
            raise ContextError("PERMISSION_DENIED", "当前成员没有该来源的读取授权")
        connection = await self._connection(scope.actor, str(source["connection_id"]))
        if scope.audience == "group":
            bindings = await self.store.list(scope.actor.workspace_id, "binding")
            if not any(
                binding.get("enabled") is True
                and binding.get("user_id") == str(scope.actor.user_id)
                and binding.get("connection_id") == source["connection_id"]
                and (scope.origin_open_id is None or binding.get("open_id") == scope.origin_open_id)
                for binding in bindings
            ):
                raise ContextError("PERMISSION_DENIED", "飞书发起人的成员关联已解除")
        version = f"{source['version']}:{grant['version']}:{connection['version']}"
        return source, connection, version

    async def authorize_channel(self, actor: ContextActor, source_id: UUID, open_id: str) -> tuple[Json, Json, str]:
        """主 Agent 没有伪造的 Run；按原群、人和当前授权验证其消息上下文范围。"""
        await self.authorize_actor(actor)
        source = await self.store.get(actor.workspace_id, "source", str(source_id))
        grant = await self.store.get(actor.workspace_id, "grant", f"{source_id}:{actor.user_id}")
        if not source or source.get("enabled") is not True or not grant or grant.get("enabled") is not True:
            raise ContextError("PERMISSION_DENIED", "当前来源授权已撤销")
        connection = await self._connection(actor, str(source["connection_id"]))
        binding = await self.store.get(actor.workspace_id, "binding", f"{source['connection_id']}:{open_id}")
        if not binding or binding.get("enabled") is not True or binding.get("user_id") != str(actor.user_id):
            raise ContextError("PERMISSION_DENIED", "消息作者身份关联已变化")
        return source, connection, f"{source['version']}:{grant['version']}:{connection['version']}"

    async def link_channel_run(self, actor: ContextActor, origin: Json, run_id: UUID) -> None:
        """协调器只能关联自己原收件的 Run，不能直接修改 Context 表或延长正文保留期。"""
        key = f"{origin['connection_id']}:{origin['message_id']}"

        def link(old: Json | None) -> Json:
            if (
                not old
                or old["id"] != origin["inbox_id"]
                or old.get("actor_id") != str(actor.user_id)
                or old.get("chat_id") != origin["chat_id"]
                or not isinstance(old.get("frozen_route"), dict)
                or cast(Json, old["frozen_route"]).get("source_id") != origin["source_id"]
            ):
                raise ContextError("PERMISSION_DENIED", "运行关联必须属于当前作者的原收件")
            # 这里只补审计关联，不授予读取或执行权限；撤权/过期时仍须关联以便拒绝访问和清理副本。
            return dict(old, coordinated_run_ids=sorted(channel_run_ids(old) | {str(run_id)}))

        await self.store.change(actor.workspace_id, "inbox", key, link)

    async def channel_message(
        self, actor: ContextActor, source_id: UUID, open_id: str, *, message_id: str, cutoff: datetime
    ) -> Message:
        """单条证据仍按群和作者授权；调用方必须验证根/父/分页引用的来源。"""
        source, connection, _ = await self.authorize_channel(actor, source_id, open_id)
        item = await self.reader.message(connection, message_id)
        if item.id != message_id or item.chat_id != source["chat_id"] or item.created_at > utc(cutoff):
            raise ContextError("PERMISSION_DENIED", "证据消息不属于当前来源或晚于触发时刻")
        if item.created_at <= datetime.now(UTC) - timedelta(days=self.retention_days):
            raise ContextError("CONTEXT_EXPIRED", "证据消息已超出保留期")
        return item

    async def channel_messages(
        self,
        actor: ContextActor,
        source_id: UUID,
        open_id: str,
        *,
        cutoff: datetime,
        thread_id: str | None,
        cursor: str | None = None,
        request_key: str = "",
        anchor_id: str | None = None,
    ) -> Json:
        """评估上下文按来源版本签名翻页；单页最多 20 条，不延长原消息保留期。"""
        source, connection, version = await self.authorize_channel(actor, source_id, open_id)
        end = utc(cutoff)
        if end <= datetime.now(UTC) - timedelta(days=self.retention_days):
            raise ContextError("CONTEXT_EXPIRED", "评估来源正文已过期")
        expected: Json = {
            "actor": str(actor.user_id),
            "source": str(source_id),
            "open_id": open_id,
            "version": version,
            "thread": thread_id,
            "cutoff": end.isoformat(),
            "request": request_key,
        }
        if anchor_id:
            expected["anchor"] = anchor_id
        state = self._decode(cursor, expected) if cursor else expected
        start = max(
            end - timedelta(days=self.retention_days) if anchor_id and thread_id else end - timedelta(hours=1),
            datetime.now(UTC) - timedelta(days=self.retention_days),
        )
        page = await self.reader.messages(
            connection,
            chat_id=str(source["chat_id"]),
            start=start,
            end=end,
            page_token=cast(str | None, state.get("page")),
            thread_id=thread_id,
        )
        items = [
            item
            for item in page.items
            if item.chat_id == source["chat_id"]
            and item.created_at >= start
            and item.created_at <= end
            and (not thread_id or item.thread_id == thread_id)
        ]
        window: Json = {}
        if anchor_id and not cursor:
            # 对照 admission-context.ts：先读有限快照，再取开头与触发邻居；不伪造全量覆盖。
            seen_tokens: set[str] = set()
            for _ in range(3):
                if not page.next_token:
                    break
                if page.next_token in seen_tokens:
                    raise ContextError("PROVIDER_UNAVAILABLE", "线程分页重复，无法确认上下文完整性")
                seen_tokens.add(page.next_token)
                page = await self.reader.messages(
                    connection,
                    chat_id=str(source["chat_id"]),
                    start=start,
                    end=end,
                    page_token=page.next_token,
                    thread_id=thread_id,
                )
                items.extend(
                    item
                    for item in page.items
                    if item.chat_id == source["chat_id"]
                    and start <= item.created_at <= end
                    and (not thread_id or item.thread_id == thread_id)
                )
            items = sorted({item.id: item for item in items}.values(), key=lambda item: (item.created_at, item.id))
            anchor = next((index for index, item in enumerate(items) if item.id == anchor_id), None)
            positions = [
                index for index in range(len(items)) if index < 5 or (anchor is not None and abs(index - anchor) <= 5)
            ]
            selected_ids = {items[index].id for index in positions}
            window = {
                "source_found": anchor is not None,
                "loaded_messages": len(items),
                "positions": [index + 1 for index in positions],
                "stubs": [
                    {"id": item.id, "position": index + 1}
                    for index, item in enumerate(items)
                    if item.id not in selected_ids
                ],
                "omitted_messages": len(items) - len(positions),
            }
            items = [items[index] for index in positions]
        offset = int(str(state.get("offset", 0)))
        selected = items[offset : offset + 20]
        next_state = (
            dict(expected, page=state.get("page"), offset=offset + 20)
            if len(items) > offset + 20
            else dict(expected, page=page.next_token, offset=0)
            if page.next_token
            else None
        )
        return {
            **window,
            "items": [
                {
                    "id": item.id,
                    "author_id": item.author_id,
                    "human": item.human,
                    "text": item.text[:1200],
                    "created_at": item.created_at.isoformat(),
                    "parse_status": item.parse_status,
                    "resources": list(item.resources),
                }
                for item in selected
            ],
            "next_cursor": self._cursor(
                dict(next_state, expires_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat())
            )
            if next_state
            else None,
            "partial": bool(next_state),
            "cutoff": end.isoformat(),
        }

    async def sources(
        self,
        actor: ContextActor,
        scope: ContextRunScope | None = None,
        collection_id: UUID | None = None,
    ) -> list[Json]:
        await self.authorize_actor(actor)
        if scope:
            await self._scope(scope)
        allowed_ids: set[str] | None = None
        if collection_id:
            collection = await self.store.get(actor.workspace_id, "collection", str(collection_id))
            allowed_ids = set(map(str, cast(list[object], (collection or {}).get("source_ids", []))))
        result: list[Json] = []
        for item in await self.store.list(actor.workspace_id, "source"):
            if allowed_ids is not None and str(item["id"]) not in allowed_ids:
                continue
            if scope and scope.audience == "group" and str(scope.source_id) != item["id"]:
                continue
            grant = await self.store.get(actor.workspace_id, "grant", f"{item['id']}:{actor.user_id}")
            connection = await self.store.get(actor.workspace_id, "connection", str(item["connection_id"]))
            if (
                item.get("enabled") is True
                and connection
                and connection.get("enabled") is True
                and grant
                and grant.get("enabled") is True
            ):
                result.append(item)
        return result

    async def managed_sources(self, actor: ContextActor) -> list[Json]:
        await self.authorize_actor(actor, manage=True)
        return await self.store.list(actor.workspace_id, "source")

    def _cursor(self, payload: Json) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(self.cursor_key, body.encode(), hashlib.sha256).hexdigest()
        return body + "." + signature

    def _decode(self, token: str, expected: Json) -> Json:
        try:
            if len(token) > 12000:
                raise ValueError
            body, signature = token.split(".", 1)
            actual = hmac.new(self.cursor_key, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, actual):
                raise ValueError
            data = cast(Json, json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))
            if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
                raise ValueError
            if timestamp(data["expires_at"]) <= datetime.now(UTC):
                raise ValueError
            return data
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ContextError("INVALID_CURSOR", "游标无效、已过期或不属于当前查询") from None

    async def _budget(self, scope: ContextRunScope, *, automatic: bool = False) -> None:
        def update(old: Json | None) -> Json:
            value = dict(old or {"calls": 0, "messages": [], "tokens": 0})
            count = int(str(value.get("calls", 0)))
            if not automatic and count >= 10:
                raise ContextError("BUDGET_EXCEEDED", "本次运行内容查询次数已达到上限")
            if not automatic:
                value["calls"] = count + 1
            return value

        await self.store.change(scope.actor.workspace_id, "budget", str(scope.run_id), update)

    async def read(
        self,
        scope: ContextRunScope,
        source_id: UUID,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        cursor: str | None = None,
        limit: int = 30,
        thread_id: str | None = None,
        message_id: str | None = None,
        automatic: bool = False,
    ) -> Json:
        """游标保存上游页起点及页内偏移；过滤空页不等于遍历完成。"""
        source, connection, version = await self._authorize(scope, source_id)
        cutoff = utc(scope.cutoff or datetime.now(UTC))
        end = utc(end_time) if end_time else cutoff
        start = utc(start_time) if start_time else end - timedelta(days=7)
        if end > cutoff or start >= end or end - start > timedelta(days=30) or not 1 <= limit <= 50:
            raise ContextError("INVALID_PARAMETERS", "查询窗口需在运行截止点前且不超过 30 天，单页最多 50 条")
        if thread_id and message_id:
            raise ContextError("INVALID_PARAMETERS", "话题与单消息查询不能同时指定")
        expected: Json = {
            "run": str(scope.run_id),
            "actor": str(scope.actor.user_id),
            "source": str(source_id),
            "version": version,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "thread": thread_id,
            "sort": "asc",
        }
        state = self._decode(cursor, expected) if cursor else dict(expected, page=None, offset=0)
        await self._budget(scope, automatic=automatic)
        messages: list[Message] = []
        next_state: Json | None = None
        reason: str | None = None
        try:
            if message_id:
                item = await self.reader.message(connection, message_id)
                if item.chat_id != source["chat_id"]:
                    raise ContextError("PERMISSION_DENIED", "引用消息不属于当前来源")
                # 明确引用可读取较旧消息，但绝不读取触发时刻以后的消息。
                if utc(item.created_at) <= end:
                    messages = [item]
            else:
                # 最多扫描五个上游页，避免稀疏话题过滤造成无界调用。
                for _ in range(5):
                    page = await self.reader.messages(
                        connection,
                        chat_id=str(source["chat_id"]),
                        start=start,
                        end=end,
                        page_token=cast(str | None, state.get("page")),
                        thread_id=thread_id,
                    )
                    offset = int(str(state.get("offset", 0)))
                    if offset < 0 or offset > len(page.items):
                        raise ContextError("INVALID_CURSOR", "上游页面已变化，请重新查询")
                    for index in range(offset, len(page.items)):
                        item = page.items[index]
                        if item.chat_id != source["chat_id"] or (thread_id and item.thread_id != thread_id):
                            raise ContextError("PERMISSION_DENIED", "上游返回的消息不属于授权群或话题")
                        if not start <= utc(item.created_at) <= end:
                            continue
                        if not any(existing.id == item.id for existing in messages):
                            messages.append(item)
                        if len(messages) >= limit:
                            if index + 1 < len(page.items):
                                next_state = dict(state, offset=index + 1)
                            elif page.next_token:
                                next_state = dict(state, page=page.next_token, offset=0)
                            break
                    if len(messages) >= limit or not page.next_token:
                        break
                    state = dict(state, page=page.next_token, offset=0)
                else:
                    next_state = state
                    reason = "scan_budget_exceeded"
        except TimeoutError:
            reason = "timeout"
            next_state = state if not message_id else None
        except ContextError as error:
            if error.code in {"PERMISSION_DENIED", "SOURCE_REVOKED", "INVALID_CURSOR"}:
                raise
            return await self._snapshot(
                scope,
                source_id,
                version,
                start,
                end,
                [],
                None,
                error.code,
                error=error,
                query={
                    "thread_id": thread_id,
                    "message_id": message_id,
                    "limit": limit,
                },
            )
        # 外部读取期间撤权时，已取得内容不得保存或交付。
        _, _, current_version = await self._authorize(scope, source_id)
        if current_version != version:
            raise ContextError("SOURCE_REVOKED", "读取期间来源授权发生变化")
        if next_state:
            next_state["expires_at"] = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
            reason = reason or "has_more"
        return await self._snapshot(
            scope,
            source_id,
            version,
            start,
            end,
            messages,
            self._cursor(next_state) if next_state else None,
            reason,
            automatic=automatic,
            query={
                "thread_id": thread_id,
                "message_id": message_id,
                "limit": limit,
            },
        )

    async def _snapshot(
        self,
        scope: ContextRunScope,
        source_id: UUID,
        version: str,
        start: datetime,
        end: datetime,
        messages: list[Message],
        cursor: str | None,
        reason: str | None,
        *,
        error: ContextError | None = None,
        automatic: bool = False,
        query: Json | None = None,
    ) -> Json:
        selected: list[Json] = []
        snapshot_id = str(uuid4())
        overflow = False

        def budget(old: Json | None) -> Json:
            nonlocal overflow
            value = dict(old or {"calls": 0, "messages": [], "tokens": 0})
            known = set(map(str, cast(list[object], value.get("messages", []))))
            tokens = int(str(value.get("tokens", 0)))
            local_tokens = 0
            for item in messages:
                identity = f"{source_id}:{item.id}"
                # 新旧适配器并存时保持旧消息的保守判定，不把未知类型升级成已完整解析。
                parse_status = item.parse_status or ("complete" if item.content_type == "text" else "unsupported")
                material: Json = {
                    "ref": f"ctx:{snapshot_id}:{len(selected)}",
                    "message_id": item.id,
                    "thread_id": item.thread_id,
                    "author_id": item.author_id,
                    "created_at": utc(item.created_at).isoformat(),
                    "text": item.text,
                    "content_type": item.content_type,
                    "parse_status": parse_status,
                    "parse_notes": list(item.parse_notes),
                    "resources": list(item.resources),
                }
                # 文件名、资源标识和缺口备注也会进入模型输入，不能绕过共享材料预算。
                estimate = max(1, len(json.dumps(material, ensure_ascii=False).encode("utf-8")) // 2)
                if (
                    (identity not in known and len(known) >= 300)
                    or tokens + estimate > 12000
                    or (automatic and (len(selected) >= 10 or local_tokens + estimate > 2000))
                ):
                    overflow = True
                    break
                known.add(identity)
                tokens += estimate
                local_tokens += estimate
                selected.append(material)
            value.update(messages=sorted(known), tokens=tokens)
            return value

        await self.store.change(scope.actor.workspace_id, "budget", str(scope.run_id), budget)
        if overflow:
            # 不推进一个未完全交付的页；预算无法恢复时显式停止，不提供误导游标。
            # 同时保留引用/读取失败等已有缺口，不能被预算截断覆盖后误判成可用的任务续接。
            reasons = reason.split(",") if reason else []
            if "budget_exceeded" not in reasons:
                reasons.append("budget_exceeded")
            cursor, reason = None, ",".join(reasons)
        # 手动读取与自动补齐共用事实边界；只检查预算后实际交付的内容，不能把占位符当成完整正文。
        if any(item["parse_status"] != "complete" for item in selected):
            reasons = reason.split(",") if reason else []
            if "unsupported_content" not in reasons:
                reasons.append("unsupported_content")
            reason = ",".join(reasons)
        now = datetime.now(UTC)
        record: Json = {
            "status": "unavailable" if error else ("partial" if reason else "complete"),
            "source_id": str(source_id),
            "snapshot_id": snapshot_id,
            "run_id": str(scope.run_id),
            "actor_id": str(scope.actor.user_id),
            "audience": scope.audience,
            "version": version,
            "window": {"start_time": start.isoformat(), "end_time": end.isoformat()},
            "query": query or {"thread_id": None, "message_id": None, "limit": 10},
            "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(days=self.retention_days)).isoformat(),
            "messages": selected,
            "next_cursor": cursor,
            "coverage": {"window_complete": not reason and error is None, "truncated": bool(reason), "reason": reason},
            "error": {"code": error.code, "message": error.message} if error else None,
            "sha256": hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest(),
        }
        await self.store.change(scope.actor.workspace_id, "snapshot", snapshot_id, lambda _: record)
        return record

    async def preview(self, scope: ContextRunScope, snapshot_id: UUID) -> Json:
        await self._scope(scope)
        record = await self.store.get(scope.actor.workspace_id, "snapshot", str(snapshot_id))
        if not record or record["run_id"] != str(scope.run_id) or record["actor_id"] != str(scope.actor.user_id):
            raise ContextError("NOT_FOUND", "快照不存在")
        if timestamp(record["expires_at"]) <= datetime.now(UTC) or record.get("expired") is True:
            raise ContextError("CONTEXT_EXPIRED", "快照正文已过期")
        _, _, version = await self._authorize(scope, UUID(str(record["source_id"])))
        if record["version"] != version:
            raise ContextError("SOURCE_REVOKED", "快照对应的授权已变化，请重新读取")
        return record

    async def snapshots(self, scope: ContextRunScope) -> list[Json]:
        await self._scope(scope)
        result: list[Json] = []
        for item in await self.store.list(scope.actor.workspace_id, "snapshot"):
            if item.get("run_id") != str(scope.run_id):
                continue
            try:
                result.append(await self.preview(scope, UUID(str(item["snapshot_id"]))))
            except ContextError:
                # 列表不返回被撤权/过期正文，也不能据此间接预览来源名称。
                result.append({"snapshot_id": item["snapshot_id"], "status": "unavailable", "expired": True})
        return result

    async def validate_refs(self, scope: ContextRunScope, refs: list[str]) -> None:
        for ref in refs:
            try:
                prefix, snapshot_id, offset = ref.split(":")
                if prefix != "ctx" or int(offset) < 0:
                    raise ValueError
                record = await self.preview(scope, UUID(snapshot_id))
                messages = cast(list[Json], record["messages"])
                if messages[int(offset)]["ref"] != ref:
                    raise ValueError
            except (ValueError, IndexError):
                raise ContextError("INVALID_REFERENCE", "引用不是本次运行已交付的证据") from None

    async def authorize_derived(self, scope: ContextRunScope) -> None:
        """输出、下载、恢复共同调用；不把撤权保护留给 Agent 自行删密。"""
        await self._scope(scope)
        if self.run_authorizer:
            await self.run_authorizer(scope)
        if scope.source_kind == "code_hosting":
            if self.run_authorizer is None:
                raise ContextError("SOURCE_REVOKED")
            return
        if scope.audience == "group" and scope.source_id:
            await self._authorize(scope, scope.source_id)
        for item in await self.store.list(scope.actor.workspace_id, "snapshot"):
            if item.get("run_id") == str(scope.run_id):
                await self.preview(scope, UUID(str(item["snapshot_id"])))

    async def register_derived_run(
        self, actor: ContextActor, source_id: str, reference: str, expires_at: str, run_id: UUID
    ) -> None:
        """调用方先验证原生 Task 归属；这里只添加保留期约束，不授予读取或执行权限。

        接单后撤权或到期也必须能登记清理依赖，否则会留下永远无法派发、也无法清理的正文副本。
        """
        expiry = timestamp(expires_at)
        if expiry > datetime.now(UTC) + timedelta(days=self.retention_days, minutes=1):
            raise ContextError("CONTEXT_EXPIRED")
        expires_at = expiry.isoformat()
        identity = hashlib.sha256(f"{source_id}\0{reference}".encode()).hexdigest()

        def update(old: Json | None) -> Json:
            return {
                "source_id": source_id,
                "expires_at": min(str((old or {}).get("expires_at", expires_at)), expires_at),
                "coordinated_run_ids": sorted(channel_run_ids(old or {}) | {str(run_id)}),
            }

        await self.store.change(actor.workspace_id, "external", identity, update)

    async def expire_derived_dependency(self, workspace_id: UUID, source_id: str, reference: str) -> None:
        """来源撤权只会缩短派生副本期限；供协调/渠道用例调用，不暴露给模型或管理 API。"""
        identity = hashlib.sha256(f"{source_id}\0{reference}".encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        await self.store.change(
            workspace_id,
            "external",
            identity,
            lambda old: dict(
                old or {"source_id": source_id}, expires_at=min(str((old or {}).get("expires_at", now)), now)
            ),
        )

    async def reserve_discovery(self, scope: ContextRunScope) -> None:
        """来源发现不消耗内容调用预算，但不能无限扫描授权目录。"""
        await self._scope(scope)

        def increment(old: Json | None) -> Json:
            calls = int(str((old or {}).get("calls", 0)))
            if calls >= 30:
                raise ContextError("RATE_LIMITED", "本次运行来源发现次数已达到上限")
            return {"calls": calls + 1}

        await self.store.change(scope.actor.workspace_id, "discovery", str(scope.run_id), increment)

    async def automatic(
        self,
        scope: ContextRunScope,
        source_id: UUID,
        *,
        trigger_message_id: str,
        quote_id: str | None = None,
        thread_id: str | None = None,
        include_nearby: bool = False,
    ) -> Json:
        """明确引用优先；话题失败不悄悄退化为整群历史。"""
        await self._authorize(scope, source_id)
        end = utc(scope.cutoff or datetime.now(UTC))
        candidates: list[Message] = []
        quoted: list[Message] = []
        reasons: list[str] = []
        source, connection, version = await self._authorize(scope, source_id)

        async def collect_quote() -> None:
            if quote_id:
                try:
                    item = await self.reader.message(connection, quote_id)
                    if item.chat_id == source["chat_id"] and utc(item.created_at) <= end:
                        quoted.append(item)
                    else:
                        reasons.append("cross_group_quote")
                except ContextError:
                    reasons.append("quote_unavailable")

        async def collect_discussion() -> None:
            if thread_id or include_nearby:
                try:
                    page = await self.reader.messages(
                        connection,
                        chat_id=str(source["chat_id"]),
                        start=end - timedelta(hours=2),
                        end=end,
                        page_token=None,
                        thread_id=thread_id,
                        descending=True,
                    )
                    for item in page.items:
                        if (
                            item.chat_id == source["chat_id"]
                            and (not thread_id or item.thread_id == thread_id)
                            and end - timedelta(hours=2) <= utc(item.created_at) <= end
                        ):
                            candidates.append(item)
                    if page.next_token:
                        reasons.append("has_more")
                except ContextError:
                    reasons.append("thread_unavailable" if thread_id else "nearby_unavailable")

        try:
            async with asyncio.timeout(3):
                # 两个独立只读请求共享原3秒上限，不串行累加网络耗时。
                # 引用仍单独保存并优先纳入预算，不以网络完成顺序改变优先级。
                await asyncio.gather(collect_quote(), collect_discussion())
        except TimeoutError:
            reasons.append("timeout")
        unique: dict[str, Message] = {}
        for item in quoted + candidates:
            if item.id != trigger_message_id:
                unique.setdefault(item.id, item)
        if len(unique) > 10:
            reasons.append("budget_exceeded")
        chosen = sorted(list(unique.values())[:10], key=lambda item: (item.created_at, item.id))
        _, _, after_version = await self._authorize(scope, source_id)
        if after_version != version:
            raise ContextError("SOURCE_REVOKED", "自动补齐期间授权发生变化")
        return await self._snapshot(
            scope,
            source_id,
            version,
            end - timedelta(hours=2),
            end,
            chosen,
            None,
            ",".join(reasons) or None,
            automatic=True,
        )

    async def purge_expired(self, workspace_id: UUID) -> int:
        """正文彻底移除，保留摘要值与审计元数据；不触碰供应商侧副本。"""
        removed = 0
        now = datetime.now(UTC)
        for record in await self.store.list(workspace_id, "snapshot"):
            if timestamp(record["expires_at"]) <= now and record.get("expired") is not True:
                await self.store.change(
                    workspace_id,
                    "snapshot",
                    str(record["snapshot_id"]),
                    lambda old: dict(old or {}, messages=[], next_cursor=None, expired=True),
                )
                removed += 1
        return removed

    async def expired_run_ids(self, workspace_id: UUID) -> set[UUID]:
        """返回需清理模型输入、工具副本与产物正文的运行，不替其他模块直接写表。"""
        now = datetime.now(UTC)
        result: set[UUID] = set()
        for kind in ("snapshot", "inbox", "delivery", "external"):
            for item in await self.store.list(workspace_id, kind):
                if timestamp(item["expires_at"]) <= now:
                    result.update(UUID(value) for value in channel_run_ids(item))
        return result
