"""验证项目知识的作用域、版本切换、幂等和召回记录。"""

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import patch
from uuid import UUID, uuid4
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import select, update

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_skills import build_skill_package
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import (
    KnowledgeAdoptionAttempt,
    KnowledgeArchivePolicy,
    KnowledgeEntry,
    KnowledgeEvent,
    KnowledgeHintPolicy,
    KnowledgeNamedScoreCall,
    KnowledgeRecall,
    KnowledgeRecallRun,
    KnowledgeRecallRunHit,
    KnowledgeRevision,
    KnowledgeScoringAttempt,
    KnowledgeSessionLabel,
    KnowledgeSessionPolicy,
    KnowledgeSharedSession,
    KnowledgeSharedSessionRevision,
)
from weora.modules.knowledge.recall_journal import purge_recall_journal
from weora.modules.skills.infrastructure.models import SkillModel

pytestmark = pytest.mark.anyio


async def test_contribute_recall_and_review(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    orders, billing = await project(runs), await project(runs, "结算")
    scope = {"project_id": str(orders["id"])}
    other_scope = {"project_id": str(billing["id"])}
    payload = {"operation_id": str(uuid4()), "title": "导出超时排查", "body": "旧版超时约束", "tags": ["导出"]}
    created = await runs.client.post(path, params=scope, json=payload, headers=runs.headers())
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["status"] == "active"
    detail = f"{path}/{entry['id']}"
    replay = await runs.client.post(path, params=scope, json=payload, headers=runs.headers())
    assert replay.status_code == 201 and replay.json()["id"] == entry["id"]
    changed = await runs.client.post(
        path, params=scope, json=payload | {"body": "不同内容"}, headers=runs.headers(),
    )
    assert changed.status_code == 409
    assert (await runs.client.get(detail, params=other_scope)).status_code == 404
    assert (await runs.client.get(path)).json()["items"] == []
    assert (await runs.client.get(path, params=scope | {"query": "超时", "check": "true"})).json() == {"relevant": True}
    for _ in range(2):
        result = await runs.client.post(
            f"{path}/recall", params=scope, json={"query": "超时", "session": "root"}, headers=runs.headers(),
        )
        assert result.status_code == 200 and result.json()["items"][0]["id"] == entry["id"]
        opened = await runs.client.post(
            f"{detail}/open", params=scope, json={"session": "root"}, headers=runs.headers(),
        )
        assert opened.status_code == 200 and opened.json()["body"] == payload["body"]
    stats = (await runs.client.get(f"{path}/maintenance", params=scope)).json()["items"][0]
    assert (stats["recalls"], stats["sessions"], stats["upvotes"]) == (2, 1, 1)

    revision = payload | {"operation_id": str(uuid4()), "body": "新版约束", "base_revision": 1}
    draft = await runs.client.post(f"{detail}/revisions", params=scope, json=revision, headers=runs.headers())
    assert draft.status_code == 201 and draft.json()["revision_status"] == "pending"
    assert (await runs.client.get(detail, params=scope)).json()["body"] == payload["body"]
    accepted = await runs.client.post(
        f"{detail}/review", params=scope, json={"decision": "accept", "revision": 2}, headers=runs.headers(),
    )
    assert accepted.status_code == 200, accepted.text
    assert (await runs.client.get(detail, params=scope)).json()["body"] == revision["body"]


async def test_recall_ranks_evidence_and_uses_only_visible_published_versions(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "结算"))["id"])}
    enabled = await runs.client.put(
        f"{path}/settings/recall-scope", params=scope, headers=runs.headers(), json={"inherit_shared": True},
    )
    assert enabled.status_code == 200, enabled.text

    async def add(title: str, body: str, target: dict[str, str], **fields: object) -> str:
        response = await runs.client.post(path, params=target, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": body, **fields,
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def get(query: str, *, check: bool = False, recall: bool = True) -> httpx.Response:
        response = await runs.client.get(
            path, params={**scope, "query": query, "recall_mode": recall, "check": check},
        )
        assert response.status_code == 200, response.text
        return response

    focus = await add("QueueLease", "保持期只在续约时更新。", scope, tags=["queue", "lease"])
    hub = await add("QueueLease metrics policy ownership contract rollout guide archive", "工作区手册。", scope)
    body_only = await add("杂项经验", "QueueLease and multicast", scope)
    doc = await add("共享文档", "QueueLease", {}, kind="doc")
    published = await runs.client.post(
        f"{path}/{doc}/review", headers=runs.headers(), json={"decision": "accept", "revision": 1},
    )
    assert published.status_code == 200, published.text
    await add("带队列的正常用法", "禁止相似标识符 queueleaser 发生误匹配。", scope)
    result = (await get("QueueLease")).json()
    assert [row["id"] for row in result["items"]] == [focus, hub, doc]
    assert result["items"][0]["matched_terms"] == ["QueueLease"]
    assert result["items"][0]["relevant"] is True
    assert result["items"][-1]["relevant"] is False
    assert result["items"][-1]["matched_terms"] == result["items"][-1]["missing_terms"] == []

    await add("QueueLease", "其他项目高相关结果。", other, tags=["queue", "lease"])
    assert (await get("QueueLease")).json()["items"] == result["items"]
    assert [row["id"] for row in (await get("multicast", recall=False)).json()["items"]] == [body_only]
    assert (await get("multicast", check=True)).json() == {"relevant": False}
    assert (await get("QueueLease", check=True)).json() == {"relevant": True}
    async with runs.sessions() as db:
        assert (await db.scalars(select(KnowledgeRecall))).all() == []

    version = await runs.client.post(
        f"{path}/{focus}/revisions", params=scope, headers=runs.headers(),
        json={"operation_id": str(uuid4()), "base_revision": 1, "title": "互斥锁续约", "body": "新版指南"},
    )
    assert version.status_code == 201, version.text
    assert (await get("互斥锁续约")).json()["items"] == []
    approval = await runs.client.post(
        f"{path}/{focus}/review", params=scope, headers=runs.headers(), json={"decision": "accept", "revision": 2},
    )
    assert approval.status_code == 200, approval.text
    assert [row["id"] for row in (await get("请问如何互斥锁续约")).json()["items"]] == [focus]
    assert [row["id"] for row in (await get("QueueLease")).json()["items"]] == [hub, doc]


async def test_recall_reports_word_coverage_from_title_and_tags(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    created = await runs.client.post(path, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "推理服务排查", "tags": ["AppID", "中间件"],
        "body": "优先排除连接池耗尽导致的错误率上升和服务崩溃。",
    })
    assert created.status_code == 201, created.text
    search = await runs.client.get(path, params={"query": "推理服务崩溃 AppID 中间件排查", "recall_mode": True})
    assert search.status_code == 200, search.text
    row = search.json()["items"][0]
    assert row["matched_terms"] == ["推理", "服务", "AppID", "中间件", "排查"]
    assert row["missing_terms"] == ["崩溃"]
    params = {"query": "推理服务崩溃 AppID 中间件排查", "recall_mode": True, "check": True, "check_details": True}
    check = await runs.client.get(path, params=params)
    assert check.status_code == 200, check.text
    verdict = check.json()
    assert verdict["relevant"] is True and verdict["score"] >= verdict["threshold"]
    assert verdict["entry_id"] == row["id"] and verdict["title"] == row["title"]
    assert verdict["matched_terms"] == row["matched_terms"] and verdict["missing_terms"] == row["missing_terms"]
    assert verdict["sources"] == [] and "body" not in verdict and "excerpt" not in verdict
    for query in ("", "absolutelyunrelated"):
        miss = await runs.client.get(path, params=params | {"query": query})
        assert miss.json() == {"relevant": False, "score": 0.0, "threshold": 4.0}
    async with runs.sessions() as db:
        workspace_id = UUID(runs.workspace_id)
        assert await db.scalar(
            select(KnowledgeRecallRun.id).where(KnowledgeRecallRun.workspace_id == workspace_id),
        ) is None
        assert await db.scalar(
            select(KnowledgeRecall.entry_id).join(KnowledgeEntry, KnowledgeEntry.id == KnowledgeRecall.entry_id)
            .where(KnowledgeEntry.workspace_id == workspace_id),
        ) is None
    boundary = await runs.client.get(path, params={"query": "理服", "recall_mode": True})
    assert boundary.status_code == 200 and boundary.json()["items"] == []


async def test_recall_feedback_breaks_scoring_ties(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    title = "缓存失效方案"
    entries = []
    for body in ("先发布的经验。", "后发布的经验。"):
        created = await runs.client.post(path, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "tags": ["缓存"], "body": body,
        })
        assert created.status_code == 201, created.text
        entries.append(created.json()["id"])
    query: dict[str, str | bool] = {"query": title, "recall_mode": True}
    before = await runs.client.get(path, params=query)
    assert [row["id"] for row in before.json()["items"]] == entries[::-1]
    recalled = await runs.client.post(
        f"{path}/recall", headers=runs.headers(), json=query | {"session": "ranking-proof"},
    )
    assert recalled.status_code == 200, recalled.text
    opened = await runs.client.post(
        f"{path}/{entries[0]}/open", headers=runs.headers(), json={"session": "ranking-proof"},
    )
    assert opened.status_code == 200, opened.text
    after = await runs.client.get(path, params=query)
    result = after.json()["items"]
    assert [row["id"] for row in result] == entries
    assert result[0]["score"] > result[1]["score"]


async def test_recall_collapses_reshare_before_limit_and_records_only_winner(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "重复经验"))["id"])}
    title = "QueueLease 恢复"
    enabled = await runs.client.put(
        f"{path}/settings/recall-scope", params=scope, headers=runs.headers(), json={"inherit_shared": True},
    )
    assert enabled.status_code == 200, enabled.text

    async def publish(body: str, tags: list[str], params: dict[str, str] = scope) -> str:
        response = await runs.client.post(path, params=params, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": body, "tags": tags,
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    best = await publish("quartz nebula", ["guard", "lease"])
    replay = await publish("nebula quartz quartz", ["lease", "guard"])
    different_body = await publish("quartz circuit", ["guard", "lease"])
    different_tags = await publish("quartz nebula", ["guard", "quartz"])
    previous_day = await publish("quartz nebula", ["guard", "lease"])
    shared = await publish("quartz nebula", ["guard", "lease"], {})
    # 固定测试日期，区分同日重复分享和另一天的独立记录。
    day = datetime(2026, 1, 2, 9, tzinfo=UTC)
    async with runs.sessions() as db, db.begin():
        for offset, identifier in enumerate((best, replay, different_body, different_tags, shared)):
            await db.execute(update(KnowledgeRevision).where(
                KnowledgeRevision.entry_id == UUID(identifier),
            ).values(created_at=day + timedelta(minutes=offset)))
        await db.execute(update(KnowledgeRevision).where(
            KnowledgeRevision.entry_id == UUID(previous_day),
        ).values(created_at=day - timedelta(days=1)))
    for number in range(4):
        response = await runs.client.post(f"{path}/{best}/feedback", params=scope, headers=runs.headers(), json={
            "session": f"duplicate-vote-{number}", "positive": True,
        })
        assert response.status_code == 204, response.text

    query = {"query": "QueueLease", "recall_mode": True, "limit": 5}
    recalled = await runs.client.post(f"{path}/recall", params=scope, headers=runs.headers(), json={
        **query, "session": "duplicate-proof",
    })
    assert recalled.status_code == 200, recalled.text
    actual = [row["id"] for row in recalled.json()["items"]]
    assert set(actual) == {best, different_body, different_tags, previous_day, shared}
    original = await runs.client.get(f"{path}/{replay}", params=scope)
    assert original.status_code == 200 and original.json()["id"] == replay
    async with runs.sessions() as db:
        recorded = await db.scalars(select(KnowledgeRecallRunHit.entry_id).join(
            KnowledgeRecallRun, KnowledgeRecallRun.id == KnowledgeRecallRunHit.run_id,
        ).where(KnowledgeRecallRun.workspace_id == UUID(runs.workspace_id)))
        assert {str(item) for item in recorded} == set(actual)


async def test_recall_journal_counts_misses_and_opened_snapshot_relations(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他项目"))["id"])}
    session = "private-root-journal"

    async def post(suffix: str, **payload: object) -> httpx.Response:
        return await runs.client.post(
            path + suffix, params=scope, headers=runs.headers() | {"X-Request-ID": "journal-fixture"}, json=payload,
        )

    linked = await post("", operation_id=str(uuid4()), title="related chapter", body="规则的补充材料。")
    assert linked.status_code == 201, linked.text
    target = str(linked.json()["id"])
    first = await post(
        "", operation_id=str(uuid4()), title="nebula operations", body="原版。",
        relations=[{"kind": "REFERENCES", "target": target}],
    )
    assert first.status_code == 201
    entry = str(first.json()["id"])
    precheck = await runs.client.get(path, params=scope | {"query": "nebula", "check": "true"})
    assert precheck.status_code == 200 and precheck.json()["relevant"] is True
    assert (await post("/recall/stats", session=session)).json() == {"items": []}
    hit = await post("/recall", session=session, query="nebula", recall_mode=True)
    assert hit.status_code == 200, hit.text
    assert [row["id"] for row in hit.json()["items"]] == [entry]
    run_id = UUID(hit.json()["run_id"])
    missed = await post("/recall", session=session, query="notavailableqxx", recall_mode=True)
    assert missed.json()["items"] == [] and missed.json()["run_id"] != str(run_id)

    revision = await post(
        f"/{entry}/revisions", operation_id=str(uuid4()), title="nebula operations", body="新版。", base_revision=1,
    )
    assert revision.status_code == 201, revision.text
    review = await post(f"/{entry}/review", decision="accept", revision=2)
    assert review.status_code == 200
    new_run = await post("/recall", session=session, query="nebula", recall_mode=True)
    assert new_run.json()["items"][0]["relations"] == []
    assert (await post(f"/{target}/open", session="unrelated-root")).status_code == 200
    before = (await post("/recall/stats", session=session)).json()["items"][0]
    assert before["via_relation"] == before["adopted"] == 0
    for _ in range(2):
        assert (await post(f"/{target}/open", session=session)).status_code == 200
    assert (await post(f"/{entry}/open", session=session)).status_code == 200

    row = (await post("/recall/stats", session=session)).json()["items"][0]
    assert (row["runs"], row["hits"], row["misses"], row["recalled"], row["adopted"], row["via_relation"]) == (
        3, 2, 1, 1, 1, 1,
    )
    assert row["session_ref"] != session and row["top_score"] > 0
    other_stats = await runs.client.post(path + "/recall/stats", params=other, headers=runs.headers(),
                                         json={"session": session})
    assert other_stats.json() == {"items": []}
    async with member(runs, "召回日志隔离") as (teammate, _):
        hidden = await teammate.post(path + "/recall/stats", params=scope, headers=headers(teammate),
                                     json={"session": session})
        assert hidden.json() == {"items": []}
    async with runs.sessions() as db, db.begin():
        run = await db.get(KnowledgeRecallRun, run_id)
        saved = await db.get(KnowledgeRecallRunHit, (run_id, 0))
        assert run is not None and run.request_id == "journal-fixture"
        assert saved is not None and saved.revision == 1 and saved.related == [target]
        assert await db.scalar(select(KnowledgeRecall).where(KnowledgeRecall.entry_id == UUID(target))) is None
        run.created_at = datetime.now(UTC) - timedelta(days=31)
        await db.flush()
        assert await purge_recall_journal(db) == 1
    async with runs.sessions() as db:
        assert await db.get(KnowledgeRecallRunHit, (run_id, 0)) is None
    remaining = (await post("/recall/stats", session=session, recent=True)).json()["items"][0]
    assert remaining["runs"] == 2 and remaining["via_relation"] == 0


async def test_shared_transcript_versions_are_readable_only_in_the_published_scope(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/shared-sessions"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他项目"))["id"])}
    native = str(uuid4())
    payload = {
        "runtime": "codex", "session": native, "title": "Approved transcript",
        "turns": [{"role": "user", "text": "Read <repo>/api.py"}, {"role": "assistant", "text": "Checked."}],
    }

    async def publish(body: object, **extra: str) -> httpx.Response:
        return await runs.client.post(path, params=scope, headers=runs.headers() | extra, json=body)

    denied = await publish(payload | {"turns": [{"role": "user", "text": "Bearer unique_fake_fixture"}]})
    assert denied.status_code == 422 and denied.json()["detail"]["code"] == "KNOWLEDGE_TRANSCRIPT_UNSCRUBBED"
    created = await publish(payload, **{"X-Request-ID": "confirmed-share"})
    assert created.status_code == 201, created.text
    first = created.json()
    archive = first["id"]
    assert first["revision"] == 1 and first["turn_count"] == 2 and native not in str(first)
    revised = await publish(payload | {"title": "New reviewed transcript"})
    assert revised.json()["id"] == archive and revised.json()["revision"] == 2
    again = await publish(payload)
    assert again.json() == first

    detail = f"{path}/{archive}"
    current = await runs.client.get(detail, params=scope)
    assert current.json()["title"] == "New reviewed transcript"
    older = await runs.client.get(detail, params=scope | {"revision": "1"})
    assert older.json()["turns"] == payload["turns"] and older.json()["title"] == payload["title"]
    listing = await runs.client.get(path, params=scope)
    assert listing.json()["items"][0]["revision"] == 2 and "turns" not in listing.json()["items"][0]
    assert (await runs.client.get(detail, params=other)).status_code == 404
    assert (await runs.client.get(path)).json() == {"items": []}
    assert (await runs.client.get(detail)).status_code == 404
    async with member(runs, "同项目归档读取") as (teammate, _):
        assert (await teammate.get(detail, params=scope)).status_code == 200
    async with member(runs, "不同工作空间", same_workspace=False) as (outsider, _):
        assert (await outsider.get(detail, params=scope)).status_code == 404
    async with runs.sessions() as db:
        old = await db.get(KnowledgeSharedSessionRevision, (UUID(archive), 1))
        assert old is not None and old.request_id == "confirmed-share"


async def test_automatic_archive_requires_current_workspace_policy_and_audits_its_version(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    policy = root + "/settings/session-archive"
    path = root + "/shared-sessions"
    scope = {"project_id": str((await project(runs))["id"])}
    payload = {
        "runtime": "codex", "session": str(uuid4()), "title": "Ready to publish",
        "turns": [{"role": "assistant", "text": "Sanitized transcript."}],
        "captured_at": datetime.now(UTC).isoformat(), "automatic_mode": "attended",
    }

    assert (await runs.client.get(policy)).json() == {
        "attended": {"action": "ask", "source": "default"},
        "unattended": {"action": "off", "source": "default"},
    }
    attempt = await runs.client.post(path, params=scope, headers=runs.headers(), json=payload)
    assert attempt.status_code == 403 and attempt.json()["detail"]["code"] == "KNOWLEDGE_ARCHIVE_AUTOMATIC_DISABLED"
    async with member(runs, "归档贡献者") as (teammate, _):
        change = await teammate.put(policy + "/attended", headers=headers(teammate), json={"action": "auto"})
        assert change.status_code == 403

    async def set_policy(action: str) -> httpx.Response:
        return await runs.client.put(policy + "/attended", headers=runs.headers(), json={"action": action})

    scoped = await runs.client.put(
        policy + "/attended", params=scope, headers=runs.headers(), json={"action": "auto"},
    )
    assert scoped.status_code == 422
    assert (await set_policy("auto")).json() == {"action": "auto", "source": "workspace"}
    assert (await set_policy("auto")).status_code == 200
    invalid = await runs.client.post(path, params=scope, headers=runs.headers(), json=payload | {"captured_at": None})
    assert invalid.status_code == 422
    unattended = await runs.client.post(
        path, params=scope, headers=runs.headers(), json=payload | {"automatic_mode": "unattended"},
    )
    assert unattended.status_code == 403
    published = await runs.client.post(path, params=scope, headers=runs.headers(), json=payload)
    assert published.status_code == 201, published.text
    receipt = published.json()
    assert receipt["revision"] == 1 and receipt["archive_policy_id"]
    assert (await runs.client.post(path, params=scope, headers=runs.headers(), json=payload)).json() == receipt
    assert (await set_policy("off")).status_code == 200
    assert (await runs.client.post(path, params=scope, headers=runs.headers(), json=payload)).status_code == 403
    manual = await runs.client.post(
        path, params=scope, headers=runs.headers(), json=payload | {"automatic_mode": None, "title": "Manual review"},
    )
    assert manual.json()["revision"] == 2 and manual.json()["archive_policy_id"] is None
    async with runs.sessions() as db:
        version = await db.get(KnowledgeSharedSessionRevision, (UUID(receipt["id"]), 1))
        assert version is not None and version.archive_policy_id == receipt["archive_policy_id"]
        rows = list(await db.scalars(select(KnowledgeArchivePolicy).order_by(KnowledgeArchivePolicy.id)))
        assert [(row.run_mode, row.action) for row in rows] == [("attended", "auto"), ("attended", "off")]
        assert rows[0].id == version.archive_policy_id


async def test_team_session_scorers_pin_scope_versions_and_keep_judge_history(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "另一个评分项目"))["id"])}
    path = root + "/session-scorers/review-quality"
    rubric = {
        "prompt": "Check whether the session verified its result.", "passing_score": 10,
        "labels": [{"value": "traced", "score": 10, "description": "Found evidence."},
                   {"value": "untraced", "score": 0, "description": "Evidence is missing."}],
    }

    async def publish(project_params: dict[str, str], body: dict[str, object]) -> httpx.Response:
        return await runs.client.put(path, params=project_params, headers=runs.headers(), json=body)

    shared = await publish({}, rubric)
    assert shared.status_code == 200, shared.text
    duplicate = await publish({}, rubric)
    assert duplicate.json() == shared.json()
    invalid = await publish(scope, rubric | {"passing_score": 21})
    assert invalid.status_code == 422
    local = await publish(scope, rubric | {"prompt": "Verify project-specific results.", "base_revision": 0})
    assert local.status_code == 200 and local.json()["id"] != shared.json()["id"]
    assert (await publish(scope, rubric | {"base_revision": 0})).status_code == 409
    foreign = await publish(other, rubric | {"prompt": "Another project's private rubric."})

    published = await runs.client.post(root + "/shared-sessions", params=scope, headers=runs.headers(), json={
        "session": str(uuid4()), "runtime": "codex", "title": "Checked snapshot",
        "turns": [{"role": "assistant", "text": "Verified with a test."}],
    })
    assert published.status_code == 201
    archive_id = published.json()["id"]
    version = scope | {"revision": "1"}
    archive = f"{root}/shared-sessions/{archive_id}"
    plan_path = archive + "/score-plan"
    labels_path = archive + "/labels"
    async with member(runs, "队友评分") as (teammate, _):
        forbidden = await teammate.put(path, params=scope, headers=headers(teammate), json=rubric)
        assert forbidden.status_code == 403
        plan = await teammate.post(
            plan_path, params=version, headers=headers(teammate), json={"scorers": ["review-quality"]},
        )
        assert plan.status_code == 200, plan.text
        assert plan.json()["archive"]["digest"] == published.json()["digest"]
        assert plan.json()["items"] == [local.json()]

        # 固定计划之后更新标准，已拿到的旧版本仍可提交，因此不会误用新阈值。
        new_labels = [{"value": "traced", "score": 99, "description": "A newer score."},
                      {"value": "untraced", "score": 0, "description": "Evidence is missing."}]
        new = await publish(scope, rubric | {"labels": new_labels, "passing_score": 5, "base_revision": 1})
        assert new.status_code == 200 and new.json()["revision"] == 2
        request_key = str(uuid4())
        first = {"scorer_id": local.json()["id"], "scorer_revision": 1,
                 "driver": "human", "label": "traced", "operation_id": request_key}

        async def submit(body: dict[str, object], params: dict[str, str] = version) -> httpx.Response:
            return await teammate.post(labels_path, params=params, headers=headers(teammate, "rubric-audit"), json=body)

        assert (await submit(first | {"scorer_id": foreign.json()["id"]})).status_code == 404
        assert (await submit(first, other | {"revision": "1"})).status_code == 404
        assert (await submit(first | {"label": "fabricated"})).status_code == 422
        verdict = await submit(first)
        assert verdict.status_code == 201, verdict.text
        assert verdict.json()["scorer_revision"] == 1 and verdict.json()["passed"] is True
        assert verdict.json()["score"] == 10
        assert (await submit(first)).json() == verdict.json()
        assert (await submit(first | {"label": "untraced"})).status_code == 409
        assert (await submit(first | {"driver": "claude", "operation_id": str(uuid4())})).status_code == 201
        assert (await submit(first | {"label": "untraced", "operation_id": str(uuid4())})).status_code == 201

    visible = await runs.client.get(labels_path, params=version)
    history = await runs.client.get(labels_path, params=version | {"history": "true"})
    assert [(row["driver"], row["label"]) for row in visible.json()["items"]] == [
        ("human", "untraced"), ("claude", "traced"),
    ]
    assert len(history.json()["items"]) == 3
    assert (await runs.client.get(labels_path, params=other | {"revision": "1"})).status_code == 404
    async with runs.sessions() as db:
        row = await db.get(KnowledgeSessionLabel, verdict.json()["id"])
        assert row is not None and str(row.operation_id) == request_key


async def test_scorer_folder_validation_and_pinned_attachments(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    catalog = root + "/session-scorers"
    rubric = {
        "prompt": "Read ${TEAMAI_SCORER_DIR}/guides/check.md.", "passing_score": 1,
        "sessions": True, "sampling_rate": 100, "labels": [
            {"value": "pass", "score": 1, "description": "Evidence found."},
            {"value": "fail", "score": 0, "description": "Evidence missing."},
        ],
    }

    async def publish(files: list[dict[str, str]] | None, revision: int = 0) -> httpx.Response:
        return await runs.client.put(
            catalog + "/evidence", params=scope, headers=runs.headers(),
            json=rubric | {"files": files, "base_revision": revision},
        )

    for invalid in (
        None,
        [{"path": "../other.md", "content": "Nothing"}],
        [{"path": "SCORER.md/other.md", "content": "Nothing"}],
        [{"path": "Doc", "content": "File"}, {"path": "doc/guide.md", "content": "Child"}],
        [{"path": "large.md", "content": "x" * (1024 * 1024)}],
    ):
        assert (await publish(invalid)).status_code == 422

    files = [{"path": "guides/check.md", "content": "Version one\n"}, {"path": "about.md", "content": "Notes"}]
    response = await publish([files[0] | {"content": "Version one\r\n"}, files[1]])
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["files"] == files[::-1] and len(first["source_hash"]) == 64
    assert (await publish(files[::-1], 1)).json() == first
    visible = (await runs.client.get(catalog, params=scope)).json()["items"][0]
    assert visible["source_hash"] == first["source_hash"] and "files" not in visible

    configured = await runs.client.put(
        root + "/settings/session-scoring/attended", headers=runs.headers(), json={"action": "auto"},
    )
    assert configured.status_code == 200, configured.text
    shared = await runs.client.post(root + "/shared-sessions", params=scope, headers=runs.headers(), json={
        "runtime": "codex", "session": str(uuid4()), "title": "Folder evidence",
        "turns": [{"role": "assistant", "text": "Checked against the guide."}],
    })
    assert shared.status_code == 201
    operation = str(uuid4())

    async def claim() -> httpx.Response:
        return await runs.client.post(
            root + "/session-scoring/claim", params=scope, headers=runs.headers(),
            json={"operation_id": operation, "run_mode": "attended"},
        )

    original_claim = await claim()
    assert original_claim.status_code == 200, original_claim.text
    assert original_claim.json()["scorer"] == first
    # 只更换附件也会发布新版本，已经领取的判定仍使用旧附件及校验值。
    response = await publish([files[0] | {"content": "Version two"}, files[1]], 1)
    assert response.status_code == 200, response.text
    latest = response.json()
    assert (latest["id"], latest["revision"]) == (first["id"], 2)
    assert latest["source_hash"] != first["source_hash"]
    assert (await claim()).json() == original_claim.json()
    receipt = await runs.client.post(
        root + f"/session-scoring/attempts/{operation}/settle", params=scope, headers=runs.headers(),
        json={"label": "pass"},
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["label"]["scorer_source_hash"] == first["source_hash"]


async def test_automatic_scoring_sampling_quota_and_recovery(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    project_id = (await project(runs))["id"]
    scope = {"project_id": str(project_id)}
    policy = root + "/settings/session-scoring/attended"
    base = root + "/session-scoring"
    rubric = {
        "prompt": "Check for verification.", "passing_score": 1, "sessions": True, "sampling_rate": 100,
        "labels": [{"value": "verified", "score": 1, "description": "Covered."},
                   {"value": "missing", "score": 0, "description": "Absent."}],
    }

    async def claim(key: str | None = None, mode: str = "attended") -> dict[str, object]:
        response = await runs.client.post(base + "/claim", params=scope, headers=runs.headers(), json={
            "operation_id": key or str(uuid4()), "run_mode": mode,
        })
        assert response.status_code == 200, response.text
        data: dict[str, object] = response.json()
        return data

    async def settle(key: str, **data: str) -> httpx.Response:
        return await runs.client.post(base + f"/attempts/{key}/settle", params=scope, headers=runs.headers(), json=data)

    async def configure(action: str, limit: int) -> None:
        changed = await runs.client.put(policy, headers=runs.headers(), json={"action": action, "daily_limit": limit})
        assert changed.status_code == 200, changed.text

    async def publish(name: str, options: dict[str, object]) -> dict[str, object]:
        saved = await runs.client.put(root + "/session-scorers/" + name, params=scope,
                                      headers=runs.headers(), json=rubric | options)
        assert saved.status_code == 200, saved.text
        definition: dict[str, object] = saved.json()
        return definition

    assert await claim() == {"status": "off"}
    await configure("ask", 1)
    assert await claim() == {"status": "ask"}
    await configure("auto", 1)
    await publish("a-disabled", {"sessions": False})
    await publish("b-unsampled", {"sampling_rate": 0})
    named = await publish("c-quality", {"tool": "claude"})
    shared = await runs.client.post(root + "/shared-sessions", params=scope, headers=runs.headers(), json={
        "session": str(uuid4()), "runtime": "codex", "title": "Finished",
        "turns": [{"role": "assistant", "text": "Verified."}],
    })
    assert shared.status_code == 201, shared.text
    key = str(uuid4())
    first = await claim(key)
    assert first["status"] == "open" and first["scorer"] == named and first["archive"] == shared.json()
    assert await claim(key) == first
    assert await claim() == {"status": "daily_limit"}
    assert await claim(mode="unattended") == {"status": "off"}
    async with member(runs, "不同评分人") as (teammate, _):
        denied = await teammate.post(base + f"/attempts/{key}/settle", params=scope,
                                     headers=headers(teammate), json={"label": "verified"})
        assert denied.status_code == 404
    assert (await settle(key, error="judge_failed")).json() == {"status": "failed", "reason": "judge_failed"}
    await configure("auto", 7)
    assert await claim() == {"status": "empty"}
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeScoringAttempt).where(KnowledgeScoringAttempt.id == UUID(key)).values(
            settled_at=datetime.now(UTC) - timedelta(minutes=3),
        ))
    retry_key = str(uuid4())
    retry = await claim(retry_key)
    assert retry["attempt"] == 2 and retry["scorer"] == named
    assert (await settle(key, label="verified")).json() == {"status": "failed", "reason": "judge_failed"}
    await configure("off", 7)
    assert (await settle(retry_key, label="verified")).json() == {"status": "failed", "reason": "policy_revoked"}
    await configure("auto", 7)
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeScoringAttempt).where(KnowledgeScoringAttempt.id == UUID(retry_key)).values(
            settled_at=datetime.now(UTC) - timedelta(minutes=3),
        ))
    expired_key = str(uuid4())
    assert (await claim(expired_key))["attempt"] == 3
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeScoringAttempt).where(KnowledgeScoringAttempt.id == UUID(expired_key)).values(
            deadline=datetime.now(UTC) - timedelta(minutes=3),
        ))
    last_key = str(uuid4())
    assert (await claim(last_key))["attempt"] == 4
    assert (await settle(expired_key, label="verified")).json() == {"status": "failed", "reason": "lease_expired"}
    result = await settle(last_key, label="verified")
    assert result.status_code == 200 and result.json()["label"]["driver"] == "claude"
    assert (await settle(last_key, label="verified")).json() == result.json()
    assert (await settle(last_key, label="missing")).status_code == 409
    assert await claim() == {"status": "empty"}


async def test_local_scoring_publishes_labels_without_exposing_or_storing_transcript(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他证据项目"))["id"])}
    local = root + "/session-scoring/local-subjects"
    evidence = {"runtime": "codex", "subject_key": "a" * 64, "digest": "d" * 64}
    rule = {
        "prompt": "Check verification.", "passing_score": 1, "sessions": True, "sampling_rate": 100,
        "labels": [{"value": "pass", "score": 1, "description": "Found."},
                   {"value": "fail", "score": 0, "description": "Missing."}],
    }
    scorer = await runs.client.put(root + "/session-scorers/review", params=scope, headers=runs.headers(), json=rule)
    assert scorer.status_code == 200, scorer.text
    plan = await runs.client.post(root + "/session-scorers/plan", params=scope, headers=runs.headers(),
                                  json={"scorers": ["review"]})
    assert plan.json() == {"items": [scorer.json()]}
    rejected = await runs.client.post(local, params=scope, headers=runs.headers(),
                                      json=evidence | {"turns": [{"text": "private content"}]})
    assert rejected.status_code == 422
    registered = await runs.client.post(local, params=scope, headers=runs.headers(), json=evidence)
    assert registered.status_code == 201, registered.text
    result = registered.json()
    identifier = result["id"]
    assert result["revision"] == 1 and result["evidence"] == "local" and "turns" not in result
    repeated = await runs.client.post(local, params=scope, headers=runs.headers(), json=evidence)
    assert repeated.json() == result
    revision = await runs.client.post(local, params=scope, headers=runs.headers(), json=evidence | {"digest": "e" * 64})
    assert revision.json()["id"] == identifier and revision.json()["revision"] == 2
    detail = root + f"/shared-sessions/{identifier}"
    labels = detail + "/labels"
    params = scope | {"revision": "1"}
    assert (await runs.client.get(detail, params=params)).status_code == 404
    adoption = await runs.client.post(root + "/adoption/candidates", params=scope, headers=runs.headers(), json={
        "session": str(uuid4()), "archive": {"archive_id": identifier, "revision": 1},
    })
    assert adoption.status_code == 404 and adoption.json()["detail"]["code"] == "KNOWLEDGE_TRANSCRIPT_NOT_FOUND"
    assert (await runs.client.get(root + "/shared-sessions", params=scope)).json() == {"items": []}
    assert (await runs.client.get(local, params=scope)).json() == {"items": []}
    policy = await runs.client.put(root + "/settings/session-scoring/attended", headers=runs.headers(),
                                   json={"action": "auto"})
    assert policy.status_code == 200
    claim = await runs.client.post(root + "/session-scoring/claim", params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "run_mode": "attended",
    })
    assert claim.json() == {"status": "empty"}
    label = {"scorer_id": scorer.json()["id"], "scorer_revision": 1,
             "operation_id": str(uuid4()), "driver": "human", "label": "pass"}
    async with member(runs, "会话标签读取者") as (teammate, _):
        assert (await teammate.get(labels, params=params)).status_code == 404
        written = await runs.client.post(labels, params=params, headers=runs.headers(), json=label)
        assert written.status_code == 201, written.text
        assert written.json()["evidence"] == "local"
        assert (await teammate.get(labels, params=params)).json() == {"items": [written.json()]}
        assert (await teammate.get(detail, params=params)).status_code == 404
        assert (await teammate.post(labels, params=params, headers=headers(teammate), json=label)).status_code == 404
        catalog = await teammate.get(local, params=scope)
        [row] = catalog.json()["items"]
        assert row["id"] == identifier and row["revision"] == 1 and "digest" not in row
    assert (await runs.client.get(labels, params=other | {"revision": "1"})).status_code == 404
    async with runs.sessions() as db:
        subject = await db.get(KnowledgeSharedSession, UUID(identifier))
        version = await db.get(KnowledgeSharedSessionRevision, (UUID(identifier), 1))
        assert subject is not None and subject.score_only
        assert version is not None and version.turns == [] and version.turn_count == 0 and version.title == "本机会话"


async def test_automatic_local_scoring_requires_device_reference_to_the_exact_version(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "另一个评分运行范围"))["id"])}
    base = root + "/session-scoring"
    evidence = {"runtime": "codex", "subject_key": "a" * 64, "digest": "d" * 64, "run_mode": "attended"}
    register = await runs.client.post(base + "/local-subjects", params=scope, headers=runs.headers(), json=evidence)
    assert register.status_code == 403
    for mode in ("attended", "unattended"):
        policy = await runs.client.put(root + f"/settings/session-scoring/{mode}", headers=runs.headers(),
                                       json={"action": "auto"})
        assert policy.status_code == 200
    scorer = await runs.client.put(root + "/session-scorers/review", params=scope, headers=runs.headers(), json={
        "prompt": "Check for a verification step.", "passing_score": 1, "sessions": True, "sampling_rate": 100,
        "labels": [{"value": "pass", "score": 1, "description": "Verified."},
                   {"value": "fail", "score": 0, "description": "Missing."}],
    })
    assert scorer.status_code == 200
    stored = await runs.client.post(base + "/local-subjects", params=scope, headers=runs.headers(), json=evidence)
    assert stored.status_code == 201 and stored.json()["run_mode"] == "attended"
    changed_mode = await runs.client.post(base + "/local-subjects", params=scope, headers=runs.headers(),
                                          json=evidence | {"run_mode": "unattended"})
    assert changed_mode.status_code == 409
    ref = {"id": stored.json()["id"], "revision": 1, "digest": evidence["digest"]}

    async def claim(references: list[dict[str, object]], **overrides: object) -> dict[str, object]:
        payload = {"operation_id": str(uuid4()), "run_mode": "attended", "local_evidence": references} | overrides
        response = await runs.client.post(base + "/claim", params=scope, headers=runs.headers(), json=payload)
        assert response.status_code == 200, response.text
        result: dict[str, object] = response.json()
        return result

    assert await claim([]) == {"status": "empty"}
    assert await claim([ref | {"digest": "e" * 64}]) == {"status": "empty"}
    assert await claim([ref], run_mode="unattended") == {"status": "empty"}
    elsewhere = await runs.client.post(base + "/claim", params=other, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "run_mode": "attended", "local_evidence": [ref],
    })
    assert elsewhere.json() == {"status": "empty"}
    key = str(uuid4())
    work = await claim([ref], operation_id=key)
    assert work["status"] == "open" and work["archive"] == stored.json()
    assert await claim([], operation_id=key) == {"status": "evidence_unavailable"}
    assert await claim([ref], operation_id=key) == work
    settle = base + f"/attempts/{key}/settle"
    finished = await runs.client.post(settle, params=scope, headers=runs.headers(), json={"label": "pass"})
    assert finished.status_code == 200 and finished.json()["label"]["evidence"] == "local"
    repeated = await runs.client.post(settle, params=scope, headers=runs.headers(), json={"label": "pass"})
    assert repeated.json() == finished.json()


async def test_scoring_offer_samples_open_standards_without_starting_or_writing(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他评分询问项目"))["id"])}
    link = root + "/session-scoring/offer"
    local = {"runtime": "codex", "subject_key": "d" * 64, "digest": "e" * 64}
    request = {"run_mode": "attended", "subject": {"kind": "local", **local}}
    rubric = {
        "prompt": "Check whether the session verified its answer.", "passing_score": 1, "sessions": True,
        "sampling_rate": 100, "labels": [{"value": "pass", "score": 1, "description": "Verified."},
                                         {"value": "fail", "score": 0, "description": "Missing."}],
    }

    async def preview(**overrides: object) -> httpx.Response:
        return await runs.client.post(link, params=scope, headers=runs.headers(), json=request | overrides)

    assert (await preview()).json() == {"status": "off", "items": []}
    policy = await runs.client.put(root + "/settings/session-scoring/attended", headers=runs.headers(),
                                   json={"action": "ask", "daily_limit": 1})
    assert policy.status_code == 200
    one = await runs.client.put(
        root + "/session-scorers/verification", params=scope, headers=runs.headers(), json=rubric,
    )
    bypass = await runs.client.put(root + "/session-scorers/alternative", params=scope, headers=runs.headers(),
                                    json=rubric | {"sampling_rate": 0})
    assert one.status_code == bypass.status_code == 200
    assert (await preview()).json() == {"status": "ask", "items": [one.json()]}
    assert (await preview(scorers=["alternative"])).json() == {"status": "ask", "items": [bypass.json()]}
    blocked = await runs.client.post(root + "/session-scoring/claim", params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "run_mode": "attended",
    })
    assert blocked.json() == {"status": "ask"}
    async with runs.sessions() as db:
        assert (await db.scalars(select(KnowledgeSharedSession))).first() is None
        assert (await db.scalars(select(KnowledgeScoringAttempt))).first() is None
        assert (await db.scalars(select(KnowledgeSessionLabel))).first() is None

    recorded = await runs.client.post(root + "/session-scoring/local-subjects", params=scope, headers=runs.headers(),
                                      json=local)
    assert recorded.status_code == 201
    identifier = recorded.json()["id"]
    labels = await runs.client.post(root + f"/shared-sessions/{identifier}/labels", params=scope | {"revision": 1},
                                    headers=runs.headers(), json={
        "scorer_id": one.json()["id"], "scorer_revision": 1, "operation_id": str(uuid4()),
        "label": "pass", "driver": "human",
    })
    assert labels.status_code == 201
    assert (await preview()).json() == {"status": "ask", "items": []}
    # 即使同一个标准已被手工评分，用户点名仍可创建下一次有审计记录的判定。
    assert (await preview(scorers=["verification"])).json() == {"status": "ask", "items": [one.json()]}
    archive = await runs.client.post(root + "/shared-sessions", params=scope, headers=runs.headers(), json={
        "runtime": "codex", "session": str(uuid4()), "title": "Archived",
        "turns": [{"role": "assistant", "text": "Checked."}],
    })
    assert archive.status_code == 201
    archived = {"kind": "archive", "archive_id": archive.json()["id"], "revision": 1}
    assert (await preview(subject=archived)).json() == {"status": "ask", "items": [one.json()]}
    denied = await runs.client.post(link, params=other, headers=runs.headers(), json=request | {"subject": archived})
    assert denied.status_code == 404


async def test_batch_scoring_charges_one_call_and_settles_all_labels_atomically(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    policy = root + "/settings/session-scoring/attended"
    base = root + "/session-scoring"

    async def configure(limit: int) -> None:
        response = await runs.client.put(policy, headers=runs.headers(), json={
            "action": "auto", "daily_limit": limit, "batch": True,
        })
        assert response.status_code == 200 and response.json()["batch"] is True

    async def claim(key: str, *, compatible: bool = True) -> dict[str, object]:
        response = await runs.client.post(base + "/claim", params=scope, headers=runs.headers(), json={
            "operation_id": key, "run_mode": "attended", "accept_batch": compatible,
        })
        assert response.status_code == 200, response.text
        data: dict[str, object] = response.json()
        return data

    await configure(1)
    rubric = {
        "prompt": "Verify the result.", "passing_score": 1, "sessions": True, "sampling_rate": 100,
        "tool": "claude", "model": "common-model", "labels": [
            {"value": "pass", "score": 1, "description": "Verified."},
            {"value": "fail", "score": 0, "description": "Missing."},
        ],
    }
    for name, model in (("accuracy", "common-model"), ("evidence", "common-model"), ("readability", "other-model")):
        created = await runs.client.put(root + f"/session-scorers/{name}", params=scope, headers=runs.headers(),
                                        json=rubric | {"model": model})
        assert created.status_code == 200, created.text
    shared = await runs.client.post(root + "/shared-sessions", params=scope, headers=runs.headers(), json={
        "runtime": "codex", "session": str(uuid4()), "title": "Review",
        "turns": [{"role": "assistant", "text": "Verified the result."}],
    })
    assert shared.status_code == 201
    operation = str(uuid4())
    plan = await claim(operation)
    members = plan["batch"]
    assert isinstance(members, list) and [row["scorer"]["name"] for row in members] == ["accuracy", "evidence"]
    assert await claim(operation) == plan
    assert await claim(str(uuid4())) == {"status": "daily_limit"}
    attempt_ids = [row["attempt_id"] for row in members]
    blocked = await runs.client.post(base + f"/attempts/{attempt_ids[0]}/settle", params=scope,
                                     headers=runs.headers(), json={"label": "pass"})
    assert blocked.status_code == 409
    settle = base + f"/batches/{operation}/settle"

    async def submit(values: dict[str, str]) -> httpx.Response:
        return await runs.client.post(settle, params=scope, headers=runs.headers(), json={"labels": values})

    assert (await submit({attempt_ids[0]: "pass"})).status_code == 422
    assert (await submit({identifier: "invalid" for identifier in attempt_ids})).status_code == 422
    group_order = sorted(attempt_ids)
    assert (await submit({group_order[0]: "pass", group_order[1]: "invalid"})).status_code == 422
    async with runs.sessions() as db:
        assert (await db.scalars(select(KnowledgeSessionLabel))).first() is None
        attempts = list(await db.scalars(select(KnowledgeScoringAttempt)))
        assert len(attempts) == 2 and all(row.state == "open" for row in attempts)
    labels = {group_order[0]: "pass", group_order[1]: "fail"}
    accepted = await submit(labels)
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["status"] == "completed" and len(result["items"]) == 2
    assert all(row["label"]["judge_call_id"] == operation for row in result["items"])
    assert (await submit(labels)).json() == result
    await configure(2)
    next_call = await claim(str(uuid4()), compatible=False)
    standard = next_call.get("scorer")
    assert isinstance(standard, dict) and standard["name"] == "readability"
    assert await claim(str(uuid4())) == {"status": "daily_limit"}

    await configure(5)
    second = await runs.client.post(root + "/shared-sessions", params=scope, headers=runs.headers(), json={
        "runtime": "codex", "session": str(uuid4()), "title": "Retry batch",
        "turns": [{"role": "assistant", "text": "Verified the second subject."}],
    })
    assert second.status_code == 201
    failed_key = str(uuid4())
    assert "batch" in await claim(failed_key)
    failed = await runs.client.post(base + f"/batches/{failed_key}/settle", params=scope,
                                    headers=runs.headers(), json={"error": "judge_failed"})
    assert failed.status_code == 200 and failed.json()["status"] == "failed"
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeScoringAttempt)
                         .where(KnowledgeScoringAttempt.judge_call_id == UUID(failed_key))
                         .values(settled_at=datetime.now(UTC) - timedelta(minutes=3)))
    updated = await runs.client.put(root + "/session-scorers/accuracy", params=scope, headers=runs.headers(),
                                     json=rubric | {"prompt": "Updated accuracy standard."})
    assert updated.json()["revision"] == 2
    retried = (await claim(str(uuid4())))["batch"]
    assert isinstance(retried, list)
    assert [(row["attempt"], row["scorer"]["revision"]) for row in retried] == [(2, 1), (2, 1)]


async def test_archived_adoption_scores_only_the_authors_recalls_before_capture(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "另一项目"))["id"])}
    session = str(uuid4())
    policy = path + "/settings/session-context/recall-adoption"
    assert (await runs.client.put(policy, headers=runs.headers(), json={"enabled": True})).status_code == 200

    async def post(suffix: str, **payload: object) -> httpx.Response:
        return await runs.client.post(path + suffix, params=scope, headers=runs.headers(), json=payload)

    early = await post("", operation_id=str(uuid4()), title="electrum telescope", body="Initial guidance.")
    late = await post("", operation_id=str(uuid4()), title="zircon compass", body="Later guidance.")
    assert early.status_code == late.status_code == 201
    early_id, late_id = early.json()["id"], late.json()["id"]
    assert (await post("/recall", session=session, query="electrum telescope", recall_mode=True)).status_code == 200
    capture = datetime.now(UTC).isoformat()
    archive_body = {
        "session": session, "runtime": "codex", "title": "Checked snapshot",
        "turns": [{"role": "assistant", "text": "Used the electrum example."}],
    }
    legacy = await post("/shared-sessions", **archive_body)
    assert legacy.status_code == 201 and legacy.json()["captured_at"] is None
    archive_id = legacy.json()["id"]
    denied = await post("/adoption/candidates", session=session, archive={"archive_id": archive_id, "revision": 1})
    assert denied.status_code == 422 and denied.json()["detail"]["code"] == "KNOWLEDGE_ADOPTION_ARCHIVE_TIME"

    published = await post("/shared-sessions", **archive_body, captured_at=capture)
    assert published.status_code == 201 and published.json()["revision"] == 2 and published.json()["id"] == archive_id
    replay = await post("/shared-sessions", **archive_body, captured_at=capture)
    assert replay.json() == published.json()
    assert (await post("/recall", session=session, query="zircon compass", recall_mode=True)).status_code == 200
    newer = await post("/shared-sessions", **(archive_body | {"title": "Third version"}),
                       captured_at=datetime.now(UTC).isoformat())
    assert newer.status_code == 201 and newer.json()["revision"] == 3

    evidence = {"archive_id": archive_id, "revision": 2}
    plan = await post("/adoption/candidates", session=session, archive=evidence)
    assert plan.status_code == 200, plan.text
    assert set(row["id"] for row in plan.json()["items"]) == {early_id}
    assert plan.json()["archive"]["revision"] == 2 and "turns" not in plan.json()["archive"]
    wrong_root = await post("/adoption/candidates", session=str(uuid4()), archive=evidence)
    assert wrong_root.status_code == 422
    wrong_project = await runs.client.post(path + "/adoption/candidates", params=other, headers=runs.headers(),
                                          json={"session": session, "archive": evidence})
    assert wrong_project.status_code == 404
    async with member(runs, "团队归档读者") as (teammate, _):
        assert (await teammate.get(f"{path}/shared-sessions/{archive_id}", params=scope)).status_code == 200
        someone_elses = await teammate.post(path + "/adoption/candidates", params=scope, headers=headers(teammate),
                                           json={"session": session, "archive": evidence})
        assert someone_elses.status_code == 422

    result = await post("/adoption/verdicts", session=session, archive=evidence, verdicts=[
        {"entry_id": early_id, "revision": 1, "label": "adopted"},
        {"entry_id": late_id, "revision": 1, "label": "ignored"},
    ])
    assert result.json() == {"applied": [early_id], "skipped": [late_id]}
    async with runs.sessions() as db:
        event = await db.scalar(select(KnowledgeEvent).where(KnowledgeEvent.action == "judge_adopted"))
        assert event is not None and event.revision == 1
        evidence_audit = json.loads(event.reason)
        assert evidence_audit["archive_id"] == archive_id and evidence_audit["archive_revision"] == 2
        assert session not in event.reason


async def test_finished_adoption_pins_recalled_version_and_reuses_scoring_quota(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他项目"))["id"])}
    switch = root + "/settings/session-context/recall-adoption"
    settings = root + "/settings/session-scoring/attended"
    session = str(uuid4())

    async def post(path: str, **body: object) -> httpx.Response:
        result = await runs.client.post(root + path, params=scope, headers=runs.headers(), json=body)
        assert result.is_success, result.text
        return result

    async def claim(operation: str, **body: object) -> dict[str, object]:
        result = await post("/session-scoring/claim", operation_id=operation, run_mode="attended", **body)
        receipt: dict[str, object] = result.json()
        return receipt

    async def set_limit(limit: int) -> None:
        policy = await runs.client.put(settings, headers=runs.headers(), json={"action": "auto", "daily_limit": limit})
        assert policy.status_code == 200, policy.text

    await set_limit(2)
    early = (await post("", operation_id=str(uuid4()), title="electrum telescope", body="Original guideline.")).json()
    opened = (await post("", operation_id=str(uuid4()), title="zircon compass", body="Already opened.")).json()
    late = (await post("", operation_id=str(uuid4()), title="cerulean mortar", body="Recalled too late.")).json()
    for query in ("electrum telescope", "zircon compass"):
        await post("/recall", query=query, session=session, recall_mode=True)
    await post(f"/{opened['id']}/open", session=session)
    capture = datetime.now(UTC).isoformat()
    archive = {
        "session": session, "runtime": "codex", "title": "Original capture", "captured_at": capture,
        "turns": [{"role": "assistant", "text": "Used the original guideline."}],
    }
    original = (await post("/shared-sessions", **archive)).json()
    await post("/recall", query="cerulean mortar", session=session, recall_mode=True)
    assert await claim(str(uuid4()), accept_adoption=True) == {"status": "empty"}
    assert (await runs.client.put(switch, headers=runs.headers(), json={"enabled": True})).status_code == 200
    assert await claim(str(uuid4())) == {"status": "empty"}
    operation = str(uuid4())
    first = await claim(operation, accept_adoption=True)
    assert first["status"] == "open" and first["archive"] == original
    candidate = first["adoption"]
    assert isinstance(candidate, dict) and candidate["id"] == early["id"] and candidate["revision"] == 1
    assert candidate["excerpt"] == "Original guideline." and session not in str(first)
    assert await claim(operation, accept_adoption=True) == first
    assert await claim(str(uuid4()), accept_adoption=True) == {"status": "empty"}
    settle = f"/adoption/attempts/{operation}/settle"
    cross_project = await runs.client.post(root + settle, params=other, headers=runs.headers(),
                                          json={"label": "adopted"})
    assert cross_project.status_code == 404
    async with member(runs, "另一位评分成员") as (teammate, _):
        denied = await teammate.post(root + settle, params=scope, headers=headers(teammate),
                                     json={"label": "adopted"})
        assert denied.status_code == 404
    assert (await post(settle, error="judge_failed")).json()["status"] == "failed"

    # 会话和知识都发布新版本，失败问题在下次领取时仍使用同一组证据。
    second = (await post("/shared-sessions", **(archive | {"title": "Updated capture"}))).json()
    assert second["revision"] == 2
    await post(f"/{early['id']}/revisions", operation_id=str(uuid4()), base_revision=1,
               title="electrum telescope", body="Updated guideline.")
    await post(f"/{early['id']}/review", revision=2, decision="accept")
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeAdoptionAttempt).where(KnowledgeAdoptionAttempt.id == UUID(operation))
                         .values(settled_at=datetime.now(UTC) - timedelta(minutes=2)))
    retry = str(uuid4())
    resumed = await claim(retry, accept_adoption=True)
    assert resumed["attempt"] == 2 and resumed["archive"] == original and resumed["adoption"] == candidate
    settled = f"/adoption/attempts/{retry}/settle"
    outcome = (await post(settled, label="adopted")).json()
    assert outcome == {"status": "completed", "adoption": {
        "id": early["id"], "revision": 1, "label": "adopted", "applied": True,
    }}
    assert (await post(settled, label="adopted")).json() == outcome
    assert await claim(str(uuid4()), accept_adoption=True) == {"status": "daily_limit"}

    # 本机转录只有拥有精确证据指纹的设备可以领取，直接打开优先于在途模型判定。
    await set_limit(4)
    local_session = str(uuid4())
    await post("/recall", query="electrum telescope", session=local_session, recall_mode=True)
    body = {
        "runtime": "codex", "subject_key": "a" * 64, "digest": "b" * 64, "run_mode": "attended",
        "recall_session_key": hashlib.sha256(local_session.encode()).hexdigest(),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    local = (await post("/session-scoring/local-subjects", **body)).json()
    assert await claim(str(uuid4()), accept_adoption=True) == {"status": "empty"}
    local_id = str(uuid4())
    reference = {key: local[key] for key in ("id", "revision", "digest")}
    private = await claim(local_id, accept_adoption=True, local_evidence=[reference])
    assert private["archive"] == local
    assert await claim(local_id, accept_adoption=True) == {"status": "evidence_unavailable"}
    await post(f"/{early['id']}/open", session=local_session)
    local_result = (await post(f"/adoption/attempts/{local_id}/settle", label="ignored")).json()
    assert local_result["adoption"]["applied"] is False
    assert await claim(str(uuid4()), accept_adoption=True, local_evidence=[reference]) == {"status": "empty"}

    disabled_session = str(uuid4())
    await post("/recall", query="electrum telescope", session=disabled_session, recall_mode=True)
    revoked_archive = archive | {"session": disabled_session, "captured_at": datetime.now(UTC).isoformat()}
    await post("/shared-sessions", **revoked_archive)
    disabled = str(uuid4())
    assert (await claim(disabled, accept_adoption=True))["status"] == "open"
    assert (await runs.client.put(switch, headers=runs.headers(), json={"enabled": False})).status_code == 200
    revoked = await post(f"/adoption/attempts/{disabled}/settle", label="adopted")
    assert revoked.json() == {"status": "failed", "reason": "policy_revoked"}

    async with runs.sessions() as db:
        events = list(await db.scalars(select(KnowledgeEvent).where(KnowledgeEvent.action == "judge_adopted")))
        assert len(events) == 1 and events[0].revision == 1
        audit = json.loads(events[0].reason)
        assert audit["attempt_id"] == retry and audit["archive_id"] == original["id"]
        assert session not in events[0].reason
        not_judged = await db.get(KnowledgeRecall, (UUID(late["id"]), UUID(runs.user_id), hashlib.sha256(
            session.encode(),
        ).hexdigest()))
        assert not_judged is not None and not_judged.positive is None


async def test_adoption_batch_fixed_members_and_atomic_settlement(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    settings = root + "/settings/session-scoring/attended"
    session = str(uuid4())

    async def post(path: str, **payload: object) -> httpx.Response:
        return await runs.client.post(root + path, params=scope, headers=runs.headers(), json=payload)

    async def claim(key: str, *, batch: bool = True) -> httpx.Response:
        return await post("/session-scoring/claim", operation_id=key, run_mode="attended",
                          accept_adoption=True, accept_adoption_template=True, accept_adoption_batch=batch)

    async def limit(value: int) -> None:
        response = await runs.client.put(settings, headers=runs.headers(),
                                         json={"action": "auto", "daily_limit": value, "batch": False})
        assert response.status_code == 200 and response.json().get("batch") is not True

    await limit(1)
    enabled = await runs.client.put(root + "/settings/session-context/recall-adoption", headers=runs.headers(),
                                    json={"enabled": True})
    assert enabled.status_code == 200
    entries = {}
    for title in ("spectral mirror", "crimson furnace"):
        created = await post("", operation_id=str(uuid4()), title=title, body=f"Use the original {title}.")
        assert created.status_code == 201, created.text
        entries[title] = created.json()["id"]
        recalled = await post("/recall", query=title, session=session, recall_mode=True)
        assert recalled.status_code == 200 and recalled.json()["items"][0]["id"] == entries[title]
    archived = await post("/shared-sessions", session=session, runtime="codex", title="Two recalled docs",
                          captured_at=datetime.now(UTC).isoformat(),
                          turns=[{"role": "assistant", "text": "Used the spectral mirror."}])
    assert archived.status_code == 201, archived.text
    operation = str(uuid4())
    request = await claim(operation)
    assert request.status_code == 200, request.text
    first = request.json()
    members = first["adoption_batch"]
    assert len(members) == 2 and {row["adoption"]["id"] for row in members} == set(entries.values())
    assert first["archive"] == archived.json()
    assert (await claim(operation)).json() == first
    assert (await claim(operation, batch=False)).status_code == 409
    assert (await claim(str(uuid4()))).json() == {"status": "daily_limit"}
    ids = [row["attempt_id"] for row in members]
    cross_member = next(identifier for identifier in ids if identifier != operation)
    assert (await claim(cross_member)).status_code == 409
    assert (await post(f"/adoption/attempts/{cross_member}/settle", label="adopted")).status_code == 409
    finish = f"/adoption/batches/{operation}/settle"
    assert (await post(finish, labels={ids[0]: "adopted"})).status_code == 422
    assert (await post(finish, labels={ids[0]: "adopted", ids[1]: "invalid"})).status_code == 422
    async with runs.sessions() as db:
        jobs = list(await db.scalars(select(KnowledgeAdoptionAttempt)))
        assert len(jobs) == 2 and all(row.state == "open" for row in jobs)
        event = await db.scalar(select(KnowledgeEvent.id).where(KnowledgeEvent.action == "judge_adopted"))
        assert event is None

    failed = await post(finish, error="judge_failed")
    assert failed.status_code == 200 and failed.json()["status"] == "failed"
    # 更新团队标准后，续做仍使用第一次领取的内置标准和原始知识版本。
    template = await runs.client.put(root + "/session-scorers/teamai-adoption", params=scope, headers=runs.headers(),
                                     json={
        "prompt": "Review recalled/{{ doc.file }}.", "passing_score": 1, "labels": [
            {"value": "pass", "score": 1, "description": "New template pass."},
            {"value": "fail", "score": 0, "description": "New template fail."},
        ],
    })
    assert template.status_code == 200, template.text
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeAdoptionAttempt)
                         .where(KnowledgeAdoptionAttempt.judge_call_id == UUID(operation))
                         .values(settled_at=datetime.now(UTC) - timedelta(minutes=2)))
    await limit(2)
    retry = str(uuid4())
    requested = await claim(retry)
    assert requested.status_code == 200, requested.text
    replayed = requested.json()["adoption_batch"]
    assert [row["adoption"] for row in replayed] == [row["adoption"] for row in members]
    assert all(row["attempt"] == 2 for row in replayed)
    labels = {row["attempt_id"]: "adopted" if row["adoption"]["id"] == entries["spectral mirror"] else "ignored"
              for row in replayed}
    done_path = f"/adoption/batches/{retry}/settle"
    completed = await post(done_path, labels=labels)
    assert completed.status_code == 200, completed.text
    result = completed.json()
    assert result["status"] == "completed"
    assert {row["adoption"]["label"] for row in result["items"]} == {"adopted", "ignored"}
    assert all(row["adoption"]["applied"] and row["adoption"]["judge_call_id"] == retry for row in result["items"])
    assert (await post(done_path, labels=labels)).json() == result
    assert (await claim(str(uuid4()))).json() == {"status": "daily_limit"}
    async with runs.sessions() as db:
        events = list(await db.scalars(select(KnowledgeEvent).where(
            KnowledgeEvent.action.in_(("judge_adopted", "judge_ignored")),
        )))
        assert len(events) == 2 and {json.loads(row.reason)["judge_call_id"] for row in events} == {retry}


async def test_named_adoption_plan_and_mixed_receipt_use_fixed_evidence(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    session = str(uuid4())

    async def post(path: str, **body: object) -> httpx.Response:
        return await runs.client.post(root + path, params=scope, headers=runs.headers(), json=body)

    async def recall(title: str) -> tuple[str, str]:
        added = await post("", title=title, body="Follow the specific guideline.", operation_id=str(uuid4()))
        assert added.status_code == 201, added.text
        entry = str(added.json()["id"])
        result = await post("/recall", query=title, session=session, recall_mode=True)
        assert result.status_code == 200 and result.json()["items"][0]["id"] == entry
        return entry, "teamai:adoption:" + hashlib.sha256(entry.encode()).hexdigest()[:12]

    async def publish(form: str, **body: object) -> httpx.Response:
        return await runs.client.put(root + "/session-scorers/" + form, params=scope, headers=runs.headers(), json=body)

    adopted, name = await recall("spectral mirror")
    opened, opened_name = await recall("crimson furnace")
    assert (await post(f"/{opened}/open", session=session)).status_code == 200
    capture = datetime.now(UTC).isoformat()
    shared = await post("/shared-sessions", session=session, title="Named evidence", runtime="codex",
                        captured_at=capture, turns=[{"role": "assistant", "text": "Followed the guideline."}])
    assert shared.status_code == 201, shared.text
    archived = {"kind": "archive", "archive_id": shared.json()["id"], "revision": 1}
    _, late = await recall("azure lantern")
    assert (await post("/session-scoring/named-plan", subject=archived, scorers=[late])).status_code == 404
    scale = [{"value": "pass", "score": 1, "description": "Good."},
             {"value": "fail", "score": 0, "description": "Evidence missing."}]
    standard = await publish("quality", prompt="Review evidence.", passing_score=1, labels=scale,
                             sessions=True, sampling_rate=100)
    assert standard.status_code == 200, standard.text
    plan = await post("/session-scoring/named-plan", subject=archived, scorers=["quality", name, opened_name])
    assert plan.status_code == 200, plan.text
    regular, question, credited = plan.json()["items"]
    assert question["adoption"]["id"] == adopted and credited["adoption"]["id"] == opened
    policy_url = root + "/settings/session-scoring/attended"
    configured = await runs.client.put(policy_url, headers=runs.headers(),
                                       json={"action": "ask", "daily_limit": 2, "batch": True})
    assert configured.status_code == 200
    suggested = await post("/session-scoring/offer", subject=archived, run_mode="attended")
    assert suggested.status_code == 200
    assert [item["name"] for item in suggested.json()["items"]] == ["quality"]
    explicit = await post("/session-scoring/offer", subject=archived, run_mode="attended", scorers=[
        "quality", name, opened_name,
    ])
    assert explicit.status_code == 200 and explicit.json()["items"] == plan.json()["items"]
    assert explicit.json()["status"] == "ask" and explicit.json()["batch"] is True
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeNamedScoreCall.id)) is None

    async with member(runs, "Other author") as (other, _):
        hidden = await other.post(root + "/session-scoring/named-plan", params=scope, headers=headers(other),
                                  json={"subject": archived, "scorers": [name]})
        assert hidden.status_code == 404
    enabled = await runs.client.put(root + "/settings/session-context/recall-adoption",
                                    headers=runs.headers(), json={"enabled": True})
    assert enabled.status_code == 200
    override = await publish("teamai-adoption", prompt="Override {{ doc.id }}.", passing_score=1, labels=scale)
    assert override.status_code == 200
    labels = [{"name": "quality", "label": "pass", "driver": "codex",
               "scorer_id": regular["id"], "scorer_revision": 1}]
    labels.extend({
        "name": item["name"], "label": "adopted", "driver": "codex", "entry_id": item["adoption"]["id"],
        "entry_revision": 1, "source_hash": item["source_hash"],
    } for item in (question, credited))
    op = str(uuid4())
    body = {"subject": archived, "operation_id": op, "items": labels, "offer_mode": "attended"}
    disabled = await runs.client.put(policy_url, headers=runs.headers(), json={"action": "off"})
    assert disabled.status_code == 200
    assert (await post("/session-scoring/named-calls", **body)).status_code == 403
    restored = await runs.client.put(policy_url, headers=runs.headers(), json={"action": "ask"})
    assert restored.status_code == 200
    invalid = await post("/session-scoring/named-calls", **(body | {"items": [labels[0] | {"label": "invalid"},
                                                                           *labels[1:]]}))
    assert invalid.status_code == 422
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeNamedScoreCall.id)) is None
        assert await db.scalar(select(KnowledgeEvent.id).where(KnowledgeEvent.action == "judge_adopted")) is None
    done = await post("/session-scoring/named-calls", **body)
    assert done.status_code == 200, done.text
    assert [item.get("applied") for item in done.json()["items"]] == [None, True, False]
    assert (await post("/session-scoring/named-calls", **body)).json() == done.json()
    replay = await runs.client.get(root + f"/session-scoring/named-calls/{op}", params=scope)
    assert replay.json() == done.json()
    assert (await post("/session-scoring/named-calls", **(body | {
        "items": [labels[0], labels[1] | {"label": "ignored"}, labels[2]],
    }))).status_code == 409
    policy = await runs.client.put(root + "/settings/session-scoring/attended", headers=runs.headers(),
                                  json={"action": "auto", "daily_limit": 2})
    assert policy.status_code == 200
    newer = await publish("quality", prompt="Review v2.", passing_score=1, labels=scale,
                          sessions=True, sampling_rate=100, base_revision=1)
    assert newer.status_code == 200
    first = await post("/session-scoring/claim", run_mode="attended", operation_id=str(uuid4()))
    assert first.status_code == 200 and first.json()["status"] == "open"
    limit = await post("/session-scoring/claim", run_mode="attended", operation_id=str(uuid4()))
    assert limit.json()["status"] == "daily_limit"

    private = {"kind": "local", "runtime": "codex", "subject_key": "a" * 64, "digest": "b" * 64,
               "recall_session_key": hashlib.sha256(session.encode()).hexdigest(), "captured_at": capture}
    trial = await post("/session-scoring/named-plan", subject=private, scorers=[name])
    assert trial.status_code == 200, trial.text
    key = trial.json()["items"][0]
    rubric = key["adoption"]["rubric"]
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeSharedSession.id).where(KnowledgeSharedSession.score_only)) is None
    private_pin = {
        "name": name, "entry_id": adopted, "entry_revision": 1, "scorer_id": rubric["id"],
        "scorer_revision": rubric["revision"], "source_hash": key["source_hash"], "label": "pass", "driver": "human",
    }
    private_op = str(uuid4())
    private_result = await post("/session-scoring/named-calls", subject=private, operation_id=private_op,
                                items=[private_pin])
    assert private_result.status_code == 200, private_result.text
    assert private_result.json()["items"][0]["applied"] is False
    recaptured = private | {"captured_at": datetime.now(UTC).isoformat()}
    replayed = await post("/session-scoring/named-calls", subject=recaptured,
                          operation_id=private_op, items=[private_pin])
    assert replayed.json() == private_result.json()
    async with runs.sessions() as db:
        private_archive = await db.get(KnowledgeSharedSession, UUID(private_result.json()["subject_id"]))
        assert private_archive is not None and private_archive.score_only
        snapshot = await db.get(KnowledgeSharedSessionRevision, (private_archive.id, 1))
        assert snapshot is not None and snapshot.turns == []


async def test_mixed_scoring_uses_one_call_and_recovers_both_families(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    settings = root + "/settings/session-scoring/attended"
    adoption_switch = root + "/settings/session-context/recall-adoption"

    async def post(path: str, **payload: object) -> httpx.Response:
        return await runs.client.post(root + path, params=scope, headers=runs.headers(), json=payload)

    async def put(path: str, payload: dict[str, object], *, workspace: bool = False) -> dict[str, object]:
        reply = await runs.client.put(path, params={} if workspace else scope, headers=runs.headers(), json=payload)
        assert reply.status_code == 200, reply.text
        result: dict[str, object] = reply.json()
        return result

    async def claim(operation: str, *, mixed: bool = True) -> httpx.Response:
        return await post("/session-scoring/claim", operation_id=operation, run_mode="attended", accept_batch=True,
                          accept_adoption=True, accept_adoption_template=True, accept_adoption_batch=True,
                          accept_mixed_batch=mixed)

    await put(settings, {"action": "auto", "daily_limit": 1, "batch": True}, workspace=True)
    await put(adoption_switch, {"enabled": True}, workspace=True)
    prefix = root + "/session-scorers/"
    standard: dict[str, object] = {
        "prompt": "Verify the result.", "passing_score": 1, "tool": "claude", "model": "shared-model",
        "sessions": True, "sampling_rate": 100,
        "labels": [
            {"value": "verified", "score": 1, "description": "Verified evidence."},
            {"value": "missing", "score": 0, "description": "Evidence missing."},
        ],
    }
    await put(prefix + "a-quality", standard)
    await put(prefix + "z-different-model", standard | {"model": "other-model"})
    await put(prefix + "teamai-adoption", standard | {"prompt": "Judge recalled/{{ doc.file }}."})
    session = str(uuid4())
    entries = set()
    for title in ("spectral mirror", "crimson furnace"):
        created = await post("", operation_id=str(uuid4()), title=title, body=f"Use {title}.")
        assert created.status_code == 201, created.text
        entries.add(created.json()["id"])
        recalled = await post("/recall", query=title, session=session, recall_mode=True)
        assert recalled.status_code == 200 and recalled.json()["items"][0]["id"] == created.json()["id"]
    archive = await post("/shared-sessions", session=session, runtime="codex", title="Mixed assessment",
                         captured_at=datetime.now(UTC).isoformat(),
                         turns=[{"role": "assistant", "text": "Verified the spectral mirror."}])
    assert archive.status_code == 201
    operation = str(uuid4())
    response = await claim(operation)
    assert response.status_code == 200, response.text
    opened = response.json()
    regular, *adoptions = opened["mixed_batch"]
    assert opened["archive"] == archive.json() and regular["scorer"]["name"] == "a-quality"
    assert {member["adoption"]["id"] for member in adoptions} == entries
    assert (await claim(operation)).json() == opened
    assert (await claim(operation, mixed=False)).status_code == 409
    assert (await claim(str(uuid4()))).json() == {"status": "daily_limit"}

    finish = f"/session-scoring/batches/{operation}/settle"
    labels = {regular["attempt_id"]: "verified"} | {row["attempt_id"]: "missing" for row in adoptions}
    assert (await post(finish, labels={row["attempt_id"]: "missing" for row in adoptions})).status_code == 422
    assert (await post(f"/adoption/batches/{operation}/settle", error="judge_failed")).status_code == 409
    assert (await post(finish, labels=labels | {adoptions[-1]["attempt_id"]: "wrong"})).status_code == 422
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeSessionLabel.id)) is None
        assert await db.scalar(select(KnowledgeEvent.id).where(KnowledgeEvent.action == "judge_ignored")) is None

    await put(adoption_switch, {"enabled": False}, workspace=True)
    failed = await post(finish, labels=labels)
    assert failed.status_code == 200 and failed.json()["status"] == "failed"
    assert {item["reason"] for item in failed.json()["items"]} == {"policy_revoked"}
    async with runs.sessions() as db, db.begin():
        for attempts in (KnowledgeScoringAttempt, KnowledgeAdoptionAttempt):
            await db.execute(update(attempts).where(attempts.judge_call_id == UUID(operation))
                             .values(settled_at=datetime.now(UTC) - timedelta(minutes=2)))
    await put(adoption_switch, {"enabled": True}, workspace=True)
    await put(settings, {"action": "auto", "daily_limit": 2, "batch": False}, workspace=True)
    await put(prefix + "a-quality", standard | {"model": "new-model", "base_revision": 1})
    await put(prefix + "teamai-adoption", standard | {"prompt": "New template.", "base_revision": 1})
    retried = str(uuid4())
    response = await claim(retried)
    assert response.status_code == 200, response.text
    pinned = response.json()["mixed_batch"]
    assert pinned[0]["scorer"] == regular["scorer"] and pinned[0]["attempt"] == 2
    assert [row["adoption"] for row in pinned[1:]] == [row["adoption"] for row in adoptions]
    assert all(row["attempt"] == 2 for row in pinned)
    labels = {pinned[0]["attempt_id"]: "verified"} | {row["attempt_id"]: "missing" for row in pinned[1:]}
    done = f"/session-scoring/batches/{retried}/settle"
    response = await post(done, labels=labels)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "completed"
    assert receipt["items"][0]["label"]["judge_call_id"] == retried
    assert {row["adoption"]["judge_call_id"] for row in receipt["items"][1:]} == {retried}
    assert (await post(done, labels=labels)).json() == receipt
    assert (await claim(str(uuid4()))).json() == {"status": "daily_limit"}


async def test_team_adoption_template_applies_threshold_of_pinned_version(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}

    async def post(path: str, **payload: object) -> dict[str, object]:
        reply = await runs.client.post(root + path, params=scope, headers=runs.headers(), json=payload)
        assert reply.is_success, reply.text
        result: dict[str, object] = reply.json()
        return result

    async def publish(params: dict[str, str], payload: dict[str, object]) -> httpx.Response:
        return await runs.client.put(root + "/session-scorers/teamai-adoption", params=params,
                                     headers=runs.headers(), json=payload)

    async def claim(key: str, *, template: bool = True) -> dict[str, object]:
        return await post("/session-scoring/claim", operation_id=key, run_mode="attended",
                          accept_adoption=True, accept_adoption_template=template)

    standard: dict[str, object] = {
        "prompt": ("Evaluate {{ doc.id }} at {{ doc.path }} using recalled/{{ doc.file }}. "
                   r"Example: \{{ doc.id }} and ${{ inputs.example }}."),
        "sessions": True, "passing_score": 8, "sampling_rate": 100, "tool": "claude", "model": "fixture-model",
        "labels": [
            {"value": "partial", "score": 8, "description": "Evidence refers to {{ doc.path }}."},
            {"value": "verified-{{ doc.id }}", "score": 10, "description": "Exact excerpt {{ doc.file }}."},
            {"value": "unconfirmed", "score": 1, "description": "Evidence is unrelated."},
        ],
    }
    unknown = await publish(scope, standard | {"prompt": "Use {{ skill.name }}."})
    assert unknown.status_code == 422 and unknown.json()["detail"]["code"] == "KNOWLEDGE_ADOPTION_TEMPLATE"
    broken = await publish(scope, standard | {"prompt": "Use {{ doc.file."})
    assert broken.status_code == 422
    assert (await publish(scope, standard | {"files": []})).status_code == 422
    shared_response = await publish({}, standard)
    assert shared_response.status_code == 200, shared_response.text
    shared = shared_response.json()
    assert shared["adoption_template"] is True and len(shared["source_hash"]) == 64
    policy = await runs.client.put(root + "/settings/session-scoring/attended", headers=runs.headers(),
                                   json={"action": "auto", "daily_limit": 8})
    assert policy.status_code == 200
    enabled = await runs.client.put(root + "/settings/session-context/recall-adoption", headers=runs.headers(),
                                    json={"enabled": True})
    assert enabled.status_code == 200
    relative = 'guides/a "quoted" #1.md'
    doc = await post("", operation_id=str(uuid4()), title="spectral mirror", body="Use the fixed mirror.",
                     source={"type": "manual", "resource_path": relative})
    entry = doc["id"]

    async def freeze() -> tuple[str, dict[str, object]]:
        root_session = str(uuid4())
        await post("/recall", query="spectral mirror", recall_mode=True, session=root_session)
        archive = await post("/shared-sessions", session=root_session, runtime="codex", title="Scorer template",
                             captured_at=datetime.now(UTC).isoformat(),
                             turns=[{"role": "assistant", "text": "Used the fixed mirror."}])
        return root_session, archive

    session, frozen = await freeze()
    legacy = await post("/adoption/candidates", session=session)
    assert isinstance(legacy["items"], list) and "rubric" not in legacy["items"][0]
    preview = await post("/adoption/candidates", session=session, include_rubric=True)
    assert isinstance(preview["items"], list) and preview["items"][0]["rubric"]["id"] == shared["id"]
    override_response = await publish(scope, standard | {"prompt": "Project. " + str(standard["prompt"])})
    assert override_response.status_code == 200, override_response.text
    override = override_response.json()
    named = await runs.client.post(root + "/session-scorers/plan", params=scope,
                                   headers=runs.headers(), json={"scorers": ["teamai-adoption"]})
    assert named.status_code == 422
    assert await claim(str(uuid4()), template=False) == {"status": "empty"}
    operation = str(uuid4())
    work = await claim(operation)
    candidate = work["adoption"]
    assert isinstance(candidate, dict)
    rubric = candidate["rubric"]
    assert rubric["id"] == override["id"] and rubric["revision"] == 1
    assert rubric["prompt"].startswith(f"Project. Evaluate {entry} at {relative}")
    assert rubric["evidence_path"] in rubric["prompt"] and len(rubric["source_hash"]) == 64
    assert "Example: {{ doc.id }} and ${{ inputs.example }}." in rubric["prompt"]
    assert rubric["labels"][0]["description"] == f"Evidence refers to {relative}."
    assert rubric["labels"][1]["value"] == f"verified-{entry}"
    assert await claim(operation, template=False) == {"status": "unsupported_rubric"}
    assert await claim(operation) == work
    settle = f"/adoption/attempts/{operation}/settle"
    assert await post(settle, error="judge_failed") == {"status": "failed", "reason": "judge_failed"}
    update_response = await publish(scope, standard | {"passing_score": 9, "base_revision": 1, "tool": "codex"})
    assert update_response.status_code == 200 and update_response.json()["revision"] == 2
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeAdoptionAttempt).where(KnowledgeAdoptionAttempt.id == UUID(operation))
                         .values(settled_at=datetime.now(UTC) - timedelta(minutes=2)))
    retried = str(uuid4())
    repeated = await claim(retried)
    assert repeated["adoption"] == candidate and repeated["archive"] == frozen and repeated["attempt"] == 2
    verdict_path = f"/adoption/attempts/{retried}/settle"
    invalid = await runs.client.post(root + verdict_path, params=scope, headers=runs.headers(),
                                     json={"label": "adopted"})
    assert invalid.status_code == 422
    receipt = (await post(verdict_path, label="partial"))["adoption"]
    assert isinstance(receipt, dict) and receipt["label"] == "adopted" and receipt["applied"] is True
    assert receipt["scorer"] == {
        "id": override["id"], "revision": 1, "source_hash": rubric["source_hash"], "label": "partial",
        "driver": "claude", "requested_model": "fixture-model",
    }
    assert (await post(verdict_path, label="partial"))["adoption"] == receipt

    await freeze()
    next_job = str(uuid4())
    next_work = await claim(next_job)
    next_candidate = next_work["adoption"]
    assert isinstance(next_candidate, dict)
    assert next_candidate["rubric"]["revision"] == 2 and next_candidate["rubric"]["tool"] == "codex"
    ignored = (await post(f"/adoption/attempts/{next_job}/settle", label="partial"))["adoption"]
    assert isinstance(ignored, dict) and ignored["label"] == "ignored" and ignored["applied"] is True

    reset_path = root + "/session-scorers/teamai-adoption"
    stale = await runs.client.delete(reset_path, params=scope | {"base_revision": "1"}, headers=runs.headers())
    assert stale.status_code == 409
    reset = await runs.client.delete(reset_path, params=scope | {"base_revision": "2"}, headers=runs.headers())
    assert reset.status_code == 200 and reset.json()["revision"] == 3 and reset.json()["archived"] is True
    fresh_session, _ = await freeze()
    inherited = await post("/adoption/candidates", session=fresh_session, include_rubric=True)
    assert isinstance(inherited["items"], list) and inherited["items"][0]["rubric"]["id"] == shared["id"]
    reset_shared = await runs.client.delete(reset_path, params={"base_revision": "1"}, headers=runs.headers())
    assert reset_shared.status_code == 200 and reset_shared.json()["archived"] is True
    built_in = await claim(str(uuid4()))
    assert isinstance(built_in["adoption"], dict) and "rubric" not in built_in["adoption"]


async def test_project_recall_shadows_shared_resource_by_path_and_kind(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "结算"))["id"])}
    for selected in (scope, other):
        enabled = await runs.client.put(
            f"{path}/settings/recall-scope", params=selected, headers=runs.headers(), json={"inherit_shared": True},
        )
        assert enabled.status_code == 200, enabled.text

    async def add(title: str, kind: str, location: dict[str, str], source: dict[str, str]) -> str:
        result = await runs.client.post(path, params=location, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": "只保留有效版本。", "kind": kind, "source": source,
        })
        assert result.status_code == 201, result.text
        return str(result.json()["id"])

    async def approve(entry: str, location: dict[str, str]) -> None:
        result = await runs.client.post(
            f"{path}/{entry}/review", params=location, headers=runs.headers(),
            json={"decision": "accept", "revision": 1},
        )
        assert result.status_code == 200, result.text

    async def hits(query: str, location: dict[str, str]) -> set[str]:
        result = await runs.client.get(path, params=location | {"query": query, "recall_mode": "true"})
        assert result.status_code == 200, result.text
        return {str(row["id"]) for row in result.json()["items"]}

    origin = {"type": "file", "ref": "runbooks/lease.md"}
    shared = await add("quasar restore plan", "learning", {}, origin)
    await approve(shared, {})
    assert await hits("quasar restore", scope) == {shared}
    inherited_doc = await add("quasar restore checklist", "doc", {}, origin)
    await approve(inherited_doc, {})

    project_copy = await add("pulsar renewal procedure", "learning", scope, {
        "type": "wiki", "ref": "https://example.test/lease", "resource_path": "./runbooks\\lease.md",
    })
    assert await hits("quasar restore", scope) == {shared, inherited_doc}
    await approve(project_copy, scope)

    # 项目条目即使不命中旧查询，也覆盖共享副本。相同路径的其他类型仍参与召回。
    assert await hits("quasar restore", scope) == {inherited_doc}
    assert await hits("pulsar renewal", scope) == {project_copy}
    assert await hits("quasar restore", other) == {shared, inherited_doc}
    assert await hits("quasar restore", {}) == {shared, inherited_doc}
    decline_move = await runs.client.post(
        f"{path}/{project_copy}/revisions", params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": "新标题", "body": "新正文", "base_revision": 1,
            "source": {"type": "file", "ref": "moved.md", "resource_path": "moved.md"},
        },
    )
    assert decline_move.status_code == 409, decline_move.text
    linked = await runs.client.post(path, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "关联说明", "body": "引用权限需要重算。",
        "relations": [{"kind": "REFERENCES", "target": shared}, {"kind": "REFERENCES", "target": inherited_doc}],
    })
    assert linked.status_code == 201, linked.text
    assert {row["target"] for row in linked.json()["relations"]} == {inherited_doc}


async def test_session_learning_batch_preserves_source_and_is_idempotent(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    source = {"type": "session", "ref": f"codex:{uuid4()}"}
    scope = {"project_id": str((await project(runs))["id"])}
    items = [
        {"operation_id": str(uuid4()), "title": label, "body": "会话分享草稿", "source": source}
        for label in ("故障复盘", "回归验证")
    ]
    first = await runs.client.post(f"{path}/imports", params=scope, json={"items": items}, headers=runs.headers())
    assert first.status_code == 201, first.text
    published = first.json()["items"]
    assert [row["title"] for row in published] == ["故障复盘", "回归验证"]
    assert all(row["status"] == "active" and row["source"] == source for row in published)
    replay = await runs.client.post(f"{path}/imports", params=scope, json={"items": items}, headers=runs.headers())
    assert replay.status_code == 201, replay.text
    assert [(row["id"], row["revision"]) for row in replay.json()["items"]] == [
        (row["id"], row["revision"]) for row in published
    ]
    scoped = await runs.client.get(path, params=scope | {"query": "会话分享草稿"})
    assert {row["id"] for row in scoped.json()["items"]} == {row["id"] for row in published}
    assert (await runs.client.get(path, params={"query": "会话分享草稿"})).json()["items"] == []
    invalid = items[0] | {"operation_id": str(uuid4()), "kind": "doc"}
    deny = await runs.client.post(f"{path}/imports", params=scope, json={"items": [invalid]}, headers=runs.headers())
    assert deny.status_code == 422


async def test_archive_and_scope_shared_knowledge(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    orders = await project(runs)
    scope = {"project_id": str(orders["id"])}
    payload = {"operation_id": str(uuid4()), "title": "团队约定", "body": "可见范围测试"}
    denied = await runs.client.post(path, json=payload)
    assert denied.status_code == 403
    created = await runs.client.post(path, json=payload, headers=runs.headers())
    assert created.status_code == 201, created.text
    assert (await runs.client.get(path, params=scope)).json()["items"][0]["id"] == created.json()["id"]
    preview = (await runs.client.get(f"{path}/maintenance")).json()["items"][0]
    assert preview["cold"] is False and preview["status"] == "active"
    recalled = await runs.client.post(
        f"{path}/recall", json={"query": "团队约定", "session": "old-session"}, headers=runs.headers(),
    )
    assert recalled.status_code == 200, recalled.text
    async with runs.sessions() as session, session.begin():
        await session.execute(update(KnowledgeRecall).where(
            KnowledgeRecall.entry_id == UUID(created.json()["id"]),
        ).values(last_recalled_at=datetime.now(UTC) - timedelta(days=181)))
    preview = (await runs.client.get(f"{path}/maintenance")).json()["items"][0]
    assert preview["cold"] is True and preview["status"] == "active"
    proposed = (await runs.client.get(f"{path}/health")).json()["maintenance"]["prune"]
    assert [row["id"] for row in proposed["items"]] == [created.json()["id"]]
    archived = await runs.client.post(f"{path}/maintenance/archive", headers=runs.headers())
    assert archived.status_code == 200 and archived.json()["items"][0]["status"] == "archived"
    assert (await runs.client.get(path, params=scope)).json()["items"] == []
    restored = await runs.client.post(f"{path}/{created.json()['id']}/restore", headers=runs.headers())
    assert restored.status_code == 200 and restored.json()["status"] == "active"
    assert (await runs.client.get(path, params=scope)).json()["items"][0]["revision"] == 1


async def test_import_is_invisible_until_review(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    item = {
        "operation_id": str(uuid4()), "kind": "rule", "title": "发布规则", "body": "工作日发布。",
        "source": {"type": "file", "ref": ".claude/rules/deploy.md"},
    }
    invalid = await runs.client.post(
        f"{path}/imports", headers=runs.headers(), json={"items": [
            item, {**item, "operation_id": str(uuid4()), "source": {"type": "manual"}},
        ]},
    )
    assert invalid.status_code == 422
    assert (await runs.client.get(f"{path}/review")).json()["items"] == []
    imported = await runs.client.post(f"{path}/imports", headers=runs.headers(), json={"items": [item]})
    assert imported.status_code == 201, imported.text
    pending = imported.json()["items"][0]
    assert pending["status"] == "pending"
    assert (await runs.client.get(path, params={"query": "发布"})).json()["items"] == []
    reviewed = await runs.client.post(
        f"{path}/{pending['id']}/review", json={"revision": 1, "decision": "accept"}, headers=runs.headers(),
    )
    assert reviewed.status_code == 200, reviewed.text
    assert (await runs.client.get(path, params={"query": "发布"})).json()["items"][0]["id"] == pending["id"]


async def test_reviewed_rule_import_publishes_atomically_for_reviewers(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    rule = {
        "operation_id": str(uuid4()), "kind": "rule", "title": "发布规则", "body": "工作日发布。",
        "source": {"type": "file", "ref": "claude/rules/release.md"},
    }
    conflict = await runs.client.post(f"{path}/imports", headers=runs.headers(), json={
        "items": [rule, rule | {"body": "相同幂等键，不同正文。"}], "publish_reviewed": True,
    })
    assert conflict.status_code == 409
    assert (await runs.client.get(path)).json()["items"] == []
    confirmed = {"items": [rule], "publish_reviewed": True}
    first = await runs.client.post(f"{path}/imports", headers=runs.headers(), json=confirmed)
    retry = await runs.client.post(f"{path}/imports", headers=runs.headers(), json=confirmed)
    assert first.status_code == retry.status_code == 201
    assert first.json()["items"][0]["id"] == retry.json()["items"][0]["id"]
    assert (await runs.client.get(path)).json()["items"][0]["status"] == "active"
    async with member(runs, "导入贡献者") as (teammate, _):
        pending = await teammate.post(f"{path}/imports", headers=headers(teammate), json=confirmed)
        assert pending.status_code == 201, pending.text
        assert pending.json()["items"][0]["status"] == "pending"


async def test_member_can_publish_reviewed_mr_learning_with_source_attribution(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    source = {
        "type": "pull_request", "ref": "https://github.com/acme/catalog/pull/7",
        "review_author": "alice", "review_date": "2026-11-18T04:12:00Z",
    }
    learning = {
        "operation_id": str(uuid4()), "kind": "learning", "title": "控制重试上限",
        "body": "分别为每次请求和整条链路设置超时。", "source": source,
    }
    async with member(runs, "评审经验贡献者") as (teammate, _):
        pending = await teammate.post(
            f"{path}/imports", headers=headers(teammate), json={"items": [learning]},
        )
        assert pending.status_code == 201, pending.text
        assert pending.json()["items"][0]["status"] == "pending"
        confirmed = {
            "items": [learning | {"operation_id": str(uuid4())}], "publish_reviewed": True,
        }
        published = await teammate.post(f"{path}/imports", headers=headers(teammate), json=confirmed)
        assert published.status_code == 201, published.text
        item = published.json()["items"][0]
        assert item["status"] == "active" and item["source"] == source
        retry = await teammate.post(f"{path}/imports", headers=headers(teammate), json=confirmed)
        assert retry.status_code == 201 and retry.json()["items"][0]["id"] == item["id"]
    visible = await runs.client.get(path, params={"query": "重试上限"})
    assert visible.status_code == 200 and [entry["id"] for entry in visible.json()["items"]] == [item["id"]]


async def test_console_search_filters_kind_and_github_before_limiting(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    for kind, source, title in (
        ("learning", "https://github.com/acme/catalog/pull/7", "发布检查经验"),
        ("learning", "https://gitlab.example.test/acme/catalog/-/merge_requests/8", "发布检查样例"),
        ("rule", "https://github.com/acme/catalog/pull/9", "发布检查规则"),
    ):
        result = await runs.client.post(f"{path}/imports", params=scope, headers=runs.headers(), json={
            "items": [{
                "operation_id": str(uuid4()), "title": title, "body": "发布检查需要核对回滚负责人。",
                "kind": kind, "source": {"type": "pull_request", "ref": source},
            }],
            "publish_reviewed": True,
        })
        assert result.status_code == 201, result.text
        if kind == "rule":
            item = result.json()["items"][0]
            approved = await runs.client.post(
                f"{path}/{item['id']}/review", params=scope, headers=runs.headers(),
                json={"revision": item["revision"], "decision": "accept"},
            )
            assert approved.status_code == 200, approved.text

    for query in ("", "发布检查"):
        response = await runs.client.get(path, params=scope | {
            "query": query, "kind": "learning", "source_type": "github_pr", "limit": "1",
        })
        assert response.status_code == 200, response.text
        assert [item["title"] for item in response.json()["items"]] == ["发布检查经验"]


async def test_learning_overlaps_compares_current_visible_body_over_recent_window(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    orders, billing = await project(runs), await project(runs, "结算")
    scope = {"project_id": str(orders["id"])}
    body = "Cache lock retry lease window"

    async def submit(title: str, text: str, params: dict[str, str] | None = None) -> str:
        created = await runs.client.post(path, params=params, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": text,
        })
        assert created.status_code == 201, created.text
        return str(created.json()["id"])

    match = await submit("项目缓存", body, scope)
    shared = await submit("共享经验", "the CACHE lock retry")
    different = await submit("同题异文", "cache export billing api", scope)
    await submit("其他项目", body, {"project_id": str(billing["id"])})
    old = await submit("十五天前", body)
    archived = await submit("已归档", body)
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id == UUID(old)).values(
            created_at=datetime.now(UTC) - timedelta(days=15),
        ))
        await db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id == UUID(archived)).values(status="archived"))
    revised = await runs.client.post(f"{path}/{different}/revisions", params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "未发布改稿", "body": body, "base_revision": 1,
    })
    assert revised.status_code == 201
    found = await runs.client.post(f"{path}/overlaps", params=scope, headers=runs.headers(), json={"body": body})
    assert found.status_code == 200, found.text
    assert [(row["id"], row["overlap"]) for row in found.json()["items"]] == [(match, 1.0), (shared, 0.6)]
    assert found.json()["items"][0].get("operation_id")
    async with member(runs, "比较草稿的成员") as (teammate, _):
        visible = await teammate.post(f"{path}/overlaps", headers=headers(teammate), json={"body": body})
        assert visible.status_code == 200
        assert visible.json() == {"items": [{"id": shared, "title": "共享经验", "overlap": 0.6}]}
    empty = await runs.client.post(
        f"{path}/overlaps", params=scope, headers=runs.headers(), json={"body": "The 和 is a"},
    )
    assert empty.json() == {"items": []}
    stats = await runs.client.get(f"{path}/maintenance", params=scope)
    assert {row["recalls"] for row in stats.json()["items"]} == {0}


async def test_session_policy_defaults_are_independent_and_record_admin_revisions(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/settings/session-context"
    recall = f"{path}/recall"
    reminder = f"{path}/share-reminder"
    assert (await runs.client.get(path)).json() == {
        "recall": {"enabled": False, "source": "default"},
        "share-reminder": {"enabled": True, "source": "default"},
        "recall-adoption": {"enabled": False, "source": "default"},
    }
    for endpoint, enabled in ((recall, True), (recall, True), (reminder, False), (recall, False)):
        result = await runs.client.put(endpoint, headers=runs.headers(), json={"enabled": enabled})
        assert result.status_code == 200 and result.json() == {"enabled": enabled, "source": "workspace"}
    project_scope = {"project_id": str((await project(runs))["id"])}
    denied = await runs.client.put(recall, params=project_scope, headers=runs.headers(), json={"enabled": True})
    assert denied.status_code == 422
    async with member(runs, "会话策略读取成员") as (teammate, _):
        current = await teammate.get(path)
        assert current.json() == {
            "recall": {"enabled": False, "source": "workspace"},
            "share-reminder": {"enabled": False, "source": "workspace"},
            "recall-adoption": {"enabled": False, "source": "default"},
        }
        unauthorized = await teammate.put(reminder, headers=headers(teammate), json={"enabled": True})
        assert unauthorized.status_code == 403
    async with runs.sessions() as db:
        changes = (await db.scalars(select(KnowledgeSessionPolicy).order_by(KnowledgeSessionPolicy.id))).all()
        assert [(row.kind, row.enabled) for row in changes] == [
            ("recall", True), ("share-reminder", False), ("recall", False),
        ]
        assert all(str(row.updated_by) == str(runs.user_id) for row in changes)


async def test_plan_reminder_configuration_is_independent_audited_and_workspace_only(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge/settings"
    path = root + "/plan-reminder"
    default = {"enabled": True, "language": None, "message": None, "source": "default"}
    assert (await runs.client.get(path)).json() == default
    project_scope = {"project_id": str((await project(runs))["id"])}
    for invalid in ({}, {"enabled": None}, {"language": "other"}, {"message": "{{team}}"}, {"message": " "}):
        assert (await runs.client.patch(path, headers=runs.headers(), json=invalid)).status_code == 422
    denied = await runs.client.patch(path, params=project_scope, headers=runs.headers(), json={"enabled": False})
    assert denied.status_code == 422
    text = "计划完成后，先检索团队经验。"
    for payload in ({"enabled": False, "language": "zh", "message": text}, {"enabled": False}):
        result = await runs.client.patch(path, headers=runs.headers(), json=payload)
        assert result.status_code == 200
        assert result.json() == {"enabled": False, "language": "zh", "message": text, "source": "workspace"}
    async with member(runs, "计划提醒成员") as (teammate, _):
        assert (await teammate.get(path)).json()["message"] == text
        assert (await teammate.patch(path, headers=headers(teammate), json={"enabled": True})).status_code == 403
    reset = await runs.client.patch(
        path, headers=runs.headers(), json={"enabled": True, "language": None, "message": None},
    )
    assert reset.json() == default | {"source": "workspace"}
    assert "plan-reminder" not in (await runs.client.get(root + "/session-context")).json()
    async with runs.sessions() as db:
        entries = (await db.scalars(select(KnowledgeSessionPolicy).order_by(KnowledgeSessionPolicy.id))).all()
        assert [(entry.kind, entry.enabled, entry.plan_language, entry.plan_message) for entry in entries] == [
            ("plan-reminder", False, "zh", text), ("plan-reminder", True, None, None),
        ]
        assert all(str(entry.updated_by) == str(runs.user_id) for entry in entries)


async def test_adoption_uses_the_recalled_revision_and_current_member_scope(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他项目"))["id"])}
    settings = f"{path}/settings/session-context/recall-adoption"
    session = "adoption-test-session"

    async def post(suffix: str, *, location: dict[str, str] = scope, **payload: object) -> httpx.Response:
        return await runs.client.post(path + suffix, params=location, headers=runs.headers(), json=payload)

    disabled = await post("/adoption/candidates", session=session)
    assert disabled.status_code == 403 and disabled.json()["detail"]["code"] == "KNOWLEDGE_ADOPTION_DISABLED"
    async with member(runs, "评分隔离成员") as (teammate, _):
        denied = await teammate.put(settings, headers=headers(teammate), json={"enabled": True})
        assert denied.status_code == 403
    assert (await runs.client.put(settings, headers=runs.headers(), json={"enabled": True})).status_code == 200
    inheritance = await runs.client.put(
        f"{path}/settings/recall-scope", params=scope, headers=runs.headers(), json={"inherit_shared": True},
    )
    assert inheritance.status_code == 200, inheritance.text

    entries: dict[str, str] = {}
    for marker, location in (("shown", scope), ("opened", scope), ("ignored", scope), ("shared", {}), ("other", other)):
        created = await post("", location=location, operation_id=str(uuid4()),
                             title=f"adoption {marker}", body=f"pinned text {marker}")
        assert created.status_code == 201, created.text
        entries[marker] = str(created.json()["id"])

    recalled = await post("/recall", session=session, query="adoption", recall_mode=True)
    assert recalled.status_code == 200
    visible = {entries[key] for key in ("shown", "opened", "ignored", "shared")}
    assert {row["id"] for row in recalled.json()["items"]} == visible
    accepted_read = await post(f"/{entries['opened']}/open", session=session, recall_mode=True)
    assert accepted_read.status_code == 200
    revision = await post(f"/{entries['shown']}/revisions", operation_id=str(uuid4()),
                          title="adoption new", body="different current version", base_revision=1)
    assert revision.status_code == 201, revision.text
    approved = await post(f"/{entries['shown']}/review", decision="accept", revision=2)
    assert approved.status_code == 200
    new_read = await post(f"/{entries['shown']}/open", session=session)
    assert new_read.json()["revision"] == 2
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeRecall).where(KnowledgeRecall.entry_id == UUID(entries["ignored"])).values(
            last_recalled_at=datetime.now(UTC) - timedelta(hours=25),
        ))
    old_read = await post(f"/{entries['ignored']}/open", session=session)
    assert old_read.status_code == 200

    plan = (await post("/adoption/candidates", session=session)).json()
    candidates = {row["id"]: row for row in plan["items"]}
    assert set(candidates) == {entries["shown"], entries["ignored"]}
    assert candidates[entries["shown"]]["revision"] == 1
    assert candidates[entries["shown"]]["excerpt"] == "pinned text shown"
    assert (await post("/adoption/candidates", location={}, session=session)).json()["items"] == []
    assert (await post("/adoption/candidates", location=other, session=session)).json()["items"] == []
    async with member(runs, "不能提交他人评分") as (teammate, _):
        hidden = await teammate.post(path + "/adoption/candidates", params=scope, headers=headers(teammate),
                                     json={"session": session})
        assert hidden.json()["items"] == []
        rejected = await teammate.post(path + "/adoption/verdicts", params=scope, headers=headers(teammate),
                                       json={"session": session, "verdicts": [
                                           {"entry_id": entries["shown"], "revision": 1, "label": "adopted"},
                                       ]})
        assert rejected.json()["applied"] == []

    stale = await post("/adoption/verdicts", session=session, verdicts=[
        {"entry_id": entries["shown"], "revision": 2, "label": "ignored"},
    ])
    assert stale.json() == {"applied": [], "skipped": [entries["shown"]]}
    verdicts = [
        {"entry_id": entries[key], "revision": 1, "label": "ignored" if key == "ignored" else "adopted"}
        for key in ("shown", "ignored", "opened", "shared", "other")
    ]
    written = await post("/adoption/verdicts", session=session, verdicts=verdicts)
    assert written.json() == {
        "applied": [entries["shown"], entries["ignored"]],
        "skipped": [entries["opened"], entries["shared"], entries["other"]],
    }
    assert (await post("/adoption/verdicts", session=session, verdicts=verdicts)).json()["applied"] == []
    assert (await post("/adoption/candidates", session=session)).json()["items"] == []
    report = (await runs.client.get(f"{path}/health", params=scope)).json()
    assert report["adoption"] == {"judged": 2, "judge_upvotes": 1, "direct_upvotes": 1}
    stats = {row["id"]: (row["upvotes"], row["judged"], row["judge_upvotes"]) for row in report["top_recalled"]}
    assert stats == {entries["shown"]: (1, 1, 1), entries["ignored"]: (0, 1, 0), entries["opened"]: (1, 0, 0)}
    async with runs.sessions() as db:
        audits = (await db.scalars(select(KnowledgeEvent).where(KnowledgeEvent.action.like("judge_%")))).all()
        assert {audit.action for audit in audits} == {"judge_adopted", "judge_ignored"}
        assert all(audit.revision == 1 and session not in audit.reason for audit in audits)
    corrected = await post(f"/{entries['shown']}/feedback", session=session, positive=False)
    assert corrected.status_code == 204
    updated = (await runs.client.get(f"{path}/health", params=scope)).json()
    assert updated["adoption"] == {"judged": 1, "judge_upvotes": 0, "direct_upvotes": 1}
    async with runs.sessions() as db:
        change = await db.scalar(select(KnowledgeEvent).where(KnowledgeEvent.action == "judge_overridden"))
        assert change is not None and change.reason == "negative"


async def test_mr_hint_policy_records_admin_settings_and_matches_visible_published_sources(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    settings = path + "/settings/mr-hint"
    assert (await runs.client.get(settings)).json() == {
        "enabled": True, "lookback_days": 7, "max_mrs": 10, "source": "default",
    }
    policy = {"enabled": False, "lookback_days": 12, "max_mrs": 30}
    for _ in range(2):
        saved = await runs.client.put(settings, headers=runs.headers(), json=policy)
        assert saved.status_code == 200 and saved.json() == policy | {"source": "workspace"}
    async with member(runs, "提醒读取成员") as (teammate, _):
        assert (await teammate.get(settings)).json() == saved.json()
        denied = await teammate.put(settings, headers=headers(teammate), json=policy | {"enabled": True})
        assert denied.status_code == 403
    async with runs.sessions() as db:
        changes = (await db.scalars(select(KnowledgeHintPolicy))).all()
        assert len(changes) == 1 and str(changes[0].updated_by) == str(runs.user_id)

    orders = await project(runs)
    scope = {"project_id": str(orders["id"])}
    common = "https://github.com/acme/catalog/pull/"
    for number, params, publish in ((1, scope, True), (2, {}, True), (3, scope, False)):
        payload = {
            "items": [{
                "operation_id": str(uuid4()), "title": "代码变更", "body": "补充缓存测试。",
                "source": {"type": "pull_request", "ref": common + str(number)},
            }],
            "publish_reviewed": publish,
        }
        created = await runs.client.post(f"{path}/imports", params=params, headers=runs.headers(), json=payload)
        assert created.status_code == 201, created.text
        if number == 1:
            async with runs.sessions() as db, db.begin():
                await db.execute(update(KnowledgeEntry).where(
                    KnowledgeEntry.id == UUID(created.json()["items"][0]["id"]),
                ).values(status="archived"))
    urls = {"urls": [common + str(number) for number in (1, 2, 3)]}
    found = await runs.client.post(f"{path}/mr-hints/check", params=scope, headers=runs.headers(), json=urls)
    assert found.status_code == 200 and found.json()["imported"] == [common + "1", common + "2"]
    shared = await runs.client.post(f"{path}/mr-hints/check", headers=runs.headers(), json=urls)
    assert shared.status_code == 200 and shared.json()["imported"] == [common + "2"]


async def test_health_and_digest_are_scoped_and_keep_full_totals_when_details_are_limited(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    shared = await runs.client.post(path, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "共享学习方法", "body": "共享经验正文。",
    })
    assert shared.status_code == 201, shared.text
    shared_id = shared.json()["id"]
    posted: list[str] = []
    for title in ("索引恢复检查", "备份计划", "历史复盘"):
        response = await runs.client.post(path, params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": "项目经验正文。",
            "relations": [{"kind": "REFERENCES", "target": shared_id}] if not posted else [],
        })
        assert response.status_code == 201, response.text
        posted.append(str(response.json()["id"]))
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id == UUID(posted[-1])).values(
            created_at=datetime.now(UTC) - timedelta(days=8),
        ))
    for _ in range(2):
        result = await runs.client.post(f"{path}/recall", params=scope, headers=runs.headers(), json={
            "query": "索引恢复", "session": "report-session",
        })
        assert result.status_code == 200 and len(result.json()["items"]) == 1
    opened = await runs.client.post(f"{path}/{posted[0]}/open", params=scope, headers=runs.headers(), json={
        "session": "report-session",
    })
    assert opened.status_code == 200
    report = (await runs.client.get(f"{path}/health", params=scope | {"limit": "1"})).json()
    assert report["summary"] == {
        "entries": 3, "recalls": 2, "upvotes": 1, "coverage": 33,
        "contributors": 1, "hot": 1, "cold": 0, "silent": 2,
    }
    assert len(report["silent"]) == 1 and report["top_recalled"][0]["id"] == posted[0]
    assert report["graph"] == {
        "relations": 1, "cross_repository_relations": 0,
        "unresolved": 0, "without_relations": 2, "silent_without_relations": 2,
    }
    assert sum(bucket["entries"] for bucket in report["last_recall_months"]) == 1
    assert report["authors"][0]["entries"] == 3
    assert (await runs.client.get(f"{path}/health")).json()["summary"]["entries"] == 1
    async with member(runs, "周报读取成员") as (teammate, _):
        assert (await teammate.get(f"{path}/health", params=scope)).status_code == 403
        digest = (await teammate.get(f"{path}/digest", params=scope)).json()
        assert digest["total_learnings"] == 3
        assert {row["id"] for row in digest["new_learnings"]} == set(posted[:2])
        shared_digest = (await teammate.get(f"{path}/digest")).json()
        assert [row["id"] for row in shared_digest["new_learnings"]] == [shared_id]


async def test_relations_follow_the_reader_scope(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    orders = await project(runs)
    scope = {"project_id": str(orders["id"])}
    shared = await runs.client.post(path, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "共享规则", "body": "共享知识不透露其他项目名称。",
    })
    assert shared.status_code == 201, shared.text
    target = shared.json()["id"]
    related = await runs.client.post(path, headers=runs.headers(), params=scope, json={
        "operation_id": str(uuid4()), "title": "项目内部约定", "body": "依赖共享规则。",
        "relations": [{"kind": "REFERENCES", "target": target}],
    })
    assert related.status_code == 201, related.text
    assert related.json()["relations"][0]["direction"] == "out"
    assert (await runs.client.get(f"{path}/{target}")).json()["relations"] == []
    incoming = (await runs.client.get(f"{path}/{target}", params=scope)).json()["relations"]
    assert incoming[0]["direction"] == "in" and incoming[0]["target"] == related.json()["id"]


@pytest.mark.parametrize("legacy_asset", [False, True], ids=["path-install-name", "legacy-install-name"])
async def test_promoting_shared_learning_publishes_one_registry_skill(runs: RunsHarness, legacy_asset: bool) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    original = await runs.client.post(path, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "分页回归流程", "body": "执行分页回归并关联测试证据。",
        "source": {"type": "manual", "resource_path": "learnings/platform/pagination-check.md"},
    })
    assert original.status_code == 201, original.text
    target = original.json()["id"]
    async with runs.sessions() as session, session.begin():
        await session.execute(update(KnowledgeEntry).where(
            KnowledgeEntry.id == UUID(target),
        ).values(created_at=datetime.now(UTC) - timedelta(days=15)))
    async with member(runs, "参与过召回的成员") as (teammate, _):
        for index in range(5):
            actor = teammate if index < 2 else runs.client
            vote = await actor.post(f"{path}/{target}/feedback", headers=headers(actor), json={
                "session": f"session-{index}", "positive": True,
            })
            assert vote.status_code == 204, vote.text
    proposed = (await runs.client.get(f"{path}/health")).json()["maintenance"]["promote"]
    assert [row["id"] for row in proposed["items"]] == [target]
    markdown = (
        "---\n# 给维护者的说明\nname: pagination-verification\n"
        "description: 核验分页\ndomain: platform\n---\n\n执行分页回归测试。"
    )
    draft = await runs.client.post(path, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "kind": "skill", "title": "分页回归", "body": markdown,
        "promoted_from": target,
    })
    assert draft.status_code == 201, draft.text
    new = draft.json()["id"]
    # 用同一注册链路造出历史包。数据库里的逻辑路径仍是 pagination-check，更新时应读取实际安装名。
    installed_before_upgrade = patch(
        "weora.modules.knowledge.promotion.entry_path", return_value="skills/platform/旧名称",
    ) if legacy_asset else nullcontext()
    with installed_before_upgrade:
        approved = await runs.client.post(
            f"{path}/{new}/review", headers=runs.headers(), json={"revision": 1, "decision": "accept"},
        )
    assert approved.status_code == 200, approved.text
    accepted_again = await runs.client.post(
        f"{path}/{new}/review", headers=runs.headers(), json={"revision": 1, "decision": "accept"},
    )
    assert accepted_again.status_code == 200 and accepted_again.json()["status"] == "active"
    registry = f"/api/v1/workspaces/{runs.workspace_id}/skills"
    skills = await runs.client.get(registry)
    assert skills.status_code == 200, skills.text
    slug = f"learning-{UUID(new).hex}" if legacy_asset else "pagination-check"
    matches = [skill for skill in skills.json()["items"] if skill["slug"] == slug]
    assert len(matches) == 1 and matches[0]["namespace"] == "platform"
    repository = f"{registry}/{matches[0]['id']}"
    logical_path = "skills/platform/pagination-check/SKILL.md"
    link = await runs.client.post(path, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "查看分页手册", "body": "分页问题参见关联 Skill。",
        "related": {"REFERENCES": [logical_path]},
    })
    assert link.status_code == 201, link.text
    assert link.json()["relation_findings"] == []
    assert [row["target"] for row in link.json()["relations"]] == [new]
    incoming = (await runs.client.get(f"{path}/by-path", params={"path": logical_path})).json()
    assert incoming["id"] == new
    assert any(row["target"] == link.json()["id"] and row["direction"] == "in" for row in incoming["relations"])
    await _quality_recalls(runs, path, {}, "执行分页回归测试")
    updated = "先记录当前分页总数，再分别执行第一页与最后一页的回归。"
    revised = f"---\nname: conflicting-name\ndescription: 不可替换安装清单\n---\n\n{updated}"
    payload = {"operation_id": str(uuid4()), "base_revision": 1, "body": revised}
    pending = await runs.client.post(f"{path}/maintenance/quality/{new}", headers=runs.headers(), json=payload)
    assert pending.status_code == 201 and pending.json()["revision_status"] == "pending", pending.text
    previous = (await runs.client.get(f"{repository}/versions")).json()["items"]
    assert [row["version"] for row in previous] == ["1.0.0"]
    accepted = await runs.client.post(
        f"{path}/{new}/review", headers=runs.headers(), json={"revision": 2, "decision": "accept"},
    )
    assert accepted.status_code == 200, accepted.text
    assert (await runs.client.get(f"{path}/{new}")).json()["body"] == revised
    versions = (await runs.client.get(f"{repository}/versions")).json()["items"]
    assert [row["version"] for row in versions] == ["1.0.1", "1.0.0"]
    digest = (await runs.client.get(f"{path}/digest")).json()
    assert {(row["id"], row["revision"]) for row in digest["skill_releases"]} == {(new, 1), (new, 2)}
    assert (await runs.client.get(f"{path}/health")).json()["maintenance"]["promote"]["total"] == 0
    for number, text in (("1.0.0", "执行分页回归测试。"), ("1.0.1", updated)):
        download = await runs.client.get(f"{repository}/versions/{number}/download")
        assert download.status_code == 200
        with ZipFile(BytesIO(download.content)) as package:
            packaged = package.read(f"{slug}/SKILL.md").decode()
            assert text in packaged and packaged.splitlines().count("---") == 2
            assert packaged.split("---", 2)[:2] == markdown.split("---", 2)[:2]
            if number == "1.0.0":
                assert packaged == markdown

    # 独立编辑的 Registry 草稿必须保留，失败的知识审核也继续待审。
    edited = "---\nname: pagination-verification\ndescription: 人工更新\n---\n\nThoriumPageWitness\n"
    package_bytes = BytesIO()
    with ZipFile(package_bytes, "w") as package:
        package.writestr(f"{slug}/SKILL.md", edited)
        package.writestr(f"{slug}/references/check.md", "人工补充的检查项")
    replaced = await runs.client.post(
        f"{repository}/draft", headers=runs.headers(),
        files={"package": (f"{slug}.skill", package_bytes.getvalue(), "application/zip")},
    )
    assert replaced.status_code == 200, replaced.text
    payload |= {"operation_id": str(uuid4()), "base_revision": 2, "body": "再次补充空列表边界。"}
    another = await runs.client.post(f"{path}/maintenance/quality/{new}", headers=runs.headers(), json=payload)
    assert another.status_code == 201, another.text
    conflict = await runs.client.post(
        f"{path}/{new}/review", headers=runs.headers(), json={"revision": 3, "decision": "accept"},
    )
    assert conflict.status_code == 409
    assert (await runs.client.get(f"{path}/{new}")).json()["revision"] == 2
    latest_draft = await runs.client.get(f"{repository}/draft")
    assert latest_draft.json()["package_sha256"] == replaced.json()["package_sha256"]

    # 两种发布入口对同一版本竞争时，保留当前发布和双方草稿。
    release_request = {"version": "2.4.0", "changelog": "补充分页检查"}
    blocked = await runs.client.post(f"{repository}/publish", headers=runs.headers(), json=release_request)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "SKILL_KNOWLEDGE_REVIEW_PENDING"
    assert (await runs.client.get(f"{repository}/versions/2.4.0")).status_code == 404
    assert (await runs.client.get(f"{repository}/draft")).json()["package_sha256"] == replaced.json()["package_sha256"]
    rejected = await runs.client.post(
        f"{path}/{new}/review", headers=runs.headers(), json={"revision": 3, "decision": "reject"},
    )
    assert rejected.status_code == 200, rejected.text
    publication = await runs.client.post(f"{repository}/publish", headers=runs.headers(), json=release_request)
    assert publication.status_code == 201, publication.text
    live = (await runs.client.get(f"{path}/by-path", params={"path": logical_path})).json()
    assert live["id"] == new and live["revision"] == 4 and live["body"] == edited
    assert live["source"]["registry_version_id"] == publication.json()["id"]
    recall = (await runs.client.get(path, params={"query": "ThoriumPageWitness"})).json()["items"]
    assert [(row["id"], row["revision"]) for row in recall] == [(new, 4)]

    payload |= {"operation_id": str(uuid4()), "base_revision": 4, "body": "人工发布后的质量修订。"}
    regenerated = await runs.client.post(f"{path}/maintenance/quality/{new}", headers=runs.headers(), json=payload)
    assert regenerated.status_code == 201, regenerated.text
    accepted = await runs.client.post(
        f"{path}/{new}/review", headers=runs.headers(), json={"revision": 5, "decision": "accept"},
    )
    assert accepted.status_code == 200 and accepted.json()["source"]["registry_version"] == "2.4.1", accepted.text
    bundle = await runs.client.get(f"{repository}/versions/2.4.1/download")
    assert bundle.status_code == 200, bundle.text
    with ZipFile(BytesIO(bundle.content)) as package:
        assert "人工发布后的质量修订" in package.read(f"{slug}/SKILL.md").decode()
        assert package.read(f"{slug}/references/check.md").decode() == "人工补充的检查项"

    if legacy_asset:
        payload |= {"operation_id": str(uuid4()), "base_revision": 5, "body": "拟增加人工复核。"}
        suggestion = await runs.client.post(f"{path}/maintenance/quality/{new}", headers=runs.headers(), json=payload)
        assert suggestion.status_code == 201, suggestion.text
        retired = await runs.client.post(f"{repository}/archive", headers=runs.headers())
        assert retired.status_code == 200, retired.text
        discarded = await runs.client.post(
            f"{path}/{new}/review", headers=runs.headers(), json={"revision": 6, "decision": "reject"},
        )
        assert discarded.status_code == 200 and discarded.json()["status"] == "archived"
        new_asset = await runs.client.post(registry, headers=runs.headers(), data={"namespace": "platform"}, files={
            "package": ("pagination-check.skill", build_skill_package(name="pagination-check"), "application/zip"),
        })
        assert new_asset.status_code == 201, new_asset.text
        replacement = f"{registry}/{new_asset.json()['id']}"
        counterpart = await runs.client.post(
            f"{replacement}/publish", headers=runs.headers(), json={"version": "1.0.0", "changelog": "现用资产"},
        )
        assert counterpart.status_code == 201, counterpart.text
        restore = await runs.client.post(f"{path}/{new}/restore", headers=runs.headers())
        assert restore.status_code == 409 and restore.json()["detail"]["code"] == "KNOWLEDGE_SKILL_RESTORE_FAILED"
        assert (await runs.client.get(repository)).json()["status"] == "archived"
        live_path = await runs.client.get(f"{path}/by-path", params={"path": logical_path})
        assert live_path.json()["id"] == new_asset.json()["id"]
        assert (await runs.client.post(f"{replacement}/archive", headers=runs.headers())).status_code == 200
        restored = await runs.client.post(f"{path}/{new}/restore", headers=runs.headers())
        assert restored.status_code == 200 and restored.json()["revision"] == 5
        assert (await runs.client.post(f"{repository}/archive", headers=runs.headers())).status_code == 200
        legacy_repromotion = await runs.client.post(path, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "kind": "skill", "title": "原路径再次晋升",
            "body": "# 重新审核分页步骤", "promoted_from": target,
        })
        assert legacy_repromotion.status_code == 201 and legacy_repromotion.json()["id"] == new
        promoted_again = await runs.client.post(f"{path}/{new}/review", headers=runs.headers(), json={
            "revision": legacy_repromotion.json()["revision"], "decision": "accept",
        })
        assert promoted_again.status_code == 200, promoted_again.text
        assert promoted_again.json()["source"]["registry_version"] == "2.4.2"
        assert (await runs.client.get(repository)).json()["slug"] == slug


async def test_promoted_project_skill_requires_project_for_sync_and_installation(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}"
    knowledge, distribution, registry = (f"{root}/{path}" for path in ("knowledge", "skill-distribution", "skills"))
    project_id = str((await project(runs))["id"])
    scope = {"project_id": project_id}
    other = {"project_id": str((await project(runs, "另一个项目"))["id"])}
    learning = await runs.client.post(knowledge, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "项目上线复核", "body": "确认项目上线指标。",
    })
    assert learning.status_code == 201, learning.text
    entry = learning.json()["id"]
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id == UUID(entry)).values(
            created_at=datetime.now(UTC) - timedelta(days=15),
        ))
    async with member(runs, "贡献者") as (teammate, _):
        for index in range(5):
            actor = teammate if index < 2 else runs.client
            vote = await actor.post(
                f"{knowledge}/{entry}/feedback", params=scope, headers=headers(actor),
                json={"session": f"project-{index}", "positive": True},
            )
            assert vote.status_code == 204, vote.text

    draft = await runs.client.post(knowledge, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "kind": "skill", "title": "项目上线检查",
        "body": "---\ndomain: delivery\n---\n\n# 发布后执行项目检查。\n", "promoted_from": entry,
    })
    assert draft.status_code == 201, draft.text
    skill_entry = draft.json()["id"]
    slug = entry
    device_id, authorization = await device(runs.client, root + "/agent-collaboration", "项目 Skill 验收设备")
    grant = await runs.client.put(
        f"{distribution}/devices/{device_id}/grant", headers=runs.headers(), json={"usage_enabled": True},
    )
    assert grant.status_code == 200, grant.text

    async def manifest(params: dict[str, str]) -> dict[str, str]:
        response = await runs.client.get(
            f"{distribution}/manifest", headers=authorization, params={"runtime": "codex"} | params,
        )
        assert response.status_code == 200, response.text
        return {item["slug"]: item["version_id"] for item in response.json()["items"]}

    assert slug not in await manifest(scope)
    for _ in range(2):
        accepted = await runs.client.post(
            f"{knowledge}/{skill_entry}/review", params=scope, headers=runs.headers(),
            json={"revision": 1, "decision": "accept"},
        )
        assert accepted.status_code == 200, accepted.text
    version = (await manifest(scope))[slug]
    project_skills = (await runs.client.get(registry, params=scope)).json()["items"]
    assert [(item["slug"], item["display_name"]) for item in project_skills] == [(slug, "项目上线检查")]
    for params in ({}, {"scope": "user"}, other):
        assert slug not in await manifest(params)
    invalid_manifest = await runs.client.get(
        f"{distribution}/manifest", params=scope | {"runtime": "codex", "scope": "user"}, headers=authorization,
    )
    assert invalid_manifest.status_code == 422

    package = f"{distribution}/versions/{version}/package"
    for params in ({}, other, {"project_id": str(uuid4())}):
        assert (await runs.client.get(package, params=params, headers=authorization)).status_code == 404
        response = await runs.client.get(
            f"{distribution}/resolve", params={"slug": slug} | params, headers=authorization,
        )
        assert response.status_code == 404
    downloaded = await runs.client.get(package, params=scope, headers=authorization)
    assert downloaded.status_code == 200, downloaded.text
    with ZipFile(BytesIO(downloaded.content)) as bundle:
        packaged = bundle.read(f"{slug}/SKILL.md").decode()
        assert packaged.splitlines().count("---") == 2
        assert f"name: {slug}" in packaged and "domain: delivery" in packaged
        assert "description: 发布后执行项目检查。" in packaged

    async with runs.sessions() as db:
        skill_id = await db.scalar(select(SkillModel.id).where(SkillModel.slug == slug))
    workspace_skills = (await runs.client.get(registry)).json()["items"]
    assert skill_id is not None and slug not in [item["slug"] for item in workspace_skills]
    for suffix in ("", "/versions/1.0.0", "/versions/1.0.0/download"):
        assert (await runs.client.get(f"{registry}/{skill_id}{suffix}")).status_code == 404

    payload = {
        "operation_id": str(uuid4()), "slot_id": str(uuid4()), "installation_id": str(uuid4()),
        "version_id": version, "occurred_at": datetime.now(UTC).isoformat(), "runtime": "codex",
    }
    receipts = f"{distribution}/installation-receipts"
    assert (await runs.client.post(receipts, headers=authorization, json=payload)).status_code == 404
    first = await runs.client.post(receipts, params=scope, headers=authorization, json=payload)
    assert first.json() == {"status": "accepted"}, first.text
    replay = await runs.client.post(receipts, params=scope, headers=authorization, json=payload)
    assert replay.json() == {"status": "duplicate"}, replay.text
    assert (await runs.client.post(receipts, params=other, headers=authorization, json=payload)).status_code == 404
    removal = {
        "operation_id": str(uuid4()), "slot_id": payload["slot_id"], "previous_id": payload["installation_id"],
        "occurred_at": datetime.now(UTC).isoformat(), "runtime": "codex",
    }
    assert (await runs.client.post(receipts, params=other, headers=authorization, json=removal)).status_code == 409
    assert (await runs.client.post(receipts, params=scope, headers=authorization, json=removal)).status_code == 200
    assert (await runs.client.post(receipts, params=other, headers=authorization, json=removal)).status_code == 404

    await _quality_recalls(runs, knowledge, scope, "项目检查")
    pending = await runs.client.post(
        f"{knowledge}/maintenance/quality/{skill_entry}", params=scope, headers=runs.headers(),
        json={"operation_id": str(uuid4()), "base_revision": 1, "body": "发布后执行项目检查并记录证据。"},
    )
    assert pending.status_code == 201, pending.text
    assert (await manifest(scope))[slug] == version
    approved = await runs.client.post(
        f"{knowledge}/{skill_entry}/review", params=scope, headers=runs.headers(),
        json={"revision": 2, "decision": "accept"},
    )
    assert approved.status_code == 200, approved.text
    assert (await manifest(scope))[slug] != version and slug not in await manifest(other)
    assert (await runs.client.get(package, params=scope, headers=authorization)).status_code == 200

    pending = await runs.client.post(
        f"{knowledge}/maintenance/quality/{skill_entry}", params=scope, headers=runs.headers(),
        json={"operation_id": str(uuid4()), "base_revision": 2, "body": "记录证据后再归档检查日志。"},
    )
    assert pending.status_code == 201, pending.text
    repository = f"{registry}/{skill_id}"
    approved_version = (await manifest(scope))[slug]
    for _ in range(2):
        archived = await runs.client.post(f"{repository}/archive", params=scope, headers=runs.headers())
        assert archived.status_code == 200 and archived.json()["status"] == "archived", archived.text
    target = f"{knowledge}/{skill_entry}"
    assert (await runs.client.get(target, params=scope)).status_code == 404
    assert (await runs.client.get(f"{knowledge}/by-path", params=scope | {"path": f"skills/{slug}"})).status_code == 404
    assert slug not in await manifest(scope)
    assert (await runs.client.get(package, params=scope, headers=authorization)).status_code == 404
    assert (await runs.client.get(f"{repository}/versions/1.0.0/download", params=scope)).status_code == 200
    remaining = (await runs.client.get(f"{knowledge}/{entry}", params=scope)).json()
    assert remaining["status"] == "active"
    queue = (await runs.client.get(f"{knowledge}/review", params=scope)).json()["items"]
    assert [(row["id"], row["revision"]) for row in queue] == [(skill_entry, 3)]
    while_archived = await runs.client.post(
        f"{target}/review", params=scope, headers=runs.headers(), json={"revision": 3, "decision": "accept"},
    )
    assert while_archived.status_code == 409
    assert (await runs.client.post(f"{target}/restore", params=other, headers=runs.headers())).status_code == 404
    competing = await runs.client.post(
        f"{knowledge}/maintenance/promote/{entry}", params=scope, headers=runs.headers(),
        json={"operation_id": str(uuid4()), "kind": "doc", "body": "以文档形式发布。",
              "source_revision": 1, "reason": "重新评审目标类别。"},
    )
    assert competing.status_code == 201, competing.text
    held = await runs.client.post(f"{repository}/restore", params=scope, headers=runs.headers())
    assert held.status_code == 409 and held.json()["detail"]["code"] == "SKILL_PROMOTION_SOURCE_CONFLICT"
    assert slug not in await manifest(scope)
    rejected = await runs.client.post(
        f"{knowledge}/{competing.json()['id']}/review", params=scope, headers=runs.headers(),
        json={"revision": 1, "decision": "reject"},
    )
    assert rejected.status_code == 200, rejected.text
    restored = await runs.client.post(f"{target}/restore", params=scope, headers=runs.headers())
    assert restored.status_code == 200 and restored.json()["status"] == "active", restored.text
    assert (await manifest(scope))[slug] == approved_version
    replayed_restore = await runs.client.post(f"{repository}/restore", params=scope, headers=runs.headers())
    assert replayed_restore.status_code == 200
    audit = (await runs.client.get(f"{repository}/events", params=scope)).json()["items"]
    assert [event["event_type"] for event in audit if event["event_type"] in {"SKILL_ARCHIVED", "SKILL_RESTORED"}] == [
        "SKILL_RESTORED", "SKILL_ARCHIVED",
    ]
    async with runs.sessions() as db:
        mirrored = (await db.scalars(select(KnowledgeEvent.action).where(
            KnowledgeEvent.entry_id == UUID(skill_entry), KnowledgeEvent.action.in_(("archived", "restored")),
        ).order_by(KnowledgeEvent.occurred_at))).all()
    assert mirrored == ["archived", "restored"]
    reopened = await runs.client.post(
        f"{target}/review", params=scope, headers=runs.headers(), json={"revision": 3, "decision": "accept"},
    )
    assert reopened.status_code == 200, reopened.text


async def _quality_recalls(runs: RunsHarness, path: str, scope: dict[str, str], query: str) -> None:
    async with member(runs, "质量修订召回成员") as (teammate, _):
        for sequence in range(5):
            actor = teammate if sequence < 2 else runs.client
            recalled = await actor.post(
                f"{path}/recall", params=scope, headers=headers(actor),
                json={"query": query, "session": f"quality-{sequence}"},
            )
            assert recalled.status_code == 200 and recalled.json()["items"]


async def test_quality_context_filters_examples_and_review_preserves_published_document(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "独立项目"))["id"])}
    original = {
        "title": "索引迁移", "body": "只说要做回放核对。", "kind": "doc", "tags": ["索引"],
        "related": {"REFERENCES": "learnings/index-replay"},
    }
    created = await runs.client.post(
        path, params=scope, headers=runs.headers(), json={"operation_id": str(uuid4()), **original},
    )
    assert created.status_code == 201, created.text
    entry = created.json()["id"]
    reviewed = await runs.client.post(
        f"{path}/{entry}/review", params=scope, headers=runs.headers(), json={"revision": 1, "decision": "accept"},
    )
    assert reviewed.status_code == 200
    await _quality_recalls(runs, path, scope, "回放核对")
    for target, title in ((scope, "索引迁移示例"), ({}, "节日排班"), (other, "索引迁移私有示例")):
        learning = await runs.client.post(path, params=target, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": "记录迁移前后的条数。",
            "source": {"type": "manual", "resource_path": "learnings/index-replay"},
        })
        assert learning.status_code == 201
        vote = await runs.client.post(
            f"{path}/{learning.json()['id']}/feedback", params=target,
            headers=runs.headers(), json={"session": "adopt", "positive": True},
        )
        assert vote.status_code == 204
    endpoint = f"{path}/maintenance/quality/{entry}"
    context = await runs.client.get(endpoint, params=scope)
    assert context.status_code == 200, context.text
    assert [row["title"] for row in context.json()["related"]] == ["索引迁移示例"]
    eligible = (await runs.client.get(f"{path}/health", params=scope)).json()["maintenance"]["quality"]
    assert [row["id"] for row in eligible["items"]] == [entry]
    assert (await runs.client.get(endpoint, params=other)).status_code == 404
    payload = {"operation_id": str(uuid4()), "base_revision": 1, "body": "迁移前后分别记录条数与错误样例。"}
    async with member(runs, "普通维护成员") as (teammate, _):
        blocked = await teammate.post(endpoint, params=scope, headers=headers(teammate), json=payload)
        assert blocked.status_code == 403
    result = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=payload)
    assert result.status_code == 201 and result.json()["revision_status"] == "pending", result.text
    assert (await runs.client.get(f"{path}/health", params=scope)).json()["maintenance"]["quality"]["total"] == 0
    assert (await runs.client.get(f"{path}/{entry}", params=scope)).json()["body"] == original["body"]
    approved = await runs.client.post(
        f"{path}/{entry}/review", params=scope, headers=runs.headers(), json={"revision": 2, "decision": "accept"},
    )
    assert approved.status_code == 200
    replayed = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=payload)
    assert replayed.status_code == 201 and replayed.json()["revision"] == 2
    changed = await runs.client.post(
        endpoint, params=scope, headers=runs.headers(), json=payload | {"operation_id": str(uuid4())},
    )
    assert changed.status_code == 409
    latest = (await runs.client.get(f"{path}/{entry}", params=scope)).json()
    assert latest["body"] == payload["body"] and latest["tags"] == original["tags"]
    assert [(row["kind"], row["title"]) for row in latest["relations"]] == [("REFERENCES", "索引迁移示例")]
