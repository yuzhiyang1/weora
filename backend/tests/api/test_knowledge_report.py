"""验证周报权限、归档去重和评分失败的历史口径。"""

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
from weora.modules.projects.persistence import ProjectRepositoryModel

pytestmark = pytest.mark.anyio


async def test_digest_tracks_first_shared_snapshots_in_two_weeks(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    rule = await runs.client.put(
        f"{root}/settings/session-archive/attended", headers=runs.headers(), json={"action": "auto"},
    )
    assert rule.status_code == 200, rule.text
    source = {
        "session": str(uuid4()), "runtime": "codex", "title": "第一次共享",
        "turns": [{"role": "assistant", "text": "HIDDEN-TRANSCRIPT-BODY"}], "automatic_mode": "attended",
        "captured_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
    }
    first = await runs.client.post(f"{root}/shared-sessions", params=scope, headers=runs.headers(), json=source)
    assert first.status_code == 201, first.text
    existing = first.json()["id"]
    revision = await runs.client.post(
        f"{root}/shared-sessions", params=scope, headers=runs.headers(), json=source | {"title": "后续修订"},
    )
    assert revision.status_code == 201 and revision.json()["revision"] == 2, revision.text
    prior = await runs.client.post(
        f"{root}/shared-sessions", params=scope, headers=runs.headers(),
        json=source | {"session": str(uuid4()), "runtime": "claude-code", "title": "上一周", "automatic_mode": None},
    )
    assert prior.status_code == 201, prior.text
    async with runs.sessions() as db, db.begin():
        version = await db.get(KnowledgeSharedSessionRevision, (UUID(prior.json()["id"]), 1))
        assert version is not None
        version.published_at = datetime.now(UTC) - timedelta(days=9)
    local = await runs.client.post(
        f"{root}/session-scoring/local-subjects", params=scope, headers=runs.headers(),
        json={"runtime": "codex", "subject_key": "a" * 64, "digest": "b" * 64},
    )
    assert local.status_code == 201, local.text
    async with member(runs, "周报观察者") as (teammate, _):
        digest = await teammate.get(f"{root}/digest", params=scope)
        assert digest.status_code == 200, digest.text
        assert "HIDDEN-TRANSCRIPT-BODY" not in digest.text and local.json()["id"] not in digest.text
        activity = digest.json()["team_activity"]
        current, previous = activity["current"], activity["previous"]
        assert current["shared_sessions"] == current["shared_turns"] == current["archive_contributors"] == 1
        assert current["by_run_mode"] == {"attended": 1, "unattended": 0, "not_recorded": 0}
        assert current["highlights"] == [{
            "id": existing, "title": "第一次共享", "runtime": "codex", "turn_count": 1,
        }]
        assert previous["by_runtime"] == {"claude-code": 1} and previous["by_run_mode"]["not_recorded"] == 1
        assert previous["until"] == current["since"]
        assert current["skill_usage"] is None
        shared = (await teammate.get(f"{root}/digest")).json()["team_activity"]
        assert shared["current"]["shared_sessions"] == shared["previous"]["shared_sessions"] == 0


async def test_scoring_failure_digest_keeps_latest_versioned_samples_and_full_totals(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他评分范围"))["id"])}
    repo = await repository(runs, scope["project_id"], "evidence")
    native = uuid4()
    now = datetime.now(UTC)
    evidence = {
        "repository": "git@git.example.test:team/evidence.git",
        "commit": "a" * 40, "tracked_changes": True, "observed_at": (now - timedelta(minutes=1)).isoformat(),
    }
    rule = {
        "prompt": "HIDDEN-SCORER-PROMPT", "passing_score": 1,
        "labels": [{"value": "pass", "score": 1, "description": "Verified."},
                   {"value": "fail", "score": 0, "description": "Missing."}],
    }
    scorer_path = f"{root}/session-scorers/verification"
    old = await runs.client.put(scorer_path, headers=runs.headers(), json=rule)
    assert old.status_code == 200, old.text
    scorer = old.json()["id"]
    new = await runs.client.put(
        scorer_path, headers=runs.headers(), json=rule | {"prompt": "Revised standard.", "base_revision": 1},
    )
    assert new.status_code == 200 and new.json()["revision"] == 2, new.text
    archive = await runs.client.post(f"{root}/shared-sessions", params=scope, headers=runs.headers(), json={
        "session": str(native), "runtime": "codex", "title": "回归证据",
        "git_start": evidence, "captured_at": now.isoformat(),
        "turns": [{"role": "assistant", "text": "HIDDEN-SCORE-TRANSCRIPT"}],
    })
    assert archive.status_code == 201, archive.text
    subject = archive.json()["id"]
    local = await runs.client.post(
        f"{root}/session-scoring/local-subjects", params=scope, headers=runs.headers(),
        json={"runtime": "codex", "subject_key": score_source_key("codex", native), "digest": "d" * 64,
              "git_start": evidence | {"commit": "b" * 40, "tracked_changes": False}},
    )
    assert local.status_code == 201, local.text

    async def save(target: str, driver: str, verdict: str, version: int = 1) -> int:
        label = await runs.client.post(
            f"{root}/shared-sessions/{target}/labels", params=scope | {"revision": "1"}, headers=runs.headers(),
            json={"scorer_id": scorer, "scorer_revision": version, "operation_id": str(uuid4()),
                  "driver": driver, "label": verdict},
        )
        assert label.status_code == 201, label.text
        return int(label.json()["id"])

    manual = await save(subject, "human", "pass")
    replaced = await save(subject, "codex", "fail")
    await save(subject, "codex", "pass")
    claude = await save(subject, "claude", "fail")
    v2_failure = await save(subject, "codex", "fail", 2)
    local_failure = await save(local.json()["id"], "human", "fail")
    expired = await save(subject, "claude", "fail", 2)
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeSessionLabel).where(KnowledgeSessionLabel.id.in_((expired, manual))).values(
            created_at=datetime.now(UTC) - timedelta(days=31),
        ))

    async with member(runs, "评分周报成员") as (teammate, _):
        path = f"{root}/digest/failures"
        limited = await teammate.get(path, params=scope | {"limit": 1})
        assert limited.status_code == 200, limited.text
        assert "HIDDEN-SCORE-TRANSCRIPT" not in limited.text and "HIDDEN-SCORER-PROMPT" not in limited.text
        digest = limited.json()
        assert digest["total_failed"] == 3 and digest["truncated"]
        assert [row["label_id"] for row in digest["failures"]] == [local_failure]
        cohorts = {
            (row["scorer_revision"], row["driver"], row["evidence"]): row for row in digest["cohorts"]
        }
        original = cohorts[(1, "codex", "archive")]
        assert original["stats"] == {"total": 1, "passed": 1, "failed": 0, "pass_rate": 1}
        assert original["agreement"] == {"paired": 1, "agree": 1}
        assert (1, "human", "archive") not in cohorts
        assert cohorts[(1, "claude", "archive")]["agreement"] == {"paired": 1, "agree": 0}
        assert cohorts[(2, "codex", "archive")]["agreement"] == {"paired": 0, "agree": 0}
        assert any(week["pass_rate"] is None for week in original["weeks"])
        full = (await teammate.get(path, params=scope)).json()
        assert {row["label_id"] for row in full["failures"]} == {claude, v2_failure, local_failure}
        assert replaced not in {row["label_id"] for row in full["failures"]}
        for row in full["failures"]:
            assert row["git_start"] == {
                "status": "tracked_changes", "repository_id": repo["id"], "commit": "a" * 40,
                "observed_at": evidence["observed_at"],
            }
        assert "git.example.test" not in limited.text
        async with runs.sessions() as db, db.begin():
            await db.execute(update(ProjectRepositoryModel).where(
                ProjectRepositoryModel.id == UUID(str(repo["id"])),
            ).values(connection_version=ProjectRepositoryModel.connection_version + 1))
        unavailable = (await teammate.get(path, params=scope)).json()["failures"]
        assert all(row["git_start"] == {"status": "repository_unavailable"} for row in unavailable)
        assert (await teammate.get(path, params=other)).json()["total_failed"] == 0
        assert (await teammate.get(path)).json()["total_failed"] == 0
