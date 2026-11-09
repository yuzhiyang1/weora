"""相同交付用例替换平台策略；不使用云效/GitLab 账户，不执行远端写入。"""

import hashlib
import json
from dataclasses import asdict, replace
from uuid import UUID, uuid4

import pytest

from tests.delivery.fakes import Remote
from tests.delivery.test_service import harness, scope
from weora.modules.delivery.application.code_hosting import (
    PublishedBranch,
    RepositoryConnection,
    RepositoryDelivery,
    RepositoryRegistry,
    ReviewCapabilities,
)
from weora.modules.delivery.application.ports import RemoteResult
from weora.modules.delivery.domain.models import DeliveryError, PlanScope


def connection(*, workspace: UUID | None = None, provider: str = "codeup") -> RepositoryConnection:
    return RepositoryConnection(
        workspace or uuid4(),
        "test-remote",
        "1",
        provider,
        "test-repo",
        "123",
        "https://git.example.test/team/project.git",
        "https://api.example.test",
        "https://git.example.test/team/project",
        "test-credential-v1",
        ("main",),
        "organization",
    )


class Git:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(
        self,
        run_id: UUID,
        scope: PlanScope,
        connection: RepositoryConnection,
        business_key: str,
        title: str,
    ) -> PublishedBranch:
        self.calls += 1
        return PublishedBranch("weora/" + business_key[:24], "b" * 40)


class Reviews:
    capabilities = ReviewCapabilities(draft=True)

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.reads = self.writes = 0

    async def create_draft(
        self,
        connection: RepositoryConnection,
        branch: PublishedBranch,
        scope: PlanScope,
        business_key: str,
        title: str,
        body: str,
    ) -> RemoteResult:
        self.writes += 1
        return RemoteResult(self.provider, connection.web_url + "/review/1")

    async def reconcile(
        self,
        connection: RepositoryConnection,
        scope: PlanScope,
        business_key: str,
    ) -> RemoteResult | None:
        self.reads += 1
        return None


@pytest.mark.anyio
async def test_registry_swaps_strategies_without_business_provider_branches() -> None:
    workspace, run_id = uuid4(), uuid4()
    git = Git()
    entries = {}
    for name in ("codeup", "gitlab-test-double", "github-test-double"):
        target = replace(connection(workspace=workspace, provider=name), remote_id=name)
        entries[target] = RepositoryDelivery(target, git, Reviews(name))
    registry = RepositoryRegistry(entries)
    for target in entries:
        approved = registry.freeze(workspace, replace(scope(remote=True), remote_id=target.remote_id))
        result = await registry.create(run_id, approved, "d" * 64, "修复", "说明")
        assert result.remote_id == target.provider
        assert await registry.reconcile(approved, "d" * 64) is None
    assert git.calls == 3


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "gitlab"),
        ("version", "2"),
        ("clone_url", "https://new.example.test/repo.git"),
        ("api_origin", "https://new-api.example.test"),
        ("platform_repository", "456"),
        ("credential_ref", "new-credential"),
        ("web_url", "https://new.example.test/repo"),
    ],
)
async def test_old_plan_never_retargets_after_connection_change(field: str, value: str) -> None:
    target, remote = connection(), Remote()
    approved = RepositoryRegistry({target: remote}).freeze(target.workspace_id, scope(remote=True))
    new = replace(target, **{field: value})  # type: ignore[arg-type]
    changed = RepositoryRegistry({new: remote})
    for operation in (
        changed.create(uuid4(), approved, "d" * 64, "修复", "说明"),
        changed.reconcile(approved, "d" * 64),
    ):
        with pytest.raises(DeliveryError) as error:
            await operation
        assert error.value.code == "REMOTE_BINDING_CHANGED"
    assert remote.calls == 0


def test_workspace_repository_branch_and_legacy_plan_fail_closed() -> None:
    target = connection()
    registry = RepositoryRegistry({target: Remote()})
    for workspace, requested in (
        (uuid4(), scope(remote=True)),
        (target.workspace_id, replace(scope(remote=True), repository_id="another-repo")),
        (target.workspace_id, replace(scope(remote=True), target_branch="master")),
    ):
        with pytest.raises(DeliveryError) as error:
            registry.freeze(workspace, requested)
        assert error.value.code == "REMOTE_DENIED"
    with pytest.raises(DeliveryError, match="原远端"):
        registry._resolve(scope(remote=True))
    assert registry.freeze(uuid4(), scope()) == scope()


@pytest.mark.anyio
async def test_capability_is_checked_before_git_side_effect_and_readback_never_publishes() -> None:
    target, git, review = connection(), Git(), Reviews("codeup")
    review.capabilities = ReviewCapabilities(draft=False)
    adapter = RepositoryDelivery(target, git, review)
    with pytest.raises(DeliveryError) as error:
        await adapter.create(uuid4(), scope(remote=True), "d" * 64, "修复", "说明")
    assert error.value.code == "CAPABILITY_UNSUPPORTED"
    assert await adapter.reconcile(scope(remote=True), "d" * 64) is None
    assert git.calls == review.writes == 0 and review.reads == 1


@pytest.mark.anyio
async def test_platform_freezes_plan_and_unsupported_capability_is_not_unknown() -> None:
    service, actor, access, store, _, _ = harness()
    target = connection(workspace=actor.workspace_id)
    git, review = Git(), Reviews("codeup")
    review.capabilities = ReviewCapabilities(draft=False)
    registry = RepositoryRegistry({target: RepositoryDelivery(target, git, review)})
    service.remote = registry
    service.remote_bindings = registry
    plan = await service.propose(actor, access.run_id, scope(remote=True), "修复", "风险")
    assert plan.scope.remote_binding == target.fingerprint
    assert await service.propose(actor, access.run_id, scope(remote=True), "修复", "风险") == plan
    await service.decide(actor, access.run_id, plan.id, "approve")
    with pytest.raises(DeliveryError) as error:
        await service.create_pr(actor, access.run_id, "修复", "说明", 7)
    assert error.value.code == "CAPABILITY_UNSUPPORTED"
    assert (await store.remotes(actor.workspace_id, access.run_id))[0].status == "FAILED"
    assert "RECOVERY_REQUIRED" not in access.states and git.calls == 0


def test_legacy_plan_digest_is_preserved_and_bound_plan_differs() -> None:
    original = scope(remote=True)
    values = asdict(original)
    values.pop("remote_binding")
    # 历史审批没有该可选字段；新增能力不能改变既有摘要。
    values.pop("source_delivery_id")
    values.pop("source_branch")
    values.pop("source_revision")
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert original.digest == digest
    assert replace(original, remote_binding="b" * 64).digest != digest


def test_duplicate_registration_is_rejected() -> None:
    target = connection()
    with pytest.raises(ValueError):
        RepositoryRegistry({target: Remote(), replace(target, version="2"): Remote()})
