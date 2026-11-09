"""协调事务工作副本与纯领域效果队列，不依赖 SQL 或运行时适配器。"""

from datetime import UTC, datetime
from typing import Protocol

from weora.modules.coordination.domain import Effect, EffectKind, Json, Records, State
from weora.modules.coordination.engine import key


class Identified(Protocol):
    @property
    def id(self) -> str: ...


class RecordCollection[T: Identified]:
    """当前事务工作副本的记录视图；持久化与回滚统一由 scope 存储负责。"""

    def __init__(self, items: list[T]) -> None:
        self.items = items

    def get(self, identity: str) -> T | None:
        return next((item for item in self.items if item.id == identity), None)

    def exists(self, identity: str) -> bool:
        return self.get(identity) is not None

    def save(self, value: T) -> None:
        for index, item in enumerate(self.items):
            if item.id == value.id:
                self.items[index] = value
                return
        self.items.append(value)

    def list(self) -> list[T]:
        """枚举事务内记录；线程及生命周期条件由对应领域规则明确判断。"""
        return list(self.items)


class Effects:
    def __init__(self, state: State) -> None:
        self.state = state

    def enqueue(self, kind: EffectKind, identity: str, data: Json, *, priority: int = 0) -> None:
        """同业务键合并唤醒；运行中新增工作递增 generation，结算后再派发，不丢失新输入。"""
        effect_id = key(kind, identity)
        old = next((item for item in self.state.effects if item.id == effect_id), None)
        if old:
            if old.data == data:
                old.priority = max(priority, old.priority)
                return
            if old.state in {"running", "pending"}:
                # 合并唤醒，但保留运行中的代次；完成时发现 dirty 会再跑一轮。
                old.generation += 1
                old.priority = max(priority, old.priority)
                old.data = data
                return
            old.generation += 1
            old.data, old.state, old.priority = data, "pending", priority
            old.queued_at = datetime.now(UTC).isoformat()
            old.owner = old.lease_until = None
            return
        self.state.effects.append(Effect(effect_id, kind, identity, data, priority))

    def cancel(self, kind: EffectKind, identity: str) -> None:
        """撤销尚未发送或执行的副作用，并使旧 generation 的完成记录失效。"""
        effect = next((item for item in self.state.effects if item.id == key(kind, identity)), None)
        if effect:
            effect.generation += 1
            effect.state = "cancelled"
            effect.owner = effect.lease_until = None


def records(state: State) -> Records:
    return Records(
        RecordCollection(state.triggers),
        RecordCollection(state.threads),
        RecordCollection(state.evaluations),
        RecordCollection(state.decisions),
        RecordCollection(state.tickets),
        RecordCollection(state.replies),
        RecordCollection(state.notices),
        RecordCollection(state.sessions),
        Effects(state),
    )
