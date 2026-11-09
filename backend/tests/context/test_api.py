"""HTTP 契约测试；认证依赖单独覆盖，业务主体只由认证上下文生成。"""

import asyncio
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.context.test_context import setup
from weora.modules.context.api import router
from weora.modules.context.domain import ContextActor, ContextRunScope, Json, MessagePage
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord


def test_context_management_and_snapshot_http_contracts() -> None:
    async def scenario() -> None:
        f = await setup()
        principal = PrincipalRecord(
            f.actor.user_id, "管理员", "admin@example.com", f.actor.workspace_id, "研发", "owner",
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.state.context_service = f.service

        async def scope(actor: ContextActor, run_id: UUID) -> ContextRunScope:
            return replace(f.scope, actor=actor, run_id=run_id)

        async def identity() -> PrincipalRecord:
            return principal

        app.state.context_scope_factory = scope
        app.dependency_overrides[current_principal] = identity
        app.dependency_overrides[write_principal] = identity
        path = f"/api/v1/workspaces/{f.actor.workspace_id}/context"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            connections = await client.get(f"{path}/connections")
            assert connections.status_code == 200 and "secret" not in connections.text
            denied_workspace = await client.get(f"/api/v1/workspaces/{uuid4()}/context/connections")
            assert denied_workspace.status_code == 404
            source_list = await client.get(f"{path}/sources")
            assert source_list.status_code == 200 and len(source_list.json()["items"]) == 1
            source_update = await client.post(f"{path}/sources", json={
                "connection_id": str(f.connection), "source_id": str(f.source),
                "chat_id": "oc_project", "name": "新名称", "enabled": True,
            })
            assert source_update.status_code == 200
            f.reader.pages[None] = MessagePage((f.message(1),))
            read = await client.post(f"{path}/runs/{f.scope.run_id}/messages", json={"source_id": str(f.source)})
            assert read.status_code == 200 and read.json()["status"] == "complete"
            snapshot_id = read.json()["snapshot_id"]
            preview = await client.get(f"{path}/runs/{f.scope.run_id}/snapshots/{snapshot_id}")
            assert preview.status_code == 200 and preview.json()["messages"][0]["author_id"] == "ou_external_author"
            snapshots = await client.get(f"{path}/runs/{f.scope.run_id}/snapshots")
            assert snapshots.status_code == 200 and len(snapshots.json()["items"]) == 1
            channel = await client.get(f"{path}/runs/{f.scope.run_id}/channel-status")
            assert channel.status_code == 200 and channel.json() == {"inbox": None, "deliveries": []}
            denied_channel = await client.get(
                f"/api/v1/workspaces/{uuid4()}/context/runs/{f.scope.run_id}/channel-status",
            )
            assert denied_channel.status_code == 404
            await client.put(f"{path}/sources/{f.source}/grants/{f.actor.user_id}", json={"enabled": False})
            preview = await client.get(f"{path}/runs/{f.scope.run_id}/snapshots/{snapshot_id}")
            assert preview.status_code == 403
            assert "登录方案" not in preview.text
    asyncio.run(scenario())


def test_unconfigured_context_reports_503_instead_of_fake_success() -> None:
    async def scenario() -> None:
        actor = ContextActor(uuid4(), uuid4())
        principal = PrincipalRecord(actor.user_id, "管理员", "admin@example.com", actor.workspace_id, "研发", "owner")
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        async def identity() -> PrincipalRecord:
            return principal

        app.dependency_overrides[current_principal] = identity
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/workspaces/{actor.workspace_id}/context/connections")
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "CONTEXT_NOT_CONFIGURED"
    asyncio.run(scenario())


def test_readonly_user_cannot_manage_connections_or_grants() -> None:
    async def scenario() -> None:
        f = await setup()
        f.access.admin = False
        principal = PrincipalRecord(
            f.actor.user_id, "成员", "member@example.com", f.actor.workspace_id, "研发", "member",
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.state.context_service = f.service

        async def identity() -> PrincipalRecord:
            return principal

        app.dependency_overrides[current_principal] = identity
        app.dependency_overrides[write_principal] = identity
        path = f"/api/v1/workspaces/{f.actor.workspace_id}/context"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for method, suffix, body in [
                ("GET", "/connections", None),
                ("GET", "/sources?managed=true", None),
                ("PUT", f"/sources/{f.source}/grants/{f.actor.user_id}", {"enabled": True}),
                ("POST", "/bindings", {"connection_id": str(f.connection), "open_id": "ou_user",
                                       "user_id": str(f.actor.user_id)}),
            ]:
                response = await client.request(method, path + suffix, json=body)
                assert response.status_code == 403
            allowed = await client.get(path + "/sources")
            assert allowed.status_code == 200
            assert cast(list[Json], allowed.json()["items"])[0]["id"] == str(f.source)
    asyncio.run(scenario())
