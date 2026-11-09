"""群入口项目授权集成测试：真实应用/数据库，飞书仅使用明确的运输替身。"""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from tests.api.test_projects import project, repository
from tests.api.test_runs import runs as runs
from tests.context.test_context import inbound
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from weora.bootstrap.worker import _pump_context
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextError
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunProjectSelectionError


async def selection(h: ContextRuns) -> dict[str, object]:
    """项目资料来自真实应用 API，不把 Git 密码塞进路由或模型输入。"""
    p = await project(h.original)
    repo = await repository(h.original, p["id"], "api")
    return {
        "source_id": str(h.source_id), "agent_id": h.original.agent_id, "agent_version": "0.1.0",
        "project_id": p["id"], "repository_ids": [repo["id"]], "share_project_with_group": True,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid", ["no_confirmation", "no_repository", "duplicate", "foreign_repository", "no_project"]
)
async def test_project_route_rejects_ambiguous_or_unauthorized_selection(contextual: ContextRuns, invalid: str) -> None:
    h = contextual
    payload = await selection(h)
    if invalid == "no_confirmation":
        payload["share_project_with_group"] = False
    elif invalid == "no_repository":
        payload["repository_ids"] = []
    elif invalid == "duplicate":
        payload["repository_ids"] = payload["repository_ids"] * 2
    elif invalid == "foreign_repository":
        payload["repository_ids"] = [str(uuid4())]
    else:
        payload["project_id"] = None
    response = await h.client.post(h.base + "/context/routes", json=payload, headers=h.headers)
    assert response.status_code == 422, response.text
    assert not await h.context.store.list(h.actor.workspace_id, "route")


@pytest.mark.anyio
async def test_group_project_freezes_selection_and_route_replacement_revokes_all_derived_access(
    contextual: ContextRuns,
) -> None:
    h = contextual
    payload = await selection(h)
    saved = await h.client.post(h.base + "/context/routes", json=payload, headers=h.headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["project_granted_by"] == h.original.user_id
    channels = FeishuChannels(h.context)
    await h.context.probe(h.context_actor, h.connection_id)
    await h.context.bind_identity(h.context_actor, h.connection_id, "ou_user", h.actor.user_id)
    await channels.receive(h.actor.workspace_id, h.connection_id, inbound())
    await _pump_context(h.app)
    inbox = (await h.context.store.list(h.actor.workspace_id, "inbox"))[0]
    assert inbox["status"] == "SUBMITTED"
    run_id = UUID(str(inbox["run_id"]))
    service: RunService = h.app.state.run_service
    detail = await service.get(actor=h.actor, workspace_id=h.actor.workspace_id, run_id=run_id)
    assert detail.task.audience == "group"
    snapshot = detail.task.project_context
    assert snapshot and snapshot["project_id"] == payload["project_id"]
    assert [r["id"] for r in snapshot["repositories"]] == payload["repository_ids"]
    frozen = deepcopy(inbox["frozen_route"])
    assert (await h.client.get(h.base + f"/runs/{run_id}")).status_code == 200
    # 同事件重投不新建任务，也不把路由配置当成新的写入批准。
    await channels.receive(h.actor.workspace_id, h.connection_id, inbound())
    await _pump_context(h.app)
    assert len(await h.context.store.list(h.actor.workspace_id, "inbox")) == 1
    assert not (await h.client.get(h.base + f"/runs/{run_id}/plan")).json().get("plan")
    replaced = await h.client.post(h.base + "/context/routes", json={
        "source_id": str(h.source_id), "agent_id": h.original.agent_id, "agent_version": "0.1.0",
    }, headers=h.headers)
    assert replaced.status_code == 200
    for suffix in ("", "/tools", "/artifacts", "/plan", "/deliveries"):
        assert (await h.client.get(h.base + f"/runs/{run_id}{suffix}")).status_code == 404
    scope = await h.app.state.context_scope_factory(h.context_actor, run_id)
    with pytest.raises(ContextError):
        await h.context.authorize_derived(scope)
    current = (await h.context.store.list(h.actor.workspace_id, "inbox"))[0]
    assert current["frozen_route"] == frozen
    assert (await service.access_metadata(h.actor, run_id)).task.project_context == snapshot


@pytest.mark.anyio
async def test_message_cannot_forge_group_project_selection_without_administrator_route(
    contextual: ContextRuns,
) -> None:
    h = contextual
    payload = await selection(h)
    service: RunService = h.app.state.run_service
    with pytest.raises(RunProjectSelectionError):
        await service.create(
            actor=h.actor, workspace_id=h.actor.workspace_id, agent_id=UUID(h.original.agent_id),
            agent_version="0.1.0", instruction="不能凭项目 ID 把私有上下文发到群里", idempotency_key="forged",
            request_id="forged", origin_metadata={"source_id": str(h.source_id)},
            project_id=UUID(payload["project_id"]), repository_ids=tuple(UUID(x) for x in payload["repository_ids"]),
        )
