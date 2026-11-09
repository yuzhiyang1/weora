"""移植 sources/feishu-loops.ts 的游标补偿；实时接收和补读共用同一 Inbox 去重。"""

import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextActor, ContextError, Json, Message
from weora.modules.context.service import timestamp
from weora.modules.coordination.domain import State
from weora.modules.coordination.engine import Engine, key
from weora.modules.coordination.service import CoordinationService


async def compensate(service: CoordinationService, workspace_id: UUID, scope_id: str, effect: object = None) -> int:
    """按来源游标补读群及订阅话题，消息仍通过原有授权 Inbox 去重；返回候选投递次数。

    首轮只建立边界，完整分页后才推进游标；失败保留原位置，下一轮依靠消息 ID 安全重读。
    外部读取不持有协调事务，游标和话题关联分别在短事务内保存。
    """
    state = await service.authorized_state(workspace_id, scope_id)
    config = state.config
    if not config.enabled:
        return 0
    if config.source_kind == "code_hosting":
        from weora.modules.coordination.domain import Effect

        if not service.repository_intake:
            raise ContextError("SOURCE_REVOKED")
        return await service.repository_intake.poll(
            service, workspace_id, scope_id, effect if isinstance(effect, Effect) else None
        )
    source = await service.context.store.get(workspace_id, "source", config.source_id)
    if not source:
        raise ContextError("SOURCE_REVOKED")
    # 阅读身份只是运输权限；每条消息仍按其真实作者绑定和授权，不用管理员身份代用户接单。
    bindings = await service.context.store.list(workspace_id, "binding")
    binding = next(
        (
            item
            for item in bindings
            if item.get("enabled")
            and item.get("open_id") in config.allowed_actors
            and item.get("connection_id") == source["connection_id"]
        ),
        None,
    )
    if not binding:
        raise ContextError("PERMISSION_DENIED")
    actor = ContextActor(UUID(str(binding["user_id"])), workspace_id)
    source, connection, version = await service.context.authorize_channel(
        actor, UUID(config.source_id), str(binding["open_id"])
    )
    route = await service.context.store.get(workspace_id, "route", config.route_key or "")
    thread_ids = {str(route["thread_id"])} if route and route.get("thread_id") else {"-"}
    # 订阅话题独立补偿；退订或过期后不继续读该话题。
    active_threads = {
        follow.thread
        for ticket in state.tickets
        if ticket.ended_by != "human"
        for follow in ticket.follows
        if not follow.closed_at
    }
    thread_ids.update(
        str(item.origin["thread_id"])
        for item in state.triggers
        if item.authorized and item.origin.get("thread_id") and set(item.threads) & active_threads
    )
    boundaries: dict[str, str] = {}
    for ticket in state.tickets:
        if ticket.ended_by == "human":
            continue
        origin = next((item for item in state.triggers if item.id == ticket.origin_id and item.authorized), None)
        if not origin:
            continue
        topic = origin.origin.get("thread_id") or state.checkpoints.get(f"discovered_topic:{origin.id}")
        if not topic:
            # 对照上游 pollThread 的 root/parent 初始化；发现 thread_id 只补元数据，不改写原始来源。
            try:
                root = await service.context.reader.message(connection, str(origin.origin.get("root_id") or origin.id))
                if root.chat_id == source["chat_id"]:
                    topic = root.thread_id
            except ContextError:
                continue
        if topic:
            topic_id = str(topic)
            origin_id, canonical = origin.id, origin.threads[0]
            thread_ids.add(topic_id)
            boundaries[topic_id] = min(boundaries.get(topic_id, ticket.created_at), ticket.created_at)

            def remember_topic(
                _: Engine,
                current: State,
                topic: str = topic_id,
                origin_id: str = origin_id,
                canonical: str = canonical,
            ) -> None:
                current.checkpoints[f"discovered_topic:{origin_id}"] = topic
                current.checkpoints.setdefault(f"topic:{topic}", canonical)

            await service.store.transact(workspace_id, scope_id, remember_topic)
    count = 0
    for thread_id in sorted(thread_ids):
        checkpoint_key = f"poll:{thread_id}"
        current = await service.store.snapshot(workspace_id, scope_id)
        checkpoint = cast(Json, current.checkpoints.get(checkpoint_key, {}))
        now = datetime.now(UTC)
        if (
            checkpoint
            and checkpoint.get("version") == version
            and checkpoint.get("next_at")
            and timestamp(checkpoint["next_at"]) > now
        ):
            continue
        if not checkpoint or checkpoint.get("version") != version:
            # 与原版一致：首次只建游标，不能部署后把群里旧消息全部变成新任务。
            boundary = boundaries.get(thread_id) if not checkpoint else None
            checkpoint = {
                "since": boundary or now.isoformat(),
                "version": version,
                "next_at": now.isoformat() if boundary else (now + timedelta(seconds=60)).isoformat(),
            }
            await _save(service, workspace_id, scope_id, checkpoint_key, checkpoint)
            if not boundary:
                continue
        start = timestamp(checkpoint["since"]) - timedelta(seconds=2)
        initialized = timestamp(checkpoint.get("initialized_at", checkpoint["since"]))
        end = timestamp(checkpoint["until"]) if checkpoint.get("until") else now
        page_token = cast(str | None, checkpoint.get("page"))
        seen = set(cast(list[str], checkpoint.get("pages", [])))
        for _ in range(10):
            page = await service.context.reader.messages(
                connection,
                chat_id=str(source["chat_id"]),
                start=start,
                end=end,
                page_token=page_token,
                thread_id=None if thread_id == "-" else thread_id,
            )
            for item in sorted(page.items, key=lambda item: (item.created_at, item.id)):
                if (
                    item.chat_id != source["chat_id"]
                    or (thread_id != "-" and item.thread_id != thread_id)
                    or item.created_at < max(start, initialized)
                    or item.created_at > end
                    or not item.human
                    or item.content_type not in {"text", "post"}
                    or not item.text
                ):
                    continue
                canonical_ref = current.checkpoints.get(f"topic:{item.thread_id}") if item.thread_id else None
                fallback = (
                    str(canonical_ref).split(":", 1)[1]
                    if isinstance(canonical_ref, str) and ":" in canonical_ref
                    else None
                )
                payload = _event(item, connection, fallback_root=fallback)
                await FeishuChannels(service.context, service.candidate_admission).receive(
                    workspace_id, UUID(str(source["connection_id"])), payload, ingress="compensation"
                )
                count += 1
            if not page.next_token:
                checkpoint = {
                    "since": end.isoformat(),
                    "initialized_at": initialized.isoformat(),
                    "version": version,
                    "next_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
                }
                break
            if page.next_token == page_token or page.next_token in seen:
                raise ContextError("PROVIDER_UNAVAILABLE", "飞书补偿分页未前进，保留原游标")
            seen.add(page.next_token)
            page_token = page.next_token
            checkpoint = {
                "since": checkpoint["since"],
                "until": end.isoformat(),
                "initialized_at": initialized.isoformat(),
                "version": version,
                "page": page_token,
                "pages": sorted(seen),
            }
        await _save(service, workspace_id, scope_id, checkpoint_key, checkpoint)
    return count


async def _save(service: CoordinationService, workspace_id: UUID, scope_id: str, name: str, value: Json) -> None:
    def save(_: Engine, state: State) -> None:
        state.checkpoints[name] = value

    await service.store.transact(workspace_id, scope_id, save)


def _event(message: Message, connection: Json, *, fallback_root: str | None = None) -> Json:
    # OpenAPI 历史 mentions 的 id 是字符串，实时事件是 id.open_id；归一化后复用既有严格入口。
    mentions = [
        {"key": item.get("key"), "id": {"open_id": item.get("id")}} if isinstance(item.get("id"), str) else dict(item)
        for item in message.mentions
    ]
    raw: Json = {
        "message_id": message.id,
        "chat_id": message.chat_id,
        "thread_id": message.thread_id,
        "parent_id": message.parent_id,
        "root_id": message.root_id or (fallback_root if message.id != fallback_root else None),
        "chat_type": "group",
        "message_type": "text",
        "mentions": mentions,
        "content": json.dumps({"text": message.text}, ensure_ascii=False),
        "create_time": str(int(message.created_at.timestamp() * 1000)),
    }
    return {
        "header": {
            "event_id": key("compensation", message.id),
            "app_id": connection["app_id"],
            "event_type": "im.message.receive_v1",
        },
        "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": message.author_id}}, "message": raw},
    }
