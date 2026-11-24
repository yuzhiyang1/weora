"""公开接口验证 TeamAI 工作流评分导入、分组与评分口径。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member

pytestmark = pytest.mark.anyio


def workflow_sample(run: str, now: datetime, **changes: object) -> dict[str, object]:
    return {
        "source": "workflow", "subject": {"runId": run, "step": "ship", "pass": 2}, "runMode": "attended",
        "scorer": "delivery-review", "rubric": {"sourceHash": "sha256:" + "d" * 64, "teamRevision": "a" * 40},
        "scoredAt": now.isoformat(), "label": "failed", "score": 0, "passed": False, "decidedBy": "judge",
        "judge": {"kind": "model", "tool": "codex", "model": {"reported": "review-model"}, "access": "read-only"},
    } | changes


def workflow_samples(now: datetime) -> list[dict[str, object]]:
    latest = now - timedelta(minutes=1)
    earlier = latest - timedelta(minutes=1)
    agent = {
        "v": 1, "tool": {"name": "codex"}, "model": {"reported": "task-model"}, "definition": {"name": "shipper"},
        "team": {"revision": "b" * 40, "localChanges": False, "selection": {
            "scope": "project", "stepRole": "release", "primaryRole": "dev", "additionalRoles": [],
            "projects": ["api", "web"], "subscribedTags": ["prod", "quality"],
        }},
        "fingerprint": "f" * 64,
    }
    return [
        workflow_sample("r-rescored", earlier),
        workflow_sample("r-rescored", latest, label="pass", score=1, passed=True, agent=agent,
                        used={"skills": ["checks", "shipping", "checks"]}, reasoning="NEVER STORE THIS"),
        workflow_sample("r-ci", earlier, decidedBy="ci", agent=agent, used={"skills": ["checks", "shipping"]}),
        workflow_sample("r-check", earlier, decidedBy="check", judge={"kind": "stage"}, used={"skills": []}),
        workflow_sample("r-protected", earlier, decidedBy="protected", judge={"kind": "stage"}),
        workflow_sample("r-legacy", earlier, decidedBy=None, agent=agent | {"model": {"requested": "task-model"}}),
        workflow_sample("r-human", earlier, decidedBy=None, judge={"kind": "human"}),
        workflow_sample("r-other-judge", earlier, judge={
            "kind": "model", "tool": "codex", "model": {"requested": "review-model"},
        }),
        workflow_sample("r-other-mode", earlier, runMode="unattended", decidedBy="ci", judge={"kind": "stage"}),
        workflow_sample("r-old", now - timedelta(days=40)),
        {"source": "session", "prompt": "NEVER STORE THIS"},
    ]


async def exercise_workflow_scores(runs: RunsHarness, *, postgres: bool = False) -> None:
    scope = {"project_id": str((await project(runs, "流水线评分"))["id"])}
    other = {"project_id": str((await project(runs, "工作流评分隔离项目"))["id"])}
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/dashboard/scorers/workflow"
    now = datetime.now(UTC)
    records = workflow_samples(now)
    payload = {"jsonl": "\ufeff" + "\n".join(json.dumps(row) for row in records)}

    async def read(**params: str | int) -> dict:
        response = await runs.client.get(path, params=scope | params)
        assert response.status_code == 200, response.text
        assert "NEVER STORE THIS" not in response.text
        return response.json()

    async def store() -> dict:
        response = await runs.client.post(path + "/import", params=scope, headers=runs.headers(), json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    assert (await read())["selected"] is None
    if postgres:
        stores = await asyncio.gather(store(), store())
    else:
        stores = [await store(), await store()]
    assert sorted(item["recorded"] for item in stores) == [0, 10]
    assert all(item["skipped_sessions"] == 1 for item in stores)
    assert (await runs.client.get(path, params=other)).json()["selected"] is None
    assert (await runs.client.get(path)).json()["selected"] is None

    summary = await read(group_by="stage", limit=1)
    chosen = summary["selected"]
    assert chosen["judge"]["model_source"] == "reported"
    assert chosen["stats"] == {"total": 5, "passed": 1, "failed": 4, "pass_rate": 0.2}
    assert chosen["model_stats"] == {"total": 2, "passed": 1, "failed": 1, "pass_rate": 0.5}
    assert chosen["before_judge"] == 3
    assert len(chosen["failures"]["items"]) == 1 and chosen["failures"]["total"] == 4
    stages = {row["value"]: row for row in chosen["stages"]}
    assert set(stages) == {"check", "protected", "ci", "judge", None}
    assert stages[None]["kind"] == "unknown"
    cohort = chosen["id"]
    only_ci = (await read(cohort=cohort, group_by="stage", group=stages["ci"]["id"]))["selected"]
    assert only_ci["active_group"] == stages["ci"]["id"]
    assert [row["run_id"] for row in only_ci["failures"]["items"]] == ["r-ci"]
    assert only_ci["stats"] == chosen["stats"]
    assert (await read(cohort=cohort, days=90))["selected"]["stats"]["total"] == 6

    others = {row["judge"]["model_source"] or row["judge"]["kind"]: row for row in summary["measures"]}
    assert others["requested"]["stats"]["total"] == 4
    assert others["human"]["stats"]["total"] == 1
    assert others["stage"]["run_mode"] == "unattended" and others["stage"]["stats"]["total"] == 1
    skill = (await read(cohort=cohort, group_by="skill"))["selected"]
    assert {(row["value"], row["kind"], row["stats"]["total"]) for row in skill["groups"]} == {
        ("checks", "value", 2), ("shipping", "value", 2), (None, "none", 1), (None, "unknown", 2),
    }
    skill_group = next(row for row in skill["groups"] if row["value"] == "shipping")
    skill_failures = (await read(cohort=cohort, group_by="skill", group=skill_group["id"]))["selected"]["failures"]
    assert [item["run_id"] for item in skill_failures["items"]] == ["r-ci"]
    models = (await read(cohort=cohort, group_by="model"))["selected"]["groups"]
    assert {row["model_source"] for row in models if row["value"] == "task-model"} == {"reported", "requested"}
    for dimension, values in {
        "tool": {"codex"}, "team_revision": {"b" * 40}, "definition": {"shipper"}, "role": {"release"},
        "project": {"api", "web"}, "tag": {"prod", "quality"}, "fingerprint": {"f" * 64},
    }.items():
        groups = (await read(cohort=cohort, group_by=dimension))["selected"]["groups"]
        assert {row["value"] for row in groups if row["kind"] == "value"} == values
        if dimension == "role":
            assert groups[0]["role_source"] == "step"

    conflicting = records[1] | {"label": "different", "passed": False}
    new = workflow_sample("r-failed-batch", now - timedelta(seconds=15))
    for error, line in [(409, json.dumps(conflicting)), (422, "{malformed}"),
                        (422, json.dumps(new | {"judge": {"kind": "stage"}}))]:
        fail = await runs.client.post(path + "/import", params=scope, headers=runs.headers(), json={
            "jsonl": json.dumps(new) + "\n" + line,
        })
        assert fail.status_code == error, fail.text
        assert (await read(cohort=cohort))["selected"]["stats"]["total"] == 5
    assert (await runs.client.post(path + "/import", params=scope, json=payload)).status_code == 403
    async with member(runs, "评分查看者") as (reader, _):
        assert (await reader.get(path, params=scope)).json()["selected"]["stats"]["total"] == 5
        denied = await reader.post(path + "/import", params=scope, headers=headers(reader), json=payload)
        assert denied.status_code == 403
    async with member(runs, "另一空间", same_workspace=False) as (outsider, _):
        assert (await outsider.get(path, params=scope)).status_code == 404


async def test_workflow_scores_import_and_group_with_scope_and_idempotency(runs: RunsHarness) -> None:
    await exercise_workflow_scores(runs)
