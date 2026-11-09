"""文本 Runtime 恢复截图证据时只接收引用，不把 Base64 当成可理解的图像。"""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from tests.api.test_runs import RunsHarness, runs
from tests.delivery.fakes import Browser, Remote, Sandbox
from tests.delivery.test_service import scope
from weora.modules.agents.application.service import AgentRegistryService
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import ArtifactRecord
from weora.modules.delivery.infrastructure.persistence import SqlAlchemyDeliveryStore
from weora.modules.identity.application.service import IdentityService
from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.ports import RunActor, RunRecord
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.module_access import ContextModuleAccess, DeliveryModuleAccess
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork

media_runs = runs


async def prepared(
    runs: RunsHarness, screenshot: str
) -> tuple[ModulesExecutionAccess, RunRecord, DeliveryService, list[ArtifactRecord]]:
    """真实应用材料和私有数据库，只有不在本场景执行的外部适配器使用 Fake。"""
    response = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert response.status_code == 201, response.text
    run_id = UUID(response.json()["id"])
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    delivery = DeliveryService(
        SqlAlchemyDeliveryStore(runs.sessions),
        DeliveryModuleAccess(
            ContextModuleAccess(runs.app.state.identity_service, runs.app.state.run_service),
            SqlAlchemyExecutionStore(runs.sessions),
            None,
            execution_workspace_id=actor.workspace_id,
        ),
        Sandbox(),
        Browser(),
        Remote(),
    )
    plan = await delivery.propose(actor, run_id, scope(), "修复后验证截图", "只操作测试仓库")
    await delivery.decide(actor, run_id, plan.id, "approve")
    now = datetime.now(UTC)
    records = []
    for kind, body in (("screenshot", screenshot), ("command", "1 passed in 0.01s"), ("diff", "+ 修复真实代码")):
        record = ArtifactRecord(
            uuid4(),
            run_id,
            actor.workspace_id,
            kind,
            "PASSED",
            body,
            now,
            now + timedelta(days=1),
            hashlib.sha256(body.encode()).hexdigest(),
            plan_id=plan.id,
        )
        records.append(await delivery.store.add_artifact(record))
    details = await runs.app.state.run_service.access_metadata(actor, run_id)
    assert details is not None
    access = ModulesExecutionAccess(
        identity=cast(IdentityService, runs.app.state.identity_service),
        agents=cast(AgentRegistryService, runs.app.state.agent_registry_service),
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(runs.sessions),
        delivery=delivery,
    )
    return access, details.run, delivery, records


@pytest.mark.anyio
@pytest.mark.parametrize("read_only", [False, True])
async def test_large_screenshot_does_not_exhaust_text_recovery_context(
    media_runs: RunsHarness, read_only: bool
) -> None:
    """正常审批继续和只读恢复都不能因为图像编码超出文本材料上限。"""
    screenshot = "iVBORw0KGgo" + "A" * 200000
    access, run, _, records = await prepared(media_runs, screenshot)
    payload = await access.load_input(replace(run, recovery_read_only=read_only))
    assert len((payload.system_prompt + payload.instruction).encode()) < 128000
    assert screenshot not in payload.instruction
    assert str(records[0].id) in payload.instruction
    assert records[0].sha256 in payload.instruction
    assert "1 passed in 0.01s" in payload.instruction and "+ 修复真实代码" in payload.instruction


@pytest.mark.anyio
async def test_image_projection_preserves_authorized_original_and_metadata(media_runs: RunsHarness) -> None:
    """即使小图也不灌入文本；装配只创建投影，不覆盖原始可预览截图。"""
    screenshot = "iVBORw0KGgo-small-image-body"
    access, run, delivery, records = await prepared(media_runs, screenshot)
    payload = await access.load_input(run)
    assert screenshot not in payload.instruction
    assert "artifact:" + str(records[0].id) in payload.instruction
    assert '"kind": "screenshot"' in payload.instruction and '"status": "PASSED"' in payload.instruction
    assert "文本 Runtime 未读取图像内容" in payload.instruction
    assert await delivery.artifacts(RunActor(run.created_by, run.workspace_id), run.id) == records


@pytest.mark.anyio
async def test_image_metadata_is_not_available_to_inactive_member(media_runs: RunsHarness) -> None:
    """媒体元数据也属于授权材料，不能因为移除正文就绕过成员校验。"""
    from sqlalchemy import update

    from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel

    access, run, _, _ = await prepared(media_runs, "iVBORw0KGgo-private-image")
    async with media_runs.sessions() as session, session.begin():
        await session.execute(
            update(WorkspaceMembershipModel)
            .where(
                WorkspaceMembershipModel.user_id == run.created_by,
                WorkspaceMembershipModel.workspace_id == run.workspace_id,
            )
            .values(status="inactive")
        )
    with pytest.raises(RuntimeFailure) as error:
        await access.load_input(run)
    assert error.value.code == "ACCESS_REVOKED"
