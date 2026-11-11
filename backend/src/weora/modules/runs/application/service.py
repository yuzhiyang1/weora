"""私有 Console 运行创建、回读与排队取消用例。"""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from weora.modules.collaboration.application.tasks import TaskInput, TaskRecord, summarize_task_title
from weora.modules.runs.application.execution import RunLease, RuntimeFailure, RuntimeSession
from weora.modules.runs.application.harness import OMP_RUNTIME, PI_RUNTIME
from weora.modules.runs.application.ports import (
    ExecutionProfile,
    ProjectContextCatalog,
    PublishedAgentCatalog,
    RunActor,
    RunDetail,
    RunEventRecord,
    RunRecord,
    RunStatus,
    RunSubmission,
    RunsUnitOfWork,
    RunsUnitOfWorkFactory,
)
from weora.modules.runs.domain.errors import (
    RunCancellationConflictError,
    RunContinuationLimitError,
    RunIdempotencyConflictError,
    RunModelSelectionError,
    RunNotFoundError,
    RunProjectSelectionError,
    RunSteeringUnavailableError,
    RunSubmissionRaceError,
)

_RETRY_PROFILE = "_feishu_retry_execution_profile"


class RunService:
    """不在 HTTP 进程调用模型；成功提交只表示可靠排队，而非已经执行。"""

    def __init__(
        self,
        *,
        uow_factory: RunsUnitOfWorkFactory,
        agent_catalog: PublishedAgentCatalog,
        execution_profile: ExecutionProfile | None = None,
        profile_provider: Callable[[RunActor], Awaitable[ExecutionProfile | None]] | None = None,
        git_identity_provider: Callable[[RunActor, str], Awaitable[dict[str, object]]] | None = None,
        content_authorizer: Callable[[RunActor, RunRecord, TaskRecord], Awaitable[bool]] | None = None,
        projects: ProjectContextCatalog | None = None,
        channel_project_authorizer: Callable[[RunActor, dict[str, object], dict[str, object]], Awaitable[bool]]
        | None = None,
        historical_title: Callable[[TaskRecord], Awaitable[str | None]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._agent_catalog = agent_catalog
        self._execution_profile = execution_profile
        self._profile_provider = profile_provider
        self._git_identity_provider = git_identity_provider
        self._content_authorizer = content_authorizer
        self._projects = projects
        self._channel_project_authorizer = channel_project_authorizer
        self._historical_title = historical_title

    async def create(
        self,
        *,
        actor: RunActor,
        workspace_id: UUID,
        agent_id: UUID,
        agent_version: str,
        instruction: str,
        idempotency_key: str,
        request_id: str,
        origin_metadata: dict[str, object] | None = None,
        project_id: UUID | None = None,
        repository_ids: tuple[UUID, ...] = (),
        thread_keys: tuple[str, ...] = (),
        model_id: str | None = None,
        task_title: str | None = None,
    ) -> tuple[RunDetail, bool]:
        """固定版本并原子排队；同键同内容回读，同键异内容拒绝。"""
        self._require_workspace(actor, workspace_id)
        instruction = instruction.strip()
        profile = self._execution_profile
        if model_id is not None:
            if profile is None or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", model_id):
                raise RunModelSelectionError
            # 模型提供方、凭据和预算仍由部署配置控制，仅固定内部管理员选择的模型。
            profile = replace(profile, model_id=model_id)
        agent_version = agent_version.strip()
        selection = [str(project_id), sorted(str(item) for item in repository_ids)] if project_id else None
        if bool(project_id) != bool(repository_ids):
            raise RunProjectSelectionError
        fingerprint = hashlib.sha256(
            json.dumps(
                [str(agent_id), agent_version, instruction, origin_metadata, selection]
                if selection
                else [str(agent_id), agent_version, instruction, origin_metadata]
                if origin_metadata
                else [str(agent_id), agent_version, instruction],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if model_id is not None:
            fingerprint = hashlib.sha256(f"{fingerprint}\0{model_id}".encode()).hexdigest()
        existing = await self._replay(actor, idempotency_key, fingerprint)
        if existing is not None:
            return existing, False

        if self._profile_provider is not None:
            profile = await self._profile_provider(actor)
            if model_id is not None and profile is not None:
                profile = replace(profile, model_id=model_id)

        version = await self._agent_catalog.resolve(actor=actor, agent_id=agent_id, version=agent_version)
        project_context = None
        if project_id is not None:
            if self._projects is None:
                raise RunProjectSelectionError
            project_context = await self._projects.snapshot(actor, project_id, repository_ids)
            if origin_metadata and (
                self._channel_project_authorizer is None
                or not await self._channel_project_authorizer(actor, origin_metadata, project_context)
            ):
                raise RunProjectSelectionError
            if self._git_identity_provider is not None:
                project_context = dict(project_context) | {
                    "git_identity": await self._git_identity_provider(actor, version.display_name)
                }
        now = datetime.now(UTC)
        task = TaskRecord(
            uuid4(),
            workspace_id,
            actor.user_id,
            instruction,
            now,
            origin=("code_hosting" if origin_metadata.get("source_kind") == "code_hosting" else "feishu")
            if origin_metadata
            else "console",
            audience="group" if origin_metadata else "private",
            origin_metadata=origin_metadata,
            project_context=project_context,
            title=summarize_task_title(task_title or "", 200) or None,
        )
        run = RunRecord(
            id=uuid4(),
            task_id=task.id,
            workspace_id=workspace_id,
            created_by=actor.user_id,
            agent_version_id=version.id,
            agent_name=version.display_name,
            agent_version=version.version,
            status="QUEUED",
            last_sequence=1,
            created_at=now,
            updated_at=now,
            execution_profile=profile,
        )
        try:
            async with self._uow_factory() as uow:
                await uow.tasks.create_task(task)
                await uow.store.add(RunSubmission(run, idempotency_key, fingerprint), request_id=request_id)
                for key in thread_keys:
                    await uow.tasks.bind_thread(task, key, run.id)
                await uow.commit()
        except RunSubmissionRaceError:
            # 唯一键是并发幂等的最终防线；失败事务中的临时 Task 已回滚。
            replay = await self._replay(actor, idempotency_key, fingerprint)
            if replay is None:
                raise
            return replay, False
        return RunDetail(run, task), True

    async def continue_task(
        self,
        *,
        actor: RunActor,
        run_id: UUID,
        instruction: str,
        idempotency_key: str,
        request_id: str,
        origin_metadata: dict[str, object] | None = None,
        thread_keys: tuple[str, ...] = (),
        steer: bool = False,
        refresh_model: bool = False,
        retry_model_id: str | None = None,
    ) -> TaskInput:
        """向已有私有 Task 可靠追加输入，提交后尝试按队列顺序创建下一轮 Run。

        先验证任务访问与渠道范围，再锁定 Task，以请求键幂等保存输入及线程关联；
        活跃 Run 存在时保留排队状态，不覆盖初始目标、不继承旧审批。返回当前输入回执，
        run_id 为空表示尚未派发；提交后的调度失败由持久扫描补偿。
        """
        # 跟进必须保持原任务受众及来源身份，不能借已有群任务扩大发送范围。
        instruction = instruction.strip()
        if not instruction or len(instruction) > 16000:
            raise RunIdempotencyConflictError
        detail = await self.get(actor=actor, workspace_id=actor.workspace_id, run_id=run_id)
        if detail.run.collaboration_context is not None:
            raise RunNotFoundError
        if (detail.task.audience == "group") != (origin_metadata is not None):
            # Console 私有输入不能借已有群任务转发到群。
            raise RunNotFoundError
        if steer and (origin_metadata is not None or detail.task.audience != "private"):
            raise RunNotFoundError
        previous = detail.task.origin_metadata or {}
        if origin_metadata and _RETRY_PROFILE in origin_metadata:
            raise RunNotFoundError
        if refresh_model and (
            not origin_metadata or detail.task.origin != "feishu"
            or not previous.get("coordination_ticket_id")
            or any(
                origin_metadata.get(key) != previous.get(key)
                for key in ("coordination_scope_id", "coordination_ticket_id")
            )
        ):
            raise RunNotFoundError
        if origin_metadata and any(
            origin_metadata.get(key) != previous.get(key)
            for key in (
                "source_kind", "source_id", "connection_id", "chat_id", "open_id", "route_key", "project_grant_id",
            )
        ):
            raise RunNotFoundError

        # 请求摘要固定本轮指令与来源，重复键只允许取回同一份输入。
        digest = hashlib.sha256(
            json.dumps(
                [str(detail.task.id), instruction, origin_metadata]
                + (["steer", str(run_id)] if steer else [])
                + (["refresh_model", retry_model_id] if refresh_model else []),
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()

        if refresh_model:
            # 已接收的重试复用第一次提交的模型快照，配置再次变化也不会改写它。
            async with self._uow_factory() as uow:
                replay = await uow.tasks.input_by_key(actor.workspace_id, actor.user_id, idempotency_key)
            if replay is not None:
                if replay.request_sha256 != digest:
                    raise RunIdempotencyConflictError
                await self.dispatch_task_input(replay)
                return await self.task_input(actor, replay.id)
            profile = await self._profile_provider(actor) if self._profile_provider else self._execution_profile
            if profile is None:
                raise RuntimeFailure("RUNTIME_NOT_CONFIGURED")
            if detail.run.execution_profile is not None:
                # 原会话只能由其内核读取；切换模型不会迁移会话检查点格式。
                profile = replace(profile, runtime_id=detail.run.execution_profile.runtime_id)
            if retry_model_id is not None:
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", retry_model_id):
                    raise RunModelSelectionError
                profile = replace(profile, model_id=retry_model_id)
            # 输入与快照同事务持久化，Beat 补偿时无需回读可能已经变化的配置；快照不含密钥。
            origin_metadata = {**(origin_metadata or {}), _RETRY_PROFILE: asdict(profile)}

        for attempt in range(3):
            try:
                async with self._uow_factory() as uow:
                    if not await uow.tasks.lock_private(actor.workspace_id, detail.task.id, actor.user_id):
                        raise RunNotFoundError
                    collaboration = await uow.tasks.collaboration(actor.workspace_id, detail.task.id)
                    if collaboration and (
                        collaboration.assignment or collaboration.pending or collaboration.status == "in_review"
                    ):
                        raise RunNotFoundError
                    existing = await uow.tasks.input_by_key(actor.workspace_id, actor.user_id, idempotency_key)
                    if existing:
                        if existing.request_sha256 != digest:
                            raise RunIdempotencyConflictError
                        item = existing
                    else:
                        fence = None
                        if steer:
                            current = await uow.store.get_private(actor=actor, run_id=run_id, lock=True)
                            if not current or not self._can_steer(current):
                                raise RunSteeringUnavailableError
                            fence = current.fencing_token
                            pending = await uow.tasks.steering_inputs(
                                actor.workspace_id, run_id, actor.user_id, fence,
                            )
                        else:
                            inputs = await uow.tasks.inputs(actor.workspace_id, detail.task.id, actor.user_id)
                            pending = [entry for entry in inputs if entry.status == "PENDING"]
                        if len(pending) >= 32:
                            raise RunContinuationLimitError
                        item = await uow.tasks.add_input(
                            TaskInput(
                                id=uuid4(),
                                task_id=detail.task.id,
                                workspace_id=actor.workspace_id,
                                created_by=actor.user_id,
                                anchor_run_id=run_id,
                                instruction=instruction,
                                idempotency_key=idempotency_key,
                                request_sha256=digest,
                                request_id=request_id[:128],
                                created_at=datetime.now(UTC),
                                origin_metadata=origin_metadata,
                                status="STEERING" if steer else "PENDING",
                                steer_fence=fence,
                            )
                        )
                        for key in thread_keys:
                            if await uow.tasks.thread_run(actor.workspace_id, actor.user_id, key) is None:
                                await uow.tasks.bind_thread(detail.task, key, run_id)
                    await uow.commit()
                break
            except RunSubmissionRaceError:
                if attempt == 2:
                    raise

        # 输入先提交再尝试派发，进程中断也不会丢失已经接收的人类消息。
        if not steer:
            await self.dispatch_task_input(item)
        return await self.task_input(actor, item.id)

    @staticmethod
    def _can_steer(run: RunRecord) -> bool:
        return (
            run.status == "RUNNING"
            and run.cancellation_requested_at is None
            and run.collaboration_context is None
            and not run.recovery_read_only
            and run.lease_expires_at is not None
            and run.lease_expires_at > datetime.now(UTC)
            and run.execution_profile is not None
            and run.execution_profile.runtime_id in {PI_RUNTIME, OMP_RUNTIME}
        )

    def console_session(self, lease: RunLease) -> RuntimeSession:
        """接入执行内核的已读回执，只允许领取者消费本轮消息。"""
        run = lease.run
        actor = RunActor(run.created_by, run.workspace_id)

        async def pull() -> list[tuple[str, str]]:
            async with self._uow_factory() as uow:
                items = await uow.tasks.steering_inputs(run.workspace_id, run.id, run.created_by, lease.fencing_token)
                if not items:
                    return []
                try:
                    current, task = await self._authorized_run(uow, actor, run.id)
                except RunNotFoundError:
                    raise RuntimeFailure("ACCESS_REVOKED") from None
                if task.audience != "private" or not self._can_steer(current) or (
                    current.fencing_token != lease.fencing_token
                ):
                    return []
                return [(str(item.id), item.instruction) for item in items]

        async def checkpoint(_: list[dict[str, object]], read: list[str]) -> None:
            if not read:
                return
            async with self._uow_factory() as uow:
                items = await uow.tasks.steering_inputs(run.workspace_id, run.id, run.created_by, lease.fencing_token)
                consumed = [item for item in items if str(item.id) in read]
                if not consumed:
                    return
                current = await uow.store.get_private(actor=actor, run_id=run.id, lock=True)
                if current and self._can_steer(current) and current.fencing_token == lease.fencing_token:
                    for item in consumed:
                        await uow.tasks.mark_input_read(item)
                    await uow.commit()

        return RuntimeSession(f"{run.id}:{lease.fencing_token}", pull=pull, checkpoint=checkpoint)

    async def conversation(self, actor: RunActor, workspace_id: UUID, run_id: UUID) -> dict[str, object]:
        """返回本人可访问的运行输出和续接指令，群聊及共享运行仅展示当前轮。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            run, task = await self._authorized_run(uow, actor, run_id)
            private = task.audience == "private" and run.collaboration_context is None
            collaboration = await uow.tasks.collaboration(workspace_id, task.id) if private else None
            can_send = private and not (
                collaboration
                and (collaboration.assignment or collaboration.pending or collaboration.status == "in_review")
            )
            items = await uow.tasks.inputs(workspace_id, task.id, actor.user_id, recent=True) if private else []
            candidates = await uow.store.task_runs(actor=actor, task_id=task.id, limit=12) if private else [run]
            candidates = [row for row in candidates if row.id != run.id] + [run]
            visible: dict[UUID, RunRecord] = {}
            history = []
            for row in sorted(candidates, key=lambda entry: (entry.created_at, entry.id)):
                if row.id == run.id:
                    authorized, source = run, task
                elif row.collaboration_context is not None:
                    continue
                else:
                    try:
                        authorized, source = await self._authorized_run(uow, actor, row.id)
                    except RunNotFoundError:
                        continue
                visible[row.id] = authorized
                history.append({
                    "id": str(row.id), "instruction": source.instruction, "output": authorized.output_text,
                    "status": authorized.status, "created_at": authorized.created_at,
                    "updated_at": authorized.updated_at,
                    "agent_name": authorized.agent_name,
                })
            messages = []
            for item in items:
                status = item.status
                target = visible.get(item.anchor_run_id)
                if status == "STEERING" and (
                    not target or not self._can_steer(target) or target.fencing_token != item.steer_fence
                ):
                    status = "MISSED"
                messages.append({
                    "id": str(item.id), "instruction": item.instruction, "status": status,
                    "mode": "steer" if item.steer_fence is not None else "continue",
                    "run_id": str(item.anchor_run_id) if item.steer_fence is not None else (
                        str(item.run_id) if item.run_id else None
                    ),
                    "created_at": item.created_at,
                })
            return {
                "runs": history, "inputs": messages, "can_send": can_send,
                "can_steer": can_send and self._can_steer(run),
            }

    async def task_input(self, actor: RunActor, input_id: UUID) -> TaskInput:
        """查询发起人自己的输入回执，并复核其原任务当前仍可访问。"""
        async with self._uow_factory() as uow:
            item = await uow.tasks.input_by_id(actor.workspace_id, actor.user_id, input_id)
        if item is None:
            raise RunNotFoundError
        await self.get(actor=actor, workspace_id=actor.workspace_id, run_id=item.anchor_run_id)
        return item

    async def task_inputs(self, actor: RunActor, workspace_id: UUID, run_id: UUID) -> list[TaskInput]:
        """授权后查询同一 Task 的有界输入窗口，供调用方区分排队、派发和拒绝状态。"""
        detail = await self.get(actor=actor, workspace_id=workspace_id, run_id=run_id)
        async with self._uow_factory() as uow:
            return await uow.tasks.inputs(workspace_id, detail.task.id, actor.user_id)

    async def execution_predecessors(self, actor: RunActor, run_id: UUID) -> tuple[UUID, ...]:
        """按 Task 输入顺序返回本轮之前的运行；旧页面发起续接也不能读取后来运行的工作文件。"""
        inputs = await self.task_inputs(actor, actor.workspace_id, run_id)
        target = next((item for item in inputs if item.run_id == run_id), None)
        if target is None:
            return ()
        return tuple(
            dict.fromkeys(
                identity
                for item in inputs
                if item.sequence <= target.sequence
                for identity in (item.anchor_run_id, item.run_id)
                if identity is not None and identity != run_id
            )
        )

    async def dispatch_task_input(self, item: TaskInput) -> UUID | None:
        """锁定 Task 并派发最早待处理输入，在同一事务保存新 Run、调度记录与输入关联。

        已有活跃 Run 时延后检查；取消前排队或已失权的输入被拒绝，不能自动唤醒已停止工作。
        新 Run 固定原 Agent 版本；执行配置取本轮显式快照，否则沿用锚点。
        """
        actor = RunActor(item.created_by, item.workspace_id)
        async with self._uow_factory() as uow:
            task = await uow.tasks.lock_private(item.workspace_id, item.task_id, item.created_by)
            if task is None:
                return None
            if await uow.store.active_task_run(actor=actor, task_id=item.task_id):
                await uow.tasks.defer_input(item)
                await uow.commit()
                return None
            inputs = await uow.tasks.inputs(item.workspace_id, item.task_id, item.created_by)
            selected = next((entry for entry in inputs if entry.status == "PENDING"), None)
            if selected is None:
                return None
            collaboration = await uow.tasks.collaboration(item.workspace_id, item.task_id)
            if collaboration and (
                collaboration.assignment or collaboration.pending or collaboration.status == "in_review"
            ):
                # 指派或提交验收后，旧发起人的排队输入不得重新启动任务。
                await uow.tasks.reject_input(selected)
                await uow.commit()
                return None
            latest = await uow.store.task_runs(actor=actor, task_id=task.id, limit=1)
            if latest and latest[0].status == "CANCELLED":
                stopped_at = latest[0].updated_at
                queued_at = selected.created_at
                if queued_at.replace(tzinfo=UTC) <= stopped_at.replace(tzinfo=UTC):
                    # 叫停之前的排队消息不能在停止后把目标自动唤醒。
                    await uow.tasks.reject_input(selected)
                    await uow.commit()
                    return None
            # 锚点决定固定执行版本；拒绝失效输入，不复制旧计划、批准或工具回执。
            try:
                anchor = await self._detail(uow, actor, selected.anchor_run_id)
            except RunNotFoundError:
                await uow.tasks.reject_input(selected)
                await uow.commit()
                return None
            snapshot = (selected.origin_metadata or {}).get(_RETRY_PROFILE)
            profile = (
                TypeAdapter(ExecutionProfile).validate_python(snapshot) if snapshot else anchor.run.execution_profile
            )
            now = datetime.now(UTC)
            run = RunRecord(
                id=uuid4(),
                task_id=task.id,
                workspace_id=task.workspace_id,
                created_by=task.created_by,
                agent_version_id=anchor.run.agent_version_id,
                agent_name=anchor.run.agent_name,
                agent_version=anchor.run.agent_version,
                status="QUEUED",
                last_sequence=1,
                created_at=now,
                updated_at=now,
                execution_profile=profile,
            )
            await uow.store.add(
                RunSubmission(
                    run,
                    f"task-input:{selected.id}",
                    selected.request_sha256,
                ),
                request_id=selected.request_id,
            )
            await uow.tasks.assign_input(selected, run.id)
            await uow.commit()
            return run.id

    async def dispatch_task_inputs(self, limit: int = 50) -> int:
        """Beat 补偿尚未派发的持久输入，返回本轮创建的 Run 数；通知丢失不丢弃跟进。"""
        async with self._uow_factory() as uow:
            items = await uow.tasks.pending_inputs(limit)
        count = 0
        for item in items:
            run_id = await self.dispatch_task_input(item)
            if run_id is not None:
                count += 1
        return count

    async def submit_channel(
        self,
        *,
        actor: RunActor,
        agent_id: UUID,
        agent_version: str,
        instruction: str,
        idempotency_key: str,
        request_id: str,
        origin_metadata: dict[str, object],
        project_id: UUID | None = None,
        repository_ids: tuple[UUID, ...] = (),
    ) -> dict[str, object]:
        """明确新任务或精确线程续接；无法确定归属时保留新任务行为，不让模型猜身份。"""
        new_task = instruction.startswith(("/new ", "新任务：", "新任务:"))
        clean = instruction.split(" ", 1)[1] if instruction.startswith("/new ") else instruction
        # 路由和授权版本属于关联范围；更换 Agent 或项目授权不会接回旧任务。
        scope = [
            str(agent_id),
            agent_version,
            str(project_id),
            sorted(map(str, repository_ids)),
            [
                origin_metadata.get(key)
                for key in (
                    "source_id",
                    "connection_id",
                    "chat_id",
                    "open_id",
                    "route_key",
                    "project_grant_id",
                )
            ],
        ]

        def key(kind: str, value: object) -> str:
            return hashlib.sha256(json.dumps([scope, kind, value], sort_keys=True).encode()).hexdigest()

        message_key = key("message", origin_metadata["message_id"])
        candidates = (
            []
            if new_task
            else [
                key(kind, origin_metadata[field])
                for kind, field in (
                    ("message", "quote_id"),
                    ("message", "root_id"),
                    ("thread", "thread_id"),
                )
                if origin_metadata.get(field)
            ]
        )
        bindings = (
            message_key,
            *(
                [key("thread", origin_metadata["thread_id"])]
                if origin_metadata.get("thread_id") and not new_task
                else []
            ),
        )
        async with self._uow_factory() as uow:
            initial = await uow.store.find_submission(actor=actor, key=idempotency_key)
        if initial is not None:
            # 初始事件的重投仍是 create 的幂等回读，不能被新建的线程关联误判成一次跟进。
            detail, _ = await self.create(
                actor=actor,
                workspace_id=actor.workspace_id,
                agent_id=agent_id,
                agent_version=agent_version,
                instruction=clean,
                idempotency_key=idempotency_key,
                request_id=request_id,
                origin_metadata=origin_metadata,
                project_id=project_id,
                repository_ids=repository_ids,
            )
            return {"task_id": str(detail.task.id), "run_id": str(detail.run.id)}
        for attempt in range(3):
            async with self._uow_factory() as uow:
                anchor_id = None
                for candidate in candidates:
                    anchor_id = await uow.tasks.thread_run(actor.workspace_id, actor.user_id, candidate)
                    if anchor_id is not None:
                        break
            if anchor_id:
                item = await self.continue_task(
                    actor=actor,
                    run_id=anchor_id,
                    instruction=clean,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    origin_metadata=origin_metadata,
                    thread_keys=(message_key,),
                )
                return {
                    "task_id": str(item.task_id),
                    "input_id": str(item.id),
                    "run_id": str(item.run_id) if item.run_id else None,
                }
            try:
                detail, _ = await self.create(
                    actor=actor,
                    workspace_id=actor.workspace_id,
                    agent_id=agent_id,
                    agent_version=agent_version,
                    instruction=clean,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    origin_metadata=origin_metadata,
                    project_id=project_id,
                    repository_ids=repository_ids,
                    thread_keys=bindings,
                )
                return {"task_id": str(detail.task.id), "run_id": str(detail.run.id)}
            except RunSubmissionRaceError:
                if attempt == 2:
                    raise
        raise RunSubmissionRaceError

    async def get(self, *, actor: RunActor, workspace_id: UUID, run_id: UUID) -> RunDetail:
        """刷新页面或进程重启后均从持久化恢复，不依赖内存会话。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            return await self._detail(uow, actor, run_id)

    async def list_runs(
        self,
        *,
        actor: RunActor,
        workspace_id: UUID,
        limit: int,
        before_id: UUID | None,
    ) -> list[RunRecord]:
        """按创建时间和 ID 进行稳定翻页；游标也必须属于当前私有视图。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            before = None
            if before_id is not None:
                before = await uow.store.get_private(actor=actor, run_id=before_id)
                if before is None:
                    raise RunNotFoundError
            visible: list[RunRecord] = []
            while len(visible) < limit:
                batch = await uow.store.list_private(actor=actor, limit=limit, before=before)
                owned = {
                    task.id: task
                    for task in await uow.tasks.list_private(
                        workspace_id, actor.user_id,
                        {run.task_id for run in batch if run.collaboration_context is None},
                    )
                }
                for run in batch:
                    task = owned.get(run.task_id)
                    if run.collaboration_context is not None:
                        task = await self._task_for_run(uow, run)
                        if task is None:
                            continue
                    if task is not None and self._content_authorizer and (
                        task.audience == "group" or task.project_context or run.collaboration_context is not None
                        or run.failure_code == "CONTEXT_EXPIRED"
                    ):
                        # 群聊续接的来源可能已撤权，标题与详情使用相同的内容权限。
                        if task.audience == "group":
                            task = await uow.tasks.for_run(task, run.id)
                        if not await self._content_authorizer(actor, run, task):
                            task = None
                    title = await self._display_title(task) if task else None
                    visible.append(replace(run, task_title=title))
                if len(batch) < limit:
                    break
                before = batch[-1]
            return visible[:limit]

    async def events(
        self,
        *,
        actor: RunActor,
        workspace_id: UUID,
        run_id: UUID,
        after: int,
        limit: int,
    ) -> tuple[list[RunEventRecord], int, RunStatus]:
        """按持久序号补读；事件与正文共用当前来源授权。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            run, _ = await self._authorized_run(uow, actor, run_id)
            # 限定到先读取的高水位，避免并发追加导致同页边界自相矛盾。
            events = await uow.store.list_events(actor=actor, run_id=run_id, after=after, limit=limit)
            return [event for event in events if event.sequence <= run.last_sequence], run.last_sequence, run.status

    async def cancel(
        self,
        *,
        actor: RunActor,
        workspace_id: UUID,
        run_id: UUID,
        request_id: str,
    ) -> RunDetail:
        """仅取消 QUEUED；事件、步骤与待调度记录同事务更新，重复取消不重复记事件。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            changed = await uow.store.cancel_queued(actor=actor, run_id=run_id, request_id=request_id)
            if not changed:
                changed = await uow.store.request_running_cancellation(
                    actor=actor, run_id=run_id, request_id=request_id
                )
            detail = await self._detail(uow, actor, run_id)
            if not changed and detail.run.status != "CANCELLED" and detail.run.cancellation_requested_at is None:
                raise RunCancellationConflictError
            await uow.commit()
            return detail

    async def _replay(self, actor: RunActor, key: str, fingerprint: str) -> RunDetail | None:
        async with self._uow_factory() as uow:
            submission = await uow.store.find_submission(actor=actor, key=key)
            if submission is None:
                return None
            if submission.request_sha256 != fingerprint:
                raise RunIdempotencyConflictError
            return await self._detail(uow, actor, submission.run.id)

    async def _detail(self, uow: RunsUnitOfWork, actor: RunActor, run_id: UUID) -> RunDetail:
        run, task = await self._authorized_run(uow, actor, run_id)
        return RunDetail(replace(run, task_title=await self._display_title(task)), task)

    async def watch_state(self, actor: RunActor, workspace_id: UUID, run_id: UUID) -> tuple[RunStatus, int, str | None]:
        """复验正文授权，只取订阅所需的状态、持久序号和已生成标题。"""
        self._require_workspace(actor, workspace_id)
        async with self._uow_factory() as uow:
            run, task = await self._authorized_run(uow, actor, run_id)
            return run.status, run.last_sequence, task.title

    async def _authorized_run(
        self, uow: RunsUnitOfWork, actor: RunActor, run_id: UUID,
    ) -> tuple[RunRecord, TaskRecord]:
        run = await uow.store.get_private(actor=actor, run_id=run_id)
        if run is None:
            raise RunNotFoundError
        task = await self._task_for_run(uow, run)
        if task is None:
            raise RunNotFoundError
        if self._content_authorizer and not await self._content_authorizer(actor, run, task):
            raise RunNotFoundError
        return run, task

    async def _display_title(self, task: TaskRecord) -> str:
        if task.title is None and self._historical_title and (task.origin_metadata or {}).get("coordination_ticket_id"):
            title = await self._historical_title(task)
            if title:
                return summarize_task_title(title, 200)
        return task.display_title

    async def access_metadata(self, actor: RunActor, run_id: UUID) -> RunDetail | None:
        """仅供跨模块权限装配，不返回给 HTTP，也不递归调用派生正文鉴权。"""
        async with self._uow_factory() as uow:
            run = await uow.store.get_private(actor=actor, run_id=run_id)
            if run is None:
                return None
            task = await self._task_for_run(uow, run)
            return RunDetail(run, task) if task else None

    @staticmethod
    async def _task_for_run(uow: RunsUnitOfWork, run: RunRecord) -> TaskRecord | None:
        if run.collaboration_context is not None:
            return await uow.tasks.for_collaboration_run(
                run.workspace_id, run.task_id, run.created_by, run.collaboration_context,
            )
        task = await uow.tasks.get_private(
            workspace_id=run.workspace_id, task_id=run.task_id, user_id=run.created_by,
        )
        return await uow.tasks.for_run(task, run.id) if task else None

    @staticmethod
    def _require_workspace(actor: RunActor, workspace_id: UUID) -> None:
        if actor.workspace_id != workspace_id:
            raise RunNotFoundError
