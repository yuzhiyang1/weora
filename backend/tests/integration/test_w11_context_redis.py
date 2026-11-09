"""W11 真实 PostgreSQL/Redis 边界；飞书权限故障用明确替身，不修改真实群授权。"""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from celery import Celery
from redis.asyncio import Redis

from tests.api.test_runs import RunsHarness
from tests.context.test_context import inbound
from tests.context.test_run_integration import ContextRuns, approval_scope, one_message
from tests.context.test_run_integration import contextual as contextual
from tests.integration.test_approval_recovery_postgres import PostgresHarness, prepared
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs
from weora.bootstrap.retention import purge_derived_content
from weora.bootstrap.worker import _pump_context
from weora.modules.collaboration.infrastructure.tasks import TaskModel
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextError, ContextRunScope, Json, MessagePage
from weora.modules.delivery.domain.models import ArtifactRecord
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.models import RunModel
from weora.modules.tools.application.gateway import ToolScope
from weora.modules.tools.infrastructure.store import SqlAlchemyToolStore

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def runs(postgres_runs: PostgresHarness) -> AsyncIterator[RunsHarness]:
    """复用现有跨模块场景，但实际存储换成本例独占 PostgreSQL schema。"""
    yield postgres_runs.runs


async def group_run(h: ContextRuns) -> tuple[UUID, ContextRunScope, FeishuChannels, str]:
    channels = FeishuChannels(h.context)
    await h.context.probe(h.context_actor, h.connection_id)
    await h.context.bind_identity(h.context_actor, h.connection_id, "ou_user", h.actor.user_id)
    await channels.save_route(
        h.context_actor,
        source_id=h.source_id,
        agent_id=UUID(h.original.agent_id),
        agent_version="0.1.0",
        include_nearby=True,
    )
    received = await channels.receive(h.actor.workspace_id, h.connection_id, inbound())
    await _pump_context(h.app)
    inbox = (await h.context.store.list(h.actor.workspace_id, "inbox"))[0]
    run_id = UUID(str(inbox["run_id"]))
    scope = cast(ContextRunScope, await h.app.state.context_scope_factory(h.context_actor, run_id))
    return run_id, scope, channels, str(received["inbox_id"])


@pytest.mark.parametrize("kind", ["unbind", "grant", "connection", "membership"])
async def test_postgres_revocation_blocks_every_derived_surface(contextual: ContextRuns, kind: str) -> None:
    h = contextual
    run_id, scope, channels, inbox_id = await group_run(h)
    h.reader.pages[None] = MessagePage((one_message(),))
    snapshot = await h.context.read(scope, h.source_id)
    await channels.enqueue_delivery(scope, inbox_id, "不得在撤权后发送的正文", "w11-revoke")
    executions: SqlAlchemyExecutionStore = h.app.state.execution_store
    lease = await executions.claim(
        workspace_id=h.actor.workspace_id, run_id=run_id, owner="revocation-test", lease_seconds=30
    )
    assert lease
    gateway = h.app.state.tool_gateway
    tool_scope = ToolScope(run_id, h.actor, lease.owner, lease.fencing_token)
    await gateway.invoke(tool_scope, "history", "feishu.messages.list", {"source_id": str(h.source_id)})
    if kind == "unbind":
        await h.context.bind_identity(h.context_actor, h.connection_id, "ou_user", h.actor.user_id, False)
    elif kind == "grant":
        await h.context.set_grant(h.context_actor, h.source_id, h.actor.user_id, False)
    elif kind == "connection":
        await h.context.save_connection(
            h.context_actor, connection_id=h.connection_id, app_id="cli_local_test", name="禁用测试连接", enabled=False
        )
    else:
        from sqlalchemy import update

        from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel

        async with h.original.sessions() as session, session.begin():
            await session.execute(
                update(WorkspaceMembershipModel)
                .where(
                    WorkspaceMembershipModel.workspace_id == h.actor.workspace_id,
                    WorkspaceMembershipModel.user_id == h.actor.user_id,
                )
                .values(status="inactive")
            )
    for suffix in ("", "/tools", "/artifacts", "/plan", "/deliveries"):
        denied = await h.client.get(h.base + f"/runs/{run_id}{suffix}")
        assert denied.status_code in {401, 403, 404}, (kind, suffix, denied.text)
        assert "企业 SSO" not in denied.text
    # 纯事件接口只保留 ID/状态/时刻审计，不包含正文；撤来源授权不等于删除本人的审计。
    audit = await h.client.get(h.base + f"/runs/{run_id}/events")
    assert audit.status_code == (401 if kind == "membership" else 200)
    assert "企业 SSO" not in audit.text
    preview = await h.client.get(h.base + f"/context/runs/{run_id}/snapshots/{snapshot['snapshot_id']}")
    assert preview.status_code in {401, 403, 404}
    result = await gateway.invoke(tool_scope, "history", "feishu.messages.list", {"source_id": str(h.source_id)})
    assert cast(Json, result["error"])["code"] == "ACCESS_REVOKED"
    denied = await h.client.post(h.base + f"/runs/{run_id}/recover", json={"mode": "read_only"}, headers=h.headers)
    assert denied.status_code in {401, 403, 404}
    with pytest.raises(ContextError):
        await channels.deliver(scope, "w11-revoke")
    assert h.reader.replies == 0
    assert len(await h.context.store.list(h.actor.workspace_id, "route")) == 1


async def test_postgres_retention_removes_all_body_copies_and_is_idempotent(contextual: ContextRuns) -> None:
    h = contextual
    run_id, scope, channels, inbox_id = await group_run(h)
    h.reader.pages[None] = MessagePage((one_message(),))
    snapshot = await h.context.read(scope, h.source_id)
    service = h.app.state.delivery_service
    plan = await service.propose(h.actor, run_id, approval_scope(), "企业 SSO 计划正文", "企业 SSO 风险正文")
    artifact = ArtifactRecord(
        uuid4(),
        run_id,
        h.actor.workspace_id,
        "report",
        "PASSED",
        "企业 SSO 产物正文",
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(days=1),
        "b" * 64,
        plan_id=plan.id,
    )
    await service.store.add_artifact(artifact)
    await channels.enqueue_delivery(scope, inbox_id, "企业 SSO 待发送正文", "w11-ttl")
    # 待审批 Run 没有活动执行权；单独用真实持久工具 Store 存储已有检查点副本。
    tools: SqlAlchemyToolStore = h.app.state.tool_store
    tool_scope = ToolScope(run_id, h.actor, "ttl-checkpoint", 1)
    await tools.reserve(
        tool_scope, "history", "feishu.messages.list", "c" * 64, {"query": "企业 SSO 参数"}, max_calls=24
    )
    await tools.finish(tool_scope, "history", result={"text": "企业 SSO 工具正文"}, status="SUCCEEDED", error_code=None)
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    for kind, key in (
        ("snapshot", str(snapshot["snapshot_id"])),
        ("inbox", f"{h.connection_id}:om_trigger"),
        ("delivery", "w11-ttl"),
    ):
        await h.context.store.change(h.actor.workspace_id, kind, key, lambda old: dict(old or {}, expires_at=expired))
    await _pump_context(h.app)
    await _pump_context(h.app)
    async with h.original.sessions() as session:
        run = await session.get(RunModel, run_id)
        assert run and run.output_text is None and run.failure_code == "CONTEXT_EXPIRED"
        task = await session.get(TaskModel, run.task_id)
        assert task and "企业 SSO" not in task.instruction
    calls = await tools.list_calls(tool_scope)
    assert calls[0].result is None and calls[0].arguments == {}
    artifacts = await service.store.artifacts(h.actor.workspace_id, run_id)
    assert artifacts[0].body is None and artifacts[0].sha256 == "b" * 64
    saved_plan = await service.store.latest_plan(h.actor.workspace_id, run_id)
    assert saved_plan and "企业 SSO" not in saved_plan.summary and not saved_plan.scope.commands
    for kind, body in (("snapshot", "messages"), ("inbox", "instruction"), ("delivery", "text")):
        rows = await h.context.store.list(h.actor.workspace_id, kind)
        assert all(not row.get(body) for row in rows)
    assert h.reader.replies == 0
    assert (await h.client.get(h.base + f"/runs/{run_id}")).status_code == 404
    await purge_derived_content(h.app, h.actor.workspace_id, {run_id})


@pytest.mark.parametrize("kind", ["inbox", "delivery"])
async def test_channel_ttl_denies_reads_before_background_purge(contextual: ContextRuns, kind: str) -> None:
    """授权失效必须即时发生，不能等 Beat 清理正文后才禁止读取。"""
    h = contextual
    run_id, scope, channels, inbox_id = await group_run(h)
    h.reader.pages[None] = MessagePage((one_message(),))
    snapshot = await h.context.read(scope, h.source_id)
    await channels.enqueue_delivery(scope, inbox_id, "尚未物理清理的派生正文", "w11-channel-ttl")
    key = f"{h.connection_id}:om_trigger" if kind == "inbox" else "w11-channel-ttl"
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await h.context.store.change(h.actor.workspace_id, kind, key, lambda old: dict(old or {}, expires_at=expired))
    for suffix in ("", "/tools", "/artifacts", "/plan", "/deliveries"):
        response = await h.client.get(h.base + f"/runs/{run_id}{suffix}")
        assert response.status_code == (404 if suffix in {"", "/tools"} else 409)
        assert "尚未物理清理" not in response.text
    assert (
        await h.client.get(h.base + f"/context/runs/{run_id}/snapshots/{snapshot['snapshot_id']}")
    ).status_code == 409
    with pytest.raises(ContextError) as denied:
        await channels.deliver(scope, "w11-channel-ttl")
    assert denied.value.code == "CONTEXT_EXPIRED"
    assert h.reader.replies == 0


async def test_redis_lost_queue_rebuilt_from_postgres_without_reexecuting_terminal(
    postgres_runs: PostgresHarness,
) -> None:
    """只删除本用例唯一队列键；不 FLUSHDB，不接触 weora.runs 或其他项目。"""
    broker = os.environ.get("WEORA_TEST_REDIS_URL")
    if not broker:
        pytest.skip("未显式提供本机 Redis 测试地址")
    assert urlsplit(broker).hostname in {"127.0.0.1", "localhost"}
    h = postgres_runs
    _, _, actor, run_id = await prepared(h)
    store = SqlAlchemyExecutionStore(h.runs.sessions)
    queue = "weora.w11-test." + uuid4().hex
    producer = Celery("w11-isolated-producer", broker=broker)
    redis = Redis.from_url(broker)

    def send() -> None:
        # 显式关闭本次连接，不把线程局部池留给已结束的 Windows 事件循环回收。
        with producer.connection_for_write() as connection:
            producer.send_task(
                "weora.runs.execute", args=[str(actor.workspace_id), str(run_id)], queue=queue, connection=connection
            )

    try:
        send()
        await store.mark_dispatched(workspace_id=actor.workspace_id, run_id=run_id)
        assert await redis.llen(queue) == 1
        assert await redis.delete(queue) == 1
        assert (await h.runs.client.get(f"{h.runs.path}/{run_id}")).json()["status"] == "QUEUED"
        assert (actor.workspace_id, run_id) not in await store.pending_dispatches()
        # 生产补投阈值30秒，使用真实时钟等待，不修改记录使测试提前通过。
        await asyncio.sleep(30.2)
        assert (actor.workspace_id, run_id) in await store.pending_dispatches()
        for _ in range(2):
            send()
        assert await redis.llen(queue) == 2
        claims = await asyncio.gather(
            *(
                store.claim(workspace_id=actor.workspace_id, run_id=run_id, owner=f"redis-worker-{i}", lease_seconds=30)
                for i in range(6)
            )
        )
        winners = [lease for lease in claims if lease]
        assert len(winners) == 1
        assert await store.finish(winners[0], status="SUCCEEDED", output="队列丢失后只执行一次")
        assert (actor.workspace_id, run_id) not in await store.pending_dispatches()
        assert (
            await store.claim(
                workspace_id=actor.workspace_id, run_id=run_id, owner="redelivered-terminal", lease_seconds=30
            )
            is None
        )
        events = (await h.runs.client.get(f"{h.runs.path}/{run_id}/events")).json()["items"]
        assert [event["event_type"] for event in events] == ["run.queued", "run.started", "run.succeeded"]
    finally:
        await redis.delete(queue, "_kombu.binding." + queue)
        await redis.aclose()
        producer.close()


async def test_real_http_accepted_then_worker_killed_never_resends(contextual: ContextRuns) -> None:
    """真实 HTTP 已返回200，客户端强杀丢失结果；不是在调用前抛一个假 Timeout。"""
    h = contextual
    run_id, scope, channels, inbox_id = await group_run(h)
    await channels.enqueue_delivery(scope, inbox_id, "W11 独立HTTP验收消息", "w11-outbound-unknown")
    receipts: list[bytes] = []

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            size = next(
                int(line.split(b":", 1)[1])
                for line in headers.splitlines()
                if line.lower().startswith(b"content-length:")
            )
            receipts.append(await reader.readexactly(size))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = h.app.state.settings
    from sqlalchemy.engine import make_url

    # 原 Session 工厂的引擎只用于定位私有 schema；凭据仍由测试显式环境传入。
    bind = h.original.sessions.kw["bind"]
    async with bind.connect() as connection:
        from sqlalchemy import text

        schema = await connection.scalar(text("select current_schema()"))
    env = dict(
        os.environ,
        WEORA_TEST_POSTGRES_URL=str(
            make_url(os.environ["WEORA_TEST_POSTGRES_URL"]).render_as_string(hide_password=False)
        ),
        WEORA_CRASH_SCHEMA=schema,
        WEORA_CRASH_RUN=str(run_id),
        WEORA_CRASH_WORKSPACE=str(h.actor.workspace_id),
        WEORA_CRASH_MODE="outbound",
        WEORA_CRASH_HTTP_PORT=str(port),
        WEORA_CRASH_CIPHER=settings.context_encryption_key.get_secret_value(),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        "tests.support.crash_worker",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout
        assert (await asyncio.wait_for(process.stdout.readline(), 20)).strip() == b"REMOTE_REPLY_ACCEPTED"
        process.kill()
        await asyncio.wait_for(process.wait(), 10)
        assert len(receipts) == 1
        pending = await h.context.store.get(h.actor.workspace_id, "delivery", "w11-outbound-unknown")
        assert pending and pending["status"] == "SENDING" and pending["attempts"] == 1
        # 使用真实30秒在途保护窗口；新实例不能抢占仍可能成功的请求。
        assert (await channels.deliver(scope, "w11-outbound-unknown"))["status"] == "SENDING"
        await asyncio.sleep(30.2)
        await channels.recover_and_purge(h.actor.workspace_id)
        for _ in range(3):
            result = await channels.deliver(scope, "w11-outbound-unknown")
            assert result["status"] == "RECOVERY_REQUIRED" and result["attempts"] == 1
        assert h.reader.replies == 0 and len(receipts) == 1
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        server.close()
        await server.wait_closed()
