"""上下文边界测试：Fake 飞书不代表真实应用与权限已经联调。"""

import asyncio
import hashlib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet

from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextActor, ContextError, ContextRunScope, Json, Message, MessagePage
from weora.modules.context.feishu import FernetSecrets
from weora.modules.context.receiver import NON_TRIGGER_EVENTS, InboxAckBridge
from weora.modules.context.service import ContextService
from weora.modules.context.tools import TOOL_SPECS, ContextTools


class MemoryStore:
    """用同一业务键串行更新，模拟生产 Repository 的行锁语义。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str, str], Json] = {}
        self.locks: dict[tuple[UUID, str, str], asyncio.Lock] = {}

    async def get(self, workspace_id: UUID, kind: str, key: str) -> Json | None:
        return deepcopy(self.rows.get((workspace_id, kind, key)))

    async def list(self, workspace_id: UUID, kind: str) -> list[Json]:
        return [deepcopy(value) for (workspace, category, _), value in self.rows.items()
                if workspace == workspace_id and category == kind]

    async def change(
        self, workspace_id: UUID, kind: str, key: str, update: Callable[[Json | None], Json],
    ) -> Json:
        identity = (workspace_id, kind, key)
        async with self.locks.setdefault(identity, asyncio.Lock()):
            row = update(deepcopy(self.rows.get(identity)))
            self.rows[identity] = deepcopy(row)
            return deepcopy(row)


class Access:
    def __init__(self) -> None:
        self.inactive: set[UUID] = set()
        self.denied_runs: set[UUID] = set()
        self.admin = True

    async def is_active(self, actor: ContextActor) -> bool:
        return actor.user_id not in self.inactive

    async def can_manage(self, actor: ContextActor) -> bool:
        return self.admin

    async def can_access_run(self, scope: ContextRunScope) -> bool:
        return scope.run_id not in self.denied_runs


class Reader:
    def __init__(self) -> None:
        self.pages: dict[str | None, MessagePage] = {None: MessagePage(())}
        self.single: Message | None = None
        self.error: str | None = None
        self.thread_fails = False
        self.reply_fails = False
        self.reads = 0
        self.replies = 0
        self.uploads: list[bytes] = []
        self.sent_images: tuple[str, ...] = ()

    async def probe(self, connection: Json, chat_id: str | None = None) -> Json:
        return {"connection_status": "ready", "bot_open_id": "ou_bot", "history_status": "readable",
                "bot_membership": "verified"}

    async def messages(
        self, connection: Json, *, chat_id: str, start: datetime, end: datetime,
        page_token: str | None, thread_id: str | None = None, descending: bool = False,
    ) -> MessagePage:
        self.reads += 1
        if self.error:
            raise ContextError(self.error, "可重试的安全提示")
        if self.thread_fails and thread_id:
            raise ContextError("PROVIDER_UNAVAILABLE")
        return self.pages[page_token]

    async def message(self, connection: Json, message_id: str) -> Message:
        self.reads += 1
        if not self.single:
            raise ContextError("PROVIDER_UNAVAILABLE")
        return self.single

    async def upload_image(self, connection: Json, png: bytes) -> str:
        self.uploads.append(png)
        return f"img_test_{len(self.uploads)}"

    async def reply(
        self, connection: Json, message_id: str, text: str, business_key: str, *, image_keys: tuple[str, ...] = ()
    ) -> str:
        self.replies += 1
        self.sent_images = image_keys
        if self.reply_fails:
            raise TimeoutError
        return "om_reply"


def test_automatic_quote_and_thread_share_deadline_without_serial_timeouts() -> None:
    """引用和话题是独立只读请求；串行相加不应无谓耗尽3秒总预算。"""
    async def scenario() -> None:
        f = await setup()

        class SlowReader(Reader):
            async def message(self, connection: Json, message_id: str) -> Message:
                await asyncio.sleep(1.6)
                return f.message(1, thread="omt_discussion")

            async def messages(
                self, connection: Json, *, chat_id: str, start: datetime, end: datetime,
                page_token: str | None, thread_id: str | None = None, descending: bool = False,
            ) -> MessagePage:
                await asyncio.sleep(1.6)
                return MessagePage((f.message(2, thread="omt_discussion"),))

        f.service.reader = SlowReader()
        result = await f.service.automatic(f.scope, f.source, trigger_message_id="om_trigger",
                                           quote_id="om_quote", thread_id="omt_discussion")
        assert result["status"] == "complete", result["coverage"]
        assert len(cast(list[Json], result["messages"])) == 2

    asyncio.run(scenario())


@dataclass
class Fixture:
    service: ContextService
    store: MemoryStore
    reader: Reader
    access: Access
    actor: ContextActor
    scope: ContextRunScope
    connection: UUID
    source: UUID

    def message(self, number: int, *, chat: str = "oc_project", thread: str | None = None) -> Message:
        return Message(f"om_{number}", chat, thread, "ou_external_author",
                       self.scope.cutoff - timedelta(minutes=10) if self.scope.cutoff else datetime.now(UTC),
                       f"登录方案 {number}")


async def setup() -> Fixture:
    store, reader, access = MemoryStore(), Reader(), Access()
    actor = ContextActor(uuid4(), uuid4())
    scope = ContextRunScope(uuid4(), actor, cutoff=datetime.now(UTC))
    service = ContextService(store, reader, FernetSecrets(Fernet.generate_key().decode()), b"c" * 32, access)
    connection = await service.save_connection(actor, app_id="cli_testing", name="企业开发应用", secret="never-return")
    source = await service.save_source(actor, connection_id=UUID(str(connection["id"])),
                                       chat_id="oc_project", name="研发项目群")
    source_id = UUID(str(source["id"]))
    await service.set_grant(actor, source_id, actor.user_id, True)
    return Fixture(service, store, reader, access, actor, scope, UUID(str(connection["id"])), source_id)


def test_connection_secret_is_encrypted_and_never_returned() -> None:
    async def scenario() -> None:
        f = await setup()
        public = (await f.service.connections(f.actor))[0]
        assert "secret" not in public
        raw = await f.store.get(f.actor.workspace_id, "connection", str(f.connection))
        assert raw and raw["secret"] != "never-return"
        assert f.service.cipher.decrypt(str(raw["secret"])) == "never-return"
        await f.service.save_connection(f.actor, connection_id=f.connection, app_id="cli_testing",
                                        name="已更新", secret="rotated")
        new = await f.store.get(f.actor.workspace_id, "connection", str(f.connection))
        assert new and f.service.cipher.decrypt(str(new["secret"])) == "rotated"
    asyncio.run(scenario())


def test_management_requires_admin_and_existing_workspace_member() -> None:
    async def scenario() -> None:
        f = await setup()
        f.access.admin = False
        with pytest.raises(ContextError, match="管理员"):
            await f.service.connections(f.actor)
        f.access.admin = True
        f.access.inactive.add(f.actor.user_id)
        with pytest.raises(ContextError, match="停用"):
            await f.service.sources(f.actor)
    asyncio.run(scenario())


def test_binding_and_collection_do_not_implicitly_grant_source() -> None:
    async def scenario() -> None:
        f = await setup()
        user = ContextActor(uuid4(), f.actor.workspace_id)
        await f.service.bind_identity(f.actor, f.connection, "ou_same_name", user.user_id)
        await f.service.save_collection(f.actor, "登录项目", [f.source])
        assert await f.service.sources(user) == []
        assert await f.service.collections(user) == []
    asyncio.run(scenario())


def test_pagination_keeps_partial_upstream_page_offset() -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.pages[None] = MessagePage(tuple(f.message(i) for i in range(5)), "upstream-next")
        f.reader.pages["upstream-next"] = MessagePage((f.message(5),))
        first = await f.service.read(f.scope, f.source, limit=2)
        assert first["status"] == "partial"
        second = await f.service.read(f.scope, f.source, limit=2, cursor=str(first["next_cursor"]))
        third = await f.service.read(f.scope, f.source, limit=2, cursor=str(second["next_cursor"]))
        ids = [item["message_id"] for page in (first, second, third) for item in cast(list[Json], page["messages"])]
        assert ids == [f"om_{i}" for i in range(6)]
        assert third["status"] == "complete"
        assert cast(Json, third["coverage"])["window_complete"] is True
        assert "upstream-next" not in str(first["next_cursor"])
    asyncio.run(scenario())


@pytest.mark.parametrize("attack", ["tamper", "other_run", "other_window", "revoked", "expired"])
def test_cursor_is_bound_and_authenticated(attack: str) -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.pages[None] = MessagePage((f.message(1), f.message(2)))
        first = await f.service.read(f.scope, f.source, limit=1)
        cursor = str(first["next_cursor"])
        scope = f.scope
        start_time: datetime | None = None
        if attack == "tamper":
            cursor += "x"
        elif attack == "other_run":
            scope = replace(scope, run_id=uuid4())
        elif attack == "other_window":
            start_time = datetime.now(UTC) - timedelta(days=1)
        elif attack == "revoked":
            await f.service.set_grant(f.actor, f.source, f.actor.user_id, True)
        else:
            body = f.service._decode(cursor, {})
            body["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            cursor = f.service._cursor(body)
        with pytest.raises(ContextError) as error:
            await f.service.read(scope, f.source, limit=1, cursor=cursor, start_time=start_time)
        assert error.value.code == "INVALID_CURSOR"
    asyncio.run(scenario())


def test_thread_filter_has_scan_budget_and_never_claims_empty_complete() -> None:
    async def scenario() -> None:
        f = await setup()
        assert f.scope.cutoff
        old = replace(f.message(1, thread="omt_topic"), created_at=f.scope.cutoff - timedelta(days=20))
        for index in range(6):
            f.reader.pages[None if index == 0 else str(index)] = MessagePage((old,), str(index + 1))
        result = await f.service.read(f.scope, f.source, thread_id="omt_topic")
        assert result["messages"] == [] and result["status"] == "partial"
        assert cast(Json, result["coverage"])["reason"] == "scan_budget_exceeded"
        assert result["next_cursor"]
        assert f.reader.reads == 5
    asyncio.run(scenario())


def test_cross_group_get_and_thread_are_rejected() -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.single = f.message(1, chat="oc_private")
        with pytest.raises(ContextError) as error:
            await f.service.read(f.scope, f.source, message_id="om_1")
        assert error.value.code == "PERMISSION_DENIED"
        f.reader.pages[None] = MessagePage((f.message(1, thread="wrong_thread"),))
        with pytest.raises(ContextError):
            await f.service.read(f.scope, f.source, thread_id="omt_correct")
    asyncio.run(scenario())


@pytest.mark.parametrize("revoke", ["grant", "source", "connection", "member", "run"])
def test_preview_and_derived_outputs_reauthorize(revoke: str) -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.pages[None] = MessagePage((f.message(1),))
        result = await f.service.read(f.scope, f.source)
        if revoke == "grant":
            await f.service.set_grant(f.actor, f.source, f.actor.user_id, False)
        elif revoke == "source":
            await f.service.save_source(f.actor, connection_id=f.connection, source_id=f.source,
                                        chat_id="oc_project", name="项目群", enabled=False)
        elif revoke == "connection":
            await f.service.save_connection(f.actor, connection_id=f.connection, app_id="cli_testing",
                                            name="开发应用", enabled=False)
        elif revoke == "member":
            f.access.inactive.add(f.actor.user_id)
        else:
            f.access.denied_runs.add(f.scope.run_id)
        with pytest.raises(ContextError):
            await f.service.preview(f.scope, UUID(str(result["snapshot_id"])))
        with pytest.raises(ContextError):
            await f.service.authorize_derived(f.scope)
    asyncio.run(scenario())


def test_refs_must_come_from_delivered_same_run_snapshot() -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.pages[None] = MessagePage((f.message(1),))
        result = await f.service.read(f.scope, f.source)
        ref = str(cast(list[Json], result["messages"])[0]["ref"])
        await f.service.validate_refs(f.scope, [ref])
        for candidate in ["made-up", ref.rsplit(":", 1)[0] + ":99"]:
            with pytest.raises(ContextError):
                await f.service.validate_refs(f.scope, [candidate])
        with pytest.raises(ContextError):
            await f.service.validate_refs(replace(f.scope, run_id=uuid4()), [ref])
    asyncio.run(scenario())


def test_budget_counts_failed_calls_and_returns_safe_provider_failure() -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.error = "RATE_LIMITED"
        for _ in range(10):
            result = await f.service.read(f.scope, f.source)
            assert result["status"] == "unavailable"
            assert cast(Json, result["error"])["code"] == "RATE_LIMITED"
        with pytest.raises(ContextError) as error:
            await f.service.read(f.scope, f.source)
        assert error.value.code == "BUDGET_EXCEEDED"
        assert f.reader.reads == 10
    asyncio.run(scenario())


def test_content_budget_cannot_be_bypassed_by_new_role_or_page() -> None:
    async def scenario() -> None:
        f = await setup()
        await f.store.change(f.actor.workspace_id, "budget", str(f.scope.run_id),
                             lambda _: {"calls": 0, "messages": [], "tokens": 11999})
        f.reader.pages[None] = MessagePage((f.message(1),))
        result = await f.service.read(f.scope, f.source)
        assert result["status"] == "partial" and result["messages"] == []
        assert cast(Json, result["coverage"])["reason"] == "budget_exceeded"
        assert result["next_cursor"] is None
    asyncio.run(scenario())


def test_expiry_purges_body_but_retains_audit_digest() -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.pages[None] = MessagePage((f.message(1),))
        result = await f.service.read(f.scope, f.source)
        snapshot = str(result["snapshot_id"])
        expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        await f.store.change(f.actor.workspace_id, "snapshot", snapshot,
                             lambda old: dict(old or {}, expires_at=expired_at))
        assert await f.service.expired_run_ids(f.actor.workspace_id) == {f.scope.run_id}
        assert await f.service.purge_expired(f.actor.workspace_id) == 1
        retained = await f.store.get(f.actor.workspace_id, "snapshot", snapshot)
        assert retained and retained["messages"] == [] and retained["sha256"] == result["sha256"]
        with pytest.raises(ContextError) as error:
            await f.service.preview(f.scope, UUID(snapshot))
        assert error.value.code == "CONTEXT_EXPIRED"
    asyncio.run(scenario())


def test_automatic_prioritizes_quote_and_does_not_fallback_after_thread_failure() -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.single = f.message(90)
        f.reader.thread_fails = True
        f.reader.pages[None] = MessagePage((f.message(1),))
        result = await f.service.automatic(f.scope, f.source, trigger_message_id="om_trigger",
                                         quote_id="om_90", thread_id="omt_topic", include_nearby=True)
        assert [item["message_id"] for item in cast(list[Json], result["messages"])] == ["om_90"]
        assert "thread_unavailable" in str(cast(Json, result["coverage"])["reason"])
        assert f.reader.reads == 2
    asyncio.run(scenario())


def test_automatic_cross_group_quote_not_expanded_and_trigger_excluded() -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.single = f.message(90, chat="oc_secret")
        f.reader.pages[None] = MessagePage((f.message(1), f.message(2), f.message(1)))
        result = await f.service.automatic(f.scope, f.source, trigger_message_id="om_1",
                                         quote_id="om_90", include_nearby=True)
        assert [item["message_id"] for item in cast(list[Json], result["messages"])] == ["om_2"]
        assert "cross_group_quote" in str(cast(Json, result["coverage"])["reason"])
    asyncio.run(scenario())


@pytest.mark.parametrize("channel", ["nearby", "thread", "quote", "manual"])
def test_selected_unsupported_content_never_reports_complete(channel: str) -> None:
    async def scenario() -> None:
        f = await setup()
        unsupported = replace(f.message(1), content_type="system", text="[不支持的消息内容，未下载附件]")
        f.reader.pages[None] = MessagePage((unsupported, f.message(2)))
        f.reader.single = unsupported
        if channel == "manual":
            result = await f.service.read(f.scope, f.source)
        else:
            if channel == "thread":
                f.reader.pages[None] = MessagePage((replace(unsupported, thread_id="omt_topic"),))
            result = await f.service.automatic(
                f.scope, f.source, trigger_message_id="om_trigger", include_nearby=channel == "nearby",
                thread_id="omt_topic" if channel == "thread" else None,
                quote_id=unsupported.id if channel == "quote" else None,
            )
        assert result["status"] == "partial"
        coverage = cast(Json, result["coverage"])
        assert coverage["window_complete"] is False
        assert "unsupported_content" in str(coverage["reason"])
        # 占位消息仍是可审计事实，不能伪装成已经理解的文字内容。
        assert cast(list[Json], result["messages"])[0]["content_type"] == "system"
    asyncio.run(scenario())


@pytest.mark.parametrize("excluded", ["trigger", "other_group", "future", "over_budget"])
def test_automatic_unsupported_reason_only_covers_delivered_messages(excluded: str) -> None:
    async def scenario() -> None:
        f = await setup()
        unsupported = replace(f.message(1), content_type="image", text="[不支持的消息内容，未下载附件]")
        if excluded == "other_group":
            unsupported = replace(unsupported, chat_id="oc_other")
        elif excluded == "future":
            unsupported = replace(unsupported, created_at=datetime.now(UTC) + timedelta(days=1))
        elif excluded == "over_budget":
            # 先交付接近自动补齐预算的文本；图片占位被预算裁掉，不应归因于不支持的正文。
            f.reader.single = replace(f.message(0), text="a" * 3980)
        f.reader.pages[None] = MessagePage((unsupported,))
        result = await f.service.automatic(
            f.scope, f.source, trigger_message_id=unsupported.id if excluded == "trigger" else "om_trigger",
            include_nearby=True, quote_id="om_0" if excluded == "over_budget" else None,
        )
        assert "unsupported_content" not in str(cast(Json, result["coverage"])["reason"])
        assert all(item["content_type"] == "text" for item in cast(list[Json], result["messages"]))
        assert result["status"] == ("partial" if excluded == "over_budget" else "complete")
    asyncio.run(scenario())


def test_automatic_unsupported_content_preserves_existing_coverage_gap() -> None:
    async def scenario() -> None:
        f = await setup()
        unsupported = replace(f.message(1), content_type="image", text="[不支持的消息内容，未下载附件]")
        f.reader.pages[None] = MessagePage((unsupported,), next_token="unread-page")
        result = await f.service.automatic(f.scope, f.source, trigger_message_id="om_trigger", include_nearby=True)
        assert result["status"] == "partial"
        assert cast(Json, result["coverage"])["reason"] == "has_more,unsupported_content"
    asyncio.run(scenario())


def test_group_run_cannot_read_other_source_and_binding_revocation_applies() -> None:
    async def scenario() -> None:
        f = await setup()
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
        scope = replace(f.scope, audience="group", source_id=f.source, origin_open_id="ou_user")
        await f.service.read(scope, f.source)
        with pytest.raises(ContextError):
            await f.service.read(replace(scope, source_id=uuid4()), f.source)
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id, False)
        with pytest.raises(ContextError):
            await f.service.read(scope, f.source)
    asyncio.run(scenario())


def test_four_tool_contracts_reject_model_identity_injection() -> None:
    async def scenario() -> None:
        f = await setup()
        assert {item.name for item in TOOL_SPECS} == {
            "context.sources.list", "feishu.messages.list", "feishu.thread.read", "feishu.message.get",
        }
        tools = ContextTools(f.service)
        with pytest.raises(ContextError) as error:
            await tools.execute(f.scope, "feishu.messages.list", {"source_id": str(f.source), "api_key": "injected"})
        assert error.value.code == "INVALID_PARAMETERS"
        result = await tools.execute(f.scope, "arbitrary.http", {})
        assert cast(Json, result["error"])["code"] == "TOOL_NOT_ALLOWED"
    asyncio.run(scenario())


def inbound(event_id: str = "event1", message_id: str = "om_trigger", open_id: str = "ou_user") -> Json:
    return {"header": {"event_id": event_id, "event_type": "im.message.receive_v1"}, "event": {
        "sender": {"sender_type": "user", "sender_id": {"open_id": open_id}},
        "message": {"message_id": message_id, "chat_id": "oc_project", "chat_type": "group", "message_type": "text",
                    "create_time": str(int(datetime.now(UTC).timestamp() * 1000)),
                    "content": '{"text":"@_user_1 修复登录"}',
                    "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}]},
    }}


async def prepare_channels(f: Fixture) -> FeishuChannels:
    channels = FeishuChannels(f.service)
    await f.service.probe(f.actor, f.connection)
    await channels.save_route(f.actor, source_id=f.source, agent_id=uuid4(), agent_version="1.0.0")
    return channels


def test_inbox_deduplicates_event_and_message_but_not_later_explicit_request() -> None:
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        first = await channels.receive(f.actor.workspace_id, f.connection, inbound())
        retry = await channels.receive(f.actor.workspace_id, f.connection, inbound())
        new_event = await channels.receive(f.actor.workspace_id, f.connection, inbound("event2"))
        later = await channels.receive(f.actor.workspace_id, f.connection, inbound("event3", "om_later"))
        assert first["inbox_id"] == retry["inbox_id"] == new_event["inbox_id"]
        assert later["inbox_id"] != first["inbox_id"]
        assert f.reader.reads == 0
        status = await channels.status(f.actor)
        assert "instruction" not in cast(list[Json], status["inbox"])[0]
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [
    ("header", []), ("event", "invalid"), ("message", []), ("sender", []),
    ("sender_id", []), ("mentions", None), ("mentions", [None]),
    ("mentions", [{"id": []}]), ("message_id", None), ("chat_id", None),
    ("open_id", None), ("content", "[]"), ("content", '{"text": null}'),
])
def test_malformed_event_is_safe_error_without_inbox(field: str, value: object) -> None:
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        payload = inbound()
        event = cast(Json, payload["event"])
        message = cast(Json, event["message"])
        sender = cast(Json, event["sender"])
        if field in {"header", "event"}:
            payload[field] = value
        elif field in {"message", "sender"}:
            event[field] = value
        elif field == "sender_id":
            sender[field] = value
        elif field == "open_id":
            cast(Json, sender["sender_id"])[field] = value
        else:
            message[field] = value
        with pytest.raises(ContextError) as error:
            await channels.receive(f.actor.workspace_id, f.connection, payload)
        assert error.value.code == "INVALID_EVENT"
        assert await f.store.list(f.actor.workspace_id, "inbox") == []
        assert f.reader.reads == 0
    asyncio.run(scenario())


def test_unknown_identity_never_reads_history_or_submits_run() -> None:
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        inbox = await channels.receive(f.actor.workspace_id, f.connection, inbound())

        async def submit(actor: ContextActor, payload: Json) -> str:
            pytest.fail("未知身份不能提交")

        result = await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        assert result["status"] == "REJECTED" and f.reader.reads == 0
    asyncio.run(scenario())


def test_inbox_processing_freezes_route_and_idempotency_key() -> None:
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
        inbox = await channels.receive(f.actor.workspace_id, f.connection, inbound())
        calls: list[Json] = []

        async def submit(actor: ContextActor, payload: Json) -> str:
            calls.append(payload)
            return str(f.scope.run_id)

        result = await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        assert result["status"] == "SUBMITTED" and len(calls) == 1
        assert calls[0]["idempotency_key"] == f"feishu:{inbox['inbox_id']}"
        assert calls[0]["instruction"] == "修复登录" and calls[0]["agent_version"] == "1.0.0"
    asyncio.run(scenario())


def test_bot_ordinary_message_and_edits_do_not_start_run() -> None:
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        payload = inbound()
        event = cast(Json, payload["event"])
        cast(Json, event["sender"])["sender_type"] = "app"
        assert (await channels.receive(f.actor.workspace_id, f.connection, payload))["status"] == "ignored"
        payload = inbound("event2")
        cast(Json, cast(Json, payload["event"])["message"])["mentions"] = []
        assert (await channels.receive(f.actor.workspace_id, f.connection, payload))["status"] == "ignored"
        payload = inbound("event3")
        cast(Json, payload["header"])["event_type"] = "im.message.recalled_v1"
        assert (await channels.receive(f.actor.workspace_id, f.connection, payload))["status"] == "ignored"
        for index, event_type in enumerate(NON_TRIGGER_EVENTS):
            payload = {"header": {"event_id": f"aux_{index}", "event_type": event_type},
                       "event": {"message_id": "om_trigger"}}
            result = await channels.receive(f.actor.workspace_id, f.connection, payload)
            assert result == {"status": "ignored", "reason": "not_a_new_message"}
        assert await f.store.list(f.actor.workspace_id, "inbox") == []
        assert len(await f.store.list(f.actor.workspace_id, "event")) == 3 + len(NON_TRIGGER_EVENTS)
    asyncio.run(scenario())


@pytest.mark.parametrize("purpose", ["ack", "result"])
def test_same_sender_can_follow_up_after_bot_reply_without_mention(purpose: str) -> None:
    """飞书首次回复可创建话题，根消息入库时没有 thread_id。"""
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
        ws = f.actor.workspace_id
        root = await channels.receive(ws, f.connection, inbound())
        calls: list[Json] = []

        async def submit(actor: ContextActor, payload: Json) -> str:
            calls.append(payload)
            return str(f.scope.run_id)

        await channels.process(ws, str(root["inbox_id"]), submit)
        scope = replace(f.scope, audience="group", source_id=f.source)
        key = f"run-{purpose}:{f.scope.run_id}"
        await channels.enqueue_delivery(scope, str(root["inbox_id"]), "机器人的回复", key)

        def follow_up(event_id: str, open_id: str = "ou_user", chat_id: str = "oc_project") -> Json:
            payload = inbound(event_id, f"om_{event_id}", open_id)
            cast(Json, cast(Json, payload["event"])["message"]).update(
                chat_id=chat_id, thread_id="omt_topic", root_id="om_trigger", parent_id="om_trigger",
                mentions=[], content='{"text":"请用飞书 CLI 读取表格"}',
            )
            return payload

        assert (await channels.receive(ws, f.connection, follow_up("pending")))["status"] == "ignored"
        assert (await channels.deliver(scope, key))["status"] == "SENT"
        assert (await channels.receive(ws, f.connection, follow_up("other", "ou_other")))["status"] == "ignored"
        other_chat = await channels.receive(ws, f.connection, follow_up("different_chat", chat_id="oc_other"))
        assert other_chat["status"] == "ignored"
        payload = follow_up("continued")
        accepted = await channels.receive(ws, f.connection, payload)
        assert accepted["status"] == "persisted"
        assert (await channels.receive(ws, f.connection, payload))["inbox_id"] == accepted["inbox_id"]
        await channels.process(ws, str(accepted["inbox_id"]), submit)
        assert calls[-1]["addressed"] is True and calls[-1]["root_id"] == "om_trigger"
        assert calls[-1]["instruction"] == "请用飞书 CLI 读取表格"
        assert len(calls) == 2
    asyncio.run(scenario())


def test_outbox_unknown_result_not_retried_and_private_audience_denied() -> None:
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
        inbox = await channels.receive(f.actor.workspace_id, f.connection, inbound())

        async def submit(actor: ContextActor, payload: Json) -> str:
            return str(f.scope.run_id)

        await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        with pytest.raises(ContextError):
            await channels.enqueue_delivery(f.scope, str(inbox["inbox_id"]), "私有答案", "delivery1")
        scope = replace(f.scope, audience="group", source_id=f.source)
        await channels.enqueue_delivery(scope, str(inbox["inbox_id"]), "测试完成", "delivery1")
        f.reader.reply_fails = True
        result = await channels.deliver(scope, "delivery1")
        assert result["status"] == "RECOVERY_REQUIRED"
        await channels.deliver(scope, "delivery1")
        assert f.reader.replies == 1
        await f.service.set_grant(f.actor, f.source, f.actor.user_id, False)
        with pytest.raises(ContextError):
            await channels.deliver(scope, "delivery1")
    asyncio.run(scenario())


def test_manual_history_notifications_always_label_provenance_without_rewriting_legacy_outbox() -> None:
    """群里看到的入口类型由平台保证；同键升级重放不改写已发送记录。"""
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
        inbox = await channels.receive(f.actor.workspace_id, f.connection, inbound())

        async def submit(actor: ContextActor, payload: Json) -> str:
            return str(f.scope.run_id)

        await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        scope = replace(f.scope, audience="group", source_id=f.source)
        await f.store.change(f.actor.workspace_id, "inbox", f"{f.connection}:om_trigger",
                             lambda old: dict(old or {}, ingress="manual_history"))
        result = await channels.enqueue_delivery(scope, str(inbox["inbox_id"]), "测试完成", "new-notice")
        text = f.service.cipher.decrypt(str(result["text"]))
        assert text.startswith("【人工补收】") and "不代表长连接自动入站通过" in text
        assert text.endswith("测试完成")
        assert await channels.enqueue_delivery(scope, str(inbox["inbox_id"]), "测试完成", "new-notice") == result
        with pytest.raises(ContextError):
            await channels.enqueue_delivery(scope, str(inbox["inbox_id"]), "另一答案", "new-notice")

        # 模拟修复前的已送达记录：不补造新发送、不改原文或历史摘要。
        legacy = dict(result, id="legacy", business_key="legacy-notice", status="SENT",
                      text=f.service.cipher.encrypt("历史答案"), sha256=hashlib.sha256("历史答案".encode()).hexdigest())
        legacy.pop("source_text_sha256", None)
        await f.store.change(f.actor.workspace_id, "delivery", "legacy-notice", lambda _: legacy)
        replay = await channels.enqueue_delivery(scope, str(inbox["inbox_id"]), "历史答案", "legacy-notice")
        assert replay == legacy
        assert f.service.cipher.decrypt(str(replay["text"])) == "历史答案"
        assert f.reader.replies == 0
    asyncio.run(scenario())


def test_ack_bridge_propagates_failed_durable_commit_without_success() -> None:
    async def save(payload: Json) -> Json:
        raise RuntimeError("sensitive database parameters")

    bridge = InboxAckBridge(save)
    try:
        with pytest.raises(ContextError) as error:
            bridge.accept({})
        assert "sensitive" not in str(error.value)
    finally:
        bridge.close()


def test_automatic_timeout_keeps_quote_already_obtained(monkeypatch: pytest.MonkeyPatch) -> None:
    real_timeout = asyncio.timeout

    def fast_deadline(delay: float | None) -> asyncio.Timeout:
        return real_timeout(0.01)

    class SlowReader(Reader):
        async def messages(
            self, connection: Json, *, chat_id: str, start: datetime, end: datetime,
            page_token: str | None, thread_id: str | None = None, descending: bool = False,
        ) -> MessagePage:
            await asyncio.Event().wait()
            return MessagePage(())

    async def scenario() -> None:
        f = await setup()
        reader = SlowReader()
        reader.single = f.message(90)
        f.service.reader = reader
        monkeypatch.setattr(asyncio, "timeout", fast_deadline)
        result = await f.service.automatic(f.scope, f.source, trigger_message_id="om_trigger",
                                         quote_id="om_90", thread_id="omt_topic")
        assert result["status"] == "partial"
        assert "timeout" in str(cast(Json, result["coverage"])["reason"])
        assert [item["message_id"] for item in cast(list[Json], result["messages"])] == ["om_90"]
    asyncio.run(scenario())


def test_channel_retention_purges_all_encrypted_body_copies() -> None:
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
        inbox = await channels.receive(f.actor.workspace_id, f.connection, inbound())

        async def submit(actor: ContextActor, payload: Json) -> str:
            return str(f.scope.run_id)

        await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        scope = replace(f.scope, audience="group", source_id=f.source)
        await channels.enqueue_delivery(scope, str(inbox["inbox_id"]), "测试完成", "delivery1")
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await f.store.change(f.actor.workspace_id, "inbox", f"{f.connection}:om_trigger",
                             lambda old: dict(old or {}, expires_at=expired))
        await f.store.change(f.actor.workspace_id, "delivery", "delivery1",
                             lambda old: dict(old or {}, expires_at=expired))
        assert await f.service.expired_run_ids(f.actor.workspace_id) == {f.scope.run_id}
        assert await channels.recover_and_purge(f.actor.workspace_id) == 2
        messages = await f.store.list(f.actor.workspace_id, "inbox")
        deliveries = await f.store.list(f.actor.workspace_id, "delivery")
        assert messages[0]["instruction"] is None and deliveries[0]["text"] is None
        assert messages[0]["status"] == deliveries[0]["status"] == "EXPIRED"
    asyncio.run(scenario())


def test_inbox_route_is_frozen_before_submit_and_reused_after_crash() -> None:
    class CrashOnSubmittedStore(MemoryStore):
        fail_submitted = True

        async def change(
            self, workspace_id: UUID, kind: str, key: str, update: Callable[[Json | None], Json],
        ) -> Json:
            if kind == "inbox" and self.fail_submitted:
                candidate = update(await self.get(workspace_id, kind, key))
                if candidate.get("status") == "SUBMITTED":
                    self.fail_submitted = False
                    raise ContextError("DATABASE_UNAVAILABLE", "模拟提交成功但本地回写失败")
            return await super().change(workspace_id, kind, key, update)

    async def scenario() -> None:
        f = await setup()
        store = CrashOnSubmittedStore()
        store.rows = deepcopy(f.store.rows)
        f.service.store = store
        channels = await prepare_channels(f)
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
        inbox = await channels.receive(f.actor.workspace_id, f.connection, inbound())
        submitted: list[Json] = []

        async def submit(actor: ContextActor, payload: Json) -> str:
            submitted.append(payload)
            # 真实 RunService 对同键同指纹回读；此处只检查所有重试指纹材料固定。
            return str(f.scope.run_id)

        with pytest.raises(ContextError, match="回写失败"):
            await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        await channels.save_route(f.actor, source_id=f.source, agent_id=uuid4(), agent_version="2.0.0",
                                   include_nearby=True)
        result = await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        assert result["status"] == "SUBMITTED" and len(submitted) == 2
        for field in ("agent_id", "agent_version", "source_id", "include_nearby", "idempotency_key", "instruction"):
            assert submitted[0][field] == submitted[1][field]
        assert submitted[1]["agent_version"] == "1.0.0" and submitted[1]["include_nearby"] is False
    asyncio.run(scenario())
