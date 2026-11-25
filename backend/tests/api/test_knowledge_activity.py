"""验证人工干预累计值、作用域与工作空间上报开关。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member

pytestmark = pytest.mark.anyio


async def test_digest_counts_observed_root_sessions_once(runs: RunsHarness) -> None:
    await exercise_activity(runs)


async def exercise_activity(runs: RunsHarness, *, postgres: bool = False) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    project_id = (await project(runs))["id"]
    scope = {"project_id": str(project_id)}
    other = {"project_id": str((await project(runs, "活动数据隔离"))["id"])}
    route, digest = f"{root}/activity/sessions", f"{root}/digest"
    now = datetime.now(UTC)
    record = {
        "runtime": "codex", "subject_key": "a" * 64, "run_mode": "attended",
        "interrupts": 2, "tool_rejections": 0, "corrections": 1,
        "observed_at": (now - timedelta(minutes=2)).isoformat(),
    }
    growth = record | {"tool_rejections": 3, "run_mode": "unattended",
                       "observed_at": (now - timedelta(minutes=1)).isoformat()}
    headers = runs.headers()
    collaboration = f"/api/v1/workspaces/{runs.workspace_id}/agent-collaboration"
    device_id, credential = await device(runs.client, collaboration, "上报测试设备")

    async def put(payload: dict[str, object]) -> None:
        response = await runs.client.put(route, params=scope, headers=credential, json=payload)
        assert response.status_code == 200, response.text

    assert (await runs.client.get(digest, params=scope)).json()["interventions"] is None
    if postgres:
        await asyncio.gather(put(record), put(growth))
    else:
        await put(growth)
    await put(record)
    # 零干预仍是一份有记录的独立根会话，旧窗口或缺少模式不会放大均值。
    empty = record | {"subject_key": "b" * 64, "interrupts": 0, "corrections": 0, "run_mode": "not_recorded"}
    await put(empty)
    rejected = await runs.client.put(route, params=scope, headers=headers,
                                     json=record | {"observed_at": (now + timedelta(minutes=5)).isoformat()})
    assert rejected.status_code == 422, rejected.text
    async with member(runs, "活动统计成员") as (teammate, _):
        response = await teammate.get(digest, params=scope)
        assert response.status_code == 200, response.text
        assert response.json()["interventions"] == {
            "sessions": 2, "total": 6, "avg_per_session": 3.0, "interrupts": 2, "tool_rejections": 3,
            "corrections": 1, "by_run_mode": {"attended": 0, "unattended": 1, "not_recorded": 1},
        }
        assert record["subject_key"] not in response.text
        assert (await teammate.get(digest, params=other)).json()["interventions"] is None
        assert (await teammate.get(digest)).json()["interventions"] is None
    policy = f"{root}/settings/usage-report"
    assert (await runs.client.get(policy)).json() == {"enabled": True, "source": "default"}
    turned_off = await runs.client.put(policy, headers=headers, json={"enabled": False})
    assert turned_off.status_code == 200, turned_off.text
    denied = await runs.client.put(route, params=scope, headers=credential, json=record)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "KNOWLEDGE_USAGE_DISABLED"
    assert (await runs.client.get(digest, params=scope)).json()["interventions"]["sessions"] == 2
    revoked = await runs.client.delete(f"{collaboration}/devices/{device_id}", headers=headers)
    assert revoked.status_code == 204, revoked.text
    stopped = await runs.client.put(route, params=scope, headers=credential, json=record)
    assert stopped.status_code == 401, stopped.text
