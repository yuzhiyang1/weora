"""Celery 执行进程入口。HTTP、队列通知与持久运行状态互不替代。"""

import asyncio
import re
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from celery import Celery
from fastapi import FastAPI

from weora.bootstrap.application import create_app
from weora.bootstrap.console_settings import resolve_run_profile
from weora.bootstrap.coordination import dispatch_effects, execute_effect
from weora.bootstrap.harness import harness_factory
from weora.bootstrap.run_profile import execution_profile
from weora.bootstrap.settings import AppSettings, get_settings
from weora.modules.agents.application.service import AgentRegistryService
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextActor, ContextError, Json
from weora.modules.context.service import ContextService
from weora.modules.context.store import SqlContextStore
from weora.modules.coordination.domain import CoordinationError
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.docker import DockerSandbox
from weora.modules.identity.application.service import IdentityService
from weora.modules.runs.application.execution import RunExecutor, RunLease, RuntimeFailure, TextRuntime
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunError
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.pi_runtime import PiTextRuntime
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork
from weora.modules.tools.application.gateway import ToolGateway, ToolScope
from weora.modules.tools.infrastructure.store import SqlAlchemyToolStore
from weora.shared.infrastructure.database import SessionFactory

celery_app = Celery("weora", broker=get_settings().celery_broker_url)
celery_app.conf.update(
    task_default_queue="weora.runs",
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_publish_retry=False,
    broker_connection_timeout=5,
    broker_transport_options={"socket_connect_timeout": 5, "socket_timeout": 5, "retry_on_timeout": False},
    worker_hijack_root_logger=False,
    beat_schedule={
        "dispatch-persisted-runs": {"task": "weora.runs.dispatch", "schedule": 5.0},
        "dispatch-superagent": {
            "task": "weora.coordination.dispatch",
            "schedule": 2.0,
            "options": {"queue": "weora.coordination.control"},
        },
    },
)


async def _execute(workspace_id: UUID, run_id: UUID) -> bool:
    settings = get_settings()
    profile = execution_profile(settings)
    if profile is None:
        return False
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        sessions = cast(SessionFactory, app.state.db_session_factory)
        gateway = cast(ToolGateway, app.state.tool_gateway)
        delivery = cast(DeliveryService, app.state.delivery_service)
        runs = cast(RunService, app.state.run_service)
        context = cast(ContextService | None, app.state.context_service)
        claimed_actor: RunActor | None = None
        resolved_key = ""

        async def resolve_profile(lease: RunLease) -> ExecutionProfile:
            nonlocal resolved_key
            resolved, resolved_key = await resolve_run_profile(
                app.state.console_settings_service,
                lease.run.workspace_id,
                lease.run.execution_profile,
                profile,
                settings.deepseek_api_key.get_secret_value() if settings.deepseek_api_key else "",
            )
            return resolved

        def runtime_for(lease: RunLease) -> TextRuntime:
            nonlocal claimed_actor
            claimed_actor = RunActor(lease.run.created_by, lease.run.workspace_id)

            async def tool_call(call_id: str, name: str, arguments: Json) -> Json:
                actor = RunActor(lease.run.created_by, lease.run.workspace_id)
                plan = await delivery.get_plan(actor, lease.run.id)
                role = (
                    "recovery"
                    if lease.run.recovery_read_only
                    else "sandbox"
                    if plan and plan.status == "APPROVED"
                    else "console"
                )
                scope = ToolScope(lease.run.id, actor, lease.owner, lease.fencing_token, role)
                if name == "plan.propose":
                    # 新计划会结束旧批准和租约；先保存上一段已获准修改，供审批后继续。
                    await delivery.checkpoint_workspace(actor, lease.run.id)
                declared = next((item for item in gateway.specifications() if item.name == name), None)
                executions = cast(SqlAlchemyExecutionStore, app.state.execution_store)
                try:
                    result = await gateway.invoke(scope, call_id, name, arguments)
                except asyncio.CancelledError:
                    if declared and declared.effect != "read":
                        # 取消写命令可能已发生部分效果；旧租约不能借清理回调改变新执行者的状态。
                        await executions.transition(actor, lease.run.id, "RECOVERY_REQUIRED", expected_lease=lease)
                    raise
                error = result.get("error")
                unknown = isinstance(error, dict) and error.get("code") in {
                    "TOOL_TIMEOUT",
                    "TOOL_FAILED",
                    "TOOL_INTERRUPTED",
                    "TOOL_RECOVERY_REQUIRED",
                }
                if unknown and declared and declared.effect != "read":
                    await executions.transition(actor, lease.run.id, "RECOVERY_REQUIRED", expected_lease=lease)
                if result.get("ok") is True and name in {"sandbox.file.write", "sandbox.command"}:
                    # 保存已完成的修改；以后长命令取消/超时时仍能恢复最后已知状态。
                    await delivery.checkpoint_workspace(actor, lease.run.id)
                # plan.propose 会让旧租约失效，立即停止模型，避免审批前继续消耗或执行。
                current = await runs.access_metadata(actor, lease.run.id)
                if current and current.run.status != "RUNNING":
                    raise RuntimeFailure("RUN_PAUSED")
                return result

            assert lease.run.execution_profile is not None
            create_runtime = harness_factory(settings, lease.run.execution_profile, resolved_key)
            if app.state.coordination_service:
                from weora.modules.coordination.worker_runtime import CoordinatedRuntime

                async def authorize() -> bool:
                    return await cast(SqlAlchemyExecutionStore, app.state.execution_store).authorize_token(
                        claimed_actor, lease.run.id, lease.fencing_token, lease.owner
                    )

                return CoordinatedRuntime(
                    app.state.coordination_service,
                    lease,
                    runtime_factory=create_runtime,
                    call_tool=tool_call,
                    authorize=authorize,
                )
            return create_runtime(None, tool_call)

        async def validate_output(lease: RunLease, text: str) -> str:
            # 模型调用期间也可能停用仓库；只读恢复和未启用飞书的部署同样必须复核。
            actor = RunActor(lease.run.created_by, lease.run.workspace_id)
            current = await runs.access_metadata(actor, lease.run.id)
            if current is None:
                raise RuntimeFailure("ACCESS_REVOKED")
            if current.task.project_context and (
                app.state.context_access.projects is None
                or not await app.state.context_access.projects.authorize(actor, current.task.project_context)
            ):
                raise RuntimeFailure("PROJECT_ACCESS_REVOKED")
            if not lease.run.recovery_read_only:
                try:
                    await delivery.validate_completion(
                        RunActor(lease.run.created_by, lease.run.workspace_id), lease.run.id, lease.fencing_token
                    )
                except DeliveryError as error:
                    raise RuntimeFailure(error.code) from None
            if context:
                try:
                    scope = await app.state.context_scope_factory(
                        ContextActor(lease.run.created_by, lease.run.workspace_id), lease.run.id
                    )
                    await context.authorize_derived(scope)
                    await context.validate_refs(scope, re.findall(r"ctx:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", text))
                except ContextError as error:
                    raise RuntimeFailure(error.code) from None
            return text

        async def checkpoint_before_finish(lease: RunLease) -> None:
            """仅当前执行者保存工作，过期执行者不能覆盖后来者的恢复依据。"""
            actor = RunActor(lease.run.created_by, workspace_id)
            if not await app.state.execution_store.authorize_token(
                actor, lease.run.id, lease.fencing_token, lease.owner
            ):
                return
            try:
                await delivery.checkpoint_workspace(actor, lease.run.id)
            except DeliveryError as error:
                raise RuntimeFailure(error.code) from None

        executor = RunExecutor(
            store=SqlAlchemyExecutionStore(sessions),
            access=ModulesExecutionAccess(
                identity=cast(IdentityService, app.state.identity_service),
                agents=cast(AgentRegistryService, app.state.agent_registry_service),
                uow_factory=lambda: SqlAlchemyRunsUnitOfWork(sessions),
                context=context,
                delivery=delivery,
                gateway=gateway,
                tool_store=cast(SqlAlchemyToolStore, app.state.tool_store),
                executions=cast(SqlAlchemyExecutionStore, app.state.execution_store),
                projects=app.state.context_access.projects,
            ),
            runtime=PiTextRuntime(
                node_executable=settings.pi_node_executable,
                entry=Path(settings.pi_runtime_entry or ""),
                api_key=settings.deepseek_api_key.get_secret_value() if settings.deepseek_api_key is not None else "",
            ),
            profile=profile,
            lease_seconds=settings.run_lease_seconds,
            heartbeat_seconds=min(5, settings.run_lease_seconds / 3),
            runtime_factory=runtime_for,
            validate_output=validate_output,
            before_finish=checkpoint_before_finish,
            profile_resolver=resolve_profile,
        )
        try:
            completed = await executor.execute(workspace_id=workspace_id, run_id=run_id, owner=str(uuid4()))
            if context and claimed_actor:
                # 内部只读状态；发送仍由渠道重新核验来源与受众。
                detail = await runs.access_metadata(claimed_actor, run_id)
                if (
                    detail
                    and detail.run.status == "SUCCEEDED"
                    and detail.task.audience == "group"
                    and detail.run.output_text
                    and not (detail.task.origin_metadata or {}).get("coordination_scope_id")
                ):
                    scope = await app.state.context_scope_factory(
                        ContextActor(detail.run.created_by, workspace_id), run_id
                    )
                    origin = detail.task.origin_metadata or {}
                    await FeishuChannels(context).enqueue_delivery(
                        scope, str(origin["inbox_id"]), detail.run.output_text, f"run-result:{run_id}"
                    )
            return completed
        finally:
            if claimed_actor:
                # 当前实例只清理自己拥有的容器，迟到 Worker 不会删除后来者的环境。
                await delivery.cleanup(claimed_actor, run_id)


async def _dispatch() -> int:
    settings = get_settings()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        sessions = cast(SessionFactory, app.state.db_session_factory)
        store = SqlAlchemyExecutionStore(sessions)
        await store.recover_expired()
        await _reconcile_tools(app)
        await _reconcile_control(app)
        await cast(RunService, app.state.run_service).dispatch_task_inputs()
        await app.state.task_collaboration_service.settle_handoffs()
        await _pump_context(app)
        await cast(DeliveryService, app.state.delivery_service).purge_expired()
        await cast(SqlAlchemyToolStore, app.state.tool_store).purge()
        sandbox = cast(DockerSandbox, app.state.sandbox)
        try:
            failed_cleanups = await sandbox.reap_orphans(store.is_live)
        except DeliveryError:
            # 清理基础设施不可用不阻塞只读任务；下个 Beat 周期继续补偿。
            failed_cleanups = []
        for run_id in failed_cleanups:
            actor = await store.actor_for_maintenance(run_id)
            if actor:
                await cast(DeliveryService, app.state.delivery_service).record_cleanup_failure(actor, run_id)
        return await _dispatch_pending_runs(app)


async def _dispatch_pending_runs(app: FastAPI) -> int:
    """只投递已绑定的原生 Run，不调用模型、渠道或容器清理；队列确认后才保存投递回执。"""
    store = cast(SqlAlchemyExecutionStore, app.state.execution_store)
    if execution_profile(cast(AppSettings, app.state.settings)) is None:
        return 0
    jobs = await store.pending_dispatches()
    for workspace_id, run_id in jobs:
        priority = 0
        coordinator = app.state.coordination_service
        if coordinator:
            actor = await store.actor_for_maintenance(run_id)
            detail = await cast(RunService, app.state.run_service).access_metadata(actor, run_id) if actor else None
            origin = (detail.task.origin_metadata or {}) if detail else {}
            if origin.get("coordination_scope_id") and not await coordinator.dispatchable(workspace_id, run_id, origin):
                continue
            if origin.get("coordination_scope_id") and not origin.get("coordination_priority"):
                # Redis 的 0 为最高优先级；普通自动接单低于人工跟进和既有 Console 请求。
                priority = 3
        # 只有队列确认发送后才标记投递。中间崩溃会重复发送，由领取 CAS 去重。
        await asyncio.to_thread(
            celery_app.send_task, "weora.runs.execute", args=[str(workspace_id), str(run_id)], priority=priority
        )
        await store.mark_dispatched(workspace_id=workspace_id, run_id=run_id)
    return len(jobs)


async def _reconcile_tools(app: FastAPI) -> int:
    """硬退出没有 finally；按原执行尝试补偿工具状态，不把未知结果误显示为仍在执行。"""
    executions = cast(SqlAlchemyExecutionStore, app.state.execution_store)
    tools = cast(SqlAlchemyToolStore, app.state.tool_store)
    count = 0
    for scope in await tools.pending_attempts():
        if scope.fencing_token:
            active = await executions.authorize_token(scope.actor, scope.run_id, scope.fencing_token, scope.lease_owner)
        else:
            # 升级前缺少执行标识的记录不能猜测 owner；先等整个 Run 不活跃再补偿。
            active = await executions.is_live(scope.run_id)
        if not active:
            count += await tools.interrupt_attempt(scope)
    return count


async def _reconcile_control(app: FastAPI) -> None:
    """补偿计划已提交而 Run 状态尚未写入的窗口；恢复排队，不直接重放写工具。"""
    executions = cast(SqlAlchemyExecutionStore, app.state.execution_store)
    delivery = cast(DeliveryService, app.state.delivery_service)
    for run in await executions.maintenance_runs():
        actor = RunActor(run.created_by, run.workspace_id)
        try:
            plan = await delivery.get_plan(actor, run.id)
            if plan is None:
                continue
            if run.status == "WAITING_APPROVAL" and plan.status == "APPROVED":
                await delivery.decide(actor, run.id, plan.id, "approve")
            elif run.status == "WAITING_APPROVAL" and plan.status == "REVISION_REQUIRED":
                # 补偿补充意见已保存、规划任务尚未排队的窗口；此状态没有执行授权。
                await executions.transition(actor, run.id, "QUEUED")
            elif plan.status == "REJECTED" and run.status == "WAITING_APPROVAL":
                await delivery.decide(actor, run.id, plan.id, "reject")
            elif run.status == "RECOVERY_REQUIRED" and plan.status == "PENDING":
                # 只恢复审批界面；未知写结果不重放，仍需人类明确批准当前计划。
                await executions.transition(actor, run.id, "WAITING_APPROVAL")
        except DeliveryError:
            continue


async def _pump_inbox(app: FastAPI) -> None:
    """按消息顺序接入持久 Inbox 并更新输入回执，不读取外部历史、不发送消息或清理容器。"""
    context = cast(ContextService | None, app.state.context_service)
    if context is None:
        return
    runs = cast(RunService, app.state.run_service)
    channels = FeishuChannels(context)
    for workspace_id in await cast(SqlContextStore, context.store).workspace_ids():

        async def submit(actor: ContextActor, payload: Json) -> Json:
            coordinator = app.state.coordination_service
            if coordinator:
                managed = await coordinator.ingest(actor, payload)
                if managed is not None:
                    return cast(Json, managed)
            if payload.get("addressed") is False:
                # 候选入库后配置被撤销，不能回退到旧入口自动创建任务。
                raise ContextError("SOURCE_REVOKED", "主 Agent 候选配置已撤销")
            origin = {
                key: payload.get(key)
                for key in (
                    "source_id",
                    "connection_id",
                    "chat_id",
                    "thread_id",
                    "message_id",
                    "open_id",
                    "created_at",
                    "quote_id",
                    "include_nearby",
                )
            }
            origin["inbox_id"] = payload["id"]
            if payload.get("root_id"):
                origin["root_id"] = payload["root_id"]
            if payload.get("project_id"):
                # 普通/历史群任务保留原始指纹；只给新项目路由增加分享授权绑定。
                origin.update(route_key=payload.get("route_key"), project_grant_id=payload.get("project_grant_id"))
            return await runs.submit_channel(
                actor=RunActor(actor.user_id, actor.workspace_id),
                agent_id=UUID(str(payload["agent_id"])),
                agent_version=str(payload["agent_version"]),
                instruction=str(payload["instruction"]),
                idempotency_key=str(payload["idempotency_key"]),
                request_id=f"feishu:{payload['id']}",
                origin_metadata=origin,
                project_id=UUID(str(payload["project_id"])) if payload.get("project_id") else None,
                repository_ids=tuple(UUID(str(item)) for item in cast(list[str], payload.get("repository_ids", []))),
            )

        inboxes = await context.store.list(workspace_id, "inbox")
        # 供应商 message_id 不按时间排序；同一批的根请求必须先于后续消息建立 Task 关联。
        for item in sorted(inboxes, key=lambda row: (str(row.get("created_at", "")), str(row["id"]))):
            if item.get("status") == "WAITING_RUN" and item.get("input_id") and item.get("actor_id"):
                try:
                    accepted = await runs.task_input(
                        RunActor(UUID(str(item["actor_id"])), workspace_id),
                        UUID(str(item["input_id"])),
                    )
                    key = f"{item['connection_id']}:{item['message_id']}"
                    if accepted.run_id or accepted.status == "REJECTED":

                        def bind_receipt(old: Json | None, run_id: UUID | None = accepted.run_id) -> Json:
                            return dict(
                                old or {},
                                status="SUBMITTED" if run_id else "REJECTED",
                                run_id=str(run_id) if run_id else None,
                            )

                        item = await context.store.change(
                            workspace_id,
                            "inbox",
                            key,
                            bind_receipt,
                        )
                except RunError:
                    continue
            if item.get("status") == "RECEIVED":
                try:
                    await channels.process(workspace_id, str(item["id"]), submit)
                except (ContextError, RunError, CoordinationError):
                    continue


async def _pump_context(app: FastAPI) -> None:
    """维护入口先接收入站，再补齐旧渠道 Outbox 和保留期；慢 I/O 不进入协调控制快路径。"""
    await _pump_inbox(app)
    context = cast(ContextService | None, app.state.context_service)
    if context is None:
        return
    from weora.bootstrap.retention import purge_derived_content

    runs = cast(RunService, app.state.run_service)
    channels = FeishuChannels(context)
    for workspace_id in await cast(SqlContextStore, context.store).workspace_ids():
        inboxes = await context.store.list(workspace_id, "inbox")
        for item in sorted(inboxes, key=lambda row: (str(row.get("created_at", "")), str(row["id"]))):
            if item.get("status") == "SUBMITTED" and item.get("actor_id") and item.get("run_id"):
                # 覆盖 finish 已提交、enqueue 前崩溃；业务键由 Run 与状态版本决定。
                actor = RunActor(UUID(str(item["actor_id"])), workspace_id)
                detail = await runs.access_metadata(actor, UUID(str(item["run_id"])))
                if (
                    detail
                    and detail.task.audience == "group"
                    and not (detail.task.origin_metadata or {}).get("coordination_scope_id")
                ):
                    text: str | None = None
                    business_key = f"run-result:{detail.run.id}"
                    if detail.run.status == "SUCCEEDED" and detail.run.output_text:
                        text = detail.run.output_text
                    elif detail.run.status in {"WAITING_APPROVAL", "WAITING_CONTEXT", "RECOVERY_REQUIRED"}:
                        settings = cast(AppSettings, app.state.settings)
                        link = (
                            f"{settings.console_public_url.rstrip('/')}/runs/{detail.run.id}"
                            if settings.console_public_url
                            else str(detail.run.id)
                        )
                        text = f"运行当前状态：{detail.run.status}。请在 Weora Console 查看并处理：{link}"
                        business_key = f"run-waiting:{detail.run.id}:{detail.run.last_sequence}"
                    if text:
                        try:
                            scope = await app.state.context_scope_factory(
                                ContextActor(actor.user_id, workspace_id), detail.run.id
                            )
                            await channels.enqueue_delivery(scope, str(item["id"]), text, business_key)
                        except ContextError:
                            continue
        for item in await context.store.list(workspace_id, "delivery"):
            if item.get("status") == "PENDING":
                try:
                    scope = await app.state.context_scope_factory(
                        ContextActor(UUID(str(item["actor_id"])), workspace_id), UUID(str(item["run_id"]))
                    )
                    await channels.deliver(scope, str(item["business_key"]))
                except ContextError:
                    continue
        expired = await context.expired_run_ids(workspace_id)
        await purge_derived_content(app, workspace_id, expired)
        await context.purge_expired(workspace_id)
        await channels.recover_and_purge(workspace_id)


def execute_run(workspace_id: str, run_id: str) -> bool:
    """每条消息使用独立事件循环及连接池；不向队列结果后端写模型正文。"""
    try:
        return asyncio.run(_execute(UUID(workspace_id), UUID(run_id)))
    except Exception:
        # 队列日志不携带 SQL、SDK 链式异常或后台凭据；权威恢复状态由过期扫描处理。
        raise RuntimeError("RUN_EXECUTION_INFRASTRUCTURE_ERROR") from None


def dispatch_runs() -> int:
    """Beat 定期补投 PostgreSQL 中的任务，Redis 不是唯一可靠队列记录。"""
    try:
        return asyncio.run(_dispatch())
    except Exception:
        raise RuntimeError("RUN_DISPATCH_INFRASTRUCTURE_ERROR") from None


# 保留普通函数的类型，避免无类型 Celery 装饰器污染应用层签名。
celery_app.task(name="weora.runs.execute")(execute_run)

celery_app.task(name="weora.coordination.execute")(execute_effect)
celery_app.task(name="weora.coordination.dispatch")(dispatch_effects)
celery_app.task(name="weora.runs.dispatch")(dispatch_runs)
