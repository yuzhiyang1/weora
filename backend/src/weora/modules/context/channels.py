"""可靠飞书入口与原会话交付。ACK、身份绑定、读取授权与投递是四个独立阶段。"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from weora.modules.context.domain import (
    CandidateAdmission,
    ContextActor,
    ContextError,
    ContextRunScope,
    InboundSubmit,
    Json,
)
from weora.modules.context.message_content import parse_content
from weora.modules.context.service import ContextService, channel_run_ids, timestamp


def _event_object(value: object) -> Json:
    """cast 不做运行时校验；运输边界的畸形结构不能变成未处理异常。"""
    if not isinstance(value, dict):
        raise ContextError("INVALID_EVENT", "飞书事件结构无效")
    return cast(Json, value)


def _event_id(value: object) -> str:
    """身份与路由键只接受供应商标识，不把 null 或对象强转成可持久化文本。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ContextError("INVALID_EVENT", "飞书事件标识无效")
    return value


class FeishuChannels:
    def __init__(self, context: ContextService, candidate_admission: CandidateAdmission | None = None) -> None:
        self.context = context
        self.candidate_admission = candidate_admission

    async def save_route(
        self,
        actor: ContextActor,
        *,
        source_id: UUID,
        agent_id: UUID,
        agent_version: str,
        thread_id: str | None = None,
        include_nearby: bool = False,
        project_id: UUID | None = None,
        repository_ids: tuple[UUID, ...] = (),
        share_project_with_group: bool = False,
    ) -> Json:
        await self.context.authorize_actor(actor, manage=True)
        if (
            bool(project_id) != bool(repository_ids)
            or len(set(repository_ids)) != len(repository_ids)
            or (project_id is not None) != share_project_with_group
        ):
            raise ContextError("INVALID_PARAMETERS", "项目路由须选择仓库并明确确认向该群分享项目上下文与结果")
        source = await self.context.store.get(actor.workspace_id, "source", str(source_id))
        if not source or not agent_version:
            raise ContextError("INVALID_PARAMETERS", "路由需要有效来源与明确发布版本")
        key = f"{source['connection_id']}:{source['chat_id']}:{thread_id or '-'}"
        return await self.context.store.change(
            actor.workspace_id,
            "route",
            key,
            lambda _: {
                "source_id": str(source_id),
                "agent_id": str(agent_id),
                "agent_version": agent_version,
                "connection_id": source["connection_id"],
                "chat_id": source["chat_id"],
                "thread_id": thread_id,
                "include_nearby": include_nearby,
                "project_id": str(project_id) if project_id else None,
                "repository_ids": sorted(str(item) for item in repository_ids),
                # 每次保存轮换授权，旧 Inbox 不得随路由编辑获得新项目或继续旧授权。
                "project_grant_id": str(uuid4()) if project_id else None,
                "project_granted_by": str(actor.user_id) if project_id else None,
                "route_key": key,
            },
        )

    async def project_allowed(self, actor: ContextActor, origin: Json, snapshot: Json) -> bool:
        """群内分享是独立授权；模型不能凭项目 UUID 扩大 Console 私有任务的受众。"""
        key, grant = origin.get("route_key"), origin.get("project_grant_id")
        if not isinstance(key, str) or not isinstance(grant, str):
            return False
        route = await self.context.store.get(actor.workspace_id, "route", key)
        if not route or route.get("project_grant_id") != grant:
            return False
        repositories = snapshot.get("repositories")
        if not isinstance(repositories, list) or not all(isinstance(item, dict) for item in repositories):
            return False
        if (
            route.get("project_id") != snapshot.get("project_id")
            or sorted(str(item.get("id")) for item in repositories) != route.get("repository_ids")
            or any(route.get(field) != origin.get(field) for field in ("source_id", "connection_id", "chat_id"))
            or (route.get("thread_id") and route["thread_id"] != origin.get("thread_id"))
        ):
            return False
        try:
            grantor = ContextActor(UUID(str(route["project_granted_by"])), actor.workspace_id)
        except (KeyError, ValueError):
            return False
        return await self.context.access.can_manage(grantor)

    async def receive(
        self,
        workspace_id: UUID,
        connection_id: UUID,
        payload: Json,
        *,
        ingress: Literal["feishu_event", "compensation"] = "feishu_event",
    ) -> Json:
        """运输层成功返回此方法后才 ACK；这里只落库，不读取历史、不调用模型。"""
        connection = await self.context.store.get(workspace_id, "connection", str(connection_id))
        if not connection or connection.get("enabled") is not True:
            raise ContextError("SOURCE_REVOKED", "飞书连接已禁用")
        try:
            header, event = _event_object(payload["header"]), _event_object(payload["event"])
            event_id = _event_id(header["event_id"])
            event_type = header["event_type"]
            if not isinstance(event_type, str) or not event_type:
                raise ValueError
            if header.get("app_id") and header["app_id"] != connection["app_id"]:
                raise ValueError
            message = _event_object(event.get("message", {}))
            message_id = (
                _event_id(message.get("message_id"))
                if event_type == "im.message.receive_v1" or message.get("message_id")
                else ""
            )
        except (KeyError, TypeError, ValueError):
            raise ContextError("INVALID_EVENT", "飞书事件结构无效") from None
        event_key = f"{connection_id}:{event_id}"

        def event_record(old: Json | None) -> Json:
            if old and old["message_id"] != message_id:
                raise ContextError("INVALID_EVENT", "事件标识冲突")
            return old or {
                "event_id": event_id,
                "connection_id": str(connection_id),
                "event_type": event_type,
                "message_id": message_id,
                "received_at": datetime.now(UTC).isoformat(),
                "ingress": ingress,
            }

        await self.context.store.change(workspace_id, "event", event_key, event_record)
        if event_type != "im.message.receive_v1":
            # 编辑和撤回留下变更事实；不能当成新的显式请求执行。
            return {"status": "ignored", "reason": "not_a_new_message"}
        sender = _event_object(event.get("sender", {}))
        if sender.get("sender_type") != "user":
            return {"status": "ignored", "reason": "bot_message"}
        if message.get("chat_type") != "group" or message.get("message_type") not in {"text", "post"}:
            return {"status": "ignored", "reason": "unsupported_message"}
        bot_open_id = connection.get("bot_open_id")
        raw_mentions = message.get("mentions", [])
        if not isinstance(raw_mentions, list):
            raise ContextError("INVALID_EVENT", "飞书提及信息无效")
        mentions = [_event_object(item) for item in raw_mentions]
        mentioned = bool(bot_open_id) and any(
            _event_object(item.get("id", {})).get("open_id") == bot_open_id for item in mentions
        )
        if not mentioned:
            chat = _event_id(message.get("chat_id"))
            thread = _event_id(message["thread_id"]) if message.get("thread_id") else None
            root = _event_id(message["root_id"]) if message.get("root_id") else None
            if not self.candidate_admission or not await self.candidate_admission(
                workspace_id, connection_id, chat, thread, root
            ):
                return {"status": "ignored", "reason": "no_explicit_bot_mention"}
        try:
            chat_id = _event_id(message.get("chat_id"))
            open_id = _event_id(_event_object(sender.get("sender_id"))["open_id"])
            thread_id = _event_id(message["thread_id"]) if message.get("thread_id") is not None else None
            quote_id = _event_id(message["parent_id"]) if message.get("parent_id") is not None else None
            root_id = _event_id(message["root_id"]) if message.get("root_id") is not None else None
            content = _event_object(json.loads(str(message["content"])))
            instruction = (
                content["text"]
                if message.get("message_type") == "text"
                else parse_content("post", str(message["content"])).text
            )
            if not isinstance(instruction, str):
                raise ValueError
            for mention in mentions:
                if _event_object(mention.get("id", {})).get("open_id") == bot_open_id:
                    mention_key = mention.get("key")
                    if not isinstance(mention_key, str) or not mention_key:
                        raise ValueError
                    instruction = instruction.replace(mention_key, "")
            instruction = instruction.strip()
            created_at = datetime.fromtimestamp(int(str(message["create_time"])) / 1000, UTC)
            if not instruction or len(instruction) > 24000 or not message_id:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ContextError("INVALID_EVENT", "飞书消息正文无效") from None
        key = f"{connection_id}:{message_id}"
        inbox_id = str(uuid4())

        def message_record(old: Json | None) -> Json:
            return old or {
                "id": inbox_id,
                "connection_id": str(connection_id),
                "message_id": message_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "quote_id": quote_id,
                **({"root_id": root_id} if root_id else {}),
                "open_id": open_id,
                "addressed": mentioned,
                "ingress": ingress,
                "instruction": self.context.cipher.encrypt(instruction),
                "created_at": created_at.isoformat(),
                "status": "RECEIVED",
                "attempts": 0,
                "expires_at": (datetime.now(UTC) + timedelta(days=self.context.retention_days)).isoformat(),
            }

        result = await self.context.store.change(workspace_id, "inbox", key, message_record)
        await self.context.store.change(
            workspace_id, "connection", str(connection_id), lambda old: dict(old or {}, event_status="received")
        )
        return {"status": "persisted", "inbox_id": result["id"]}

    async def process(self, workspace_id: UUID, inbox_id: str, submit: InboundSubmit) -> Json:
        """Run 提交必须用 Inbox 幂等键；提交成功但回写前崩溃时可以安全重试。"""
        rows = await self.context.store.list(workspace_id, "inbox")
        row = next((item for item in rows if item["id"] == inbox_id), None)
        if not row:
            raise ContextError("NOT_FOUND", "入站记录不存在")
        if row["status"] in {"SUBMITTED", "WAITING_RUN", "COORDINATING", "REJECTED", "EXPIRED"}:
            return row
        key = f"{row['connection_id']}:{row['message_id']}"
        if timestamp(row["expires_at"]) <= datetime.now(UTC):
            return await self.context.store.change(
                workspace_id, "inbox", key, lambda old: dict(old or {}, instruction=None, status="EXPIRED")
            )
        binding = await self.context.store.get(workspace_id, "binding", f"{row['connection_id']}:{row['open_id']}")
        if not binding or binding.get("enabled") is not True:
            return await self.context.store.change(
                workspace_id, "inbox", key, lambda old: dict(old or {}, status="REJECTED", error="IDENTITY_UNBOUND")
            )
        actor = ContextActor(UUID(str(binding["user_id"])), workspace_id)
        if row.get("actor_id") and row["actor_id"] != str(actor.user_id):
            raise ContextError("PERMISSION_DENIED", "入站请求原发起人的成员关联已变化，不能切换身份重放")
        await self.context.authorize_actor(actor)
        await self.context._connection(actor, str(row["connection_id"]))
        route = cast(Json | None, row.get("frozen_route"))
        if route is None:
            route_key = f"{row['connection_id']}:{row['chat_id']}:{row['thread_id'] or '-'}"
            route = await self.context.store.get(workspace_id, "route", route_key)
            if route is None and row["thread_id"]:
                route = await self.context.store.get(
                    workspace_id,
                    "route",
                    f"{row['connection_id']}:{row['chat_id']}:-",
                )
        if not route:
            raise ContextError("ROUTE_UNAVAILABLE", "当前群尚未配置 Agent 发布版本")
        chosen_route = route

        def freeze(old: Json | None) -> Json:
            if not old:
                raise ContextError("NOT_FOUND", "入站记录不存在")
            if old.get("frozen_route") is not None:
                if old.get("actor_id") != str(actor.user_id):
                    raise ContextError("PERMISSION_DENIED", "入站请求不能切换发起人")
                return old
            return dict(old, frozen_route=chosen_route, actor_id=str(actor.user_id))

        # 先冻结再提交 Run；提交成功但回写 Inbox 前崩溃，也不会随路由更新改变重试指纹。
        row = await self.context.store.change(workspace_id, "inbox", key, freeze)
        route = cast(Json, row["frozen_route"])
        # 在提交前验证当前来源读者，不给未知发起人读取历史的机会。
        source_id = str(route["source_id"])
        grant = await self.context.store.get(workspace_id, "grant", f"{source_id}:{actor.user_id}")
        source = await self.context.store.get(workspace_id, "source", source_id)
        if (
            not source
            or source.get("enabled") is not True
            or not grant
            or grant.get("enabled") is not True
            or source["chat_id"] != row["chat_id"]
            or source["connection_id"] != row["connection_id"]
        ):
            raise ContextError("PERMISSION_DENIED", "发起人没有当前群上下文授权")
        payload = dict(row, **{key: value for key, value in route.items() if key not in {"thread_id", "chat_id"}})
        payload["instruction"] = self.context.cipher.decrypt(str(row["instruction"]))
        payload["idempotency_key"] = f"feishu:{row['id']}"
        submitted = await submit(actor, payload)
        receipt = submitted if isinstance(submitted, dict) else {"run_id": submitted}
        status = "WAITING_RUN"
        if receipt.get("rejection_code"):
            status = "REJECTED"
        elif receipt.get("coordination_scope_id"):
            status = "COORDINATING"
        elif receipt.get("run_id"):
            status = "SUBMITTED"
        return await self.context.store.change(
            workspace_id,
            "inbox",
            key,
            lambda old: dict(
                old or {},
                status=status,
                **receipt,
                actor_id=str(actor.user_id),
            ),
        )

    async def enqueue_delivery(self, scope: ContextRunScope, inbox_id: str, text: str, business_key: str) -> Json:
        await self.context.authorize_derived(scope)
        if scope.audience != "group" or scope.source_id is None:
            raise ContextError("PERMISSION_DENIED", "私有运行不能向群聊扩大受众")
        rows = await self.context.store.list(scope.actor.workspace_id, "inbox")
        inbox = next((item for item in rows if item["id"] == inbox_id), None)
        if not inbox or inbox.get("run_id") != str(scope.run_id):
            raise ContextError("PERMISSION_DENIED", "交付目标不是本运行原始会话")
        source, _, _ = await self.context._authorize(scope, scope.source_id)
        if inbox["chat_id"] != source["chat_id"] or inbox["connection_id"] != source["connection_id"]:
            raise ContextError("PERMISSION_DENIED", "原会话来源不匹配")
        source_digest = hashlib.sha256(text.encode()).hexdigest()
        if inbox.get("ingress") == "manual_history":
            # 入口事实由平台表达，不能让模型的“链路正常”遮蔽补收边界。
            text = "【人工补收】本任务由历史消息补收触发，不代表长连接自动入站通过。\n\n" + text
        digest = hashlib.sha256(text.encode()).hexdigest()

        def create(old: Json | None) -> Json:
            if old:
                # 修复前的已发/未知记录保留原文与摘要，重放不重写或重新发送。
                if old.get("source_text_sha256", old.get("sha256")) != source_digest or old.get("run_id") != str(
                    scope.run_id
                ):
                    raise ContextError("CONFLICT", "交付幂等键不能复用于不同正文或运行")
                return old
            return {
                "id": str(uuid4()),
                "run_id": str(scope.run_id),
                "actor_id": str(scope.actor.user_id),
                "source_id": str(scope.source_id),
                "inbox_id": inbox_id,
                "connection_id": inbox["connection_id"],
                "message_id": inbox["message_id"],
                "thread_id": inbox["thread_id"],
                "chat_id": inbox["chat_id"],
                "text": self.context.cipher.encrypt(text),
                "sha256": digest,
                "source_text_sha256": source_digest,
                "business_key": business_key,
                "status": "PENDING",
                "attempts": 0,
                "expires_at": (datetime.now(UTC) + timedelta(days=self.context.retention_days)).isoformat(),
            }

        return await self.context.store.change(scope.actor.workspace_id, "delivery", business_key, create)

    async def deliver(self, scope: ContextRunScope, business_key: str) -> Json:
        await self.context.authorize_derived(scope)
        row = await self.context.store.get(scope.actor.workspace_id, "delivery", business_key)
        if not row or row["run_id"] != str(scope.run_id):
            raise ContextError("NOT_FOUND", "交付记录不存在")
        if row["status"] in {"SENT", "RECOVERY_REQUIRED", "SENDING", "EXPIRED"}:
            return row
        if timestamp(row["expires_at"]) <= datetime.now(UTC):
            return await self.context.store.change(
                scope.actor.workspace_id,
                "delivery",
                business_key,
                lambda old: dict(old or {}, text=None, status="EXPIRED"),
            )
        source, connection, _ = await self.context._authorize(scope, UUID(str(row["source_id"])))
        if scope.audience != "group" or source["chat_id"] != row["chat_id"]:
            raise ContextError("PERMISSION_DENIED", "交付受众与来源不匹配")
        claimed = False

        def claim(old: Json | None) -> Json:
            nonlocal claimed
            if not old:
                raise ContextError("NOT_FOUND", "交付记录不存在")
            if old["status"] != "PENDING":
                return old
            claimed = True
            return dict(
                old, status="SENDING", attempts=int(str(old["attempts"])) + 1, started_at=datetime.now(UTC).isoformat()
            )

        row = await self.context.store.change(scope.actor.workspace_id, "delivery", business_key, claim)
        if not claimed:
            return row
        try:
            remote_id = await self.context.reader.reply(
                connection, str(row["message_id"]), self.context.cipher.decrypt(str(row["text"])), str(row["id"])
            )
        except (ContextError, TimeoutError):
            # 一旦进入发送，失败可能发生在远端成功之后；第一版无安全回查 API，必须人工确认。
            return await self.context.store.change(
                scope.actor.workspace_id,
                "delivery",
                business_key,
                lambda old: dict(old or {}, status="RECOVERY_REQUIRED"),
            )
        return await self.context.store.change(
            scope.actor.workspace_id,
            "delivery",
            business_key,
            lambda old: dict(old or {}, status="SENT", remote_message_id=remote_id),
        )

    async def status(self, actor: ContextActor) -> Json:
        await self.context.authorize_actor(actor, manage=True)
        inboxes = await self.context.store.list(actor.workspace_id, "inbox")
        deliveries = await self.context.store.list(actor.workspace_id, "delivery")
        events = await self.context.store.list(actor.workspace_id, "event")
        receivers = await self.context.store.list(actor.workspace_id, "receiver")
        connections = await self.context.store.list(actor.workspace_id, "connection")
        active = {row["id"]: row for row in connections if row.get("enabled")}
        now = datetime.now(UTC)
        public_receivers: list[Json] = []
        for row in receivers:
            connection = active.get(row.get("connection_id"))
            if not connection or row.get("connection_version") != connection.get("version"):
                continue
            try:
                recent = timedelta(0) <= now - timestamp(row["heartbeat_at"]) < timedelta(seconds=50)
            except (KeyError, ValueError, TypeError):
                recent = False
            public_receivers.append(
                {
                    key: row.get(key)
                    for key in (
                        "instance_id",
                        "connection_id",
                        "heartbeat_at",
                        "last_callback_at",
                        "last_inbox_commit_at",
                        "last_error_code",
                        "last_error_at",
                        "reconnects",
                        "data_frames",
                        "last_data_frame_at",
                        "last_pong_at",
                    )
                }
                | {"transport_state": row.get("transport_state") if recent else "stale"}
            )
        return {
            "inbox": [{key: value for key, value in row.items() if key != "instruction"} for row in inboxes],
            "deliveries": [{key: value for key, value in row.items() if key != "text"} for row in deliveries],
            "receivers": public_receivers,
            "events": [
                {key: row.get(key) for key in ("connection_id", "event_id", "event_type", "message_id", "received_at")}
                for row in sorted(events, key=lambda item: str(item.get("received_at", "")), reverse=True)[:50]
            ],
        }

    async def run_status(self, scope: ContextRunScope) -> Json:
        """只读原会话元数据；按 Run 和来源重验权限，不向普通成员开放工作空间收件箱。"""
        await self.context.authorize_derived(scope)
        if scope.audience != "group" or scope.source_id is None:
            return {"inbox": None, "deliveries": []}
        source, connection, _ = await self.context._authorize(scope, scope.source_id)
        inbox = next(
            (
                row
                for row in await self.context.store.list(scope.actor.workspace_id, "inbox")
                if str(scope.run_id) in channel_run_ids(row)
                and row.get("actor_id") == str(scope.actor.user_id)
                and row.get("connection_id") == connection["id"]
                and row.get("chat_id") == source["chat_id"]
            ),
            None,
        )
        if inbox is None:
            return {"inbox": None, "deliveries": []}
        # 白名单投影不携带密文正文、用户 open_id、凭据或任意供应商字段。
        public_inbox = {key: inbox.get(key) for key in ("id", "message_id", "status", "created_at", "imported_at")}
        public_inbox["ingress"] = inbox.get("ingress", "feishu_event")
        public_inbox["addressed"] = inbox.get("addressed", True)
        public_inbox["coordination_scope_id"] = inbox.get("coordination_scope_id")
        deliveries: list[Json] = []
        for row in await self.context.store.list(scope.actor.workspace_id, "delivery"):
            if (
                row.get("run_id") != str(scope.run_id)
                or row.get("inbox_id") != inbox["id"]
                or row.get("source_id") != str(scope.source_id)
                or row.get("connection_id") != connection["id"]
                or row.get("chat_id") != source["chat_id"]
            ):
                continue
            deliveries.append(
                {
                    key: row.get(key)
                    for key in (
                        "id",
                        "status",
                        "attempts",
                        "remote_message_id",
                        "business_key",
                    )
                }
            )
        return {"inbox": public_inbox, "deliveries": deliveries}

    async def recover_and_purge(self, workspace_id: UUID) -> int:
        """重启后中断的发送不盲重试；正文 TTL 覆盖 Inbox 与 Outbox 加密副本。"""
        now = datetime.now(UTC)
        count = 0
        for kind, body_field in (("inbox", "instruction"), ("delivery", "text")):
            for row in await self.context.store.list(workspace_id, kind):
                key = f"{row['connection_id']}:{row['message_id']}" if kind == "inbox" else str(row["business_key"])
                if timestamp(row["expires_at"]) <= now and row.get(body_field) is not None:

                    def clear(old: Json | None, field: str = body_field) -> Json:
                        return dict(old or {}, **{field: None}, status="EXPIRED")

                    await self.context.store.change(workspace_id, kind, key, clear)
                    count += 1
                elif kind == "delivery" and row["status"] == "SENDING":
                    # Worker 正常发送时不抢占；只把超过供应商请求总时限的陈旧发送转为待核对。
                    if timestamp(row["started_at"]) < now - timedelta(seconds=30):
                        await self.context.store.change(
                            workspace_id, kind, key, lambda old: dict(old or {}, status="RECOVERY_REQUIRED")
                        )
        return count
