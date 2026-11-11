"""跨模块 SQLite 主链路：真实应用与持久化，飞书/模型使用明确的测试替身。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.context.test_context import Reader
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.bootstrap.retention import purge_derived_content
from weora.bootstrap.worker import _pump_context, _reconcile_control
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextActor, ContextRunScope, Json, Message, MessagePage
from weora.modules.context.service import ContextService
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import PlanRecord, PlanScope, PlanStatus
from weora.modules.identity.application.service import IdentityService
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork
from weora.modules.tools.application.gateway import ToolGateway, ToolScope
from weora.modules.tools.infrastructure.store import SqlAlchemyToolStore


@dataclass
class ContextRuns:
    original: RunsHarness
    app: FastAPI
    client: AsyncClient
    context: ContextService
    reader: Reader
    connection_id: UUID
    source_id: UUID

    @property
    def base(self) -> str:
        return f"/api/v1/workspaces/{self.original.workspace_id}"

    @property
    def actor(self) -> RunActor:
        return RunActor(UUID(self.original.user_id), UUID(self.original.workspace_id))

    @property
    def context_actor(self) -> ContextActor:
        return ContextActor(self.actor.user_id, self.actor.workspace_id)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies["weora_csrf"]}


@pytest.fixture
async def contextual(runs: RunsHarness) -> AsyncIterator[ContextRuns]:
    app = create_app(
        IsolatedAppSettings(environment="test", session_cookie_secure=False,
                            context_encryption_key=SecretStr(Fernet.generate_key().decode()),
                            context_cursor_key=SecretStr("z" * 40)),
        session_factory=runs.sessions, object_storage_factory=lambda _: None,
    )
    context = cast(ContextService, app.state.context_service)
    reader = Reader()
    context.reader = reader
    actor = ContextActor(UUID(runs.user_id), UUID(runs.workspace_id))
    connection = await context.save_connection(actor, app_id="cli_local_test", name="测试应用", secret="not-real")
    connection_id = UUID(str(connection["id"]))
    source = await context.save_source(actor, connection_id=connection_id, chat_id="oc_project", name="研发群")
    source_id = UUID(str(source["id"]))
    await context.set_grant(actor, source_id, actor.user_id, True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=runs.client.cookies) as client,
    ):
        yield ContextRuns(runs, app, client, context, reader, connection_id, source_id)


async def submit_console(harness: ContextRuns, key: str = "context-run") -> UUID:
    response = await harness.client.post(
        harness.base + "/runs", json=harness.original.payload("查询研发群的登录约定"),
        headers={**harness.headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def one_message() -> Message:
    return Message("om_context", "oc_project", "omt_login", "ou_external_author",
                   datetime.now(UTC) - timedelta(minutes=1), "登录使用企业 SSO，修改前先跑单元测试")


@pytest.mark.anyio
async def test_console_manual_and_tool_history_then_revocation_denies_all_derived_views(
    contextual: ContextRuns,
) -> None:
    h = contextual
    run_id = await submit_console(h)
    h.reader.pages[None] = MessagePage((one_message(),))
    manual = await h.client.post(h.base + f"/context/runs/{run_id}/messages",
                                  json={"source_id": str(h.source_id)}, headers=h.headers)
    assert manual.status_code == 200, manual.text
    snapshot = manual.json()["snapshot_id"]
    execution = cast(SqlAlchemyExecutionStore, h.app.state.execution_store)
    lease = await execution.claim(workspace_id=h.actor.workspace_id, run_id=run_id,
                                  owner="context-worker", lease_seconds=30)
    assert lease is not None
    gateway = cast(ToolGateway, h.app.state.tool_gateway)
    tool_scope = ToolScope(run_id, h.actor, lease.owner, lease.fencing_token)
    tool_result = await gateway.invoke(tool_scope, "history1", "feishu.messages.list", {"source_id": str(h.source_id)})
    assert tool_result["status"] == "complete", tool_result
    assert cast(list[Json], tool_result["messages"])[0]["author_id"] == "ou_external_author"
    stored_tools = await h.client.get(h.base + f"/runs/{run_id}/tools")
    assert stored_tools.status_code == 200 and stored_tools.json()["items"][0]["name"] == "feishu.messages.list"
    revoked = await h.client.put(h.base + f"/context/sources/{h.source_id}/grants/{h.actor.user_id}",
                                 json={"enabled": False}, headers=h.headers)
    assert revoked.status_code == 200
    for suffix in ("", "/tools", "/artifacts", "/plan", "/deliveries"):
        denied = await h.client.get(h.base + f"/runs/{run_id}{suffix}")
        assert denied.status_code == 404, (suffix, denied.text)
        assert "企业 SSO" not in denied.text
    preview = await h.client.get(h.base + f"/context/runs/{run_id}/snapshots/{snapshot}")
    assert preview.status_code == 403
    replay = await gateway.invoke(tool_scope, "history1", "feishu.messages.list", {"source_id": str(h.source_id)})
    assert cast(Json, replay["error"])["code"] == "ACCESS_REVOKED"
    recover = await h.client.post(h.base + f"/runs/{run_id}/recover", json={"mode": "read_only"}, headers=h.headers)
    assert recover.status_code == 404


@pytest.mark.anyio
async def test_feishu_inbox_pump_creates_group_run_and_assembles_automatic_first_input(contextual: ContextRuns) -> None:
    from tests.context.test_context import inbound

    h = contextual
    channels = FeishuChannels(h.context)
    await h.context.probe(h.context_actor, h.connection_id)
    await h.context.bind_identity(h.context_actor, h.connection_id, "ou_user", h.actor.user_id)
    await channels.save_route(h.context_actor, source_id=h.source_id, agent_id=UUID(h.original.agent_id),
                               agent_version="0.1.0", include_nearby=True)
    payload = inbound()
    received = await channels.receive(h.actor.workspace_id, h.connection_id, payload)
    await _pump_context(h.app)
    inboxes = await h.context.store.list(h.actor.workspace_id, "inbox")
    assert len(inboxes) == 1 and inboxes[0]["status"] == "SUBMITTED"
    assert inboxes[0]["id"] == received["inbox_id"]
    run_id = UUID(str(inboxes[0]["run_id"]))
    service = cast(RunService, h.app.state.run_service)
    detail = await service.access_metadata(h.actor, run_id)
    assert detail is not None and detail.task.audience == "group" and detail.task.origin == "feishu"
    assert detail.run.status == "QUEUED"
    ack = await h.context.store.get(h.actor.workspace_id, "delivery", f"run-ack:{run_id}")
    assert ack and ack["status"] == "SENT" and ack["message_id"] == "om_trigger"
    assert h.context.cipher.decrypt(str(ack["text"])) == "好，我来处理这件事。有结果我会在这里告诉你。"
    origin = detail.task.origin_metadata
    assert origin and origin["message_id"] == "om_trigger" and origin["source_id"] == str(h.source_id)
    assert "route_key" not in origin and "project_grant_id" not in origin
    scope = cast(ContextRunScope, await h.app.state.context_scope_factory(h.context_actor, run_id))
    assert scope.source_id == h.source_id and scope.origin_open_id == "ou_user"
    h.reader.pages[None] = MessagePage((one_message(),))
    access = ModulesExecutionAccess(
        identity=cast(IdentityService, h.app.state.identity_service), agents=h.app.state.agent_registry_service,
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(h.original.sessions), context=h.context,
        executions=h.app.state.execution_store,
    )
    model_input = await access.load_input(detail.run)
    assert "最终回复会发回飞书原话题" in model_input.system_prompt
    assert "企业 SSO" in model_input.instruction and "不是操作指令" in model_input.instruction
    snapshots = await h.context.snapshots(scope)
    refs = [str(message["ref"]) for item in snapshots for message in cast(list[Json], item["messages"])]
    assert len(refs) == 1
    await h.context.validate_refs(scope, refs)
    # 重连重复事件只回读原 Inbox/Run；同话题后续新消息仍可成为另一显式请求。
    await channels.receive(h.actor.workspace_id, h.connection_id, payload)
    await _pump_context(h.app)
    assert len(await h.context.store.list(h.actor.workspace_id, "inbox")) == 1
    assert h.reader.replies == 1
    await h.context.bind_identity(h.context_actor, h.connection_id, "ou_user", h.actor.user_id, False)
    denied = await h.client.get(h.base + f"/runs/{run_id}")
    assert denied.status_code == 404


@pytest.mark.anyio
async def test_context_retention_removes_run_task_and_tool_body_copies(contextual: ContextRuns) -> None:
    h = contextual
    run_id = await submit_console(h)
    h.reader.pages[None] = MessagePage((one_message(),))
    scope = cast(ContextRunScope, await h.app.state.context_scope_factory(h.context_actor, run_id))
    snapshot = await h.context.read(scope, h.source_id)
    execution = cast(SqlAlchemyExecutionStore, h.app.state.execution_store)
    lease = await execution.claim(workspace_id=h.actor.workspace_id, run_id=run_id,
                                  owner="context-worker", lease_seconds=30)
    assert lease is not None
    gateway = cast(ToolGateway, h.app.state.tool_gateway)
    tool_scope = ToolScope(run_id, h.actor, lease.owner, lease.fencing_token)
    await gateway.invoke(tool_scope, "history1", "feishu.messages.list", {"source_id": str(h.source_id)})
    assert await execution.finish(lease, status="SUCCEEDED", output="根据研发群，登录使用企业 SSO")
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await h.context.store.change(h.actor.workspace_id, "snapshot", str(snapshot["snapshot_id"]),
                                 lambda old: dict(old or {}, expires_at=expired))
    await purge_derived_content(h.app, h.actor.workspace_id, await h.context.expired_run_ids(h.actor.workspace_id))
    await h.context.purge_expired(h.actor.workspace_id)
    detail = await cast(RunService, h.app.state.run_service).access_metadata(h.actor, run_id)
    assert detail is not None and detail.run.output_text is None and detail.run.failure_code == "CONTEXT_EXPIRED"
    assert "查询研发群" not in detail.task.instruction
    calls = await cast(SqlAlchemyToolStore, h.app.state.tool_store).list_calls(tool_scope)
    assert calls and calls[0].result is None and calls[0].arguments == {}
    denied = await h.client.get(h.base + f"/runs/{run_id}")
    assert denied.status_code == 404


def approval_scope() -> PlanScope:
    """仅是审批材料，测试不配置也不调用真实沙箱。"""
    return PlanScope("test-repo", "a" * 40, ("src",), (), ("file.write",))


@pytest.mark.anyio
@pytest.mark.parametrize("decision_status,expected_run_status", [("APPROVED", "QUEUED"), ("REJECTED", "FAILED")])
async def test_beat_repairs_persisted_decision_before_run_projection_crash(
    contextual: ContextRuns, decision_status: PlanStatus, expected_run_status: str,
) -> None:
    """模拟决定已提交、Run 投影未提交即退出；Beat 只补状态，不执行模型或沙箱。"""
    h = contextual
    run_id = await submit_console(h)
    delivery = cast(DeliveryService, h.app.state.delivery_service)
    executions = cast(SqlAlchemyExecutionStore, h.app.state.execution_store)
    runs_service = cast(RunService, h.app.state.run_service)
    plan = await delivery.propose(h.actor, run_id, approval_scope(), "只修改登录处理", "仅隔离测试仓库")
    before = await runs_service.access_metadata(h.actor, run_id)
    assert before and before.run.status == "WAITING_APPROVAL"
    now = datetime.now(UTC)
    persisted = replace(plan, status=decision_status, decided_by=h.actor.user_id, decided_at=now,
                        expires_at=now + timedelta(hours=1) if decision_status == "APPROVED" else None)
    assert await delivery.store.save_plan(persisted, expected_version=plan.version, expected_status="PENDING")
    unchanged = await runs_service.access_metadata(h.actor, run_id)
    assert unchanged and unchanged.run.status == "WAITING_APPROVAL"
    await _reconcile_control(h.app)
    repaired = await runs_service.access_metadata(h.actor, run_id)
    assert repaired and repaired.run.status == expected_run_status
    assert repaired.run.last_sequence == before.run.last_sequence + 1
    latest_plan = await delivery.get_plan(h.actor, run_id)
    assert latest_plan and latest_plan.id == plan.id and latest_plan.status == decision_status
    assert latest_plan.decided_by == h.actor.user_id and latest_plan.decided_at == now
    await _reconcile_control(h.app)
    repeated = await runs_service.access_metadata(h.actor, run_id)
    assert repeated and repeated.run.last_sequence == repaired.run.last_sequence
    queued = await executions.pending_dispatches()
    assert ((h.actor.workspace_id, run_id) in queued) == (expected_run_status == "QUEUED")
    assert await delivery.store.artifacts(h.actor.workspace_id, run_id) == []


@pytest.mark.anyio
@pytest.mark.parametrize("persisted_status", ["PENDING", "APPROVED"])
async def test_beat_pending_proposal_crash_requires_human_but_approved_recovery_never_requeues(
    contextual: ContextRuns, persisted_status: PlanStatus,
) -> None:
    """提案落库后旧 Worker 租约失效：待审批只恢复界面，已批准的未知尝试绝不重放。"""
    h = contextual
    run_id = await submit_console(h)
    delivery = cast(DeliveryService, h.app.state.delivery_service)
    executions = cast(SqlAlchemyExecutionStore, h.app.state.execution_store)
    runs_service = cast(RunService, h.app.state.run_service)
    lease = await executions.claim(workspace_id=h.actor.workspace_id, run_id=run_id,
                                  owner="crashed-before-plan-projection", lease_seconds=3)
    assert lease
    now = datetime.now(UTC)
    plan = PlanRecord(uuid4(), run_id, h.actor.workspace_id, h.actor.user_id, 1, approval_scope(),
                      "保存提案后模拟进程退出", "必须人工确认", persisted_status, now,
                      decided_by=h.actor.user_id if persisted_status == "APPROVED" else None,
                      decided_at=now if persisted_status == "APPROVED" else None,
                      expires_at=now + timedelta(hours=1) if persisted_status == "APPROVED" else None)
    assert await delivery.store.save_plan(plan, expected_version=0, expected_status=None)
    expired_store = SqlAlchemyExecutionStore(h.original.sessions, clock=lambda: now + timedelta(seconds=10))
    assert await expired_store.recover_expired() == 1
    interrupted = await runs_service.access_metadata(h.actor, run_id)
    assert interrupted and interrupted.run.status == "RECOVERY_REQUIRED"
    await _reconcile_control(h.app)
    repaired = await runs_service.access_metadata(h.actor, run_id)
    expected = "WAITING_APPROVAL" if persisted_status == "PENDING" else "RECOVERY_REQUIRED"
    assert repaired and repaired.run.status == expected
    assert (h.actor.workspace_id, run_id) not in await executions.pending_dispatches()
    assert not await executions.authorize_token(h.actor, run_id, lease.fencing_token, lease.owner, write=True)
    latest = await delivery.get_plan(h.actor, run_id)
    assert latest and latest.status == persisted_status and latest.decided_by == plan.decided_by
    if persisted_status == "PENDING":
        response = await h.client.post(h.base + f"/runs/{run_id}/plan/{plan.id}/decision",
                                       json={"decision": "approve", "expires_in_seconds": 3600}, headers=h.headers)
        assert response.status_code == 200, response.text
        approved = await runs_service.access_metadata(h.actor, run_id)
        assert approved and approved.run.status == "QUEUED"
    else:
        await _reconcile_control(h.app)
        still_interrupted = await runs_service.access_metadata(h.actor, run_id)
        assert still_interrupted and still_interrupted.run.status == "RECOVERY_REQUIRED"
    assert await delivery.store.artifacts(h.actor.workspace_id, run_id) == []
