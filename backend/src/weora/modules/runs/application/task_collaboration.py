"""编排共享任务的指派、交接与同 Task 执行。"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from weora.modules.collaboration.application.tasks import TaskAssignment, TaskCollaboration, TaskStatus
from weora.modules.runs.application.ports import (
    ExecutionProfile,
    ProjectContextCatalog,
    PublishedAgentCatalog,
    PublishedAgentVersion,
    RunActor,
    RunRecord,
    RunSubmission,
    RunsUnitOfWork,
    RunsUnitOfWorkFactory,
)
from weora.modules.runs.domain.errors import RunNotFoundError, RunProjectSelectionError, RunVersionNotFoundError


class CollaborationConflict(Exception):
    """命令和任务的当前修订号、负责人或运行状态冲突。"""


MemberName = Callable[[UUID, UUID], Awaitable[str | None]]


class TaskCollaborationService:
    def __init__(
        self,
        uow_factory: RunsUnitOfWorkFactory,
        member_name: MemberName,
        agents: PublishedAgentCatalog,
        profile: Callable[[RunActor], Awaitable[ExecutionProfile | None]],
        projects: ProjectContextCatalog,
        git_identity: Callable[[RunActor, str], Awaitable[dict[str, object]]],
    ) -> None:
        self.uow_factory, self.member_name, self.agents = uow_factory, member_name, agents
        self.profile, self.projects, self.git_identity = profile, projects, git_identity

    async def list_shared(self, actor: RunActor) -> list[TaskCollaboration]:
        async with self.uow_factory() as uow:
            return await uow.tasks.list_collaborations(actor.workspace_id, actor.user_id)

    async def visible_shared(self, actor: RunActor, task_ids: set[UUID]) -> dict[UUID, TaskCollaboration]:
        """逐个复核通知引用，避免协作任务列表的显示上限隐藏旧任务。"""
        visible = {}
        async with self.uow_factory() as uow:
            for task_id in task_ids:
                state = await uow.tasks.collaboration(actor.workspace_id, task_id)
                if state and state.title and state.can_view(actor.user_id):
                    visible[task_id] = state
        return visible

    async def detail(
        self,
        actor: RunActor,
        task_id: UUID,
    ) -> tuple[TaskCollaboration, RunRecord | None, list[dict[str, object]]]:
        async with self.uow_factory() as uow:
            state = await self._visible(uow, actor, task_id)
            active = await uow.store.active_run_for_task(actor.workspace_id, task_id)
            return state, active, await uow.tasks.activities(actor.workspace_id, task_id)

    async def share(
        self,
        actor: RunActor,
        task_id: UUID,
        *,
        revision: int,
        title: str,
        summary: str,
        participant_ids: tuple[UUID, ...],
    ) -> None:
        async with self.uow_factory() as uow:
            state = await self._visible(uow, actor, task_id, lock=True)
            self._require_owner(state, actor)
            self._require_revision(state, revision)
            people = set(participant_ids) - {state.owner_id}
            for assignment in (state.assignment, state.pending):
                if assignment and assignment.kind == "member" and assignment.id != state.owner_id:
                    people.add(assignment.id)
            for person in people:
                if await self.member_name(actor.workspace_id, person) is None:
                    raise CollaborationConflict("所选成员已失效，请重新选择")
            state = replace(
                state, title=title, summary=summary, participants=tuple(sorted(people)), revision=state.revision + 1
            )
            await uow.tasks.change_collaboration(state, actor_id=actor.user_id, action="shared")
            await uow.commit()

    async def assign(
        self,
        actor: RunActor,
        task_id: UUID,
        *,
        revision: int,
        kind: str | None,
        assignee_id: UUID | None,
        version: str | None,
        mode: str,
        stop_current: bool,
        key: str,
        request_id: str,
    ) -> UUID | None:
        # 为同一个请求保留稳定的幂等键，且与旧 Run 创建接口隔离。
        fingerprint = hashlib.sha256(
            json.dumps(
                [str(task_id), revision, kind, str(assignee_id), version, mode, stop_current],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        submission_key = "task-collab:" + hashlib.sha256(key.encode()).hexdigest()
        async with self.uow_factory() as uow:
            state = await self._visible(uow, actor, task_id, lock=True)
            if mode == "start":
                previous = await uow.store.find_submission(actor=actor, key=submission_key)
                if previous:
                    if previous.request_sha256 != fingerprint:
                        raise CollaborationConflict("此请求键已用于其他指派")
                    return previous.run.id
            if not state.can_manage(actor.user_id):
                raise RunNotFoundError
            self._require_revision(state, revision)
            if state.title is None or state.summary is None:
                raise CollaborationConflict("请先填写并发布协作摘要")
            if state.status == "in_review":
                raise CollaborationConflict("请先完成验收再更换负责人")
            if state.pending:
                raise CollaborationConflict("已有交接等待本轮结束")
            if (kind is None) != (assignee_id is None):
                raise CollaborationConflict("请选择完整的负责人")
            target, published = (
                await self._target(actor, kind, assignee_id, version) if kind and assignee_id else (None, None)
            )
            active = await uow.store.active_run_for_task(actor.workspace_id, task_id)
            if active and mode == "start":
                raise CollaborationConflict("任务已有活跃运行，请等待本轮结束")
            if active and stop_current:
                await self._stop(uow, active, request_id)

            run_id = None
            if mode == "start":
                if published is None:
                    raise CollaborationConflict("请选择可运行的 Agent 版本")
                if state.status == "backlog":
                    raise CollaborationConflict("请先将任务移出待规划")
                run_id = await self._launch(uow, actor, state, published, submission_key, fingerprint, request_id)
            await self._apply_assignment(
                uow,
                state,
                actor.user_id,
                target,
                stopped=bool(active and stop_current),
                started=run_id is not None,
            )
            await uow.commit()
            return run_id

    async def set_status(
        self, actor: RunActor, task_id: UUID, revision: int, status: TaskStatus, note: str | None = None
    ) -> None:
        async with self.uow_factory() as uow:
            state = await self._visible(uow, actor, task_id, lock=True)
            if not state.can_manage(actor.user_id):
                raise RunNotFoundError
            self._require_revision(state, revision)
            if state.title is None:
                raise CollaborationConflict("请先发布协作任务")
            if status == state.status:
                return
            action = "status_changed"
            if state.status == "in_review":
                self._require_owner(state, actor)
                if status not in {"done", "in_progress"}:
                    raise CollaborationConflict("审核中只能通过验收或退回修改")
                action = "review_accepted" if status == "done" else "review_returned"
            elif status == "in_review":
                action = "review_submitted"
                if state.status in {"done", "cancelled"}:
                    raise CollaborationConflict("请先重新打开任务再提交验收")
                if state.pending or await uow.store.active_run_for_task(actor.workspace_id, task_id):
                    raise CollaborationConflict("请等待本轮运行和交接结束后再提交验收")
            if action in {"review_submitted", "review_returned"} and not note:
                raise CollaborationConflict("请填写交付说明或退回原因")
            await uow.tasks.change_collaboration(
                replace(state, status=status, revision=state.revision + 1),
                actor_id=actor.user_id,
                action=action,
                note=note or (status if action == "status_changed" else ""),
            )
            await uow.commit()

    async def settle_handoffs(self, limit: int = 50) -> None:
        """由已有持久调度周期补偿，进程重启不丢失待交接对象。"""
        async with self.uow_factory() as uow:
            pending = await uow.tasks.pending_collaborations(limit)
        for workspace_id, task_id in pending:
            async with self.uow_factory() as uow:
                state = await uow.tasks.collaboration(workspace_id, task_id, lock=True)
                if state is None or state.pending is None or state.pending_by is None:
                    continue
                if await uow.store.active_run_for_task(workspace_id, task_id):
                    await uow.tasks.defer_collaboration(workspace_id, task_id)
                    await uow.commit()
                    continue
                initiator = RunActor(state.pending_by, workspace_id)
                try:
                    if (
                        not state.can_manage(initiator.user_id)
                        or await self.member_name(
                            workspace_id,
                            initiator.user_id,
                        )
                        is None
                    ):
                        raise CollaborationConflict("交接发起人已失去任务管理权限")
                    target, _ = await self._target(
                        initiator, state.pending.kind, state.pending.id, state.pending.version
                    )
                except (CollaborationConflict, RunVersionNotFoundError):
                    await uow.tasks.change_collaboration(
                        replace(state, pending=None, pending_by=None, revision=state.revision + 1),
                        actor_id=initiator.user_id,
                        action="handoff_expired",
                        target=state.pending,
                    )
                else:
                    await self._apply_assignment(uow, state, initiator.user_id, target)
                await uow.commit()

    async def _target(
        self,
        actor: RunActor,
        kind: str,
        assignee_id: UUID,
        version: str | None,
    ) -> tuple[TaskAssignment, PublishedAgentVersion | None]:
        if kind == "member":
            name = await self.member_name(actor.workspace_id, assignee_id)
            if name is None:
                raise CollaborationConflict("接收人已失效，请重新选择")
            return TaskAssignment(kind, assignee_id, name), None
        if not version:
            raise CollaborationConflict("请选择已发布的 Agent 版本")
        published = await self.agents.resolve(actor=actor, agent_id=assignee_id, version=version)
        return TaskAssignment(kind, assignee_id, published.display_name, published.version), published

    async def _launch(
        self,
        uow: RunsUnitOfWork,
        actor: RunActor,
        state: TaskCollaboration,
        agent: PublishedAgentVersion,
        key: str,
        fingerprint: str,
        request_id: str,
    ) -> UUID:
        # 仓库选择可作为引用沿用，内容及 Git 身份由本次启动人重新取得。
        original = await uow.tasks.get_private(
            workspace_id=actor.workspace_id,
            task_id=state.task_id,
            user_id=state.owner_id,
        )
        snapshot = None
        if original and original.project_context:
            context = original.project_context
            repositories = context.get("repositories")
            if not isinstance(repositories, list) or not repositories:
                raise RunProjectSelectionError
            snapshot = await self.projects.snapshot(
                actor,
                UUID(str(context["project_id"])),
                tuple(UUID(str(repo["id"])) for repo in repositories),
            )
            snapshot = snapshot | {"git_identity": await self.git_identity(actor, agent.display_name)}
        now = datetime.now(UTC)
        run = RunRecord(
            id=uuid4(),
            task_id=state.task_id,
            workspace_id=actor.workspace_id,
            created_by=actor.user_id,
            agent_version_id=agent.id,
            agent_name=agent.display_name,
            agent_version=agent.version,
            status="QUEUED",
            last_sequence=1,
            created_at=now,
            updated_at=now,
            execution_profile=await self.profile(actor),
            collaboration_context={
                "instruction": state.summary or "",
                "project": snapshot,
                "created_at": now.isoformat(),
            },
        )
        await uow.store.add(RunSubmission(run, key, fingerprint), request_id=request_id)
        return run.id

    @staticmethod
    async def _apply_assignment(
        uow: RunsUnitOfWork,
        state: TaskCollaboration,
        actor_id: UUID,
        target: TaskAssignment | None,
        *,
        stopped: bool = False,
        started: bool = False,
    ) -> None:
        people = set(state.participants)
        if target and target.kind == "member" and target.id != state.owner_id:
            people.add(target.id)
        await uow.tasks.change_collaboration(
            replace(
                state,
                assignment=target,
                participants=tuple(sorted(people)),
                pending=None,
                pending_by=None,
                manager_id=target.id if target and target.kind == "member" else state.manager_id,
                revision=state.revision + 1,
                status="in_progress" if started else state.status,
            ),
            actor_id=actor_id,
            action="started" if started else "assigned",
            note="已请求停止上一轮运行" if stopped else "",
            previous=state.assignment,
            target=target,
        )

    @staticmethod
    async def _stop(uow: RunsUnitOfWork, run: RunRecord, request_id: str) -> None:
        # 仅在调用方已验证交接管理权限后操作该 Task 的活跃 Run；原执行身份保留。
        run_actor = RunActor(run.created_by, run.workspace_id)
        if not await uow.store.cancel_queued(actor=run_actor, run_id=run.id, request_id=request_id):
            await uow.store.request_running_cancellation(actor=run_actor, run_id=run.id, request_id=request_id)

    @staticmethod
    async def _visible(
        uow: RunsUnitOfWork,
        actor: RunActor,
        task_id: UUID,
        *,
        lock: bool = False,
    ) -> TaskCollaboration:
        state = await uow.tasks.collaboration(actor.workspace_id, task_id, lock=lock)
        if state is None or not state.can_view(actor.user_id):
            raise RunNotFoundError
        return state

    @staticmethod
    def _require_owner(state: TaskCollaboration, actor: RunActor) -> None:
        if state.owner_id != actor.user_id:
            raise RunNotFoundError

    @staticmethod
    def _require_revision(state: TaskCollaboration, revision: int) -> None:
        if state.revision != revision:
            raise CollaborationConflict("任务已更新，请刷新后再试")
