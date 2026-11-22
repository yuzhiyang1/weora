"""验证领域声明的优先级、发布边界及 Registry Skill 排序。"""

from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.models import KnowledgeRevision
from weora.modules.skills.infrastructure.models import SkillPackageModel, SkillSearchVersion, SkillVersionModel
from weora.shared.object_storage import BytesObjectSource, ObjectKey
from weora.shared.object_storage.testing import InMemoryObjectStorage

pytestmark = pytest.mark.anyio


async def test_recall_domain_switches_only_when_a_revision_is_approved(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    scope = {"project_id": str((await project(runs, "召回领域验证"))["id"])}
    title = "GPU RelayPlan"

    async def post(title: str, body: str, **metadata: object) -> str:
        response = await runs.client.post(root, params=scope, headers=runs.headers(), json={
            "operation_id": str(uuid4()), "title": title, "body": body, **metadata,
        })
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    technical = await post(title, "Quartz change log.", domain="technical", tags=["oncall"])
    ops = await post(title, "Circuit incident outline.", domain="ops", tags=["oncall"])
    exact_tag = await post(
        "Operational notes", "Details for the team.", tags=["DoCkEr"],
        source={"type": "manual", "resource_path": "docs/sre/diagnostics.md"},
    )
    path_inferred = await post(
        "SRE handover", "Details for the team.", tags=["cache repair"],
        source={"type": "manual", "resource_path": "docs/sre/handovers.md"},
    )
    fallback = await post("General checklist", "Details for the team.", kind="rule")
    async with runs.sessions() as db:
        for entry, expected in ((technical, "technical"), (ops, "ops"), (exact_tag, "technical"),
                                (path_inferred, "ops"), (fallback, "technical")):
            row = await db.get(KnowledgeRevision, (UUID(entry), 1))
            assert row is not None and row.search_domain == expected

    async def recall() -> list[str]:
        response = await runs.client.get(root, params=scope | {"query": "GPU", "recall_mode": "1"})
        assert response.status_code == 200, response.text
        return [str(item["id"]) for item in response.json()["items"]]

    assert await recall() == [ops, technical]
    updated = await runs.client.post(f"{root}/{ops}/revisions", params=scope, headers=runs.headers(), json={
        "operation_id": str(uuid4()), "title": title, "body": "Circuit incident outline.",
        "domain": "support", "tags": ["oncall"], "base_revision": 1,
    })
    assert updated.status_code == 201 and updated.json()["source"]["domain"] == "support", updated.text
    assert await recall() == [ops, technical]
    approved = await runs.client.post(f"{root}/{ops}/review", params=scope, headers=runs.headers(), json={
        "revision": 2, "decision": "accept",
    })
    assert approved.status_code == 200, approved.text
    assert await recall() == [technical, ops]


async def test_registry_recall_uses_skill_domain_from_the_fixed_package(runs: RunsHarness) -> None:
    root = f"/api/v1/workspaces/{runs.workspace_id}"

    async def release(slug: str, metadata: str) -> tuple[str, UUID]:
        bundle = BytesIO()
        with ZipFile(bundle, "w") as archive:
            archive.writestr(f"{slug}/SKILL.md", (
                f"---\nname: {slug}\ndescription: Check status\ntitle: GPU RelayPlan\n{metadata}---\n\n"
                f"Run the {slug} checklist.\n"
            ))
        saved = await runs.client.post(f"{root}/skills", headers=runs.headers(), files={
            "package": (f"{slug}.skill", bundle.getvalue(), "application/zip"),
        })
        assert saved.status_code == 201, saved.text
        entry = saved.json()["id"]
        result = await runs.client.post(f"{root}/skills/{entry}/publish", headers=runs.headers(), json={
            "version": "1.0.0", "changelog": "召回领域测试",
        })
        assert result.status_code == 201, result.text
        return str(entry), UUID(result.json()["id"])

    technical, first = await release("support-scope-one", "")
    ops, second = await release("support-scope-two", "domain: ops\ntags: [api]\n")
    response = await runs.client.get(f"{root}/knowledge", params={"query": "gpu", "recall_mode": "1"})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [ops, technical]
    async with runs.sessions() as db:
        a = await db.get(SkillSearchVersion, first)
        b = await db.get(SkillSearchVersion, second)
        assert a is not None and a.domain == "technical"
        assert b is not None and b.domain == "ops"

    # 旧发布可能只留下对象存储和不含显式领域的词项索引。
    storage = InMemoryObjectStorage()
    runs.app.state.object_storage = storage
    workspace = UUID(runs.workspace_id)
    key = ObjectKey(f"skills/{workspace}/legacy-domain.skill")
    async with runs.sessions() as db, db.begin():
        released = await db.get(SkillVersionModel, second)
        indexed = await db.get(SkillSearchVersion, second)
        assert released is not None and indexed is not None
        archived = await db.get(SkillPackageModel, (workspace, released.package_sha256))
        assert archived is not None
        content = archived.archive_bytes
        await db.delete(archived)
        released.storage_key = key.value
        indexed.domain = "technical"
    await storage.put(key, BytesObjectSource(content, content_type="application/zip"))
    opened = await runs.client.get(f"{root}/knowledge/{ops}")
    assert opened.status_code == 200, opened.text
    async with runs.sessions() as db:
        restored = await db.get(SkillSearchVersion, second)
        assert restored is not None and restored.domain == "ops"
