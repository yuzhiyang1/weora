"""验证所选评分标准的失败去重、起点回退和经验来源隔离。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from tests.api.test_projects import project, repository
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member
from weora.modules.knowledge.models import KnowledgeSessionLabel, KnowledgeSharedSessionRevision
from weora.modules.knowledge.transcript_archive import score_source_key

pytestmark = pytest.mark.anyio


async def test_plan_links_published_learnings_and_selects_latest_usable_session_evidence(runs: RunsHarness) -> None:
    await exercise_plan(runs, "a" * 40)


async def exercise_plan(
    runs: RunsHarness, start_commit: str, *, repository_url: str = "https://git.example.test/team/review.git",
) -> str:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    path = f"{root}/self-improvement/candidates"
    project_id = str((await project(runs))["id"])
    other = str((await project(runs, "另一个回归项目"))["id"])
    scope = {"project_id": project_id}
    repo = await repository(runs, project_id, "review", git_url=repository_url)
    now = datetime.now(UTC)
    observed = now - timedelta(minutes=1)
    rule = await runs.client.put(
        f"{root}/session-scorers/review-quality", headers=runs.headers(), json={
            "prompt": "PRIVATE_JUDGE_PROMPT", "passing_score": 1,
            "labels": [{"value": "pass", "score": 1, "description": "Complete."},
                       {"value": "fail", "score": 0, "description": "Incomplete."}],
        },
    )
    assert rule.status_code == 200, rule.text
    scorer = rule.json()["id"]

    async def archive(session: UUID, dirty: bool | None) -> str:
        payload = {"session": str(session), "runtime": "codex", "title": "回归证据",
                   "captured_at": now.isoformat(),
                   "turns": [{"role": "assistant", "text": "PRIVATE_SOURCE_BODY"}]}
        if dirty is not None:
            payload["git_start"] = {
                "repository": repository_url, "commit": start_commit,
                "tracked_changes": dirty, "observed_at": observed.isoformat(),
            }
        receipt = await runs.client.post(f"{root}/shared-sessions", params=scope, headers=runs.headers(), json=payload)
        assert receipt.status_code == 201, receipt.text
        return str(receipt.json()["id"])

    async def score(archive_id: str, label: str = "fail") -> int:
        response = await runs.client.post(
            f"{root}/shared-sessions/{archive_id}/labels", params=scope | {"revision": "1"}, headers=runs.headers(),
            json={"scorer_id": scorer, "scorer_revision": 1, "operation_id": str(uuid4()),
                  "driver": "human", "label": label},
        )
        assert response.status_code == 201, response.text
        return int(response.json()["id"])

    native = uuid4()
    archived = await archive(native, False)
    usable = await score(archived)
    dirty = await score(await archive(uuid4(), True))
    legacy = await score(await archive(uuid4(), None))
    superseded = await archive(uuid4(), False)
    await score(superseded)
    await score(superseded, "pass")
    expired = await score(await archive(uuid4(), False))
    local = await runs.client.post(
        f"{root}/session-scoring/local-subjects", params=scope, headers=runs.headers(),
        json={"runtime": "codex", "subject_key": score_source_key("codex", native), "digest": "d" * 64},
    )
    assert local.status_code == 201, local.text
    local_id = local.json()["id"]
    newest = await score(local_id)
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeSharedSessionRevision).where(
            KnowledgeSharedSessionRevision.archive_id == UUID(local_id),
        ).values(captured_at=observed - timedelta(seconds=1)))
        await db.execute(update(KnowledgeSessionLabel).where(
            KnowledgeSessionLabel.id == expired,
        ).values(created_at=now - timedelta(days=31)))

    learning = {
        "operation_id": str(uuid4()), "kind": "learning", "title": "从评审反馈改进",
        "body": "PRIVATE_LEARNING_BODY", "source": {"type": "session", "ref": f"codex:{native}"},
    }
    published = await runs.client.post(root, params=scope, headers=runs.headers(), json=learning)
    assert published.status_code == 201, published.text
    hidden = await runs.client.post(
        root, params={"project_id": other}, headers=runs.headers(),
        json=learning | {"operation_id": str(uuid4()), "title": "CROSS_PROJECT_PRIVATE"},
    )
    assert hidden.status_code == 201, hidden.text
    async with member(runs, "回归读者") as (teammate, _):
        impersonation = await teammate.post(
            root, params=scope, headers={"X-CSRF-Token": teammate.cookies["weora_csrf"]},
            json=learning | {"operation_id": str(uuid4()), "title": "OTHER_AUTHOR"},
        )
        assert impersonation.status_code == 201, impersonation.text
        response = await teammate.get(path, params=scope | {"scorer": "review-quality"})
        assert response.status_code == 200, response.text
        assert all(text not in response.text for text in ("PRIVATE", "OTHER_AUTHOR", str(native)))
        subjects = response.json()["subjects"]
        assert [item["latest"]["label_id"] for item in subjects] == [newest, legacy, dirty]
        chosen, old, changed = subjects
        assert chosen["recorded"]["label_id"] == usable
        assert chosen["recorded"]["git_start"]["repository_id"] == repo["id"]
        assert chosen["recorded"]["connection_version"] == repo["connection_version"]
        assert chosen["learnings"] == [{"id": published.json()["id"], "revision": 1, "title": learning["title"]}]
        assert old["recorded"] is None and old["latest"]["git_start"]["status"] == "not_recorded"
        assert changed["recorded"] is None and changed["latest"]["git_start"]["status"] == "tracked_changes"
        again = await teammate.get(path, params=scope | {"scorer": "review-quality"})
        assert again.json()["subjects"][0]["subject_id"] == chosen["subject_id"]
        assert (await teammate.get(path, params={"scorer": "review-quality"})).status_code == 422
        assert (await teammate.get(path, params=scope | {"scorer": "unknown"})).status_code == 404
        empty = await teammate.get(path, params={"project_id": other, "scorer": "review-quality"})
        assert empty.status_code == 200 and empty.json()["subjects"] == []
    return project_id
