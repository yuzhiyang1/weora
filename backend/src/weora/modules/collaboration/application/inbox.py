"""按成员读取收件箱，并沿用来源任务和运行的访问权限。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.application.task_collaboration import TaskCollaborationService
from weora.modules.runs.domain.errors import RunNotFoundError


@dataclass(frozen=True)
class InboxItem:
    id: UUID
    type: str
    task_id: UUID | None
    run_id: UUID | None
    plan_id: UUID | None
    read: bool
    archived: bool
    created_at: datetime
    latest_review: bool


InboxBulkAction = Literal["read_all", "archive_all", "archive_read"]


class InboxRepository(Protocol):
    async def list_items(self, workspace_id: UUID, user_id: UUID, archived: bool) -> list[InboxItem]: ...

    async def archive_page(
        self,
        workspace_id: UUID,
        user_id: UUID,
        *,
        before: tuple[datetime, UUID] | None,
        limit: int,
        unread_only: bool,
    ) -> list[InboxItem]: ...

    async def set_group_archive(self, workspace_id: UUID, user_id: UUID, task_id: UUID, archived: bool) -> bool: ...

    async def bulk(self, workspace_id: UUID, user_id: UUID, action: InboxBulkAction) -> None: ...

    async def revision(self, workspace_id: UUID, user_id: UUID) -> int: ...

    async def update(
        self,
        workspace_id: UUID,
        user_id: UUID,
        item_ids: list[UUID],
        *,
        read: bool | None,
        archived: bool | None,
    ) -> bool: ...


class InboxService:
    def __init__(
        self,
        repository: InboxRepository,
        tasks: TaskCollaborationService,
        runs: RunService,
        delivery: DeliveryService,
    ) -> None:
        self.repository, self.tasks, self.runs, self.delivery = repository, tasks, runs, delivery

    async def list_items(self, actor: RunActor, archived: bool) -> list[dict[str, object]]:
        items = await self.repository.list_items(actor.workspace_id, actor.user_id, archived)
        return await self._visible_items(actor, items)

    async def archive_page(
        self,
        actor: RunActor,
        *,
        before: tuple[datetime, UUID] | None,
        limit: int,
        unread_only: bool,
    ) -> tuple[list[dict[str, object]], tuple[datetime, UUID] | None]:
        rows = await self.repository.archive_page(
            actor.workspace_id,
            actor.user_id,
            before=before,
            limit=limit + 1,
            unread_only=unread_only,
        )
        page = rows[:limit]
        # 即使当前页有已失权的记录，游标也从已扫描的最后一组继续。
        next_before = (page[-1].created_at, page[-1].id) if len(rows) > limit else None
        return await self._visible_items(actor, page), next_before

    async def set_group_archive(self, actor: RunActor, task_id: UUID, archived: bool) -> bool:
        return await self.repository.set_group_archive(actor.workspace_id, actor.user_id, task_id, archived)

    async def bulk(self, actor: RunActor, action: InboxBulkAction) -> None:
        await self.repository.bulk(actor.workspace_id, actor.user_id, action)

    async def revision(self, actor: RunActor) -> int:
        return await self.repository.revision(actor.workspace_id, actor.user_id)

    async def _visible_items(self, actor: RunActor, items: list[InboxItem]) -> list[dict[str, object]]:
        if not items:
            return []
        tasks = await self.tasks.visible_shared(actor, {item.task_id for item in items if item.task_id is not None})
        result: list[dict[str, object]] = []
        for item in items:
            task_id = item.task_id
            if item.run_id:
                try:
                    detail = await self.runs.get(actor=actor, workspace_id=actor.workspace_id, run_id=item.run_id)
                    plan = await self.delivery.get_plan(actor, item.run_id)
                except (RunNotFoundError, DeliveryError):
                    continue
                task_id = detail.run.task_id
                if task_id not in tasks:
                    tasks.update(await self.tasks.visible_shared(actor, {task_id}))
                shared = tasks.get(task_id)
                title = (shared.title if shared else None) or f"{detail.run.agent_name} · 执行计划"
                pending = bool(
                    plan
                    and plan.id == item.plan_id
                    and plan.status == "PENDING"
                    and detail.run.status == "WAITING_APPROVAL"
                )
            elif task_id in tasks:
                task = tasks[task_id]
                title = task.title or "协作任务"
                pending = item.type == "review" and item.latest_review and task.status == "in_review"
            else:
                continue
            result.append(
                {
                    "id": item.id,
                    "type": item.type,
                    "title": title,
                    "task_id": task_id,
                    "run_id": item.run_id,
                    "pending": pending,
                    "read": item.read,
                    "archived": item.archived,
                    "created_at": item.created_at,
                }
            )
        return result

    async def update(
        self,
        actor: RunActor,
        item_ids: list[UUID],
        *,
        read: bool | None,
        archived: bool | None,
    ) -> bool:
        return await self.repository.update(
            actor.workspace_id,
            actor.user_id,
            item_ids,
            read=read,
            archived=archived,
        )
