"""普通 MR 只能消费成功分支，审批、租约、幂等和未知结果不能因不执行沙箱而绕过。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.delivery.test_service import harness, scope
from weora.modules.delivery.application.ports import RemoteResult
from weora.modules.delivery.domain.models import DeliveryError, PlanRecord, RemoteRecord, ReviewNotSent


def review_harness():
    service, actor, access, store, sandbox, _ = harness()
    now = datetime.now(UTC)
    original = PlanRecord(
        uuid4(),
        uuid4(),
        actor.workspace_id,
        actor.user_id,
        1,
        replace(scope(remote=True), actions=("git.commit", "git.push")),
        "原分支",
        "隔离验收",
        "APPROVED",
        now,
        expires_at=now + timedelta(hours=1),
    )
    # 原计划单独保存，当前任务提案不会把它的批准状态改写成 SUPERSEDED。
    store.plans.append(original)
    branch = RemoteRecord(
        uuid4(),
        original.run_id,
        actor.workspace_id,
        "d" * 64,
        "SUCCEEDED",
        1,
        now,
        now,
        remote_id="branch:weora/" + "d" * 24 + ":" + "b" * 40,
        plan_id=original.id,
    )
    store.remote_records[branch.business_key] = branch
    selected = replace(original.scope, actions=("review.create",), commands=(), source_delivery_id=str(branch.id))
    # 显式允许同一成员读取原 Run；仍复用 Fake 的身份、撤权和 fencing 校验。
    authorize = access.authorize

    async def authorize_two_runs(current, run_id, *, write=False, fencing_token=None):
        await authorize(
            current, access.run_id if run_id == original.run_id else run_id, write=write, fencing_token=fencing_token
        )

    access.authorize = authorize_two_runs
    return service, actor, access, store, sandbox, selected, branch


@pytest.mark.anyio
@pytest.mark.parametrize("unknown", [False, True])
async def test_review_is_approved_once_and_never_replays_unknown(unknown: bool) -> None:
    service, actor, access, store, sandbox, selected, _ = review_harness()
    calls = 0

    async def create(*args):
        nonlocal calls
        calls += 1
        if unknown:
            raise TimeoutError
        return RemoteResult("42", "https://git.example.test/team/repo/change/42")

    service.review_creator = create
    plan = await service.propose(actor, access.run_id, selected, "仅创建普通MR", "不合并")
    with pytest.raises(DeliveryError):
        await service.create_review(actor, access.run_id, "验收", "测试", 7)
    assert calls == 0
    await service.decide(actor, access.run_id, plan.id, "approve")
    with pytest.raises(DeliveryError):
        await service.create_review(actor, access.run_id, "验收", "测试", 6)
    if unknown:
        with pytest.raises(TimeoutError):
            await service.create_review(actor, access.run_id, "验收", "测试", 7)
        with pytest.raises(DeliveryError, match="不自动重建"):
            await service.create_review(actor, access.run_id, "验收", "测试", 7)
        assert next(r for r in store.remote_records.values() if r.remote_id == "review:pending").status == "UNKNOWN"
    else:
        first = await service.create_review(actor, access.run_id, "验收", "测试", 7)
        assert first == await service.create_review(actor, access.run_id, "验收", "测试", 7)
        assert first.remote_id == "review:42"
        await service.validate_completion(actor, access.run_id, 7)
    assert calls == 1 and sandbox.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["missing", "unknown", "wrong-repo", "wrong-path", "not-review-only"])
async def test_unconfirmed_or_mismatched_source_cannot_be_proposed(change: str) -> None:
    service, actor, access, store, _, selected, branch = review_harness()
    if change == "missing":
        selected = replace(selected, source_delivery_id=str(uuid4()))
    elif change == "unknown":
        store.remote_records[branch.business_key] = replace(branch, status="UNKNOWN")
    elif change == "wrong-repo":
        selected = replace(selected, repository_id="other")
    elif change == "wrong-path":
        selected = replace(selected, paths=("elsewhere",))
    else:
        selected = replace(selected, actions=("review.create", "git.push"))
    with pytest.raises(DeliveryError):
        await service.propose(actor, access.run_id, selected, "MR", "不合并")


@pytest.mark.anyio
async def test_other_run_cannot_create_second_review_for_same_branch() -> None:
    service, actor, access, _, _, selected, _ = review_harness()
    calls = 0

    async def create(*args):
        nonlocal calls
        calls += 1
        return RemoteResult("42", "https://git.example.test/change/42")

    service.review_creator = create
    first = await service.propose(actor, access.run_id, selected, "普通MR", "不合并")
    await service.decide(actor, access.run_id, first.id, "approve")
    await service.create_review(actor, access.run_id, "验收", "测试", 7)
    access.run_id = uuid4()
    second = await service.propose(actor, access.run_id, selected, "普通MR", "不合并")
    await service.decide(actor, access.run_id, second.id, "approve")
    with pytest.raises(DeliveryError) as error:
        await service.create_review(actor, access.run_id, "验收", "测试", 7)
    assert error.value.code == "REVIEW_ALREADY_REQUESTED" and calls == 1


@pytest.mark.anyio
async def test_private_source_run_cannot_be_borrowed_by_record_id() -> None:
    service, actor, access, _, _, selected, _ = review_harness()

    async def current_run_only(current, run_id, **kwargs):
        if current != actor or run_id != access.run_id:
            raise DeliveryError("PERMISSION_DENIED")

    access.authorize = current_run_only
    with pytest.raises(DeliveryError) as error:
        await service.propose(actor, access.run_id, selected, "普通MR", "不合并")
    assert error.value.code == "PERMISSION_DENIED"


@pytest.mark.anyio
async def test_preflight_not_sent_needs_new_approved_plan_before_retry() -> None:
    service, actor, access, _, _, selected, _ = review_harness()
    calls = 0

    async def create(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ReviewNotSent("REMOTE_PROTOCOL", "预检失败，尚未发送")
        return RemoteResult("42", "https://git.example.test/change/42")

    service.review_creator = create
    first = await service.propose(actor, access.run_id, selected, "普通MR", "不合并")
    await service.decide(actor, access.run_id, first.id, "approve")
    with pytest.raises(ReviewNotSent):
        await service.create_review(actor, access.run_id, "验收", "测试", 7)
    with pytest.raises(DeliveryError, match="新计划审批"):
        await service.create_review(actor, access.run_id, "验收", "测试", 7)
    second = await service.propose(actor, access.run_id, selected, "修复预检后的新计划", "仍不合并")
    await service.decide(actor, access.run_id, second.id, "approve")
    result = await service.create_review(actor, access.run_id, "验收", "测试", 7)
    assert result.status == "SUCCEEDED" and result.attempts == 2 and calls == 2
