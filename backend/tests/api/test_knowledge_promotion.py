"""从成员候选到管理员评审验证经验晋升及出处。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import Response
from sqlalchemy import select, update

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_skills import build_skill_package
from tests.api.test_task_collaboration import headers, member
from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeEvent
from weora.modules.skills.application.service import SkillActor, SkillRegistryService
from weora.modules.skills.infrastructure.unit_of_work import SqlAlchemySkillsUnitOfWork

pytestmark = pytest.mark.anyio


async def test_promotion_tracks_source_revision_and_reopens_after_rejection(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs))["id"])}
    other = {"project_id": str((await project(runs, "其他项目"))["id"])}
    created = await runs.client.post(root, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "游标分页经验", "body": "This is a workflow and recipe.",
    })
    assert created.status_code == 201, created.text
    source = created.json()["id"]
    async with runs.sessions() as db, db.begin():
        await db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id == UUID(source)).values(
            created_at=datetime.now(UTC) - timedelta(days=15),
        ))
    candidates = f"{root}/maintenance/promote"
    lookup = f"{candidates}/{source}"
    async with member(runs, "提出晋升的成员") as (teammate, _):
        for index in range(5):
            actor = teammate if index < 2 else runs.client
            feedback = await actor.post(f"{root}/{source}/feedback", headers=headers(actor), params=scope, json={
                "session": f"promote-{index}", "positive": True,
            })
            assert feedback.status_code == 204, feedback.text
        assert not (await teammate.get(candidates)).json()["items"]
        assert (await teammate.get(lookup, params=other)).status_code == 404
        found = (await teammate.get(candidates, params=scope)).json()
        assert [(row["id"], row["kind"]) for row in found["items"]] == [(source, "skill")]
        preview = (await teammate.get(lookup, params=scope)).json()
        assert preview["revision"] == 1 and preview["targets"]["doc"] == f"docs/{source}.md"

        updated = await runs.client.post(f"{root}/{source}/revisions", params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "kind": "learning", "base_revision": 1,
            "title": "游标分页经验", "body": "A second workflow with a cursor recipe.",
        })
        assert updated.status_code == 201, updated.text
        approved = await runs.client.post(f"{root}/{source}/review", params=scope, headers=runs.headers(), json={
            "decision": "accept", "revision": 2,
        })
        assert approved.status_code == 200, approved.text
        payload = {
            "operation_id": str(uuid4()), "source_revision": 1, "kind": "doc",
            "reason": "可作团队参考。", "body": "游标包含排序字段和唯一键。",
        }
        assert (await teammate.post(lookup, params=scope, headers=headers(teammate), json=payload)).status_code == 409
        payload["source_revision"] = 2
        first = await teammate.post(lookup, params=scope, headers=headers(teammate), json=payload)
        assert first.status_code == 201, first.text
        draft = first.json()
        assert draft["status"] == "pending" and draft["promoted_from"] == source
        assert draft["source"]["promotion_revision"] == "2"
        assert (await teammate.get(f"{root}/{draft['id']}", params=scope)).status_code == 404
        assert not (await teammate.get(candidates, params=scope)).json()["items"]
        same = await teammate.post(lookup, params=scope, headers=headers(teammate), json=payload)
        assert same.json()["id"] == draft["id"]
        blocked = await teammate.post(lookup, params=scope, headers=headers(teammate), json=payload | {
            "operation_id": str(uuid4()), "kind": "rule",
        })
        assert blocked.status_code == 409
        rejected = await runs.client.post(f"{root}/{draft['id']}/review", params=scope, headers=runs.headers(), json={
            "decision": "reject", "revision": 1,
        })
        assert rejected.status_code == 200, rejected.text
        occupied = await runs.client.post(root, params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "kind": "rule", "title": "人工规则", "body": "保留原资源。",
            "source": {"type": "manual", "resource_path": f"rules/{source}"},
        })
        assert occupied.status_code == 201, occupied.text
        again = await teammate.get(lookup, params=scope)
        assert again.status_code == 200 and again.json()["occupied_kinds"] == ["rule"]
        invalid_target = await teammate.post(lookup, params=scope, headers=headers(teammate), json=payload | {
            "operation_id": str(uuid4()), "kind": "rule",
        })
        assert invalid_target.status_code == 409
        retry = await teammate.post(lookup, params=scope, headers=headers(teammate), json=payload | {
            "operation_id": str(uuid4()),
        })
        assert retry.status_code == 201, retry.text
        new = retry.json()["id"]
        accepted = await runs.client.post(f"{root}/{new}/review", params=scope, headers=runs.headers(), json={
            "decision": "accept", "revision": 1,
        })
        assert accepted.status_code == 200, accepted.text
        detail = (await teammate.get(f"{root}/{new}", params=scope)).json()
        assert detail["body"] == payload["body"]
        assert detail["relations"][0]["target"] == source
        assert not (await teammate.get(candidates, params=scope)).json()["items"]
        async with runs.sessions() as db:
            event = await db.scalar(select(KnowledgeEvent).where(
                KnowledgeEvent.entry_id == UUID(new), KnowledgeEvent.action == "submitted",
            ))
            assert event is not None and event.reason == payload["reason"]


async def test_promotion_reserves_logical_path_for_all_contributors(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "晋升目标竞争"))["id"])}
    other = {"project_id": str((await project(runs, "晋升目标隔离"))["id"])}
    auth = runs.headers()

    async def create_learning(path: str) -> str:
        seeded = await runs.client.post(root, params=scope, headers=auth, json={
            "operation_id": str(uuid4()), "title": "晋升路径竞争样例", "body": "Validate release checklist.",
            "source": {"type": "manual", "resource_path": path},
        })
        assert seeded.status_code == 201, seeded.text
        return str(seeded.json()["id"])

    # 两份既有经验分别来自文件导入和 API，路径只在后缀写法上有差异。
    first = await create_learning("learnings/release-plan.md")
    second = await create_learning("learnings/release-plan")
    async with runs.sessions.begin() as db:
        await db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id.in_([UUID(first), UUID(second)])).values(
            created_at=datetime.now(UTC) - timedelta(days=15),
        ))
        postgres = db.get_bind().dialect.name == "postgresql"
    async with member(runs, "晋升目标第二位成员") as (peer, _):
        for entry in (first, second):
            for index in range(5):
                actor = peer if index < 2 else runs.client
                vote = await actor.post(f"{root}/{entry}/feedback", params=scope, headers=headers(actor), json={
                    "session": f"reserve-{index}", "positive": True,
                })
                assert vote.status_code == 204, vote.text

        endpoint = f"{root}/maintenance/promote"
        payload = {
            "operation_id": str(uuid4()), "source_revision": 1, "kind": "doc",
            "reason": "共享可复用的发布计划。", "body": "Approved release procedure.",
        }
        legacy = {
            "operation_id": str(uuid4()), "kind": "doc", "title": "第二份晋升草稿",
            "body": "A competing promotion.", "promoted_from": second,
        }
        # 专用 CLI 入口与通用贡献入口竞争；通用入口从经验目录推导同一个目标。
        if postgres:
            proposals = await asyncio.gather(
                peer.post(f"{endpoint}/{first}", params=scope, headers=headers(peer), json=payload),
                runs.client.post(root, params=scope, headers=auth, json=legacy),
            )
        else:
            proposals = (
                await peer.post(f"{endpoint}/{first}", params=scope, headers=headers(peer), json=payload),
                await runs.client.post(root, params=scope, headers=auth, json=legacy),
            )
        assert sorted(item.status_code for item in proposals) == [201, 409], [item.text for item in proposals]
        winner = next(item.json() for item in proposals if item.status_code == 201)
        assert winner["source"]["resource_path"] == "docs/release-plan.md"
        attempts = (
            (peer, f"{endpoint}/{first}", payload),
            (runs.client, root, legacy),
        )
        submitter, url, successful_payload = attempts[0 if winner["promoted_from"] == first else 1]
        replay = await submitter.post(url, params=scope, headers=headers(submitter), json=successful_payload)
        assert replay.status_code == 201 and replay.json()["id"] == winner["id"], replay.text

        doc = {
            "operation_id": str(uuid4()), "kind": "doc", "title": "同路径手工文档", "body": "Manual draft.",
            "source": {"type": "manual", "resource_path": "docs/release-plan"},
        }
        blocked = await runs.client.post(root, params=scope, headers=auth, json=doc)
        assert blocked.status_code == 409, blocked.text
        independent = await runs.client.post(root, params=other, headers=auth, json=doc)
        assert independent.status_code == 201, independent.text

        reject = await runs.client.post(f"{root}/{winner['id']}/review", params=scope, headers=auth, json={
            "decision": "reject", "revision": 1,
        })
        assert reject.status_code == 200, reject.text
        opened = await runs.client.post(root, params=scope, headers=auth, json=doc | {"operation_id": str(uuid4())})
        assert opened.status_code == 201, opened.text
        retry = await peer.post(f"{endpoint}/{first}", params=scope, headers=headers(peer), json=payload | {
            "operation_id": str(uuid4()),
        })
        assert retry.status_code == 409, retry.text

        async def request_rule(entry: str) -> Response:
            return await peer.post(f"{endpoint}/{entry}", params=scope, headers=headers(peer), json=payload | {
                "operation_id": str(uuid4()), "kind": "rule",
            })

        rules = (
            await asyncio.gather(request_rule(first), request_rule(second))
            if postgres else (await request_rule(first), await request_rule(second))
        )
        assert sorted(row.status_code for row in rules) == [201, 409], [row.text for row in rules]
        rule = next(row.json() for row in rules if row.status_code == 201)
        released = await runs.client.post(f"{root}/{rule['id']}/review", params=scope, headers=auth, json={
            "decision": "accept", "revision": 1,
        })
        assert released.status_code == 200, released.text
        blocked = await runs.client.post(root, params=scope, headers=auth, json=doc | {
            "operation_id": str(uuid4()), "kind": "rule",
            "source": {"type": "manual", "resource_path": "rules/release-plan"},
        })
        assert blocked.status_code == 409, blocked.text


async def test_registry_respects_pending_promotion_and_rechecks_before_review(runs: RunsHarness) -> None:
    workspace = UUID(runs.workspace_id)
    root = f"/api/v1/workspaces/{workspace}/knowledge"
    registry = f"/api/v1/workspaces/{workspace}/skills"
    project_id = UUID(str((await project(runs, "发布目标竞争"))["id"]))
    scope = {"project_id": str(project_id)}
    headers_owner = runs.headers()

    async def learning(slug: str) -> str:
        response = await runs.client.post(root, params=scope, headers=headers_owner, json={
            "operation_id": str(uuid4()), "title": slug, "body": "A reusable operational workflow.",
            "source": {"type": "manual", "resource_path": f"learnings/platform/{slug}.md"},
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def upload(slug: str, params: dict[str, str]) -> str:
        response = await runs.client.post(registry, params=params, headers=headers_owner,
                                          data={"namespace": "platform"}, files={
            "package": (f"{slug}.skill", build_skill_package(name=slug), "application/zip"),
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def publish(skill: str, params: dict[str, str]) -> Response:
        return await runs.client.post(f"{registry}/{skill}/publish", params=params, headers=headers_owner, json={
            "version": "1.0.0", "changelog": "第一次发布",
        })

    async def review(draft: str, decision: str) -> Response:
        return await runs.client.post(f"{root}/{draft}/review", params=scope, headers=headers_owner, json={
            "decision": decision, "revision": 1,
        })

    root_slug, race_slug, old_slug, draft_slug = (
        "reviewed-release", "racing-release", "legacy-release", "draft-release",
    )
    entries = {slug: await learning(slug) for slug in (root_slug, race_slug, old_slug, draft_slug)}
    async with runs.sessions.begin() as db:
        await db.execute(update(KnowledgeEntry).where(
            KnowledgeEntry.id.in_([UUID(ident) for ident in entries.values()]),
        ).values(created_at=datetime.now(UTC) - timedelta(days=15)))
        postgres = db.get_bind().dialect.name == "postgresql"
    async with member(runs, "第二位知识贡献者") as (peer, _):
        for entry in entries.values():
            for index in range(5):
                actor = peer if index < 2 else runs.client
                vote = await actor.post(f"{root}/{entry}/feedback", params=scope, headers=headers(actor), json={
                    "session": f"registry-path-{index}", "positive": True,
                })
                assert vote.status_code == 204, vote.text

        async def promote(slug: str) -> Response:
            return await peer.post(f"{root}/maintenance/promote/{entries[slug]}",
                                   params=scope, headers=headers(peer), json={
                "operation_id": str(uuid4()), "source_revision": 1, "kind": "skill",
                "reason": "将经验发布为可安装流程。", "body": "Validate the release evidence.",
            })

        shared = await upload(root_slug, {})
        promoted = await promote(root_slug)
        assert promoted.status_code == 201, promoted.text
        draft = str(promoted.json()["id"])
        shared_release = await publish(shared, {})
        assert shared_release.status_code == 201, shared_release.text
        approved = await review(draft, "accept")
        assert approved.status_code == 200, approved.text
        project_skills = (await runs.client.get(registry, params=scope)).json()["items"]
        assert [(item["slug"], item["namespace"]) for item in project_skills] == [(root_slug, "platform")]

        # 同名的项目 Registry 草稿可以先占用安装名。两个入口都必须保护对方的原始草稿。
        local = await upload(draft_slug, scope)
        awaiting = await promote(draft_slug)
        assert awaiting.status_code == 201, awaiting.text
        awaiting_id = str(awaiting.json()["id"])
        blocked = await publish(local, scope)
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "SKILL_PROMOTION_PATH_CONFLICT"
        failed_review = await review(awaiting_id, "accept")
        assert failed_review.status_code == 409
        assert failed_review.json()["detail"]["code"] == "KNOWLEDGE_SKILL_PUBLISH_FAILED"
        pending = await runs.client.get(f"{root}/{awaiting_id}/review", params=scope | {"revision": 1})
        assert pending.status_code == 200 and pending.json()["revision_status"] == "pending"
        assert (await review(awaiting_id, "reject")).status_code == 200
        assert (await publish(local, scope)).status_code == 201

        competing = await upload(race_slug, scope)
        raced = (
            await asyncio.gather(promote(race_slug), publish(competing, scope))
            if postgres else (await promote(race_slug), await publish(competing, scope))
        )
        assert sorted(item.status_code for item in raced) == [201, 409], [item.text for item in raced]
        if raced[0].status_code == 201:
            assert (await review(str(raced[0].json()["id"]), "reject")).status_code == 200
            assert (await publish(competing, scope)).status_code == 201
        archived = await runs.client.post(f"{registry}/{competing}/archive", params=scope, headers=headers_owner)
        assert archived.status_code == 200, archived.text
        resubmitted = await promote(race_slug)
        assert resubmitted.status_code == 201, resubmitted.text
        restored = await runs.client.post(f"{registry}/{competing}/restore", params=scope, headers=headers_owner)
        assert restored.status_code == 409, restored.text

        legacy_draft = await promote(old_slug)
        assert legacy_draft.status_code == 201, legacy_draft.text
        legacy_skill = await upload(old_slug, scope)
        # 模拟升级前已发布但尚未重新审核的冲突历史。
        unguarded_registry = SkillRegistryService(
            uow_factory=lambda: SqlAlchemySkillsUnitOfWork(runs.sessions, project_id=project_id),
            object_storage=None,
        )
        await unguarded_registry.publish(
            actor=SkillActor(UUID(runs.user_id), workspace, "owner"), workspace_id=workspace,
            skill_id=UUID(legacy_skill), version="1.0.0", changelog="历史版本", request_id="legacy-fixture",
        )
        legacy_id = str(legacy_draft.json()["id"])
        refused = await review(legacy_id, "accept")
        assert refused.status_code == 409, refused.text
        queue = await runs.client.get(f"{root}/review", params=scope)
        assert legacy_id in {item["id"] for item in queue.json()["items"]}


async def test_repromoting_an_archived_skill_reuses_the_published_asset(runs: RunsHarness) -> None:
    """用归档前的安装名和资产 ID 重发，保留旧包及提交给审核的基线。"""
    workspace = f"/api/v1/workspaces/{runs.workspace_id}"
    root, registry = f"{workspace}/knowledge", f"{workspace}/skills"
    scope = {"project_id": str((await project(runs, "重新晋升项目"))["id"])}
    created = await runs.client.post(root, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "部署检查经验", "body": "Use the release workflow.",
        "source": {"type": "manual", "resource_path": "learnings/platform/review-deploy.md"},
        "authored_at": (datetime.now(UTC) - timedelta(days=15)).isoformat(),
    })
    assert created.status_code == 201, created.text
    source = created.json()["id"]
    nomination = f"{root}/maintenance/promote/{source}"
    request = {
        "operation_id": str(uuid4()), "source_revision": 1, "kind": "skill",
        "body": "# Deploy safely\n\nRun the tests.", "reason": "通用部署流程。",
    }
    async with member(runs, "重新晋升成员") as (peer, _):
        for index in range(5):
            actor = peer if index < 2 else runs.client
            vote = await actor.post(f"{root}/{source}/feedback", params=scope, headers=headers(actor), json={
                "session": f"repromote-{index}", "positive": True,
            })
            assert vote.status_code == 204, vote.text
        first = await runs.client.post(nomination, params=scope, headers=runs.headers(), json=request)
        assert first.status_code == 201, first.text
        entry = first.json()["id"]
        review = f"{root}/{entry}/review"
        accepted = await runs.client.post(review, params=scope, headers=runs.headers(), json={
            "revision": 1, "decision": "accept",
        })
        assert accepted.status_code == 200, accepted.text
        items = (await runs.client.get(registry, params=scope)).json()["items"]
        assert len(items) == 1 and items[0]["slug"] == "review-deploy"
        asset = f"{registry}/{items[0]['id']}"
        old_package = f"{asset}/versions/1.0.0/download"
        original = await runs.client.get(old_package, params=scope)
        assert original.status_code == 200, original.text
        archive = await runs.client.post(f"{asset}/archive", params=scope, headers=runs.headers())
        assert archive.status_code == 200, archive.text
        candidate = await peer.get(nomination, params=scope)
        assert candidate.status_code == 200 and candidate.json()["occupied_kinds"] == []
        assert candidate.json()["targets"]["skill"] == "skills/platform/review-deploy/SKILL.md"

        submitted = request | {"operation_id": str(uuid4()), "body": "Run a revised check after rollout."}
        again = await peer.post(nomination, params=scope, headers=headers(peer), json=submitted)
        assert again.status_code == 201, again.text
        assert (again.json()["id"], again.json()["revision"], again.json()["status"]) == (entry, 2, "pending")
        replay = await peer.post(nomination, params=scope, headers=headers(peer), json=submitted)
        assert replay.json()["id"] == entry and replay.json()["revision"] == 2
        duplicate = await peer.post(nomination, params=scope, headers=headers(peer), json=submitted | {
            "operation_id": str(uuid4()),
        })
        assert duplicate.status_code == 409
        assert (await runs.client.get(f"{root}/{entry}", params=scope)).status_code == 404
        assert (await runs.client.get(f"{root}/by-path", params=scope | {
            "path": "skills/platform/review-deploy/SKILL.md",
        })).status_code == 404
        assert (await runs.client.post(f"{asset}/archive", params=scope, headers=runs.headers())).status_code == 200
        assert (await peer.get(nomination, params=scope)).status_code == 404
        blocked = await runs.client.post(f"{asset}/restore", params=scope, headers=runs.headers())
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "SKILL_KNOWLEDGE_REVIEW_PENDING"
        rejected = await runs.client.post(review, params=scope, headers=runs.headers(), json={
            "revision": 2, "decision": "reject",
        })
        assert rejected.status_code == 200 and rejected.json()["status"] == "archived"
        assert (await peer.get(nomination, params=scope)).status_code == 200

        # 通用入口与晋升命令复用同一逻辑路径，普通成员可提交第二份重新晋升。
        updated = "---\nname: deployment-guide\ndescription: 发布后的检查指南\n---\n\nRun post-release verification."
        payload = {
            "operation_id": str(uuid4()), "kind": "skill", "promoted_from": source,
            "title": "新版部署检查", "body": updated,
        }
        next_draft = await peer.post(root, params=scope, headers=headers(peer), json=payload)
        assert next_draft.status_code == 201, next_draft.text
        assert (next_draft.json()["id"], next_draft.json()["revision"]) == (entry, 3)
        result = await runs.client.post(review, params=scope, headers=runs.headers(), json={
            "revision": 3, "decision": "accept",
        })
        assert result.status_code == 200, result.text
        assert result.json()["source"]["registry_version"] == "1.0.2"
        replay = await peer.post(root, params=scope, headers=headers(peer), json=payload)
        assert replay.json()["id"] == entry and replay.json()["revision_status"] == "published"
        assert (await runs.client.get(f"{root}/{entry}", params=scope)).json()["body"] == updated
        assert (await runs.client.get(old_package, params=scope)).content == original.content
        old_asset = (await runs.client.get(asset, params=scope)).json()
        assert old_asset["status"] == "active" and old_asset["slug"] == "review-deploy"
        published = (await runs.client.get(f"{asset}/versions/1.0.2", params=scope)).json()
        assert "重新晋升" in published["changelog"]

    # 原资产已有独立 Registry 草稿时，恢复和发布必须一起回滚，保留两种编辑。
    retained = await runs.client.post(f"{asset}/draft", params=scope, headers=runs.headers(), files={
        "package": ("review-deploy.skill", build_skill_package(name="review-deploy"), "application/zip"),
    })
    assert retained.status_code == 200, retained.text
    assert (await runs.client.post(f"{asset}/archive", params=scope, headers=runs.headers())).status_code == 200
    awaiting = await runs.client.post(nomination, params=scope, headers=runs.headers(), json=request | {
        "operation_id": str(uuid4()), "body": "A separate re-promotion.",
    })
    assert awaiting.status_code == 201 and awaiting.json()["revision"] == 4, awaiting.text
    refused = await runs.client.post(review, params=scope, headers=runs.headers(), json={
        "revision": 4, "decision": "accept",
    })
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "KNOWLEDGE_SKILL_PUBLISH_FAILED"
    stored = (await runs.client.get(asset, params=scope)).json()
    assert stored["status"] == "archived"
    draft = await runs.client.get(f"{asset}/draft", params=scope)
    assert draft.json()["package_sha256"] == retained.json()["package_sha256"]
    pending = await runs.client.get(review, params=scope | {"revision": 4})
    assert pending.json()["revision_status"] == "pending"
