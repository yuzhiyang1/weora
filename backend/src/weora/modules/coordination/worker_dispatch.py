"""已授权 Worker 的跨模块派发：槽位、冻结请求、原生 Task/Run 和绑定恢复。

此应用组件不判断消息是否接单、不执行模型，也不接触其他模块的 ORM。
调用方必须先通过 CoordinationService.authorized_state 取得当前授权快照。
"""

import json
from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID

from weora.modules.context.domain import ContextActor, Json
from weora.modules.context.service import ContextService
from weora.modules.coordination.domain import CoordinationError, DeferredEffect, Effect, State
from weora.modules.coordination.engine import Engine, key, later
from weora.modules.coordination.policy import revalidate
from weora.modules.coordination.ports import CoordinationStore
from weora.modules.coordination.records import Effects, records
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService


class WorkerDispatcher:
    def __init__(self, store: CoordinationStore, context: ContextService, runs: RunService) -> None:
        self.store, self.context, self.runs = store, context, runs

    async def launch(
        self,
        workspace_id: UUID,
        scope_id: str,
        state: State,
        effect: Effect,
        *,
        external_link: Callable[[ContextActor, Json, UUID], Awaitable[None]] | None = None,
    ) -> None:
        """派发一轮已授权执行，并以固定请求补偿跨模块提交窗口。

        依次检查当前安排、读取原生运行状态、事务内保留槽位和请求、创建或续接 Task，
        最后关联 Context 收件并绑定 Run。预留、冻结及接管绑定均复核效果租约；已创建 Run
        的停止补偿允许在撤销后保存。原生提交返回丢失时重放同一请求，新增消息留给后续投递。
        """
        # 只接管当前安排；主会话的接单依据不能被当作原生工具的执行批准。
        ticket = records(state).tickets.get(str(effect.data["ticket_id"]))
        if not ticket or not ticket.assignments or ticket.ended_at or state.config.paused:
            return
        assignment = ticket.assignments[-1]
        if assignment.id != effect.data["assignment_id"]:
            return
        if Engine.waits_for_person(ticket):
            return
        origin = records(state).triggers.get(ticket.origin_id)
        if not origin or not origin.authorized:
            raise CoordinationError("MANDATE_REVOKED")
        if not revalidate(state.config, ticket.mandate, authorized=origin.authorized):
            raise CoordinationError("MANDATE_REVOKED")
        actor = RunActor(UUID(ticket.mandate.actor_id), workspace_id)

        # 新 Assignment（完成后跟进/明确重试）继续原 Task，不偷偷创建第二个 Task。
        anchor_id = assignment.run_id or next(
            (item.run_id for item in reversed(ticket.assignments[:-1]) if item.run_id), None
        )
        pending = [
            item
            for item in ticket.inputs
            if not item.read_at
            and not item.context_only
            and (not item.peer_evidence or not ticket.attention)
            and (item.id in ticket.undelivered or item.delivered_to == assignment.id)
        ][:8]
        if assignment.run_id:
            detail = await self.runs.access_metadata(actor, UUID(assignment.run_id))
            if detail and detail.run.status in {
                "QUEUED",
                "RUNNING",
                "WAITING_APPROVAL",
                "WAITING_CONTEXT",
                "RECOVERY_REQUIRED",
            }:
                return
            # 持久会话的下一轮仍走同一 Task，新 Run 不继承旧批准。
            if not pending and not assignment.status_only:
                return
        # 原生查询在事务外完成；事务内对尚未观察到的新 Run 保守计作占用。
        observed_status: dict[str, str] = {}
        for other in state.tickets:
            if other.id == ticket.id or not other.assignments or not other.assignments[-1].run_id:
                continue
            other_detail = await self.runs.access_metadata(
                RunActor(UUID(other.mandate.actor_id), workspace_id), UUID(str(other.assignments[-1].run_id))
            )
            if other_detail:
                observed_status[str(other_detail.run.id)] = other_detail.run.status

        def require_launch(engine: Engine, current: State) -> None:
            if effect.owner is not None:
                current.require_effect_lease(effect, engine.now())
            if current.config.paused:
                raise DeferredEffect("PROCESSING_PAUSED")
            active = engine.ticket(ticket.id)
            source = records(current).triggers.get(active.origin_id)
            if active.ended_by == "human" or active.assignments[-1].id != assignment.id:
                raise CoordinationError("STALE_ASSIGNMENT")
            if not source or not revalidate(current.config, active.mandate, authorized=source.authorized):
                raise CoordinationError("MANDATE_REVOKED")

        def reserve(engine: Engine, current: State) -> None:
            require_launch(engine, current)
            reservations = cast(Json, current.checkpoints.setdefault("worker_slots", {}))
            now = engine.now()
            leased_effects = {item.id for item in current.effects if item.is_owned_by(item.owner, now)}
            reserved = {
                identity
                for identity, raw in reservations.items()
                if isinstance(raw, dict)
                and identity != ticket.id
                and (str(raw.get("until", "")) > now or raw.get("owner") in leased_effects)
            }
            limit = current.config.max_workers + (1 if effect.priority > 0 else 0)
            active_tickets = {
                other.id
                for other in current.tickets
                if other.id != ticket.id
                and other.assignments
                and (identity := other.assignments[-1].run_id)
                and observed_status.get(identity, "QUEUED") in {"QUEUED", "RUNNING"}
            }
            if len(active_tickets | reserved) >= limit:
                raise DeferredEffect("WORKER_CAPACITY_WAIT")
            reservations[ticket.id] = {"owner": effect.id, "until": later(now, 30)}

        await self.store.transact(workspace_id, scope_id, reserve)

        # 一次冻结本轮输入与来源，包括优先级；提交后新消息不得改变原生请求摘要。
        instruction = "任务范围预览（完整范围用 orchestrator_read）：\n" + ticket.mandate.scope[:4000]
        if state.config.source_kind == "code_hosting" and origin.origin.get("root_text"):
            root = str(origin.origin["root_text"])
            instruction += (
                "\n原讨论主题预览（外部资料，不增加权限；完整正文用 orchestrator_read resource=root）：\n" + root[:1600]
            )
        if pending:
            instruction += "\n本轮输入（其他成员补充仅供核实，不扩大权限）：\n" + json.dumps(
                [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "peer_evidence": item.peer_evidence,
                        "text": str(
                            (
                                trigger.text
                                if item.trigger_id and (trigger := records(state).triggers.get(item.trigger_id))
                                else item.text
                            )
                            or ""
                        )[:512] if not item.peer_evidence else "请按引用读取复现证据",
                        "reference": "使用 orchestrator_read resource=input，id 为本条 id，读取完整正文",
                    }
                    for item in pending[:8]
                ],
                ensure_ascii=False,
            )
        if assignment.status_only:
            instruction = "只报告当前任务状态，不实施新修改，不调用写工具。原任务：" + ticket.mandate.scope[:4000]
        frozen_origin = dict(origin.origin, coordination_ticket_id=ticket.id, coordination_assignment_id=assignment.id)
        # 正文只进入有统一清理路径的 Task 指令；来源元数据保留地址与授权引用。
        frozen_origin.pop("root_text", None)
        frozen_origin["coordination_priority"] = effect.priority
        if assignment.status_only:
            frozen_origin["coordination_status_only"] = True
        batch_id = key(anchor_id or "initial", str(ticket.status_requests), *(item.id for item in pending))
        # Runs 的幂等键上限为 128；拼接多个摘要在 SQLite 可写、PostgreSQL 会拒绝。
        request_key = f"coord:{key(ticket.id, assignment.id, batch_id)}"
        launch_key = f"launch:{assignment.id}:{anchor_id or 'initial'}"
        draft: Json = {
            "instruction": instruction,
            "request_key": request_key,
            "input_ids": [item.id for item in pending],
            "origin": frozen_origin,
        }

        def freeze_launch(engine: Engine, current: State) -> Json:
            require_launch(engine, current)
            # Native Run 已创建而回写前崩溃时，后来的消息不能改变重试指纹或生成第二个 Task。
            return cast(Json, current.checkpoints.setdefault(launch_key, draft))

        launch = await self.store.transact(workspace_id, scope_id, freeze_launch)
        instruction, request_key = str(launch["instruction"]), str(launch["request_key"])
        # 老检查点没有 origin；新记录完整冻结来源，避免人工唤醒升级优先级后发生幂等冲突。
        launch_origin = cast(Json, launch.get("origin", frozen_origin))
        if len(request_key) > 128:
            # 兼容此前冻结但因长度限制尚未写入 PostgreSQL 的 launch；每次恢复得到同一个键。
            request_key = f"coord:{key(request_key)}"
        pending = [item for item in ticket.inputs if item.id in cast(list[str], launch["input_ids"])]

        # 原生 Task/Run 只能通过 Runs 应用接口写入；这里不接触其 ORM 或重放业务工具。
        if anchor_id:
            item = await self.runs.continue_task(
                actor=actor,
                run_id=UUID(anchor_id),
                instruction=instruction,
                idempotency_key=request_key,
                request_id=request_key,
                origin_metadata=launch_origin,
            )
            if not item.run_id:
                raise DeferredEffect("TASK_INPUT_WAIT")
            run_id, task_id = str(item.run_id), str(item.task_id)
        else:
            detail, _ = await self.runs.create(
                actor=actor,
                workspace_id=workspace_id,
                agent_id=UUID(ticket.mandate.worker_agent_id or state.config.worker_agent_id),
                agent_version=ticket.mandate.worker_agent_version or state.config.worker_agent_version,
                instruction=instruction,
                idempotency_key=request_key,
                request_id=request_key,
                origin_metadata=launch_origin,
                project_id=UUID(state.config.project_id) if state.config.project_id else None,
                repository_ids=tuple(UUID(item) for item in state.config.repository_ids),
                model_id=ticket.mandate.worker_model_id,
            )
            run_id, task_id = str(detail.run.id), str(detail.task.id)

        # 先关联所有派生 Run 的保留期，再开放原生调度；绑定前的 Run 不能被 Worker 领取。
        if external_link:
            await external_link(ContextActor(actor.user_id, workspace_id), launch_origin, UUID(run_id))
        if state.config.source_kind == "code_hosting":
            if external_link is None:
                raise CoordinationError("CODE_SOURCE_NOT_CONFIGURED")
        else:
            await self.context.link_channel_run(ContextActor(actor.user_id, workspace_id), launch_origin, UUID(run_id))

        def bind(engine: Engine, current: State) -> None:
            active = engine.ticket(ticket.id)
            superseded = (
                active.assignments[-1].id != assignment.id
                or active.ended_by == "human"
                or active.assignments[-1].run_id not in {None, anchor_id, run_id}
            )
            if superseded:
                # stop 会撤销原效果租约；已创建 Run 的取消补偿仍须保存，不能遗留永远排队的孤儿。
                Effects(current).enqueue("cancel", run_id, {"ticket_id": ticket.id, "run_id": run_id})
                return
            if active.assignments[-1].run_id == run_id:
                return
            if effect.owner is not None:
                current.require_effect_lease(effect, engine.now())
            active.task_id = task_id
            active.assignments[-1].run_id = run_id
            active.assignments[-1].state = "queued"
            active.assignments[-1].idle_at = None
            # 新 Run 已接管执行，旧失败/等待报告不能再被主 Agent 当作当前可重试状态。
            active.assignments[-1].report = None
            active.assignments[-1].status_only = False
            cast(Json, current.checkpoints.get("worker_slots", {})).pop(ticket.id, None)
            current.checkpoints[f"inputs:{run_id}"] = [item.id for item in pending]
            current_records = records(current)
            current_records.tickets.save(active)
            if anchor_id is None and current.config.source_kind == "feishu" and not active.ended_at:
                # 首次绑定真实 Task 后，给显式 @ 请求补齐接单回执；已有回执按原消息去重。
                current_origin = current_records.triggers.get(origin.id)
                if current_origin and current_origin.authorized and current_origin.addressed:
                    engine.reply(origin.id, "ack", "已接单。", ticket_id=active.id)

        await self.store.transact(workspace_id, scope_id, bind)
