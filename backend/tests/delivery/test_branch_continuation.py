"""原 PR 的续写必须是同 Task、重新审批、精确旧 SHA；远端变化和未知结果不重放。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from tests.delivery.test_code_hosting import connection
from tests.delivery.test_git_publisher import Changes, LocalGitTransport, bare_fixture, credentials
from tests.delivery.test_service import harness, scope
from weora.modules.delivery.application.ports import RemoteResult
from weora.modules.delivery.domain.models import DeliveryError, PlanRecord, PlanScope, RemoteRecord
from weora.modules.delivery.infrastructure.git import CliGitPublisher, IsolatedGitRunner, git_environment
from weora.modules.runs.application.ports import RunActor


@pytest.mark.anyio
@pytest.mark.parametrize("revert_to_base", [False, True])
async def test_real_git_continues_same_branch_with_parent_and_refuses_stale_sha(
    tmp_path: Path, revert_to_base: bool
) -> None:
    remote = tmp_path / "upstream.git"
    revision = await bare_fixture(remote)
    target = connection()
    selected = replace(scope(remote=True), base_revision=revision, remote_binding=target.fingerprint)
    changes = Changes()
    transport = LocalGitTransport(remote, target.clone_url)
    publisher = CliGitPublisher(changes, credentials(), transport)
    first = await publisher.publish(uuid4(), selected, target, "d" * 64, "first approved delivery")
    continued = replace(
        selected, source_delivery_id=str(uuid4()), source_branch=first.name, source_revision=first.revision
    )
    if revert_to_base:
        changes.changes = []
    else:
        # 第二轮保留其余已完成修改，仅更改一个文件。
        changes.changes[0] = {"path": "src/a.py", "content": "c2Vjb25kCg=="}
    second = await publisher.publish(uuid4(), continued, target, "e" * 64, "review fix", previous=first)
    git, env = IsolatedGitRunner(), git_environment(remote, target, credentials().get(target.credential_ref))
    assert second.name == first.name and second.revision != first.revision
    assert (await git.run(remote, env, "rev-parse", second.revision + "^1")).decode().strip() == first.revision
    assert (await git.run(remote, env, "rev-parse", "main")).decode().strip() == revision
    expected = b"original\n" if revert_to_base else b"second\n"
    assert await git.run(remote, env, "show", second.revision + ":src/a.py") == expected
    with pytest.raises(DeliveryError) as error:
        await publisher.publish(uuid4(), continued, target, "f" * 64, "stale retry", previous=first)
    assert error.value.code == "BRANCH_CHANGED"
    assert (await git.run(remote, env, "rev-parse", first.name)).decode().strip() == second.revision


@pytest.mark.anyio
@pytest.mark.parametrize("same_task,result_unknown", [(False, False), (True, False), (True, True)])
async def test_service_freezes_previous_delivery_and_requires_new_approval(
    same_task: bool, result_unknown: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, actor, access, store, _, _ = harness()
    now, previous_id = datetime.now(UTC), uuid4()
    original_plan = PlanRecord(
        uuid4(),
        previous_id,
        actor.workspace_id,
        actor.user_id,
        1,
        replace(scope(remote=True), actions=("git.commit", "git.push")),
        "原分支",
        "验收",
        "APPROVED",
        now,
        expires_at=now + timedelta(hours=1),
    )
    store.plans.append(original_plan)
    original = RemoteRecord(
        uuid4(),
        previous_id,
        actor.workspace_id,
        "d" * 64,
        "SUCCEEDED",
        1,
        now,
        now,
        remote_id="branch:weora/" + "d" * 24 + ":" + "b" * 40,
        plan_id=original_plan.id,
    )
    await store.claim_remote(original)
    selected = replace(original_plan.scope, source_delivery_id=str(original.id))
    authorize = access.authorize

    async def authorize_history(
        actor: RunActor, run_id: UUID, *, write: bool = False, fencing_token: int | None = None
    ) -> None:
        await authorize(
            actor, access.run_id if run_id == previous_id else run_id, write=write, fencing_token=fencing_token
        )

    monkeypatch.setattr(access, "authorize", authorize_history)

    async def history(actor: RunActor, run_id: UUID) -> tuple[UUID, ...]:
        return (original.run_id,) if same_task else ()

    calls = 0

    async def update(
        actor: RunActor, run_id: UUID, plan: PlanScope, source: RemoteRecord, key: str, title: str
    ) -> RemoteResult:
        nonlocal calls
        calls += 1
        assert source == original and plan.source_revision == "b" * 40
        if result_unknown:
            raise TimeoutError
        return RemoteResult("branch:" + str(plan.source_branch) + ":" + "c" * 40, "https://git.example.test")

    service.workspace_history, service.update_branch = history, update
    selected = replace(selected, actions=("git.commit", "git.push"), commands=())
    if not same_task:
        with pytest.raises(DeliveryError) as error:
            await service.propose(actor, access.run_id, selected, "续写原PR", "保留原分支")
        assert error.value.code == "BRANCH_TASK_MISMATCH"
        assert calls == 0
        return
    references = await service.continuation_branches(actor, access.run_id)
    assert references[0]["delivery_id"] == str(original.id)
    plan = await service.propose(actor, access.run_id, selected, "续写原PR", "远端改变则停止")
    assert plan.scope.source_branch == "weora/" + "d" * 24 and plan.scope.source_revision == "b" * 40
    with pytest.raises(DeliveryError):
        await service.publish_branch(actor, access.run_id, "review fix", 7)
    assert calls == 0
    await service.decide(actor, access.run_id, plan.id, "approve")
    if result_unknown:
        with pytest.raises(TimeoutError):
            await service.publish_branch(actor, access.run_id, "review fix", 7)
        with pytest.raises(DeliveryError, match="需要核对"):
            await service.publish_branch(actor, access.run_id, "review fix", 7)
        assert calls == 1
        assert (await service.deliveries(actor, access.run_id))[0].status == "UNKNOWN"
        return
    first = await service.publish_branch(actor, access.run_id, "review fix", 7)
    assert first == await service.publish_branch(actor, access.run_id, "review fix", 7)
    assert first.remote_id == "branch:" + str(plan.scope.source_branch) + ":" + "c" * 40
    assert calls == 1


@pytest.mark.anyio
async def test_draft_pr_also_records_confirmed_branch_for_future_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    service, actor, access, _, _, remote = harness()

    async def create(*args: object) -> RemoteResult:
        return RemoteResult("42", "https://git.example.test/pull/42", "weora/" + str(args[2])[:24], "b" * 40)

    monkeypatch.setattr(remote, "create", create)
    selected = replace(scope(remote=True), actions=("git.commit", "git.push", "pr.create"), commands=())
    plan = await service.propose(actor, access.run_id, selected, "创建草稿PR", "不合并")
    await service.decide(actor, access.run_id, plan.id, "approve")
    result = await service.create_pr(actor, access.run_id, "draft", "test", 7)
    records = await service.deliveries(actor, access.run_id)
    assert result.status == "SUCCEEDED" and len(records) == 2
    assert any(item.remote_id == "branch:weora/" + result.business_key[:24] + ":" + "b" * 40 for item in records)


@pytest.mark.anyio
async def test_same_run_new_plan_keeps_delivered_fact_but_requires_its_own_approval() -> None:
    service, actor, access, store, _, _ = harness()
    branch = "weora/" + "d" * 24

    async def push(*args: object) -> RemoteResult:
        nonlocal branch
        branch = "weora/" + str(args[3])[:24]
        return RemoteResult("branch:" + branch + ":" + "b" * 40, "https://git.example.test")

    async def update(
        actor: RunActor, run_id: UUID, selected: PlanScope, source: RemoteRecord, key: str, title: str
    ) -> RemoteResult:
        assert selected.source_revision == "b" * 40 and source.run_id == run_id
        return RemoteResult("branch:" + branch + ":" + "c" * 40, "https://git.example.test")

    service.push_branch, service.update_branch = push, update
    selected = replace(scope(remote=True), actions=("git.commit", "git.push"), commands=())
    first_plan = await service.propose(actor, access.run_id, selected, "首轮交付", "不合并")
    await service.decide(actor, access.run_id, first_plan.id, "approve")
    first = await service.publish_branch(actor, access.run_id, "first", 7)
    second_plan = await service.propose(
        actor, access.run_id, replace(selected, source_delivery_id=str(first.id)), "根据评审修复", "SHA 漂移拒绝"
    )
    historical = await store.get_plan(actor.workspace_id, first_plan.id)
    assert historical and historical.status == "SUPERSEDED" and historical.decided_by == actor.user_id
    with pytest.raises(DeliveryError):
        await service.publish_branch(actor, access.run_id, "second", 7)
    await service.decide(actor, access.run_id, second_plan.id, "approve")
    assert (
        await service.publish_branch(actor, access.run_id, "second", 7)
    ).remote_id == "branch:" + branch + ":" + "c" * 40
