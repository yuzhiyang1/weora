"""分支交付必须幂等、保留未知结果，不能冒充 MR。"""

from dataclasses import replace

import pytest

from tests.delivery.test_service import harness, scope
from weora.modules.delivery.application.ports import RemoteResult
from weora.modules.delivery.domain.models import DeliveryError


@pytest.mark.anyio
@pytest.mark.parametrize("should_timeout", [False, True])
async def test_branch_push_requires_approval_and_never_replays_unknown(should_timeout: bool) -> None:
    service, actor, access, _, _, _ = harness()
    calls = 0

    async def push(*args: object) -> RemoteResult:
        nonlocal calls
        calls += 1
        if should_timeout:
            raise TimeoutError
        return RemoteResult("branch:weora/test:" + "b" * 40, "https://git.example.test/team/repo")

    service.push_branch = push
    selected = replace(scope(remote=True), actions=("git.commit", "git.push"), commands=())
    plan = await service.propose(actor, access.run_id, selected, "仅测试分支", "不修改主分支")
    with pytest.raises(DeliveryError):
        await service.publish_branch(actor, access.run_id, "测试", 7)
    assert calls == 0
    await service.decide(actor, access.run_id, plan.id, "approve")
    if should_timeout:
        with pytest.raises(TimeoutError):
            await service.publish_branch(actor, access.run_id, "测试", 7)
        with pytest.raises(DeliveryError):
            await service.publish_branch(actor, access.run_id, "测试", 7)
        assert (await service.deliveries(actor, access.run_id))[0].status == "UNKNOWN"
    else:
        first = await service.publish_branch(actor, access.run_id, "测试", 7)
        again = await service.publish_branch(actor, access.run_id, "测试", 7)
        assert first == again and first.status == "SUCCEEDED"
        await service.validate_completion(actor, access.run_id, 7)
    assert calls == 1


@pytest.mark.anyio
async def test_successful_branch_is_not_evidence_of_created_mr() -> None:
    service, actor, access, _, _, _ = harness()

    async def push(*args: object) -> RemoteResult:
        return RemoteResult("branch:weora/test:" + "b" * 40, "https://git.example.test/team/repo")

    service.push_branch = push
    selected = replace(scope(remote=True), actions=("git.commit", "git.push", "pr.create"), commands=())
    plan = await service.propose(actor, access.run_id, selected, "分支与MR", "不合并")
    await service.decide(actor, access.run_id, plan.id, "approve")
    await service.publish_branch(actor, access.run_id, "测试", 7)
    with pytest.raises(DeliveryError, match="PR 尚未确认"):
        await service.validate_completion(actor, access.run_id, 7)
