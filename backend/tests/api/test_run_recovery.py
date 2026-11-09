"""中断只读恢复、等待取消与安全错误响应。"""

from uuid import UUID

import pytest
from sqlalchemy import select, update

from tests.api.test_runs import RunsHarness
from weora.modules.runs.infrastructure.models import RunEventModel, RunModel

pytest_plugins = ["tests.api.test_runs"]


@pytest.mark.anyio
async def test_recovery_is_explicit_idempotent_and_never_revives_terminal(runs: RunsHarness) -> None:
    first = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    run_id = UUID(first.json()["id"])
    async with runs.sessions() as session, session.begin():
        await session.execute(update(RunModel).where(RunModel.id == run_id).values(status="RECOVERY_REQUIRED"))
    response = await runs.client.post(
        f"{runs.path}/{run_id}/recover", json={"mode": "read_only"}, headers=runs.headers()
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "QUEUED"
    assert response.json()["recovery_read_only"] is True
    replay = await runs.client.post(f"{runs.path}/{run_id}/recover", json={"mode": "read_only"}, headers=runs.headers())
    assert replay.status_code == 200
    async with runs.sessions() as session:
        events = list(await session.scalars(select(RunEventModel).where(RunEventModel.run_id == run_id)))
        assert len(events) == 2
    cancelled = await runs.client.post(f"{runs.path}/{run_id}/cancel", headers=runs.headers())
    assert cancelled.json()["status"] == "CANCELLED"
    terminal = await runs.client.post(
        f"{runs.path}/{run_id}/recover", json={"mode": "read_only"}, headers=runs.headers()
    )
    assert terminal.status_code == 409


@pytest.mark.anyio
async def test_waiting_runs_can_stop_and_validation_does_not_echo_secrets(runs: RunsHarness) -> None:
    first = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    run_id = UUID(first.json()["id"])
    async with runs.sessions() as session, session.begin():
        await session.execute(update(RunModel).where(RunModel.id == run_id).values(status="WAITING_APPROVAL"))
    stopped = await runs.client.post(f"{runs.path}/{run_id}/cancel", headers=runs.headers())
    assert stopped.status_code == 200 and stopped.json()["status"] == "CANCELLED"
    invalid = await runs.client.post(
        f"/api/v1/workspaces/{runs.workspace_id}/context/connections",
        json={"app_id": "x", "name": "", "secret": "private-secret-never-return"},
        headers=runs.headers(),
    )
    assert invalid.status_code == 422
    assert "private-secret-never-return" not in invalid.text
    assert '"input"' not in invalid.text
