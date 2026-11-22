"""验证 Skill 目录与项目选择覆盖贡献、检索、分发和审核。"""

from uuid import uuid4

import pytest

from tests.api.test_agent_mail import device
from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_skills import build_skill_package

pytestmark = pytest.mark.anyio


async def test_namespaced_skill_selection_preserves_paths_and_installation_scope(runs: RunsHarness) -> None:
    path = f"/api/v1/workspaces/{runs.workspace_id}"
    skill_route = f"{path}/skills"
    knowledge = f"{path}/knowledge"
    dist = f"{path}/skill-distribution"
    scope = {"project_id": str((await project(runs, "交付项目"))["id"])}
    other = {"project_id": str((await project(runs, "隔离项目"))["id"])}
    slug = "billing-checklist"
    device_id, auth = await device(runs.client, f"{path}/agent-collaboration", "命名空间设备")
    grant = await runs.client.put(
        f"{dist}/devices/{device_id}/grant", headers=runs.headers(), json={"usage_enabled": True},
    )
    assert grant.status_code == 200, grant.text

    def upload(description: str) -> dict[str, tuple[str, bytes, str]]:
        return {"package": (
            f"{slug}.skill", build_skill_package(name=slug, description=description), "application/zip",
        )}

    async def create(params: dict[str, str], namespace: str, description: str, *, status: int = 201) -> str:
        response = await runs.client.post(
            skill_route, params=params, headers=runs.headers(), data={"namespace": namespace},
            files=upload(description),
        )
        assert response.status_code == status, response.text
        return str(response.json().get("id"))

    async def publish(skill: str, params: dict[str, str], version: str) -> str:
        response = await runs.client.post(
            f"{skill_route}/{skill}/publish", params=params, headers=runs.headers(),
            json={"version": version, "changelog": "发布目录夹具"},
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def manifest(params: dict[str, str], status: int = 200) -> dict:
        response = await runs.client.get(f"{dist}/manifest", params={"runtime": "codex"} | params, headers=auth)
        assert response.status_code == status, response.text
        return response.json()

    shared = await create({}, "", "Shared ledger anchor.")
    shared_release = await publish(shared, {}, "1.0.0")
    shared_folder = await create({}, "retail/archive", "Catalog-only ledger anchor.")
    await publish(shared_folder, {}, "1.1.0")
    await create(scope, "../escaping", "Invalid", status=422)
    await create({"project_id": str(uuid4())}, "finance", "Forbidden", status=404)
    project_root = await create(scope, "", "Project root ledger anchor.")
    await publish(project_root, scope, "2.0.0")

    def push(params: dict[str, str], caption: str):
        return runs.client.post(
            f"{dist}/contributions", params=params, headers=auth,
            data={"namespace": r"engineering\release"}, files=upload(caption),
        )

    pending = await push(scope, "Project engineering ledger anchor.")
    assert pending.status_code == 201, pending.text
    selection = pending.json()["skill_id"]
    repeat = await push(scope, "Project engineering ledger anchor.")
    assert repeat.json() == pending.json()
    assert (await manifest(scope))["items"][0]["resource_path"] == f"skills/{slug}"
    assert (await runs.client.get(f"{skill_route}/{selection}")).status_code == 404
    assert (await runs.client.get(f"{skill_route}/{selection}", params=other)).status_code == 404
    details = await runs.client.get(f"{skill_route}/{selection}", params=scope)
    assert details.json()["namespace"] == "engineering/release"
    first_version = await publish(selection, scope, "2.1.0")
    stats_url = f"{skill_route}/{selection}/usage"
    dates = {"from": "2026-01-01T00:00:00Z", "to": "2026-01-03T00:00:00Z"}
    stats = await runs.client.get(stats_url, params=scope | dates | {"version_id": first_version})
    assert stats.status_code == 200 and stats.json()["totals"]["uses"] == 0, stats.text
    assert (await runs.client.get(stats_url, params=other | dates)).status_code == 404
    assert (await runs.client.get(stats_url, params=dates)).status_code == 404
    await create(scope, "engineering/release", "Duplicate", status=409)
    alternative_push = await push(scope, "Propose a revised procedure.")
    proposal = alternative_push.json()["contribution_id"]
    assert proposal is not None and alternative_push.json()["skill_id"] == selection
    proposals = await runs.client.get(f"{skill_route}/{selection}/contributions", params=scope)
    assert [item["id"] for item in proposals.json()] == [proposal]

    nested = f"skills/engineering/release/{slug}"
    shared_rows = (await manifest({}))["items"]
    project_rows = (await manifest(scope))["items"]
    assert [(row["resource_path"], row["version_id"]) for row in shared_rows] == [(f"skills/{slug}", shared_release)]
    assert [(row["resource_path"], row["version_id"]) for row in project_rows] == [(nested, first_version)]
    assert (await manifest(other))["items"] == shared_rows
    resolve = await runs.client.get(f"{dist}/resolve", params=scope | {"slug": slug}, headers=auth)
    assert resolve.status_code == 200 and resolve.json()["resource_path"] == nested, resolve.text
    fallback = await runs.client.get(f"{dist}/resolve", params=scope | {"slug": slug, "version": "1.0.0"}, headers=auth)
    assert fallback.status_code == 404
    release_path = f"{dist}/versions/{first_version}/package"
    assert (await runs.client.get(release_path, params=other, headers=auth)).status_code == 404
    assert (await runs.client.get(release_path, params=scope, headers=auth)).status_code == 200

    options = scope | {"path": f"{nested}/SKILL.md"}
    precise = await runs.client.get(f"{knowledge}/by-path", params=options)
    assert precise.status_code == 200, precise.text
    assert precise.json()["id"] == selection and precise.json()["source"]["resource_path"] == nested
    assert (await runs.client.get(f"{knowledge}/by-path", params=other | {"path": nested})).status_code == 404
    root_in_project = await runs.client.get(f"{knowledge}/by-path", params=scope | {"path": f"skills/{slug}"})
    assert root_in_project.json()["id"] == project_root
    created = await runs.client.post(
        knowledge, params=scope, headers=runs.headers(),
        json={
            "operation_id": str(uuid4()), "title": "审核路径", "body": f"使用 [发布核对](../../{nested}/SKILL.md)。",
            "kind": "doc", "source": {"type": "file", "ref": "docs/engineering/review.md"},
        },
    )
    ident = created.json()["id"]
    reviewed = await runs.client.post(
        f"{knowledge}/{ident}/review", params=scope, headers=runs.headers(), json={"revision": 1, "decision": "accept"},
    )
    assert reviewed.status_code == 200, reviewed.text
    linked = await runs.client.get(f"{knowledge}/{ident}", params=scope)
    assert linked.json()["relations"][0]["target"] == selection

    recall = await runs.client.post(
        f"{knowledge}/recall", params=scope, headers=runs.headers(),
        json={"query": slug, "session": "namespace-recall"},
    )
    assert recall.status_code == 200, recall.text
    assert [row["id"] for row in recall.json()["items"] if row["kind"] == "skill"] == [selection]
    duplicate = await create(scope, "partners/ledger", "Conflicting project ledger anchor.")
    await publish(duplicate, scope, "3.0.0")
    conflict = await manifest(scope, status=409)
    assert conflict["detail"]["slug"] == slug and conflict["detail"]["code"] == "SKILL_NAMESPACE_CONFLICT"
    assert (await runs.client.get(f"{dist}/resolve", params=scope | {"slug": slug}, headers=auth)).status_code == 409
    assert (await runs.client.get(release_path, params=scope, headers=auth)).status_code == 200
    archived = await runs.client.post(f"{skill_route}/{duplicate}/archive", params=scope, headers=runs.headers())
    assert archived.status_code == 200, archived.text
    assert (await manifest(scope))["items"] == project_rows


async def test_project_rule_namespace_replaces_only_matching_shared_root_rule(runs: RunsHarness) -> None:
    base = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    selected = {"project_id": str((await project(runs, "支付组"))["id"])}
    other = {"project_id": str((await project(runs, "安全组"))["id"])}

    async def submit_rule(params: dict[str, str], path: str, title: str) -> str:
        response = await runs.client.post(
            base, params=params, headers=runs.headers(),
            json={
                "operation_id": str(uuid4()), "kind": "rule", "title": title,
                "body": "namespace anchor", "source": {"type": "file", "ref": path},
            },
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    async def accept(params: dict[str, str], identity: str) -> None:
        response = await runs.client.post(
            f"{base}/{identity}/review", params=params, headers=runs.headers(),
            json={"revision": 1, "decision": "accept"},
        )
        assert response.status_code == 200, response.text

    async def query(params: dict[str, str]) -> set[str]:
        response = await runs.client.get(base, params=params | {"query": "namespace anchor"})
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    root = await submit_rule({}, "rules/review_rules.md", "Shared primary rule")
    near = await submit_rule({}, "rules/review-rules.md", "Shared different rule")
    private = await submit_rule(selected, "rules/payments/review_rules.md", "Payments rule")
    nested = await submit_rule(selected, "rules/standards/ledger/review-rules.md", "Nested standards")
    foreign = await submit_rule(other, "rules/security/review_rules.md", "Security rule")
    for params, identity in (({}, root), ({}, near), (selected, nested), (other, foreign)):
        await accept(params, identity)

    assert await query(selected) == {root, near, nested}
    await accept(selected, private)
    assert await query(selected) == {private, near, nested}
    assert await query(other) == {foreign, near}
    assert await query({}) == {root, near}
    exact = await runs.client.get(f"{base}/by-path", params=selected | {"path": "rules/payments/review_rules.md"})
    assert exact.status_code == 200 and exact.json()["id"] == private
