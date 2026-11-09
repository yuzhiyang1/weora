"""同 Task 工作快照不得变成跨任务文件或旧审批的恢复通道。"""

import base64
from collections.abc import Awaitable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from tests.delivery.fakes import Access, MemoryStore
from tests.delivery.test_service import scope
from weora.modules.delivery.application.workspaces import WorkspaceContinuity
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.runs.application.ports import RunActor


class TaskAccess(Access):
    """只授权测试中同一个 Task 的两轮运行，其他 ID 视为跨任务访问。"""

    def __init__(self, actor: RunActor, runs: set[UUID]) -> None:
        super().__init__(actor, next(iter(runs)))
        self.runs = runs

    async def authorize(
        self,
        actor: RunActor,
        run_id: UUID,
        *,
        write: bool = False,
        fencing_token: int | None = None,
    ) -> None:
        if not self.allowed or actor != self.actor or run_id not in self.runs:
            raise DeliveryError("PERMISSION_DENIED")


class Workspace:
    def __init__(self) -> None:
        self.changes: list[dict[str, str | None]] = [
            {"path": "src/a.txt", "content": base64.b64encode("已完成的修改".encode()).decode()},
        ]
        self.restored: list[dict[str, str | None]] = []

    def owns(self, run_id: UUID) -> bool:
        return True

    async def export_workspace(self, run_id: UUID, plan: PlanScope) -> list[dict[str, str | None]]:
        return self.changes

    async def restore_changes(self, run_id: UUID, plan: PlanScope, changes: list[dict[str, str | None]]) -> None:
        self.restored = changes


async def approved(operation: Awaitable[None]) -> None:
    """测试显式批准本次恢复；真实装配使用 Delivery 的审批和租约监控。"""
    await operation


@pytest.mark.anyio
async def test_files_survive_run_change_but_new_approval_is_required() -> None:
    actor, first, second = RunActor(uuid4(), uuid4()), uuid4(), uuid4()
    access, store, sandbox = TaskAccess(actor, {first, second}), MemoryStore(), Workspace()

    async def history(actor: RunActor, run_id: UUID) -> tuple[UUID, ...]:
        return (first, second)

    continuity = WorkspaceContinuity(store, access, sandbox, history, retention_days=7)
    await continuity.capture(actor, first, scope())
    without_file_write = replace(scope(), actions=("command.run", "git.diff"))
    with pytest.raises(DeliveryError, match="本轮审批"):
        await continuity.restore(actor, second, without_file_write, approved)
    assert not sandbox.restored
    await continuity.restore(actor, second, scope(), approved)
    assert sandbox.restored == sandbox.changes
    assert second in continuity.restored
    access.allowed = False
    with pytest.raises(DeliveryError):
        await continuity.capture(actor, first, scope())


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["expired", "corrupt", "base", "path"])
async def test_invalid_snapshot_does_not_write_any_file(failure: str) -> None:
    actor, first, second = RunActor(uuid4(), uuid4()), uuid4(), uuid4()
    store, sandbox = MemoryStore(), Workspace()

    async def history(actor: RunActor, run_id: UUID) -> tuple[UUID, ...]:
        return (first,)

    continuity = WorkspaceContinuity(store, TaskAccess(actor, {first, second}), sandbox, history, retention_days=7)
    await continuity.capture(actor, first, scope())
    plan = scope()
    if failure == "expired":
        store.evidence[-1] = replace(store.evidence[-1], expires_at=datetime.now(UTC) - timedelta(seconds=1))
    elif failure == "corrupt":
        store.evidence[-1] = replace(store.evidence[-1], sha256="wrong")
    elif failure == "base":
        plan = replace(plan, base_revision="b" * 40)
    else:
        plan = replace(plan, paths=("tests",))
    with pytest.raises(DeliveryError):
        await continuity.restore(actor, second, plan, approved)
    assert not sandbox.restored


@pytest.mark.anyio
async def test_latest_snapshot_can_revert_to_an_earlier_state() -> None:
    """A→B→A 必须恢复最后的 A，不能按历史摘要去重而错误留下 B。"""
    actor, run_id = RunActor(uuid4(), uuid4()), uuid4()
    store, sandbox = MemoryStore(), Workspace()

    async def history(actor: RunActor, run_id: UUID) -> tuple[UUID, ...]:
        return ()

    continuity = WorkspaceContinuity(store, TaskAccess(actor, {run_id}), sandbox, history, retention_days=7)
    await continuity.capture(actor, run_id, scope())
    original = sandbox.changes
    sandbox.changes = []
    await continuity.capture(actor, run_id, scope())
    sandbox.changes = original
    await continuity.capture(actor, run_id, scope())
    assert len(store.evidence) == 3
    assert store.evidence[0].sha256 == store.evidence[-1].sha256 != store.evidence[1].sha256


@pytest.mark.anyio
async def test_same_run_resumes_its_pre_approval_checkpoint() -> None:
    """审批释放容器后仍是同 Run；不能只查前序 Run 而丢失刚才已获准的修改。"""
    actor, run_id = RunActor(uuid4(), uuid4()), uuid4()
    store, sandbox = MemoryStore(), Workspace()
    access = TaskAccess(actor, {run_id})

    async def history(actor: RunActor, run_id: UUID) -> tuple[UUID, ...]:
        return ()

    previous = WorkspaceContinuity(store, access, sandbox, history, retention_days=7)
    await previous.capture(actor, run_id, scope())
    resumed = WorkspaceContinuity(store, access, sandbox, history, retention_days=7)
    await resumed.restore(actor, run_id, scope(), approved)
    assert sandbox.restored == sandbox.changes
