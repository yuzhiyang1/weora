"""验证旧设备兼容、会话用量重放、项目可见性及各模式汇总。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member

pytestmark = pytest.mark.anyio


async def test_conversation_digest_reports_only_scoped_measured_usage(runs: RunsHarness) -> None:
    await exercise_conversation(runs)


async def exercise_conversation(runs: RunsHarness, *, postgres: bool = False) -> None:
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "会话用量隔离"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    collab = f"/api/v1/workspaces/{runs.workspace_id}/agent-collaboration"
    _, token = await device(runs.client, collab, "会话用量设备")
    route, digest = f"{root}/activity/sessions", f"{root}/digest"
    now = datetime.now(UTC)
    base = {
        "runtime": "codex", "subject_key": "a" * 64, "run_mode": "attended",
        "interrupts": 0, "tool_rejections": 0, "corrections": 0,
        "observed_at": (now - timedelta(minutes=2)).isoformat(),
    }

    async def write(values: dict[str, object]) -> None:
        response = await runs.client.put(route, params=scope, headers=token, json=values)
        assert response.status_code == 200, response.text

    await write(base)
    assert (await runs.client.get(digest, params=scope)).json()["conversation"] is None
    await write(base | {"prompts": 2})
    partial = (await runs.client.get(digest, params=scope)).json()["conversation"]
    assert partial["prompts"] == 2 and partial["total_tokens"] is None and partial["token_sessions"] == 0
    meter = {"input": 40, "output": 15, "cache_read": 8, "cache_creation": 2}
    grown = base | {
        "prompts": 5, "tokens": meter, "run_mode": "unattended",
        "observed_at": (now - timedelta(minutes=1)).isoformat(),
    }
    stale = base | {"prompts": 3, "tokens": dict.fromkeys(meter, 1)}
    if postgres:
        await asyncio.gather(write(grown), write(stale))
    else:
        await write(grown)
        await write(stale)
    await write(base)
    # 更新后的线程快照替换四个桶。乱序重放的旧大值不能混入新快照。
    corrected = grown | {"tokens": meter | {"input": 35, "cache_read": 10}, "observed_at": now.isoformat()}
    await write(corrected)
    await write(grown)
    await write(base | {"subject_key": "b" * 64, "prompts": 1, "tokens": dict.fromkeys(meter, 3)})
    await write(base | {"subject_key": "c" * 64, "prompts": 4, "run_mode": "not_recorded"})
    rejected = await runs.client.put(route, params=scope, headers=token, json=grown | {
        "tokens": meter | {"input": False, "transcript": "unrecognized"},
    })
    assert rejected.status_code == 422
    async with member(runs, "会话用量汇总读取者") as (teammate, _):
        report = await teammate.get(digest, params=scope)
        assert report.status_code == 200 and str(base["subject_key"]) not in report.text
        result = report.json()["conversation"]
        assert result == {
            "prompts": 10, "prompts_by_run_mode": {"attended": 1, "unattended": 5, "not_recorded": 4},
            "token_sessions": 2, "total_tokens": 74,
            "tokens": {"input": 38, "output": 18, "cache_read": 13, "cache_creation": 5},
            "tokens_by_run_mode": {"attended": 12, "unattended": 62, "not_recorded": 0},
        }
        assert (await teammate.get(digest, params=other)).json()["conversation"] is None
        assert (await teammate.get(digest)).json()["conversation"] is None
