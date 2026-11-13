"""独立协调队列，主 Agent 不被工程 Worker 的长工具等待阻塞。"""

import asyncio
import logging
import re
import traceback
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from pydantic import TypeAdapter

from weora.bootstrap.application import create_app
from weora.bootstrap.console_settings import model_profile, resolve_run_profile
from weora.bootstrap.run_profile import execution_profile
from weora.modules.agents.domain.errors import AgentRegistryError
from weora.modules.context.domain import ContextError, Json
from weora.modules.coordination.domain import CoordinationError, DeferredEffect
from weora.modules.coordination.engine import key
from weora.modules.coordination.records import records
from weora.modules.coordination.service import CoordinationService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.projects.domain import ProjectError
from weora.modules.runs.application.execution import RuntimeFailure
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.domain.errors import RunError
from weora.modules.runs.infrastructure.pi_runtime import PiSession, PiTextRuntime

logger = logging.getLogger(__name__)


async def dispatch(app: FastAPI) -> int:
    coordinator = cast(CoordinationService | None, app.state.coordination_service)
    if not coordinator:
        return 0
    from weora.bootstrap.worker import _dispatch_pending_runs, _pump_inbox, celery_app

    # 即时控制只读取持久输入和调度状态；完整维护仍由 runs 队列的旧 Beat 入口执行。
    await coordinator.runs.dispatch_task_inputs()
    await _pump_inbox(app)
    await _dispatch_pending_runs(app)

    count = 0
    for workspace_id, scope_id in await coordinator.store.scopes():
        try:
            for effect in await coordinator.pending_effects(workspace_id, scope_id):
                # 运输层只负责通知；重复通知由数据库租约拦截，丢失通知由 Beat 补投。
                await asyncio.to_thread(
                    celery_app.send_task,
                    "weora.coordination.execute",
                    args=[str(workspace_id), scope_id, effect.id],
                    queue="weora.coordination.main" if effect.kind == "main" else "weora.coordination.effects",
                )
                count += 1
        except (ContextError, CoordinationError):
            continue
    return count


async def execute(workspace_id: UUID, scope_id: str, effect_id: str) -> bool:
    app = create_app()
    async with app.router.lifespan_context(app):
        coordinator = cast(CoordinationService | None, app.state.coordination_service)
        if not coordinator:
            return False
        settings = app.state.settings
        profile = execution_profile(settings)
        effect = await coordinator.store.claim(workspace_id, scope_id, effect_id, str(uuid4()), seconds=60)
        if not effect:
            return False

        async def perform() -> None:
            current = await coordinator.store.snapshot(workspace_id, scope_id)
            if current.config.paused or not current.config.enabled:
                raise DeferredEffect("PROCESSING_PAUSED")
            if effect.kind == "main":
                if profile is None:
                    raise RuntimeFailure("RUNTIME_NOT_CONFIGURED")
                journal = records(current).sessions.get(key("main-session", scope_id))
                if journal and journal.execution_profile:
                    frozen = TypeAdapter(ExecutionProfile).validate_python(journal.execution_profile)
                    active_profile, active_key = await resolve_run_profile(
                        app.state.console_settings_service, workspace_id, frozen, profile
                    )
                elif journal and journal.messages:
                    # 首次升级的历史日志继续使用部署配置，不能悄悄换成新密钥。
                    active_profile, active_key = profile, app.state.console_settings_service.deployment_key()
                else:
                    configuration = await app.state.console_settings_service.resolve_model(workspace_id)
                    active_profile, active_key = model_profile(configuration, profile), configuration.api_key
                if not active_key:
                    raise RuntimeFailure("MODEL_KEY_REQUIRED")

                def runtime(session: PiSession, invoke: Callable[[str, str, Json], Awaitable[Json]]) -> PiTextRuntime:
                    return PiTextRuntime(
                        node_executable=settings.pi_node_executable,
                        entry=Path(settings.pi_runtime_entry or ""),
                        api_key=active_key,
                        session=session,
                        call_tool=invoke,
                    )

                await coordinator.main(workspace_id, scope_id, effect, runtime, active_profile)
            elif effect.kind == "worker":
                await coordinator.launch(workspace_id, scope_id, effect, runtime_available=profile is not None)
            elif effect.kind == "poll":
                from weora.modules.coordination.polling import compensate

                await compensate(coordinator, workspace_id, scope_id, effect)
            elif effect.kind == "code_poll":
                await coordinator.poll_code_events(workspace_id, scope_id, effect)
            elif effect.kind == "reply":
                await coordinator.deliver(workspace_id, scope_id, effect)
            elif effect.kind == "cancel":
                state = await coordinator.store.snapshot(workspace_id, scope_id)
                ticket = next(item for item in state.tickets if item.id == effect.data["ticket_id"])
                actor = RunActor(UUID(ticket.mandate.actor_id), workspace_id)
                run_id = UUID(str(effect.data["run_id"]))
                detail = await coordinator.runs.access_metadata(actor, run_id)
                if detail is None or detail.run.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    return
                await coordinator.runs.cancel(
                    actor=actor,
                    workspace_id=workspace_id,
                    run_id=UUID(str(effect.data["run_id"])),
                    request_id=effect.id,
                )

        job = asyncio.create_task(perform())
        try:
            while True:
                done, _ = await asyncio.wait({job}, timeout=10)
                if done:
                    await job
                    break
                if not await coordinator.store.renew(workspace_id, scope_id, effect, seconds=60):
                    raise CoordinationError("EFFECT_LEASE_LOST")
            await coordinator.store.settle(workspace_id, scope_id, effect)
        except DeferredEffect as error:
            await coordinator.store.defer(workspace_id, scope_id, effect, error=error.code)
            return False
        except Exception as error:
            code = (
                error.code
                if isinstance(
                    error,
                    (
                        ContextError,
                        CoordinationError,
                        RuntimeFailure,
                        RunError,
                        ProjectError,
                        AgentRegistryError,
                        DeliveryError,
                    ),
                )
                else "COORDINATION_EFFECT_FAILED"
            )
            if not re.fullmatch(r"[A-Z0-9_]{1,80}", code):
                code = "COORDINATION_EFFECT_FAILED"
            # 只记录代码位置和异常类型，不输出可能携带消息正文、SQL 参数或凭据的异常原文。
            cause = error.__cause__ or error
            logger.warning(
                "coordination_effect_failed kind=%s effect=%s code=%s cause=%s frames=%s",
                effect.kind,
                effect.id,
                code,
                type(cause).__name__,
                [
                    (Path(frame.filename).name, frame.name, frame.lineno)
                    for frame in traceback.extract_tb(cause.__traceback__)
                ],
            )
            if code in {"EFFECT_LEASE_LOST", "SESSION_LEASE_LOST"}:
                await coordinator.store.defer(workspace_id, scope_id, effect, error=code)
                return False
            if effect.kind in {"poll", "code_poll"}:
                await coordinator.store.defer(
                    workspace_id, scope_id, effect, delay=min(60, 2 ** min(effect.attempts, 6)), error=code
                )
                return False
            unknown_reply = effect.kind == "reply"
            if effect.kind == "reply":
                current = await coordinator.store.snapshot(workspace_id, scope_id)
                reply = records(current).replies.get(str(effect.data["reply_id"]))
                # 图片准备阶段未发群消息；短暂故障有限重试，权限或数据错误直接阻塞。
                if reply and reply.state == "pending":
                    unknown_reply = False
                    if code in {"PROVIDER_UNAVAILABLE", "RATE_LIMITED", "TOKEN_EXPIRED"} and effect.attempts < 3:
                        await coordinator.store.defer(
                            workspace_id, scope_id, effect, delay=2 ** effect.attempts, error=code
                        )
                        return False
            # 外部写未知禁止自动重放；主会话失败等候人工重排，不把 SDK 原文写入日志。
            await coordinator.effect_failed(workspace_id, scope_id, effect, code=code)
            await coordinator.store.settle(workspace_id, scope_id, effect, error=code, unknown=unknown_reply)
            return False
        finally:
            if not job.done():
                job.cancel()
                with suppress(asyncio.CancelledError):
                    await job
        return True


def execute_effect(workspace_id: str, scope_id: str, effect_id: str) -> bool:
    return asyncio.run(execute(UUID(workspace_id), scope_id, effect_id))


def dispatch_effects() -> int:
    async def run() -> int:
        app = create_app()
        async with app.router.lifespan_context(app):
            return await dispatch(app)

    return asyncio.run(run())
