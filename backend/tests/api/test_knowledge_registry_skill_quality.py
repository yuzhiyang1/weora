"""验证独立 Skill 质量提案的基准、权限和发布事务。"""

import asyncio
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest

from tests.api.test_knowledge import _quality_recalls
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.skills.application.service import SkillActor, SkillRegistryService
from weora.modules.skills.infrastructure.unit_of_work import SqlAlchemySkillsUnitOfWork

pytestmark = pytest.mark.anyio


def package(slug: str, body: str) -> bytes:
    """用已支持的 CRLF 分隔符和额外字段验证修订保留包头。"""
    data = BytesIO()
    with ZipFile(data, "w") as bundle:
        manifest = f"---\r\nname: {slug}\r\ndescription: Billing checks\r\nreview: keep-me\r\n--- \r\n"
        bundle.writestr(f"{slug}/SKILL.md", manifest + body)
        bundle.writestr(f"{slug}/references/checklist.md", "# 核对清单\n")
    return data.getvalue()


async def test_registry_quality_proposal_releases_into_original_skill(runs: RunsHarness) -> None:
    workspace = UUID(runs.workspace_id)
    project_id = UUID(str((await project(runs, "质量修订"))["id"]))
    scope = {"project_id": str(project_id)}
    other = {"project_id": str((await project(runs, "隔离项目"))["id"])}
    root = f"/api/v1/workspaces/{workspace}/knowledge"
    actor = SkillActor(UUID(runs.user_id), workspace, "owner")
    registry = SkillRegistryService(
        uow_factory=lambda: SqlAlchemySkillsUnitOfWork(runs.sessions, project_id=project_id), object_storage=None,
    )
    slug = "billing-checks"
    filename = f"{slug}.skill"
    original = "Record original billing samples."
    archive = package(slug, original)
    skill = await registry.create_skill(
        actor=actor, workspace_id=workspace, package_name=filename, archive_bytes=archive, request_id="quality-fixture",
        namespace="billing/settlement",
    )

    async def publish(version: str) -> UUID:
        return (await registry.publish(
            actor=actor, workspace_id=workspace, skill_id=skill.id, version=version,
            changelog="质量验收", request_id=version,
        )).id

    async def replace(archive_bytes: bytes) -> str:
        return (await registry.replace_draft(
            actor=actor, workspace_id=workspace, skill_id=skill.id,
            package_name=filename, archive_bytes=archive_bytes, request_id="独立编辑",
        )).package_sha256

    # Registry 可按任意有效版本号发布，自动生成补丁号得避开历史版本。
    previous_patch = await publish("1.0.1")
    await replace(archive)
    base = await publish("1.0.0")
    quality = f"{root}/maintenance/quality"
    endpoint = f"{quality}/{skill.id}"
    assert (await runs.client.get(quality, params=scope)).json()["items"] == []
    await _quality_recalls(runs, root, scope, slug)
    listed = await runs.client.get(quality, params=scope | {"limit": "1"})
    assert listed.status_code == 200, listed.text
    candidate, = listed.json()["items"]
    assert candidate["id"] == str(skill.id) and candidate["skill_version_id"] == str(base)
    assert (candidate["recalls"], candidate["upvotes"], candidate["recallers"]) == (5, 0, 2)
    assert (await runs.client.get(quality, params=other)).json()["items"] == []
    details = await runs.client.get(endpoint, params=scope)
    assert (details.json()["skill_version_id"], details.json()["body"]) == (str(base), original)
    assert (await runs.client.get(endpoint, params=other)).status_code == 404
    health = await runs.client.get(f"{root}/health", params=scope)
    assert health.status_code == 200, health.text
    assert health.json()["maintenance"]["quality"]["items"][0]["id"] == str(skill.id)

    body = "Reconcile ledger with unique sentinel kaleidoscope."
    request = {"operation_id": str(uuid4()), "base_skill_version_id": str(base), "body": body}
    async with member(runs, "普通质量修订成员") as (teammate, _):
        assert (await teammate.post(endpoint, params=scope, headers=headers(teammate), json=request)).status_code == 403

    async def submit(payload: dict[str, str] = request, expected: int = 201) -> str:
        response = await runs.client.post(endpoint, params=scope, headers=runs.headers(), json=payload)
        assert response.status_code == expected, response.text
        return str(response.json().get("id"))

    async def review(ident: str, decision: str, expected: int = 200) -> dict[str, object]:
        response = await runs.client.post(
            f"{root}/{ident}/review", params=scope, headers=runs.headers(),
            json={"revision": 1, "decision": decision},
        )
        assert response.status_code == expected, response.text
        result: dict[str, object] = response.json()
        return result

    await submit(request | {"base_revision": "1"}, expected=422)
    bind = runs.sessions.kw.get("bind")
    if bind is not None and bind.dialect.name == "postgresql":
        first, replay = await asyncio.gather(submit(), submit())
        assert first == replay
    else:
        first = await submit()
        assert await submit() == first
    await submit(request | {"operation_id": str(uuid4())}, expected=409)
    assert (await runs.client.get(quality, params=scope)).json()["items"] == []
    queue = (await runs.client.get(f"{root}/review", params=scope)).json()["items"]
    assert [item["id"] for item in queue] == [first]
    preview = await runs.client.get(f"{root}/{first}/review", params=scope | {"revision": "1"})
    assert preview.json()["body"] == body
    published = (await runs.client.get(f"{root}/{skill.id}", params=scope)).json()
    assert published["body"].endswith(original) and published["source"]["version_id"] == str(base)
    assert (await review(first, "reject"))["revision_status"] == "rejected"

    request["operation_id"] = str(uuid4())
    second = await submit()
    draft_digest = await replace(package(slug, "An independent edit."))
    await review(second, "accept", expected=409)
    still_draft = await registry.get_skill(actor=actor, workspace_id=workspace, skill_id=skill.id)
    assert still_draft.draft is not None and still_draft.draft.package_sha256 == draft_digest
    assert still_draft.latest_published_version == "1.0.0"
    assert (await runs.client.get(f"{root}/review", params=scope)).json()["items"][0]["id"] == second
    await replace(archive)

    approved = await review(second, "accept")
    assert approved["revision_status"] == "published"
    assert await review(second, "accept") == approved
    assert await submit() == second
    await submit(request | {"operation_id": str(uuid4())}, expected=409)
    assert (await runs.client.get(f"{root}/{second}", params=scope)).status_code == 404
    blocked = await runs.client.post(f"{root}/{second}/restore", params=scope, headers=runs.headers())
    assert blocked.status_code == 409

    versions = await registry.list_versions(actor=actor, workspace_id=workspace, skill_id=skill.id)
    assert [entry.version for entry in versions] == ["1.0.2", "1.0.0", "1.0.1"]
    assert {entry.id for entry in versions[1:]} == {base, previous_patch}
    updated = await registry.read_version_archive(workspace, versions[0].id)
    with ZipFile(BytesIO(updated)) as revised, ZipFile(BytesIO(archive)) as old:
        assert b"review: keep-me" in revised.read(f"{slug}/SKILL.md")
        assert body in revised.read(f"{slug}/SKILL.md").decode()
        ref = f"{slug}/references/checklist.md"
        assert revised.read(ref) == old.read(ref)
    assert (await registry.read_version_markdown(workspace, base)).endswith(original)
    search = await runs.client.get(root, params=scope | {"query": "kaleidoscope"})
    assert search.status_code == 200, search.text
    assert [item["id"] for item in search.json()["items"]] == [str(skill.id)]
