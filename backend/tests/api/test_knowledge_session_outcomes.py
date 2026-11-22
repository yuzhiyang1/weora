"""产出结算覆盖闲置、同项目作者关联、快照冻结及并发重放。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member
from weora.modules.knowledge.session_keys import score_source_key

pytestmark = pytest.mark.anyio


async def test_session_outcomes_are_frozen_scoped_and_multi_category(runs: RunsHarness) -> None:
    await exercise_outcomes(runs)


async def exercise_outcomes(runs: RunsHarness, *, postgres: bool = False) -> None:
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "另一产出项目"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    activity, route, digest = f"{root}/activity/sessions", f"{root}/activity/session-outcomes", f"{root}/digest"
    collab = f"/api/v1/workspaces/{runs.workspace_id}/agent-collaboration"
    _, token = await device(runs.client, collab, "产出上报设备")
    native, other_native = uuid4(), uuid4()

    async def learning(
        session: str, params: dict[str, str], credential: dict[str, str] = token,
    ) -> None:
        result = await runs.client.post(f"{root}/imports", params=params, headers=credential, json={"items": [{
            "operation_id": str(uuid4()), "title": "会话复盘", "body": "内部会话经验正文",
            "source": {"type": "session", "ref": f"codex:{session}"},
        }]})
        assert result.status_code == 201, result.text

    await learning(str(native), scope)
    await learning(str(native), scope)
    await learning(str(other_native), other)
    async with member(runs, "同项目其他经验作者") as (teammate, _):
        _, teammate_token = await device(teammate, collab, "另一台产出设备")
        await learning(str(other_native), scope, teammate_token)
    now = datetime.now(UTC)
    stale = now - timedelta(hours=7)
    key = score_source_key("codex", native)
    usage = {
        "runtime": "codex", "subject_key": key, "run_mode": "attended", "observed_at": now.isoformat(),
        "interrupts": 1, "tool_rejections": 1, "corrections": 1,
        "tokens": {"input": 20, "output": 14, "cache_read": 9, "cache_creation": 3},
    }
    outcome = {
        "runtime": "codex", "subject_key": key, "run_mode": "attended", "stopped_at": stale.isoformat(),
        "has_commit": True, "has_pr": True,
    }
    missing = await runs.client.put(route, params=scope, headers=token, json=outcome)
    assert missing.status_code == 409

    async def write(data: dict[str, object]) -> None:
        response = await runs.client.put(activity, params=scope, headers=token, json=data)
        assert response.status_code == 200, response.text

    async def finalize(data: dict[str, object]) -> str:
        response = await runs.client.put(route, params=scope, headers=token, json=data)
        assert response.status_code == 200, response.text
        return str(response.json()["status"])

    await write(usage | {"observed_at": stale.isoformat()})
    if postgres:
        assert sorted(await asyncio.gather(finalize(outcome), finalize(outcome))) == ["already_recorded", "recorded"]
    else:
        assert await finalize(outcome) == "recorded"
    await write(usage | {"interrupts": 9})
    assert await finalize(outcome | {"has_commit": False, "has_pr": False}) == "already_recorded"

    extra = score_source_key("codex", other_native)
    await write(usage | {"subject_key": extra, "run_mode": "unattended", "tokens": None})
    assert (await runs.client.put(route, params=scope, headers=token, json=outcome | {
        "subject_key": extra, "stopped_at": now.isoformat(),
    })).status_code == 409

    fresh = score_source_key("codex", uuid4())
    await write(usage | {"subject_key": fresh})
    too_soon = await runs.client.put(route, params=scope, headers=token, json=outcome | {
        "subject_key": fresh, "stopped_at": now.isoformat(),
    })
    assert too_soon.status_code == 409 and too_soon.json()["detail"]["code"] == "KNOWLEDGE_OUTCOME_NOT_FINISHED"
    await finalize(outcome | {
        "subject_key": extra, "run_mode": "unattended", "stopped_at": now.isoformat(),
        "has_commit": False, "has_pr": False,
    })
    async with member(runs, "产出统计观察者") as (teammate, _):
        result = await teammate.get(digest, params=scope)
        assert result.status_code == 200, result.text
        assert key not in result.text and str(native) not in result.text
        expected = [
            {"run_mode": "attended", "outcome": kind, "sessions": 1, "token_sessions": 1,
             "tokens_per_session": 34.0, "interventions_per_session": 3.0}
            for kind in ("pr", "commit", "learning")
        ]
        expected.append(
            {"run_mode": "unattended", "outcome": "none", "sessions": 1, "token_sessions": 0,
             "tokens_per_session": None, "interventions_per_session": 3.0},
        )
        assert result.json()["outcomes"] == expected
        assert (await teammate.get(digest, params=other)).json()["outcomes"] == []
        assert (await teammate.get(digest)).json()["outcomes"] == []
