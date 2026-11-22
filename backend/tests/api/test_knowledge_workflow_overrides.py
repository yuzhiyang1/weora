"""通过实际知识 API 验证覆盖权限、版本冲突和定义校验。"""

from uuid import uuid4

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness, runs
from tests.api.test_task_collaboration import headers, member

__all__ = ["runs"]

pytestmark = pytest.mark.anyio


async def test_workflow_override_is_versioned_and_admin_managed(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/workflows/teamai-share-learnings"
    default = (await runs.client.get(path)).json()
    assert default["revision"] == 0 and default["overridden"] is False
    assert default["definition"]["invoke"] == "ask"

    definition = default["definition"]
    definition["alias"] = "share-notes"
    definition["invoke"] = "deny"
    definition["steps"][1]["approve"] = {"message": "本人确认后发布", "required": True}
    update = {"base_revision": 0, "definition": definition}
    async with member(runs, "知识贡献者") as (teammate, _):
        forbidden = await teammate.put(path, headers=headers(teammate), json=update)
        assert forbidden.status_code == 403, forbidden.text
        created = await runs.client.put(path, headers=runs.headers(), json=update)
        assert created.status_code == 200, created.text
        assert created.json() == {"revision": 1, "overridden": True, "definition": definition}
        readable = await teammate.get(path)
        assert readable.status_code == 200 and readable.json() == created.json()

    replay = await runs.client.put(path, headers=runs.headers(), json=update)
    assert replay.json() == created.json()
    stale_reset = await runs.client.put(path, headers=runs.headers(), json={"base_revision": 0, "definition": None})
    assert stale_reset.status_code == 409, stale_reset.text
    reset = await runs.client.put(path, headers=runs.headers(), json={"base_revision": 1, "definition": None})
    assert reset.status_code == 200, reset.text
    assert reset.json()["revision"] == 2 and reset.json()["overridden"] is False
    assert reset.json()["definition"]["invoke"] == "ask"


async def test_workflow_override_refuses_dependencies_that_bypass_the_draft(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/workflows/teamai-share-learnings"
    definition = (await runs.client.get(path)).json()["definition"]
    definition["steps"][2]["needs"] = []
    bypass = await runs.client.put(
        path, headers=runs.headers(), json={"base_revision": 0, "definition": definition},
    )
    assert bypass.status_code == 422, bypass.text

    definition["steps"][2]["needs"] = ["learning", "approve"]
    definition["steps"][0]["needs"] = ["contribute"]
    cycle = await runs.client.put(path, headers=runs.headers(), json={"base_revision": 0, "definition": definition})
    assert cycle.status_code == 422, cycle.text
    assert (await runs.client.get(path)).json()["revision"] == 0


async def test_agent_launch_and_required_step_publish_only_after_human_approval(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}"
    knowledge = f"{root}/knowledge"
    settings = f"{knowledge}/workflows/teamai-share-learnings"
    definition = (await runs.client.get(settings)).json()["definition"]
    definition["steps"][1]["approve"] = {"message": "请确认草稿", "required": True}
    published_definition = await runs.client.put(
        settings, headers=runs.headers(), json={"base_revision": 0, "definition": definition},
    )
    assert published_definition.status_code == 200, published_definition.text
    scope = {"project_id": str((await project(runs))["id"])}
    _, authorization = await device(runs.client, f"{root}/agent-collaboration", "工作流设备")
    payload = {"operation_id": str(uuid4()), "runtime": "codex", "session": str(uuid4()), "hitl": "auto"}
    unattended = await runs.client.post(
        f"{settings}/runs", headers=authorization, params=scope, json=payload | {"unattended": True},
    )
    assert unattended.status_code == 403, unattended.text
    launched = await runs.client.post(f"{settings}/runs", headers=authorization, params=scope, json=payload)
    assert launched.status_code == 201, launched.text
    run = launched.json()
    assert run["launch"] == "pending" and run["revision"] == 1
    path = f"{knowledge}/workflow-runs/{run['id']}"
    draft = {"items": [{"title": "流水线等待知识审批", "body": "先核对草稿，再发布到当前项目。"}]}
    early = await runs.client.put(f"{path}/drafts", headers=authorization, params=scope, json=draft)
    assert early.status_code == 409, early.text
    decision = {"decision": "approve"}
    no_agent = await runs.client.put(f"{path}/approvals/_launch", headers=authorization, params=scope, json=decision)
    assert no_agent.status_code == 403, no_agent.text
    user = await runs.client.put(f"{path}/approvals/_launch", headers=runs.headers(), params=scope, json=decision)
    assert user.status_code == 200 and user.json()["launch"] == "approved", user.text
    reset = await runs.client.put(
        settings, headers=runs.headers(), json={"base_revision": 1, "definition": None},
    )
    assert reset.status_code == 200 and reset.json()["revision"] == 2, reset.text
    ready = await runs.client.put(f"{path}/drafts", headers=authorization, params=scope, json=draft)
    assert ready.status_code == 200, ready.text
    assert ready.json()["published"] is None and ready.json()["revision"] == 1
    bypass = await runs.client.put(f"{path}/approvals/approve", headers=authorization, params=scope, json=decision)
    assert bypass.status_code == 403, bypass.text
    approved = await runs.client.put(f"{path}/approvals/approve", headers=runs.headers(), params=scope, json=decision)
    assert approved.status_code == 200, approved.text
    entry_id = approved.json()["published"][0]["id"]
    replay = await runs.client.put(f"{path}/approvals/approve", headers=runs.headers(), params=scope, json=decision)
    assert replay.json() == approved.json()
    published = await runs.client.get(f"{knowledge}/{entry_id}", params=scope)
    assert published.status_code == 200 and published.json()["title"] == draft["items"][0]["title"], published.text
    assert (await runs.client.get(f"{knowledge}/{entry_id}")).status_code == 404


@pytest.mark.parametrize("invoke", ["deny", "allow"])
async def test_workflow_invocation_policy_and_rejecting_optional_approval(runs: RunsHarness, invoke: str) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}"
    knowledge = f"{root}/knowledge"
    endpoint = f"{knowledge}/workflows/teamai-share-learnings"
    current = (await runs.client.get(endpoint)).json()["definition"]
    current["invoke"] = invoke
    definition = await runs.client.put(
        endpoint, headers=runs.headers(), json={"base_revision": 0, "definition": current},
    )
    assert definition.status_code == 200, definition.text
    _, authorization = await device(runs.client, f"{root}/agent-collaboration", "可选审批设备")
    payload = {"operation_id": str(uuid4()), "runtime": "codex", "session": str(uuid4())}
    created = await runs.client.post(f"{endpoint}/runs", headers=authorization, json=payload)
    if invoke == "deny":
        assert created.status_code == 403, created.text
        created = await runs.client.post(f"{endpoint}/runs", headers=runs.headers(), json=payload)
        authorization = runs.headers()
    assert created.status_code == 201 and created.json()["launch"] == "approved", created.text
    path = f"{knowledge}/workflow-runs/{created.json()['id']}"
    drafts = {"items": [{"title": "这份草稿会被拒绝", "body": "待审核的经验正文。"}]}
    submitted = await runs.client.put(f"{path}/drafts", headers=authorization, json=drafts)
    assert submitted.status_code == 200 and submitted.json()["published"] is None, submitted.text
    result = await runs.client.put(
        f"{path}/approvals/approve", headers=authorization, json={"decision": "reject", "reason": "草稿未整理完整"},
    )
    assert result.status_code == 200, result.text
    assert {step["id"]: step["state"] for step in result.json()["steps"]} == {
        "learning": "success", "approve": "rejected", "contribute": "skipped", "share-session": "skipped",
    }
    assert (await runs.client.get(knowledge)).json()["items"] == []


async def test_session_archive_is_independent_and_must_match_the_frozen_session(runs: RunsHarness) -> None:
    knowledge = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    headers = runs.headers()
    native = str(uuid4())
    settings = f"{knowledge}/workflows/teamai-share-session"
    child = (await runs.client.get(settings)).json()["definition"]
    child["steps"].insert(0, {"id": "confirm-archive", "approve": {"message": "确认脱敏归档", "required": True}})
    override = await runs.client.put(settings, headers=headers, json={"base_revision": 0, "definition": child})
    assert override.status_code == 200, override.text
    launch = await runs.client.post(
        f"{knowledge}/workflows/teamai-share-learnings/runs", headers=headers,
        json={"operation_id": str(uuid4()), "runtime": "codex", "session": native, "publish": True},
    )
    assert launch.status_code == 201, launch.text
    assert launch.json()["session_workflow_revision"] == 1
    path = f"{knowledge}/workflow-runs/{launch.json()['id']}"
    skipped = await runs.client.put(f"{path}/drafts", headers=headers, json={"items": []})
    assert {step["id"]: step["state"] for step in skipped.json()["steps"]} == {
        "learning": "skipped", "approve": "skipped", "contribute": "skipped",
        "session-1": "ready", "session-2": "waiting",
    }
    reset = await runs.client.put(settings, headers=headers, json={"base_revision": 1, "definition": None})
    assert reset.status_code == 200
    approved = await runs.client.put(f"{path}/approvals/session-1", headers=headers, json={"decision": "approve"})
    assert approved.status_code == 200 and approved.json()["session_workflow_revision"] == 1, approved.text
    transcript = {
        "runtime": "codex", "session": native, "title": "已脱敏的会话",
        "turns": [{"role": "user", "text": "检查发布状态。"}, {"role": "assistant", "text": "已确认。"}],
    }
    mismatch = await runs.client.post(
        f"{knowledge}/shared-sessions", headers=headers, json=transcript | {"session": str(uuid4())},
    )
    assert mismatch.status_code == 201, mismatch.text
    invalid = await runs.client.put(
        f"{path}/session", headers=headers, json={"archive_id": mismatch.json()["id"], "revision": 1},
    )
    assert invalid.status_code == 409, invalid.text
    archive = await runs.client.post(f"{knowledge}/shared-sessions", headers=headers, json=transcript)
    assert archive.status_code == 201, archive.text
    receipt = {"archive_id": archive.json()["id"], "revision": 1}
    done = await runs.client.put(f"{path}/session", headers=headers, json=receipt)
    assert done.status_code == 200, done.text
    assert done.json()["session_result"] == receipt and done.json()["published"] is None
    replay = await runs.client.put(f"{path}/session", headers=headers, json=receipt)
    assert replay.json() == done.json()
