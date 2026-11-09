"""真实 SQL/API/工具网关验证凭据隔离、撤销和明文不进入运行轨迹。"""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from weora.modules.computers.credentials import ComputerCredentialModel, ComputerCredentials
from weora.modules.computers.openbot import ComputerError, OpenBot
from weora.modules.computers.tools import register_computer_tools
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.module_access import GatewayModuleAccess
from weora.modules.tools.application.gateway import ToolGateway, ToolScope


@pytest.mark.anyio
async def test_credentials_are_encrypted_scoped_expiring_and_revocable(contextual: ContextRuns) -> None:
    h = contextual
    scope_id = "a" * 64
    h.app.state.settings = h.app.state.settings.model_copy(
        update={
            "computer_browser_origins": ("https://fixture.example",),
            "computer_feishu_scopes": {scope_id: "test"},
        }
    )
    path = h.base + "/computer-credentials"
    payload = {
        "origin": "https://fixture.example",
        "scope_id": scope_id,
        "label": "登录测试",
        "username": "test-user",
        "password": "private-test-password",
    }
    assert (await h.client.post(path, json=payload)).status_code == 403
    created = await h.client.post(path, json=payload, headers=h.headers)
    assert created.status_code == 201, created.text
    identity = UUID(created.json()["id"])
    credentials = cast(ComputerCredentials, h.app.state.computer_credentials)
    listed = await h.client.get(path, params={"scope_id": scope_id})
    assert str(identity) in listed.text and "test-user" not in listed.text and "password" not in listed.text
    async with h.original.sessions() as session:
        row = await session.scalar(select(ComputerCredentialModel).where(ComputerCredentialModel.id == identity))
        assert row and payload["password"] not in row.ciphertext and payload["username"] not in row.ciphertext
    assert await credentials.value(h.actor, scope_id, identity, payload["origin"], "password") == payload["password"]
    for actor, audience, origin in (
        (RunActor(uuid4(), h.actor.workspace_id), scope_id, payload["origin"]),
        (RunActor(h.actor.user_id, uuid4()), scope_id, payload["origin"]),
        (h.actor, "b" * 64, payload["origin"]),
        (h.actor, scope_id, "https://evil.example"),
    ):
        with pytest.raises(ComputerError):
            await credentials.value(actor, audience, identity, origin, "password")
    async with h.original.sessions.begin() as session:
        await session.execute(
            update(ComputerCredentialModel)
            .where(ComputerCredentialModel.id == identity)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await credentials.available(h.actor, scope_id) == []
    with pytest.raises(ComputerError):
        await credentials.value(h.actor, scope_id, identity, payload["origin"], "password")
    assert (await h.client.delete(path + f"/{identity}", headers=h.headers)).status_code == 204


@pytest.mark.anyio
async def test_credential_tool_never_records_plaintext_and_refuses_other_origin(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = contextual
    created = await h.client.post(
        h.base + "/runs", json=h.original.payload(), headers={**h.headers, "Idempotency-Key": uuid4().hex}
    )
    assert created.status_code == 201, created.text
    run_id = UUID(created.json()["id"])
    execution = cast(SqlAlchemyExecutionStore, h.app.state.execution_store)
    lease = await execution.claim(
        workspace_id=h.actor.workspace_id, run_id=run_id, owner="secret-test", lease_seconds=30
    )
    assert lease
    credentials = cast(ComputerCredentials, h.app.state.computer_credentials)
    identity = await credentials.create(
        h.actor, origin="https://fixture.example", scope_id="", label="测试", username="user", password="never-in-model"
    )
    bot = OpenBot("http://supervisor", "supervisor", "master", "weora")
    monkeypatch.setattr(bot, "state", AsyncMock(return_value="running"))
    location = "https://fixture.example/login"
    fills: list[dict[str, object]] = []

    async def call(
        computer: str, path: str, body: dict[str, object] | None = None, **kwargs: object
    ) -> dict[str, object]:
        if path == "/control":
            return {"holder": "bot"}
        if path == "/read":
            return {"url": location}
        if path == "/control/request":
            assert body == {"reason": "需要人工验证码"}
            return {"request": {"id": "take-test", "status": "waiting"}}
        assert path == "/credential" and body is not None and body["text"] == "never-in-model"
        fills.append(body)
        return {"filled": True}

    monkeypatch.setattr(bot, "call", call)
    access = GatewayModuleAccess(h.app.state.context_access, execution, h.context)
    gateway = ToolGateway(h.app.state.tool_store, access)
    register_computer_tools(
        gateway,
        access,
        cast(RunService, h.app.state.run_service),
        bot,
        ("https://fixture.example",),
        {},
        cast(DeliveryService, h.app.state.delivery_service),
        credentials,
    )
    scope = ToolScope(run_id, h.actor, lease.owner, lease.fencing_token)
    arguments = {"credential_id": str(identity), "ref": "e1", "snapshotId": 1, "field": "password"}
    assert (await gateway.invoke(scope, "fill", "browser.fill_credential", arguments))["ok"] is True
    assert len(fills) == 1
    location = "https://evil.example/login"
    assert (await gateway.invoke(scope, "other", "browser.fill_credential", arguments))["ok"] is False
    assert len(fills) == 1
    help_result = await gateway.invoke(scope, "human", "browser.request_human", {"reason": "需要人工验证码"})
    assert help_result["ok"] is True
    assert cast(dict[str, object], help_result["data"])["console_path"] == f"/runs/{run_id}"
    # 观察验收只核对本任务的真实产物，不借用代码交付审批工具。
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7uoAAAAASUVORK5CYII="
    delivery = cast(DeliveryService, h.app.state.delivery_service)
    artifact = await delivery.record_computer_screenshot(h.actor, run_id, png)
    checked = await gateway.invoke(
        scope, "validate", "browser.validate_evidence", {"artifact_ids": [str(artifact.id)]}
    )
    assert checked["ok"] is True and cast(dict[str, object], checked["data"])["validated"] is True
    rejected = await gateway.invoke(scope, "invalid", "browser.validate_evidence", {"artifact_ids": [str(uuid4())]})
    assert rejected["ok"] is False and cast(dict[str, object], rejected["error"])["code"] == "SCREENSHOT_UNAVAILABLE"
    calls = await h.app.state.tool_store.list_calls(scope)
    assert "never-in-model" not in json.dumps([asdict(item) for item in calls], default=str)
