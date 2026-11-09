"""W11 独立进程故障探针；仅允许随机测试 schema，不加载开发环境密钥。"""

import asyncio
import os
import re
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.context.test_context import Reader
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.bootstrap.worker import _reconcile_control, _reconcile_tools
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextActor
from weora.modules.context.service import ContextService
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import ArtifactRecord, PlanRecord, PlanScope
from weora.modules.runs.application.execution import RunExecutor, RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.module_access import ContextModuleAccess
from weora.modules.tools.application.gateway import ToolGateway, ToolScope, ToolSpec

PROFILE = ExecutionProfile("w11-controlled", "fixture", "no-paid-model", 64, 60)


async def main() -> None:
    """真实提交后阻塞，由父测试强杀；不用异常模拟 finally 被跳过。"""
    schema = os.environ["WEORA_CRASH_SCHEMA"]
    if not re.fullmatch(r"weora_approval_recovery_test_[0-9a-f]{32}", schema):
        raise ValueError("只允许本轮隔离 schema")
    engine = create_async_engine(
        os.environ["WEORA_TEST_POSTGRES_URL"],
        connect_args={"server_settings": {"search_path": f'"{schema}"'}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    mode = os.environ["WEORA_CRASH_MODE"]
    settings = IsolatedAppSettings(environment="test")
    if mode == "outbound":
        settings = IsolatedAppSettings(
            environment="test",
            context_encryption_key=SecretStr(os.environ["WEORA_CRASH_CIPHER"]),
            context_cursor_key=SecretStr("z" * 40),
        )
    app = create_app(settings, session_factory=sessions)
    run_id, workspace_id = UUID(os.environ["WEORA_CRASH_RUN"]), UUID(os.environ["WEORA_CRASH_WORKSPACE"])
    store = SqlAlchemyExecutionStore(sessions)
    found_actor = await store.actor_for_maintenance(run_id)
    assert found_actor is not None and found_actor.workspace_id == workspace_id
    actor: RunActor = found_actor
    service = cast(DeliveryService, app.state.delivery_service)
    access = cast(ContextModuleAccess, app.state.context_access)
    try:
        if mode == "outbound":
            import httpx

            class ReplyTransport(Reader):
                async def reply(
                    self, connection: dict[str, object], message_id: str, text: str, business_key: str,
                    *, image_keys: tuple[str, ...] = (),
                ) -> str:
                    # 真实 loopback HTTP 已接收成功，再卡在供应商适配器返回之前。
                    async with httpx.AsyncClient(trust_env=False) as client:
                        response = await client.post(
                            "http://127.0.0.1:" + os.environ["WEORA_CRASH_HTTP_PORT"],
                            json={"message_id": message_id, "business_key": business_key},
                        )
                        assert response.status_code == 200
                    print("REMOTE_REPLY_ACCEPTED", flush=True)
                    await asyncio.Event().wait()
                    return "om_remote_accepted"

            context = cast(ContextService, app.state.context_service)
            context.reader = ReplyTransport()
            scope = await app.state.context_scope_factory(ContextActor(actor.user_id, workspace_id), run_id)
            await FeishuChannels(context).deliver(scope, "w11-outbound-unknown")
            return
        if mode == "reconcile":
            await store.recover_expired()
            await _reconcile_tools(app)
            await _reconcile_control(app)
            print("RECONCILED", flush=True)
            return
        if mode == "decision":
            plan = await service.store.latest_plan(workspace_id, run_id)
            assert plan is not None and plan.status == "PENDING"
            from dataclasses import replace

            now = datetime.now(UTC)
            assert await service.store.save_plan(
                replace(
                    plan,
                    status="APPROVED",
                    decided_by=actor.user_id,
                    decided_at=now,
                    expires_at=now + timedelta(hours=1),
                ),
                expected_version=plan.version,
                expected_status="PENDING",
            )
            print("DECISION_COMMITTED", flush=True)
            await asyncio.Event().wait()

        class Access:
            async def is_active(self, current: RunActor) -> bool:
                return await access.is_active(ContextActor(current.user_id, current.workspace_id))

            async def load_input(self, _: object) -> RuntimeInput:
                return RuntimeInput("确定性故障断点，不请求外部模型", "验证持久化")

        class Runtime:
            async def run(self, _: RuntimeInput, __: ExecutionProfile) -> str:
                # 领取发生在生产 RunExecutor；探针只替代模型决策以准确命中崩溃窗口。
                from weora.modules.runs.infrastructure.models import RunModel

                async with sessions() as session:
                    run = await session.get(RunModel, run_id)
                    assert run is not None
                    token, owner = run.fencing_token, run.lease_owner
                assert owner is not None
                if mode == "proposal":
                    plan = PlanRecord(
                        uuid4(),
                        run_id,
                        workspace_id,
                        actor.user_id,
                        1,
                        PlanScope("fixture", "a" * 40, ("src",), (), ("file.write",)),
                        "提案已落库但投影未提交",
                        "等待真人审批",
                        "PENDING",
                        datetime.now(UTC),
                    )
                    assert await service.store.save_plan(plan, expected_version=0, expected_status=None)
                    print("PROPOSAL_COMMITTED", flush=True)
                    await asyncio.Event().wait()
                gateway: ToolGateway = app.state.tool_gateway

                async def write(scope: ToolScope, _: dict[str, object]) -> dict[str, object]:
                    artifact = ArtifactRecord(
                        uuid4(),
                        run_id,
                        workspace_id,
                        "report",
                        "PASSED",
                        "durable-write-once",
                        datetime.now(UTC),
                        datetime.now(UTC) + timedelta(days=1),
                        "a" * 64,
                    )
                    await service.store.add_artifact(artifact)
                    return {"ok": True, "artifact_id": str(artifact.id)}

                async def pending(scope: ToolScope, _: dict[str, object]) -> dict[str, object]:
                    # 外部效果已记录，但响应/本地工具终态还没写回。
                    await write(scope, {})
                    print("SIDE_EFFECT_COMMITTED", flush=True)
                    await asyncio.Event().wait()
                    return {"ok": True}

                schema: dict[str, object] = {"type": "object", "additionalProperties": False}
                gateway.register(ToolSpec("fixture.write", "测试持久写一次", schema, effect="write"), write)
                gateway.register(ToolSpec("fixture.pending", "测试未知结果窗口", schema, effect="write"), pending)
                scope = ToolScope(run_id, actor, owner, token)
                assert (await gateway.invoke(scope, "succeeded-write", "fixture.write", {}))["ok"] is True
                await gateway.invoke(scope, "pending-write", "fixture.pending", {})
                return "不得执行到这里"

        executor = RunExecutor(
            store=store, access=Access(), runtime=Runtime(), profile=PROFILE, lease_seconds=3, heartbeat_seconds=0.5
        )
        await executor.execute(workspace_id=workspace_id, run_id=run_id, owner="w11-crash-child")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
