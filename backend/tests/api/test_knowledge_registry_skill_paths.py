"""验证独立发布的 Skill 通过团队路径接入引用和会话记录。"""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_skills import build_skill_package
from weora.modules.knowledge.models import KnowledgeRecallRun, KnowledgeRecallRunHit
from weora.modules.skills.application.service import SkillActor, SkillRegistryService
from weora.modules.skills.infrastructure import backfill as package_backfill
from weora.modules.skills.infrastructure.models import SkillPackageModel, SkillSearchVersion, SkillVersionModel
from weora.modules.skills.infrastructure.unit_of_work import SqlAlchemySkillsUnitOfWork
from weora.shared.object_storage import BytesObjectSource, ObjectKey
from weora.shared.object_storage.testing import InMemoryObjectStorage

pytestmark = pytest.mark.anyio


async def test_published_registry_skills_resolve_and_open_by_scope(runs: RunsHarness) -> None:
    workspace = UUID(runs.workspace_id)
    current = str((await project(runs, "发布自动化"))["id"])
    other = str((await project(runs, "另一个项目"))["id"])
    scope, unrelated = {"project_id": current}, {"project_id": other}
    actor = SkillActor(UUID(runs.user_id), workspace, "owner")
    registry = SkillRegistryService(
        uow_factory=lambda: SqlAlchemySkillsUnitOfWork(runs.sessions, project_id=UUID(current)),
        object_storage=None,
    )
    root = f"/api/v1/workspaces/{workspace}/knowledge"
    path = "skills/ledger-verify/SKILL.md"
    slug = "ledger-verify"

    created = await runs.client.post(root, params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "LedgerReconcileFlow",
        "source": {"type": "manual", "resource_path": "learnings/reconcile"},
        "body": f"[校验](../{path})", "related": {"REFERENCES": [path]},
    })
    assert created.status_code == 201 and created.json()["relations"] == [], created.text
    assert created.json()["relation_findings"][0]["code"] == "target_unavailable"
    note = created.json()["id"]

    skill = await registry.create_skill(
        actor=actor, workspace_id=workspace, package_name=f"{slug}.skill",
        archive_bytes=build_skill_package(name=slug, description="Ledger first version"), request_id="create",
    )
    assert (await runs.client.get(f"{root}/by-path", params=scope | {"path": path})).status_code == 404
    assert (await runs.client.get(f"{root}/{skill.id}", params=scope)).status_code == 404

    async def publish(version: str) -> UUID:
        result = await registry.publish(
            actor=actor, workspace_id=workspace, skill_id=skill.id,
            version=version, changelog="发布验证", request_id=f"release-{version}",
        )
        return result.id

    first = await publish("1.0.0")
    session = {"session": "skill-relation"}
    found = await runs.client.post(
        f"{root}/recall", params=scope, headers=runs.headers(), json=session | {"query": "LedgerReconcileFlow"},
    )
    assert found.status_code == 200, found.text
    links = found.json()["items"][0]["relations"]
    assert [(link["kind"], link["target"], link["direction"]) for link in links] == [
        ("REFERENCES", str(skill.id), "out"),
    ]
    opened = await runs.client.post(
        f"{root}/by-path/open", params=scope, headers=runs.headers(), json=session | {"path": path},
    )
    assert opened.status_code == 200, opened.text
    view = opened.json()
    assert view["id"] == str(skill.id) and view["kind"] == "skill" and view["revision"] == "1.0.0"
    assert view["source"]["version_id"] == str(first) and "Ledger first version" in view["body"]
    assert view["source"]["resource_path"] == "skills/ledger-verify"
    assert view["relations"] == [
        {"kind": "REFERENCES", "target": note, "title": "LedgerReconcileFlow", "direction": "in"},
    ]
    stats = await runs.client.post(f"{root}/recall/stats", params=scope, headers=runs.headers(), json=session)
    assert stats.status_code == 200 and stats.json()["items"][0]["via_relation"] == 1, stats.text
    for params in ({}, unrelated):
        assert (await runs.client.get(f"{root}/by-path", params=params | {"path": path})).status_code == 404
        assert (await runs.client.get(f"{root}/{skill.id}", params=params)).status_code == 404

    await registry.replace_draft(
        actor=actor, workspace_id=workspace, skill_id=skill.id, package_name=f"{slug}.skill",
        archive_bytes=build_skill_package(name=slug, description="Ledger second version"), request_id="draft",
    )
    prior = (await runs.client.get(f"{root}/{skill.id}", params=scope)).json()
    assert prior["source"]["version_id"] == str(first) and "second version" not in prior["body"]
    second = await publish("1.1.0")
    latest = (await runs.client.get(f"{root}/{skill.id}", params=scope)).json()
    assert latest["source"]["version_id"] == str(second) and "Ledger second version" in latest["body"]

    async def archive(value: bool) -> None:
        await registry.set_archived(
            actor=actor, workspace_id=workspace, skill_id=skill.id, archived=value, request_id="archive",
        )

    await archive(True)
    hidden = (await runs.client.get(f"{root}/{note}", params=scope)).json()
    assert hidden["relations"] == [] and hidden["relation_findings"][0]["code"] == "target_unavailable"
    assert (await runs.client.get(f"{root}/{skill.id}", params=scope)).status_code == 404
    await archive(False)
    restored = (await runs.client.get(f"{root}/{note}", params=scope)).json()
    assert restored["relations"][0]["target"] == str(skill.id) and restored["relation_findings"] == []


async def test_shared_skill_reference_uses_verified_historical_storage(runs: RunsHarness) -> None:
    workspace = UUID(runs.workspace_id)
    root = f"/api/v1/workspaces/{workspace}"
    slug = "shared-validation"
    package = await runs.client.post(f"{root}/skills", headers=runs.headers(), files={
        "package": (f"{slug}.skill", build_skill_package(name=slug), "application/zip"),
    })
    assert package.status_code == 201, package.text
    identity = package.json()["id"]
    release = await runs.client.post(
        f"{root}/skills/{identity}/publish", headers=runs.headers(),
        json={"version": "1.0.0", "changelog": "共同体验证"},
    )
    assert release.status_code == 201, release.text
    published = release.json()

    storage = InMemoryObjectStorage()
    runs.app.state.object_storage = storage
    legacy = ObjectKey(f"skills/{workspace}/fixture-legacy.skill")
    key = (workspace, published["package_sha256"])
    async with runs.sessions() as db, db.begin():
        version = await db.get(SkillVersionModel, UUID(published["id"]))
        record = await db.get(SkillPackageModel, key)
        assert version is not None and record is not None
        archive = record.archive_bytes
        version.storage_key = legacy.value
        await db.delete(record)
        # 模拟旧版本只有对象存储，不曾经过发布事务里的正文索引。
        summary = await db.get(SkillSearchVersion, version.id)
        assert summary is not None
        await db.delete(summary)

    knowledge = f"{root}/knowledge"
    path = f"skills/{slug}"
    note = await runs.client.post(knowledge, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": "共享发布说明",
        "source": {"type": "manual", "resource_path": "learnings/release-notes"},
        "body": f"[确认说明](../{path}/SKILL.md)",
    })
    assert note.status_code == 201 and note.json()["relations"][0]["target"] == identity, note.text

    await storage.put(legacy, BytesObjectSource(b"damaged", content_type="application/zip"))
    failed = await runs.client.get(f"{knowledge}/by-path", params={"path": path})
    assert failed.status_code == 409 and failed.json()["detail"]["code"] == "KNOWLEDGE_SKILL_UNAVAILABLE"
    async with runs.sessions() as db:
        assert await db.get(SkillPackageModel, key) is None

    await storage.delete(legacy)
    await storage.put(legacy, BytesObjectSource(archive, content_type="application/zip"))
    visible = await runs.client.get(f"{knowledge}/{identity}")
    assert visible.status_code == 200 and "# 使用说明" in visible.json()["body"], visible.text
    assert visible.json()["relations"][0]["target"] == note.json()["id"]
    await storage.delete(legacy)
    cached = (await runs.client.get(f"{knowledge}/by-path", params={"path": path})).json()
    assert cached["body"] == visible.json()["body"]
    indexed = (await runs.client.get(knowledge, params={"query": slug})).json()
    assert [item["id"] for item in indexed["items"] if item["kind"] == "skill"] == [identity]
    scope = {"project_id": str((await project(runs, "共享 Skill 召回"))["id"])}
    params = scope | {"query": slug, "recall_mode": "1"}
    assert (await runs.client.get(knowledge, params=params)).json()["items"] == []
    opened = scope | {"path": path, "recall_mode": "1"}
    assert (await runs.client.get(f"{knowledge}/by-path", params=opened)).status_code == 404
    enabled = await runs.client.put(
        f"{knowledge}/settings/recall-scope", params=scope, headers=runs.headers(), json={"inherit_shared": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert [row["id"] for row in (await runs.client.get(knowledge, params=params)).json()["items"]] == [identity]
    assert (await runs.client.get(f"{knowledge}/by-path", params=opened)).json()["id"] == identity


async def test_legacy_backfill_recovers_project_skill_search(
    runs: RunsHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = UUID(runs.workspace_id)
    project_id = UUID(str((await project(runs, "迁移旧包"))["id"]))
    registry = SkillRegistryService(
        uow_factory=lambda: SqlAlchemySkillsUnitOfWork(runs.sessions, project_id=project_id),
        object_storage=None,
    )
    actor = SkillActor(UUID(runs.user_id), workspace, "owner")
    slug = "legacy-local-asset"
    skill = await registry.create_skill(
        actor=actor, workspace_id=workspace, package_name=f"{slug}.skill",
        archive_bytes=build_skill_package(name=slug), request_id="draft",
    )
    release = await registry.publish(
        actor=actor, workspace_id=workspace, skill_id=skill.id,
        version="1.0.0", changelog="旧包验证", request_id="release",
    )
    storage = InMemoryObjectStorage()
    key = ObjectKey(f"skills/{workspace}/project-legacy.skill")
    locator = (workspace, release.package_sha256)
    async with runs.sessions() as db, db.begin():
        version = await db.get(SkillVersionModel, release.id)
        record = await db.get(SkillPackageModel, locator)
        index = await db.get(SkillSearchVersion, release.id)
        assert version is not None and record is not None and index is not None
        version.storage_key = key.value
        archive = record.archive_bytes
        await db.delete(record)
        await db.delete(index)

    await storage.put(key, BytesObjectSource(archive, content_type="application/zip"))
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(package_backfill, "get_settings", lambda: SimpleNamespace(database_url="fixture"))
    monkeypatch.setattr(package_backfill, "create_database", lambda _: (engine, runs.sessions))
    monkeypatch.setattr(package_backfill, "create_object_storage", lambda _: storage)
    assert await package_backfill.backfill(workspace, apply=False) == 0
    async with runs.sessions() as db:
        assert await db.get(SkillSearchVersion, release.id) is None
    assert await package_backfill.backfill(workspace, apply=True) == 0
    assert engine.dispose.await_count == 2
    async with runs.sessions() as db:
        assert await db.get(SkillSearchVersion, release.id) is not None
        assert await db.get(SkillPackageModel, locator) is not None


async def test_registry_skill_body_search_rank_and_exact_adoption(runs: RunsHarness) -> None:
    workspace = UUID(runs.workspace_id)
    project_id = UUID(str((await project(runs, "流水验收"))["id"]))
    scope = {"project_id": str(project_id)}
    other = {"project_id": str((await project(runs, "隔离项目"))["id"])}
    root = f"/api/v1/workspaces/{workspace}/knowledge"
    slug = "ledger-procedure"
    registry = SkillRegistryService(
        uow_factory=lambda: SqlAlchemySkillsUnitOfWork(runs.sessions, project_id=project_id),
        object_storage=None,
    )
    actor = SkillActor(UUID(runs.user_id), workspace, "owner")

    def archive(body: str) -> bytes:
        content = BytesIO()
        with ZipFile(content, "w") as package:
            package.writestr(
                f"{slug}/SKILL.md",
                f"---\nname: {slug}\ndescription: 流水核对\n"
                f"title: LedgerProcedure\ntags: [receipts]\n---\n\n{body}\n",
            )
        return content.getvalue()

    async def search(query: str, params: dict[str, str] = scope) -> list[str]:
        result = await runs.client.get(root, params=params | {"query": query})
        assert result.status_code == 200, result.text
        return [item["id"] for item in result.json()["items"]]

    async def invoke(suffix: str, **data: object) -> dict[str, object]:
        result = await runs.client.post(f"{root}/{suffix}", params=scope, headers=runs.headers(), json=data)
        assert result.status_code == 200, result.text
        payload: dict[str, object] = result.json()
        return payload

    async def publish(version: str) -> UUID:
        released = await registry.publish(
            actor=actor, workspace_id=workspace, skill_id=created.id,
            version=version, changelog="验收", request_id=version,
        )
        return released.id

    created = await registry.create_skill(
        actor=actor, workspace_id=workspace, package_name=f"{slug}.skill",
        archive_bytes=archive("Run cobaltsettlement twice."), request_id="draft",
    )
    assert await search("cobaltsettlement") == []
    first = await publish("1.0.0")
    identity = str(created.id)
    assert await search("cobaltsettlement") == [identity]
    assert await search("cobaltsettlement", other) == []
    assert await search("cobaltsettlement", {}) == []
    # TeamAI 要求 Skill 的召回至少命中标题或标签，正文继续参与评分。
    assert (await invoke("recall/preview", query="cobaltsettlement", recall_mode=True))["items"] == []
    assert await invoke(
        "recall/preview", query="LedgerProcedure receipts cobaltsettlement", check=True, recall_mode=True,
    ) == {"relevant": True}
    explained = await invoke(
        "recall/preview", query="LedgerProcedure receipts cobaltsettlement", check=True,
        check_details=True, recall_mode=True,
    )
    assert explained["entry_id"] == identity and explained["resource_path"] == f"skills/{slug}"
    assert explained["matched_terms"] == ["LedgerProcedure", "receipts"]
    assert explained["missing_terms"] == ["cobaltsettlement"] and "excerpt" not in explained
    async with runs.sessions() as db:
        assert await db.scalar(select(KnowledgeRecallRun.id).where(KnowledgeRecallRun.project_id == project_id)) is None

    prior = await invoke("recall", query="LedgerProcedure cobaltsettlement", session="first", recall_mode=True)
    items = prior["items"]
    assert isinstance(items, list) and items[0]["id"] == identity
    assert items[0]["source"]["version_id"] == str(first)
    assert items[0]["matched_terms"] == ["LedgerProcedure"] and "cobaltsettlement" in items[0]["excerpt"]

    await registry.replace_draft(
        actor=actor, workspace_id=workspace, skill_id=created.id, package_name=f"{slug}.skill",
        archive_bytes=archive("Use scarletsettlement now."), request_id="revision",
    )
    assert await search("scarletsettlement") == []
    second = await publish("1.1.0")
    assert await search("cobaltsettlement") == []
    assert await search("scarletsettlement") == [identity]

    opened = await invoke("by-path/open", path=f"skills/{slug}", session="first")
    source = opened["source"]
    assert isinstance(source, dict) and source["version_id"] == str(second)
    stats = (await invoke("recall/stats", session="first"))["items"]
    assert isinstance(stats, list) and stats[0]["adopted"] == 0
    await invoke("recall", query="receipts scarletsettlement", session="second", recall_mode=True)
    await invoke("by-path/open", path=f"skills/{slug}/SKILL.md", session="second")
    stats = (await invoke("recall/stats", session="second"))["items"]
    assert isinstance(stats, list) and stats[0]["adopted"] == 1

    async with runs.sessions() as db:
        rows = list(await db.scalars(
            select(KnowledgeRecallRunHit).where(KnowledgeRecallRunHit.entry_id == created.id),
        ))
        assert {(row.skill_version_id, row.skill_opened_at is not None) for row in rows} == {
            (first, False), (second, True),
        }

    await registry.set_archived(
        actor=actor, workspace_id=workspace, skill_id=created.id, archived=True, request_id="archive",
    )
    assert await search("scarletsettlement") == []
