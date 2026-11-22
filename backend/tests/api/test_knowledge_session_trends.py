"""验证 UTC 窗口边界、跨周重放和按请求日结算成本。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member

pytestmark = pytest.mark.anyio


async def test_session_trends_keep_activity_and_cost_on_their_recorded_days(runs: RunsHarness) -> None:
    await exercise_trends(runs)


async def exercise_trends(runs: RunsHarness, *, postgres: bool = False) -> None:
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "趋势隔离项目"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    col = f"/api/v1/workspaces/{runs.workspace_id}/agent-collaboration"
    _, token = await device(runs.client, col, "趋势采集设备")
    route, digest = f"{root}/activity/sessions", f"{root}/digest"
    now = datetime.now(UTC)

    def day(offset: int) -> str:
        return (now.date() - timedelta(days=offset)).isoformat()

    def cost(offset: int, count: int, micros: int) -> dict[str, object]:
        return {"day": day(offset), "priced_requests": count, "cost_micros": micros}

    data = {
        "runtime": "codex", "subject_key": "a" * 64, "run_mode": "attended",
        "observed_at": (now - timedelta(days=6)).isoformat(), "interrupts": 0,
        "tool_rejections": 2, "corrections": 0, "prompts": 4,
        "tokens": {"input": 20, "output": 9, "cache_read": 10, "cache_creation": 5},
    }
    initial = {"first_stop_day": day(6), "active_ms": 60000, "has_error": False,
               "request_days": [cost(7, 1, 100)]}

    async def send(payload: dict[str, object]) -> None:
        response = await runs.client.put(route, params=scope, headers=token, json=payload)
        assert response.status_code == 200, response.text

    await send(data | {"trend": initial})
    snapshot = (await runs.client.get(digest, params=scope)).json()["session_trends"]
    assert snapshot["current"]["success_rate"] == 1.0
    assert snapshot["previous"]["sessions_ended"] == 0
    assert snapshot["previous"]["avg_request_cost_micros"] == 100.0
    newer = data | {"observed_at": now.isoformat(), "prompts": 6, "corrections": 1, "trend": initial | {
        "first_stop_day": day(0), "active_ms": 90000, "request_days": [cost(0, 2, 500), cost(7, 1, 100)],
    }}
    delayed = data | {"trend": initial | {"request_days": [cost(0, 1, 300), cost(7, 1, 100)]}}
    if postgres:
        await asyncio.gather(send(newer), send(delayed))
    else:
        await send(newer)
        await send(delayed)
    failed = data | {"subject_key": "b" * 64, "prompts": 2, "tokens": None}
    prior = {"first_stop_day": day(13), "active_ms": 30000, "has_error": True, "request_days": [cost(7, 1, 100)]}
    await send(failed | {"trend": prior})
    await send(failed | {"observed_at": now.isoformat(), "trend": prior | {"has_error": False}})
    await send(data | {"subject_key": "c" * 64, "trend": initial | {
        "first_stop_day": day(14), "request_days": [cost(14, 10, 90000)],
    }})
    await send(data | {"subject_key": "d" * 64, "prompts": 77})
    invalid = await runs.client.put(route, params=scope, headers=token, json=data | {
        "subject_key": "e" * 64, "trend": initial | {"request_days": [cost(1, 1, 100), cost(1, 2, 200)]},
    })
    assert invalid.status_code == 422, invalid.text
    async with member(runs, "趋势周报查看者") as (teammate, _):
        response = await teammate.get(digest, params=scope)
        assert response.status_code == 200, response.text
        assert (result := response.json()["session_trends"]) == {
            "current": {
                "from_day": day(6), "through_day": day(0), "sessions_ended": 1, "success_rate": 0.0,
                "avg_prompts": 6.0, "avg_duration_ms": 90000.0, "avg_request_cost_micros": 250.0,
                "cache_read_share": pytest.approx(10 / 35), "correction_rate": 1.0, "priced_requests": 2,
            },
            "previous": {
                "from_day": day(13), "through_day": day(7), "sessions_ended": 1, "success_rate": 0.0,
                "avg_prompts": 2.0, "avg_duration_ms": 30000.0, "avg_request_cost_micros": 100.0,
                "cache_read_share": None, "correction_rate": 0.0, "priced_requests": 2,
            },
        }
        assert result["current"]["priced_requests"] == 2
        assert "a" * 64 not in response.text
        unseen = (await teammate.get(digest, params=other)).json()["session_trends"]
        assert unseen["current"]["sessions_ended"] == unseen["previous"]["priced_requests"] == 0
