"""协调用例：模型在事务外运行，Task/Run 写入只调用 Runs 的公开应用接口。"""

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from weora.modules.agents.application.service import AgentActor, AgentRegistryService
from weora.modules.agents.domain.errors import AgentRegistryError
from weora.modules.collaboration.application.tasks import TaskRecord
from weora.modules.context.domain import ContextActor, ContextError, ContextRunScope, Json, ReplyNotSent
from weora.modules.context.service import ContextService, timestamp
from weora.modules.coordination.domain import (
    Config,
    CoordinationError,
    DeferredEffect,
    Effect,
    Evaluation,
    Report,
    State,
    Trigger,
)
from weora.modules.coordination.engine import Engine, key, later
from weora.modules.coordination.hosting import CodeEventReader
from weora.modules.coordination.hosting_poll import CodePolling
from weora.modules.coordination.main_session import MainSessionRunner
from weora.modules.coordination.ports import CoordinationStore
from weora.modules.coordination.records import Effects, records
from weora.modules.coordination.repository_source import RepositoryIntake
from weora.modules.coordination.tools import MAIN_TOOLS, ROUTE
from weora.modules.coordination.worker_dispatch import WorkerDispatcher
from weora.modules.delivery.application.ports import ReviewReference
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.execution import RuntimeSession, TextRuntime
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.application.service import RunService

FEISHU_REPLY_ERRORS = {
    "PROVIDER_UNAVAILABLE": "飞书接口暂时不可用",
    "RATE_LIMITED": "飞书接口限流",
    "TOKEN_EXPIRED": "飞书应用访问凭证已失效",
    "CREDENTIALS_UNAVAILABLE": "无法获取飞书应用访问凭证",
    "PERMISSION_DENIED": "机器人没有权限、未入群或无法访问原话题",
    "REPLY_ACCESS_REVOKED": "原话题访问授权已失效",
}

RUN_FAILURE_REASONS = {
    "MODEL_FAILED": "模型调用失败",
    "MODEL_UNKNOWN": "模型服务无法识别当前模型",
    "MODEL_TIMEOUT": "模型响应超时",
    "MODEL_KEY_REQUIRED": "模型访问凭证缺失",
    "MODEL_CONFIGURATION_MISMATCH": "模型配置不一致",
    "RUN_TIMEOUT": "任务执行超时",
    "RUNTIME_NOT_CONFIGURED": "执行环境未配置",
    "RUNTIME_PROCESS_FAILED": "执行进程异常退出",
}


class CoordinationService:
    def __init__(
        self,
        store: CoordinationStore,
        context: ContextService,
        runs: RunService,
        agents: AgentRegistryService,
        projects: ProjectService | None = None,
        delivery: DeliveryService | None = None,
        code_events: CodeEventReader | None = None,
        console_url: str | None = None,
    ) -> None:
        self.store, self.context, self.runs, self.agents = store, context, runs, agents
        self.projects = projects
        self.delivery, self.code_events = delivery, code_events
        self.console_url = console_url
        self.repository_intake: RepositoryIntake | None = None
        self.worker_dispatch = WorkerDispatcher(store, context, runs)

    async def list_scopes(self, actor: ContextActor) -> list[Json]:
        """向管理员返回本工作空间的协调投影；读取启用范围时复核来源并清理失效正文。"""
        await self.context.authorize_actor(actor, manage=True)

        items: list[Json] = []
        for _, scope_id in await self.store.scopes(actor.workspace_id):
            state = await self.store.snapshot(actor.workspace_id, scope_id)
            if state.config.enabled:
                state = await self.authorized_state(actor.workspace_id, scope_id)
            items.append(
                {
                    "config": asdict(state.config),
                    "status": self._read(state, {"resource": "overview"})
                    if state.config.enabled
                    else {"enabled": False},
                    "effects": [
                        {"kind": item.kind, "state": item.state, "attempts": item.attempts, "error": item.error}
                        for item in state.effects
                    ],
                    "deliveries": [
                        {
                            "id": item.id,
                            "purpose": item.purpose,
                            "state": item.state,
                            "remote_message_id": item.remote_message_id,
                        }
                        for item in state.replies
                    ],
                }
            )
        return items

    async def read_records(self, actor: ContextActor, scope_id: str, resource: str, offset: int) -> Json:
        """供控制台分页读取协调事实；先检查管理员权限，再清理失效来源的派生正文。"""
        await self.context.authorize_actor(actor, manage=True)
        state = await self.authorized_state(actor.workspace_id, scope_id)
        return self._read(state, {"resource": resource, "offset": offset})

    async def setup_options(self, actor: ContextActor) -> Json:
        """新建协调范围使用真实路由与发布版本；不给前端凭据或要求手抄内部 ID。"""
        await self.context.authorize_actor(actor, manage=True)
        owner = AgentActor(actor.user_id, actor.workspace_id, "member")
        agents: list[Json] = []
        for agent in await self.agents.list_agents(actor=owner, workspace_id=actor.workspace_id):
            if agent.status != "active":
                continue
            versions = await self.agents.list_versions(actor=owner, workspace_id=actor.workspace_id, agent_id=agent.id)
            agents.extend(
                {
                    "agent_id": str(agent.id),
                    "version": version.version,
                    "label": f"{agent.display_name} · {version.version}",
                }
                for version in versions
            )
        sources = {
            item["id"]: item
            for item in await self.context.store.list(actor.workspace_id, "source")
            if item.get("enabled")
        }
        bindings = await self.context.store.list(actor.workspace_id, "binding")
        grants = await self.context.store.list(actor.workspace_id, "grant")
        routes: list[Json] = []
        for route in await self.context.store.list(actor.workspace_id, "route"):
            source = sources.get(route.get("source_id"))
            if not source:
                continue
            actors = [
                item["open_id"]
                for item in bindings
                if item.get("enabled")
                and item.get("connection_id") == source.get("connection_id")
                and any(
                    grant.get("enabled")
                    and grant.get("source_id") == source["id"]
                    and grant.get("user_id") == item.get("user_id")
                    for grant in grants
                )
            ]
            routes.append(
                {
                    "source_id": source["id"],
                    "route_key": route["route_key"],
                    "label": source["name"],
                    "worker_agent_id": route["agent_id"],
                    "worker_agent_version": route["agent_version"],
                    "actors": actors,
                }
            )
        repositories = await self.repository_intake.options(actor) if self.repository_intake else []
        return {"user_id": str(actor.user_id), "agents": agents, "routes": routes, "repositories": repositories}

    async def read_code_event(
        self,
        workspace_id: UUID,
        scope_id: str,
        ticket_id: str,
        event_id: str,
        offset: int = 0,
    ) -> Json:
        """按已交付引用读取完整评审证据，长正文分段；缓存读取同样复核代码源当前许可。"""
        if not self.delivery or not self.code_events:
            raise CoordinationError("CODE_SOURCE_NOT_CONFIGURED")
        state = await self.authorized_state(workspace_id, scope_id)
        ticket = records(state).tickets.get(ticket_id)
        body = state.checkpoints.get(f"code_event:{ticket_id}:{event_id}")
        if not ticket or not isinstance(body, dict) or not 0 <= offset <= 65536:
            raise CoordinationError("EVIDENCE_NOT_DELIVERED")
        origin = records(state).triggers.get(ticket.origin_id)
        if not origin or not origin.authorized:
            raise CoordinationError("ACCESS_REVOKED")
        actor = RunActor(UUID(ticket.mandate.actor_id), workspace_id)
        references = await self.delivery.review_references(actor, UUID(str(body["run_id"])))
        reference = next((item for item in references if str(item.record_id) == body["review_id"]), None)
        if not reference:
            raise CoordinationError("CODE_SOURCE_REVIEW_MISMATCH")
        await self.code_events.authorize(actor, reference)
        refreshed = await self.authorized_state(workspace_id, scope_id)
        current = refreshed.checkpoints.get(f"code_event:{ticket_id}:{event_id}")
        if not isinstance(current, dict):
            raise CoordinationError("ACCESS_REVOKED")
        text = str(current["text"])
        return {
            "ok": True,
            "event_id": event_id,
            "kind": current["kind"],
            "thread": current["thread"],
            "text": text[offset : offset + 4000],
            "chars": len(text),
            "next_offset": offset + 4000 if len(text) > offset + 4000 else None,
        }

    async def pending_effects(self, workspace_id: UUID, scope_id: str) -> list[Effect]:
        """推进状态对账和到期评估，持久化补偿任务，再返回可派发效果；不调用模型或渠道。"""
        await self.reconcile(workspace_id, scope_id)

        def prepare(engine: Engine, state: State) -> None:
            # 领域规则决定何时评估；运输层不需要了解游标、暂停或代次格式。
            engine.pass_due()
            engine.relay_retried_results(self.console_url)
            engine.advance_console_inputs()
            checkpoints = {name: value for name, value in state.checkpoints.items() if name.startswith("poll:")}
            now = engine.now()
            compensation_due = not checkpoints or any(
                isinstance(value, dict) and str(value.get("next_at", "")) <= now for value in checkpoints.values()
            )
            if state.config.enabled and compensation_due:
                Effects(state).enqueue(
                    "poll", state.config.id, {"checkpoint": key(json.dumps(checkpoints, sort_keys=True))}
                )
            if (
                self.delivery
                and self.code_events
                and state.tickets
                and state.config.enabled
                and str(state.checkpoints.get("code_poll:next_at", "")) <= now
            ):
                Effects(state).enqueue("code_poll", state.config.id, {"round": now[:16]})

        await self.store.transact(workspace_id, scope_id, prepare)
        state = await self.store.snapshot(workspace_id, scope_id)
        if state.config.paused or not state.config.enabled:
            return []

        now = datetime.now(UTC).isoformat()
        due = [effect for effect in state.effects if effect.is_dispatchable(now)]
        return sorted(due, key=lambda item: (-item.priority, item.queued_at, item.id))

    async def dispatchable(self, workspace_id: UUID, run_id: UUID, origin: Json) -> bool:
        """跨模块提交后的绑定窗口：先完成协调记录回写，再允许原生 Run 被执行进程领取。"""
        state = await self.store.snapshot(workspace_id, str(origin["coordination_scope_id"]))
        ticket = records(state).tickets.get(str(origin.get("coordination_ticket_id", "")))
        return bool(
            state.config.enabled
            and not state.config.paused
            and ticket
            and not ticket.ended_at
            and ticket.ended_by != "human"
            and ticket.assignments
            and not ticket.assignments[-1].ended_at
            and ticket.assignments[-1].id == origin.get("coordination_assignment_id")
            and ticket.assignments[-1].run_id == str(run_id)
        )

    async def historical_task_title(self, task: TaskRecord) -> str | None:
        """为已通过正文鉴权的历史运行补读接单标题，并核对原任务和发起人。"""
        origin = task.origin_metadata or {}
        scope_id, ticket_id = origin.get("coordination_scope_id"), origin.get("coordination_ticket_id")
        if not isinstance(scope_id, str) or not isinstance(ticket_id, str):
            return None
        try:
            state = await self.store.snapshot(task.workspace_id, scope_id)
        except CoordinationError:
            return None
        ticket = records(state).tickets.get(ticket_id)
        if ticket and ticket.task_id == str(task.id) and ticket.mandate.actor_id == str(task.created_by):
            return ticket.title
        return None

    async def channel_deliveries(self, scope: ContextRunScope) -> list[Json]:
        """沿用原 Run 的私有访问检查，向现有渠道面板投影主会话 Outbox 回执。"""
        detail = await self.runs.access_metadata(RunActor(scope.actor.user_id, scope.actor.workspace_id), scope.run_id)
        if not detail:
            return []
        origin = detail.task.origin_metadata
        if not origin or not origin.get("coordination_scope_id"):
            return []
        state = await self.authorized_state(scope.actor.workspace_id, str(origin["coordination_scope_id"]))
        ticket = records(state).tickets.get(str(origin.get("coordination_ticket_id", "")))
        if not ticket or ticket.mandate.actor_id != str(scope.actor.user_id) or ticket.task_id != str(detail.task.id):
            return []
        result_id = key("result", ticket.id, str(origin.get("coordination_assignment_id", "")))
        statuses = {
            "pending": "PENDING",
            "unknown": "RECOVERY_REQUIRED",
            "sent": "SENT",
            "expired": "EXPIRED",
            "withdrawn": "WITHDRAWN",
        }
        return [
            {
                "id": item.id,
                "status": statuses[item.state],
                "attempts": next(
                    (
                        effect.attempts
                        for effect in state.effects
                        if effect.kind == "reply" and effect.data.get("reply_id") == item.id
                    ),
                    0,
                ),
                "remote_message_id": item.remote_message_id,
                "business_key": f"coordination:{item.purpose}:{item.id}",
            }
            for item in state.replies
            if item.ticket_id == ticket.id and (item.purpose != "result" or item.id == result_id)
        ]

    async def save(self, actor: ContextActor, config: Config, *, expected_revision: int) -> Config:
        """管理员发布协调配置：验证路由、已发布 Agent 和参与者授权，再以 revision CAS 保存。

        项目及仓库范围从已有冻结路由派生，不能由此接口扩权；配置与策略变更通知同事务提交。
        """
        # 管理身份、来源及固定版本先验证；资产查询不占用协调状态的行锁。
        await self.context.authorize_actor(actor, manage=True)
        if str(actor.workspace_id) != config.workspace_id or str(actor.user_id) != config.created_by:
            raise CoordinationError("WORKSPACE_MISMATCH")
        if config.source_kind == "code_hosting":
            if self.repository_intake is None:
                raise CoordinationError("CODE_SOURCE_NOT_CONFIGURED")
            previous = await self.store.snapshot(actor.workspace_id, config.id) if expected_revision else None
            if previous and previous.config.repository_source and config.repository_source:
                config = replace(
                    config,
                    repository_source=replace(
                        config.repository_source, started_at=previous.config.repository_source.started_at
                    ),
                )
            config = await self.repository_intake.validate_config(actor, config)
            await self._validate_agents(config)
            return await self._save_config(actor, config, expected_revision)
        source = await self.context.store.get(actor.workspace_id, "source", config.source_id)
        route = await self.context.store.get(actor.workspace_id, "route", config.route_key or "")
        if (
            not source
            or source.get("enabled") is not True
            or not route
            or route.get("source_id") != config.source_id
            or str(route.get("agent_id")) != config.worker_agent_id
            or route.get("agent_version") != config.worker_agent_version
        ):
            raise CoordinationError("ROUTE_MISMATCH")
        await self._validate_agents(config)
        # 仓库始终来自已明确批准向群分享的路由，模型和本接口都不能凭空扩大仓库集合。
        config = replace(
            config,
            project_id=cast(str | None, route.get("project_id")),
            repository_ids=tuple(cast(list[str], route.get("repository_ids", []))),
            project_grant_id=cast(str | None, route.get("project_grant_id")),
            revision=expected_revision + 1,
        )
        for open_id in config.allowed_actors:
            binding = await self.context.store.get(
                actor.workspace_id, "binding", f"{source['connection_id']}:{open_id}"
            )
            if not binding or binding.get("enabled") is not True:
                raise CoordinationError("ACTOR_UNBOUND")
            await self.context.authorize_channel(
                ContextActor(UUID(str(binding["user_id"])), actor.workspace_id), UUID(config.source_id), open_id
            )
        return await self._save_config(actor, config, expected_revision)

    async def _validate_agents(self, config: Config) -> None:
        """两类来源共用发布资产与规则校验，来源不能把工作副本当成已发布 Agent。"""
        for agent_id, version in (
            (config.main_agent_id, config.main_agent_version),
            (config.worker_agent_id, config.worker_agent_version),
        ):
            try:
                await self.agents.get_version(
                    actor=AgentActor(UUID(config.created_by), UUID(config.workspace_id), "member"),
                    workspace_id=UUID(config.workspace_id),
                    agent_id=UUID(agent_id),
                    version=version,
                )
            except AgentRegistryError:
                raise CoordinationError("AGENT_VERSION_NOT_FOUND") from None
        if len({item.id for item in config.rules}) != len(config.rules):
            raise CoordinationError("RULE_IDS_DUPLICATED")

    async def _save_config(self, actor: ContextActor, config: Config, expected_revision: int) -> Config:
        """所有配置以同一个 revision CAS 提交；不把来源适配差异带入协调事务。"""
        config = replace(config, revision=expected_revision + 1)

        def save(engine: Engine, state: State) -> Config:
            # 新行先放入 revision=0；并发修改用 PostgreSQL 行锁与 revision 共同保护。
            if state.config.revision != expected_revision:
                raise CoordinationError("CONFIG_REVISION_CONFLICT")
            if config.source_kind == "code_hosting" and state.config.project_grant_id != config.project_grant_id:
                state.checkpoints.pop("repository:cursor", None)
                state.checkpoints.pop("poll:repository", None)
                if config.repository_source:
                    # 换身份/连接只从本次授权开始观察，不借旧游标补读新来源的存量。
                    current_config = replace(
                        config, repository_source=replace(config.repository_source, started_at=engine.now())
                    )
                else:
                    current_config = config
            else:
                current_config = config
            state.config = current_config
            engine.config = current_config
            engine.record_notice("policy.changed", str(config.revision), {"revision": config.revision})
            return current_config

        return await self.store.transact(
            actor.workspace_id, config.id, save, create=replace(config, revision=0) if expected_revision == 0 else None
        )

    async def candidate_admission(
        self, workspace_id: UUID, connection_id: UUID, chat_id: str, thread_id: str | None, root_id: str | None = None
    ) -> bool:
        """决定是否保留普通消息；只通过来源配置或已有线程订阅准入，不代表模型接单。"""
        config = await self._matching(workspace_id, connection_id, chat_id, thread_id)
        if not config or not config.enabled:
            return False
        if config.mode != "off" and config.observe_unmentioned:
            return True
        # off 关闭推断接单，不能顺带丢弃已参与任务的人类跟进。
        state = await self.store.snapshot(workspace_id, config.id)
        return any(
            item.subscriptions
            and (
                item.id == f"{chat_id}:{root_id}"
                or (
                    thread_id
                    and any(
                        trigger.origin.get("thread_id") == thread_id and item.id in trigger.threads
                        for trigger in state.triggers
                    )
                )
            )
            for item in state.threads
        )

    async def _matching(
        self, workspace_id: UUID, connection_id: UUID, chat_id: str, thread_id: str | None
    ) -> Config | None:
        return await self.store.config_for_routes(
            workspace_id, (f"{connection_id}:{chat_id}:{thread_id or '-'}", f"{connection_id}:{chat_id}:-")
        )

    async def ingest(self, actor: ContextActor, payload: Json) -> Json | None:
        """由已完成身份绑定/来源校验/冻结路由的 Inbox 调用；不在接收 ACK 内调用模型。"""
        config = await self._matching(
            actor.workspace_id,
            UUID(str(payload["connection_id"])),
            str(payload["chat_id"]),
            cast(str | None, payload.get("thread_id")),
        )
        if config is None or not config.enabled:
            return None
        await self.context.authorize_channel(actor, UUID(config.source_id), str(payload["open_id"]))
        if payload.get("source_id") != config.source_id or payload.get("route_key") != config.route_key:
            raise CoordinationError("ROUTE_MISMATCH")
        origin = {
            name: payload.get(name)
            for name in (
                "source_id",
                "connection_id",
                "chat_id",
                "thread_id",
                "message_id",
                "open_id",
                "created_at",
                "ingress",
                "quote_id",
                "root_id",
                "include_nearby",
                "route_key",
                "project_grant_id",
            )
            if payload.get(name) is not None
        }
        origin.update(inbox_id=payload["id"], coordination_scope_id=config.id)
        trigger = Trigger(
            id=str(payload["message_id"]),
            workspace_id=str(actor.workspace_id),
            scope_id=config.id,
            actor_id=str(actor.user_id),
            actor_open_id=str(payload["open_id"]),
            human=True,
            addressed=payload.get("addressed", True) is True,
            threads=[f"{payload['chat_id']}:{payload.get('root_id') or payload['message_id']}"],
            at=str(payload["created_at"]),
            text=str(payload["instruction"]),
            origin=origin,
            expires_at=str(payload["expires_at"]) if payload.get("expires_at") else None,
        )

        def receive(engine: Engine, state: State) -> bool:
            topic = trigger.origin.get("thread_id")
            if topic:
                topic_key = f"topic:{topic}"
                canonical = str(state.checkpoints.setdefault(topic_key, trigger.threads[0]))
                # 上游 topicEvent 同样补 root；Weora 还关联实时/历史返回形态不同的同一话题。
                trigger.threads = list(
                    dict.fromkeys([canonical, *trigger.threads, f"{payload['chat_id']}:topic:{topic}"])
                )
            return engine.receive(trigger)

        await self.store.transact(actor.workspace_id, config.id, receive)
        return {"coordination_scope_id": config.id, "trigger_id": trigger.id}

    async def _authorize_trigger(self, workspace_id: UUID, config: Config, trigger: Trigger) -> None:
        if self._expired(trigger):
            raise CoordinationError("CONTEXT_EXPIRED")
        if config.source_kind == "code_hosting":
            if self.repository_intake is None:
                raise CoordinationError("CODE_SOURCE_NOT_CONFIGURED")
            await self.repository_intake.authorize(workspace_id, config, trigger)
            return
        await self.context.authorize_channel(
            ContextActor(UUID(trigger.actor_id), workspace_id), UUID(config.source_id), trigger.actor_open_id
        )
        route = await self.context.store.get(workspace_id, "route", config.route_key or "")
        if (
            not route
            or route.get("source_id") != config.source_id
            or str(route.get("agent_id")) != config.worker_agent_id
            or route.get("agent_version") != config.worker_agent_version
            or route.get("project_grant_id") != config.project_grant_id
            or trigger.origin.get("project_grant_id") != config.project_grant_id
        ):
            raise CoordinationError("ROUTE_REVOKED")

    async def authorized_state(self, workspace_id: UUID, scope_id: str) -> State:
        """读取可供模型使用的状态，复核作者授权和 TTL，并清理失效来源的全部派生正文。

        授权查询在事务外执行；事务内清理会话、输入、报告与未发回复，保留最小审计和幂等标识。
        """
        # 相同作者只复核一次来源权限，但每条消息各自判断其保留期限。
        state = await self.store.snapshot(workspace_id, scope_id)
        revoked: set[str] = set()
        permissions: dict[tuple[str, ...], bool] = {}
        for trigger in state.triggers:
            if not trigger.authorized:
                continue
            if self._expired(trigger):
                revoked.add(trigger.id)
                continue
            actor_key = (
                trigger.actor_id,
                trigger.actor_open_id,
                str(trigger.origin.get("source_id", "")),
                str(trigger.origin.get("project_grant_id", "")),
                str(trigger.origin.get("route_key", "")),
            )
            if actor_key not in permissions:
                try:
                    await self._authorize_trigger(workspace_id, state.config, trigger)
                    permissions[actor_key] = True
                except (ContextError, CoordinationError):
                    permissions[actor_key] = False
            if not permissions[actor_key]:
                revoked.add(trigger.id)
        if revoked:

            def revoke(_: Engine, current: State) -> None:
                invalid_mandates = {
                    ticket.id
                    for ticket in current.tickets
                    if ticket.origin_id in revoked
                    or any(item.trigger_id in revoked and not item.peer_evidence for item in ticket.inputs)
                }
                affected = invalid_mandates | {
                    ticket.id for ticket in current.tickets if any(item.trigger_id in revoked for item in ticket.inputs)
                }
                for trigger in current.triggers:
                    if trigger.id in revoked:
                        trigger.authorized, trigger.text = False, ""
                        trigger.origin.pop("root_text", None)
                        trigger.handbacks = {identity: "来源已失效" for identity in trigger.handbacks}
                # 会话是多个消息的派生副本；任何来源失效都放弃历史，保留业务状态与幂等标识。
                for session in current.sessions:
                    session.messages = []
                    session.pending_messages = []
                    session.model_retry = None
                for identity in list(current.checkpoints):
                    if identity.startswith("code_event:"):
                        current.checkpoints.pop(identity)
                # 后台结果与子会话历史同属派生正文；撤权后不能从状态页或模型工具读取旧副本。
                revoked_runs = {
                    assignment.run_id
                    for ticket in current.tickets
                    if ticket.id in affected
                    for assignment in ticket.assignments
                    if assignment.run_id
                }
                for activity in current.activities:
                    if activity.run_id in revoked_runs:
                        activity.result = {}
                        if activity.state == "running":
                            activity.state = "unknown"

                # 旧 Run 可能已被同安排的下一次尝试替换；孤立报告不再参与对账，也必须清除正文。
                referenced_runs = {
                    assignment.run_id
                    for ticket in current.tickets
                    for assignment in ticket.assignments
                    if assignment.run_id
                }
                for identity in list(current.checkpoints):
                    if identity.startswith("report:") and identity.removeprefix("report:") not in referenced_runs:
                        current.checkpoints.pop(identity)

                for ticket in current.tickets:
                    if ticket.id in affected:
                        ticket.attention = ticket.attention_id = ticket.attention_trigger_id = None
                        # 撤回补充人的证据不撤销原发起人的任务授权。
                        if ticket.id in invalid_mandates:
                            ticket.title = "来源已失效"
                            ticket.mandate = replace(ticket.mandate, scope="来源已失效")
                        for item in ticket.inputs:
                            item.text = None
                        for assignment in ticket.assignments:
                            if assignment.run_id:
                                current.checkpoints.pop(f"report:{assignment.run_id}", None)
                            for identity, value in current.checkpoints.items():
                                if identity.startswith(f"launch:{assignment.id}:") and isinstance(value, dict):
                                    value["instruction"] = ""
                                    if isinstance(value.get("origin"), dict):
                                        cast(Json, value["origin"]).pop("root_text", None)
                            if assignment.report:
                                assignment.report.summary = "来源已失效"
                                assignment.report.human_action = None
                for decision in current.decisions:
                    if any(identity in revoked for identity in decision.trigger_ids):
                        decision.audit, decision.reason = {}, "source_revoked"
                for reply in current.replies:
                    if reply.trigger_id in revoked or reply.ticket_id in affected:
                        reply.text = ""
                        if reply.state == "pending":
                            reply.state = "expired"

            await self.store.transact(workspace_id, scope_id, revoke)
            for identity in revoked:
                await self.context.expire_derived_dependency(workspace_id, state.config.source_id, identity)
            state = await self.store.snapshot(workspace_id, scope_id)
        return state

    def _expired(self, trigger: Trigger) -> bool:
        # 保留期继承原 Inbox，不因为模型晚启动或重新读会话延长，也不把消息年龄误当缓存年龄。
        deadline = (
            timestamp(trigger.expires_at)
            if trigger.expires_at
            else (timestamp(trigger.at) + timedelta(days=self.context.retention_days))
        )
        return deadline <= datetime.now(UTC)

    async def tool(
        self,
        workspace_id: UUID,
        scope_id: str,
        call_id: str,
        name: str,
        arguments: Json,
        *,
        lease: Effect | None = None,
    ) -> Json:
        """执行主会话受限工具：校验契约与当前权限，事务内复核租约后修改协调状态。

        证据读取在事务外进行并再次验证评估版本；批量路由逐条提交，单条失败不吞掉其他结果。
        """
        spec = next((item for item in MAIN_TOOLS if item.name == name), None)
        if not spec or not Draft202012Validator(spec.input_schema).is_valid(arguments):
            return {"ok": False, "code": "INVALID_TOOL_PARAMETERS"}
        state = await self.authorized_state(workspace_id, scope_id)
        if state.config.paused or not state.config.enabled:
            return {"ok": False, "code": "PROCESSING_PAUSED"}

        async def commit[T](action: Callable[[Engine, State], T]) -> T:
            def fenced(engine: Engine, current: State) -> T:
                if lease:
                    current.require_effect_lease(lease, engine.now())
                if current.config.paused or not current.config.enabled:
                    raise CoordinationError("PROCESSING_PAUSED")
                return action(engine, current)

            return await self.store.transact(workspace_id, scope_id, fenced)

        try:
            if name == "orchestrator_code_read":
                await commit(lambda engine, _: None)
                value = await self.read_code_event(
                    workspace_id,
                    scope_id,
                    str(arguments["ticket_id"]),
                    str(arguments["event_id"]),
                    int(str(arguments.get("offset", 0))),
                )
                await commit(lambda engine, _: None)
                return value
            if name == "orchestrator_eval_read":
                eval_id = str(arguments["eval_id"])
                evaluation = await commit(lambda engine, _: engine.pending_evaluation(eval_id))
                trigger = next(item for item in state.triggers if item.id == evaluation.primary)
                await self._authorize_trigger(workspace_id, state.config, trigger)

                def reserve_read(engine: Engine, current: State) -> None:
                    engine.pending_evaluation(eval_id)
                    budget_key = f"eval_budget:{eval_id}"
                    count = int(str(current.checkpoints.get(budget_key, 0)))
                    if count >= 12:
                        raise CoordinationError("EVAL_READ_BUDGET_EXCEEDED")
                    current.checkpoints[budget_key] = count + 1

                await commit(reserve_read)
                context, evidence_state = await self._read_evidence(workspace_id, state, evaluation, trigger, arguments)

                def finish_read(engine: Engine, current: State) -> Evaluation:
                    value = engine.read_eval(eval_id)
                    for identity, data in evidence_state.items():
                        if (
                            identity.startswith(("eval_revision:", "eval_repo_version:"))
                            and identity in current.checkpoints
                            and current.checkpoints[identity] != data
                        ):
                            raise CoordinationError("EVAL_REVISION_CHANGED")
                        current.checkpoints[identity] = data
                    return value

                evaluation = await commit(finish_read)
                return {
                    "ok": True,
                    "evaluation": asdict(evaluation),
                    "rules": [asdict(rule) for rule in state.config.rules],
                    "trigger": asdict(trigger),
                    "context": context,
                }
            if name == "orchestrator_route":
                results: list[Json] = []
                for item in cast(list[Json], arguments["decisions"]):
                    # 对照上游 FIELDS：单项参数不合法不能阻止同批其他请求落地。
                    if not Draft202012Validator(ROUTE).is_valid(item):
                        results.append({"ok": False, "code": "INVALID_TOOL_PARAMETERS"})
                        continue
                    item = dict(item)
                    if item.get("decision") in {"accept", "attach"}:
                        item.setdefault("reason", "显式人类请求" if item["decision"] == "accept" else "跟进已有任务")
                    try:
                        decision = await commit(
                            lambda engine, _: engine.route(str(item["trigger_id"]), item),  # noqa: B023 - 本轮 await 内执行
                        )
                        results.append({"ok": True, "decision": asdict(decision)})
                    except CoordinationError as error:
                        results.append({"ok": False, "code": error.code})
                    except Exception:
                        # 各项事务独立回滚；不把数据库原文或其他消息内容暴露给模型。
                        results.append({"ok": False, "code": "ROUTE_ITEM_FAILED"})
                return {"ok": True, "results": results}

            if name == "orchestrator_reply" and arguments.get("artifact_ids"):
                ticket = records(state).tickets.get(str(arguments.get("ticket_id", "")))
                if not self.delivery or not ticket or not ticket.assignments or not ticket.assignments[-1].run_id:
                    raise CoordinationError("SCREENSHOT_UNAVAILABLE")
                await self.delivery.reply_screenshots(
                    RunActor(UUID(ticket.mandate.actor_id), workspace_id),
                    UUID(ticket.assignments[-1].run_id),
                    artifact_ids=tuple(cast(list[str], arguments["artifact_ids"])),
                )

            def invoke(engine: Engine, current: State) -> Json:
                identity = str(arguments.get("ticket_id", ""))
                if name == "orchestrator_eval":
                    return {"ok": True, "evaluations": [asdict(item) for item in engine.pass_due()]}
                if name == "orchestrator_read":
                    return self._read(current, arguments)
                if name == "orchestrator_eval_decide":
                    return {"ok": True, "decision": asdict(engine.decide(str(arguments["eval_id"]), arguments))}
                if name == "orchestrator_ticket_note":
                    engine.note(identity, str(arguments["text"]), call_id)
                elif name == "orchestrator_ticket_retry":
                    engine.retry(identity, str(arguments["trigger_id"]))
                elif name == "orchestrator_ticket_request_status":
                    engine.status(identity)
                elif name == "orchestrator_thread_unsubscribe":
                    removed = engine.unsubscribe_thread(
                        str(arguments["thread"]), str(arguments["trigger_id"]), identity or None
                    )
                    return {"ok": True, "removed": removed}
                elif name == "orchestrator_reply":
                    return {
                        "ok": True,
                        "reply": asdict(
                            engine.reply(
                                str(arguments["trigger_id"]),
                                str(arguments["purpose"]),
                                str(arguments["text"]),
                                ticket_id=identity or None,
                                artifact_ids=tuple(cast(list[str], arguments.get("artifact_ids", []))),
                            )
                        ),
                    }
                elif name == "orchestrator_trigger_requeue":
                    engine.requeue_trigger(str(arguments["trigger_id"]))
                return {"ok": True}

            return await commit(invoke)
        except (CoordinationError, ContextError, ProjectError, DeliveryError) as error:
            return {"ok": False, "code": error.code}

    async def _read_evidence(
        self, workspace_id: UUID, state: State, evaluation: Evaluation, trigger: Trigger, arguments: Json
    ) -> tuple[Json, Json]:
        """对应 assessment.ts：只解引用本次已交付的证据，正文分段，仓库固定提交。"""
        resource = str(arguments.get("resource", "thread"))
        offset = int(str(arguments.get("offset", 0)))
        actor = ContextActor(UUID(trigger.actor_id), workspace_id)
        source_id = UUID(state.config.source_id)
        # 初始评估快照包含调度前已到达的邻居，后续分页固定同一截止，不读取未来消息。
        cutoff = max(timestamp(trigger.at), timestamp(evaluation.started_at)) + timedelta(milliseconds=1)
        evidence_state: Json = {}
        evidence: Json
        if state.config.source_kind == "code_hosting" and resource not in {"trigger", "file", "directory"}:
            if resource not in {"thread", "root", "message", "parent"}:
                raise CoordinationError("EVIDENCE_NOT_DELIVERED")
            evidence = RepositoryIntake.evidence(state, evaluation.id, trigger)
            if resource == "root":
                root = str(evidence["root"])
                evidence = {
                    "text": root[offset : offset + 4000],
                    "chars": len(root),
                    "next_offset": offset + 4000 if len(root) > offset + 4000 else None,
                }
            return evidence, evidence_state
        if resource == "trigger":
            identity = str(arguments.get("id", evaluation.primary))
            item = records(state).triggers.get(identity)
            if identity not in evaluation.trigger_ids or not item or not item.authorized:
                raise CoordinationError("EVIDENCE_NOT_DELIVERED")
            evidence = {"id": identity, "text": item.text, "chars": len(item.text)}
        elif resource in {"root", "parent", "message", "image"}:
            known = cast(list[str], state.checkpoints.get(f"eval_messages:{evaluation.id}", []))
            pointer = trigger.origin.get(
                "root_id" if resource == "root" else "quote_id" if resource == "parent" else "message_id"
            )
            identity = str(arguments.get("id", pointer or ""))
            allowed = (
                set(known)
                | set(evaluation.trigger_ids)
                | {str(trigger.origin.get("root_id", "")), str(trigger.origin.get("quote_id", ""))}
            )
            if not identity or identity not in allowed:
                raise CoordinationError("EVIDENCE_NOT_DELIVERED")
            message = await self.context.channel_message(
                actor, source_id, trigger.actor_open_id, message_id=identity, cutoff=cutoff
            )
            if resource == "image":
                raise CoordinationError("MODEL_VISION_UNAVAILABLE")
            evidence = {
                "id": identity,
                "text": message.text,
                "chars": len(message.text),
                "resources": list(message.resources),
                "parse_status": message.parse_status,
            }
        elif resource in {"file", "directory"}:
            repository_id = str(arguments.get("repository_id", ""))
            if not self.projects or not state.config.project_id or repository_id not in state.config.repository_ids:
                raise CoordinationError("REPOSITORY_NOT_SELECTED")
            revision_key = f"eval_revision:{evaluation.id}:{repository_id}"
            version_key = f"eval_repo_version:{evaluation.id}:{repository_id}"
            try:
                evidence = await self.projects.assessment_read(
                    ProjectActor(actor.user_id, workspace_id),
                    UUID(state.config.project_id),
                    tuple(UUID(value) for value in state.config.repository_ids),
                    UUID(repository_id),
                    str(arguments.get("path", "")),
                    revision=cast(str | None, state.checkpoints.get(revision_key)),
                    connection_version=cast(int | None, state.checkpoints.get(version_key)),
                    directory=resource == "directory",
                )
            except ProjectError as error:
                raise CoordinationError(error.code) from None
            evidence_state[revision_key] = evidence["revision"]
            evidence_state[version_key] = evidence["connection_version"]
        else:
            evidence = await self.context.channel_messages(
                actor,
                source_id,
                trigger.actor_open_id,
                cutoff=cutoff,
                thread_id=cast(str | None, trigger.origin.get("thread_id")),
                cursor=cast(str | None, arguments.get("cursor")),
                request_key=evaluation.id,
                anchor_id=str(trigger.origin.get("message_id", trigger.id)),
            )
            identity = f"eval_messages:{evaluation.id}"
            known = cast(list[str], state.checkpoints.get(identity, []))
            evidence_state[identity] = list(
                dict.fromkeys(
                    known
                    + [
                        str(item["id"])
                        for item in cast(list[Json], evidence["items"]) + cast(list[Json], evidence.get("stubs", []))
                    ]
                )
            )
        for text_key in ("text", "content"):
            content = evidence.get(text_key)
            if isinstance(content, str):
                evidence.update(
                    {
                        text_key: content[offset : offset + 4000],
                        "chars": len(content),
                        "next_offset": offset + 4000 if len(content) > offset + 4000 else None,
                    }
                )
        return evidence, evidence_state

    @staticmethod
    def _read(state: State, arguments: Json) -> Json:
        resource, identity = arguments["resource"], arguments.get("id")
        offset = int(str(arguments.get("offset", 0)))
        if resource == "ticket":
            ticket = records(state).tickets.get(str(identity))
            origin = records(state).triggers.get(ticket.origin_id) if ticket else None
            if not ticket or not origin or not origin.authorized:
                raise CoordinationError("TICKET_NOT_FOUND")
            return {"ok": True, "ticket": asdict(ticket)}
        if resource == "trigger":
            trigger = records(state).triggers.get(str(identity))
            if not trigger or not trigger.authorized:
                raise CoordinationError("TRIGGER_NOT_FOUND")
            return {"ok": True, "trigger": asdict(trigger)}
        if resource == "notices":
            return {
                "ok": True,
                "notices": [asdict(item) for item in state.notices[offset : offset + 50]],
                "next_offset": offset + 50 if len(state.notices) > offset + 50 else None,
            }
        if resource in {"threads", "decisions", "activities", "effects"}:
            collection = {
                "threads": [asdict(item) for item in state.threads],
                "decisions": [asdict(item) for item in state.decisions],
                "activities": [asdict(item) for item in state.activities],
                "effects": [asdict(item) for item in state.effects],
            }[str(resource)]
            return {
                "ok": True,
                "items": collection[offset : offset + 50],
                "next_offset": offset + 50 if len(collection) > offset + 50 else None,
            }
        if resource in {"sessions", "health"}:
            return {
                "ok": True,
                "next_offset": offset + 50 if len(state.sessions) > offset + 50 else None,
                "sessions": [
                    {
                        "id": item.id,
                        "role": item.role,
                        "model_id": item.model_id,
                        "model_retry": item.model_retry,
                        "generation": item.generation,
                    }
                    for item in state.sessions[offset : offset + 50]
                ],
            }
        return {
            "ok": True,
            "mode": state.config.mode,
            "paused": state.config.paused,
            "activities": [
                {"id": item.id, "run_id": item.run_id, "kind": item.kind, "state": item.state}
                for item in state.activities[-50:]
            ],
            "sessions": [
                {"id": item.id, "role": item.role, "model_id": item.model_id, "model_retry": item.model_retry}
                for item in state.sessions
            ],
            "evaluations": [asdict(item) for item in state.evaluations if item.state == "pending"],
            "tickets": [
                {
                    "id": item.id,
                    "title": item.title,
                    "outcome": item.outcome,
                    "attention": item.attention,
                    "assignment": asdict(item.assignments[-1]) if item.assignments else None,
                }
                for item in state.tickets[-50:]
                if (origin := records(state).triggers.get(item.origin_id)) and origin.authorized
            ],
            "notices": [asdict(item) for item in state.notices[-50:] if not item.quiet],
        }

    async def main(
        self,
        workspace_id: UUID,
        scope_id: str,
        effect: Effect,
        runtime_factory: Callable[[RuntimeSession, Callable[[str, str, Json], Awaitable[Json]]], TextRuntime],
        profile: ExecutionProfile,
    ) -> None:
        """在平台租约内恢复主会话；资产与会话生命周期由独立应用组件处理。"""
        await MainSessionRunner(self).run(workspace_id, scope_id, effect, runtime_factory, profile)

    async def launch(
        self, workspace_id: UUID, scope_id: str, effect: Effect, *, runtime_available: bool = True,
    ) -> None:
        """复核来源后派发 Worker；跨模块幂等与租约绑定由 WorkerDispatcher 统一管理。"""
        state = await self.authorized_state(workspace_id, scope_id)
        if state.config.source_kind == "code_hosting":
            await self.store.transact(workspace_id, scope_id, RepositoryIntake.apply_reviews)
            state = await self.authorized_state(workspace_id, scope_id)
        await self.worker_dispatch.launch(
            workspace_id,
            scope_id,
            state,
            effect,
            runtime_available=runtime_available,
            external_link=self.link_native_inputs,
        )

    async def link_native_inputs(self, actor: ContextActor, origin: Json, run_id: UUID) -> None:
        """新 Run 依赖整段已交付历史；每个正文期限都登记，不能只绑定最初一条消息。"""
        scope_id = str(origin["coordination_scope_id"])
        state = await self.store.snapshot(actor.workspace_id, scope_id)
        ticket = records(state).tickets.get(str(origin["coordination_ticket_id"]))
        detail = await self.runs.access_metadata(RunActor(actor.user_id, actor.workspace_id), run_id)
        metadata = detail.task.origin_metadata if detail else None
        if (
            not ticket
            or ticket.mandate.actor_id != str(actor.user_id)
            or not metadata
            or metadata.get("coordination_scope_id") != scope_id
            or metadata.get("coordination_ticket_id") != ticket.id
        ):
            raise CoordinationError("MANDATE_REVOKED")
        identities = {ticket.origin_id} | {item.trigger_id for item in ticket.inputs if item.trigger_id}
        for identity in identities:
            trigger = records(state).triggers.get(identity)
            if not trigger:
                raise CoordinationError("SOURCE_REVOKED")
            expires_at = trigger.expires_at or later(trigger.at, self.context.retention_days * 86400)
            await self.context.register_derived_run(actor, state.config.source_id, trigger.id, expires_at, run_id)
        current = await self.authorized_state(actor.workspace_id, scope_id)
        for identity in identities:
            source = records(current).triggers.get(identity)
            if not source or not source.authorized:
                raise CoordinationError("SOURCE_REVOKED")

    async def authorize_code_origin(self, actor: RunActor, origin: Json) -> Config:
        config = await self.authorize_runtime_origin(actor, origin)
        if config.source_kind != "code_hosting":
            raise CoordinationError("CODE_SOURCE_ACCESS_REVOKED")
        return config

    async def authorize_runtime_origin(self, actor: RunActor, origin: Json) -> Config:
        """原生 Run 来源闸门：只接受本范围实际保存、属于当前用户的仓库触发，不接受模型自报授权。"""
        state = await self.store.snapshot(actor.workspace_id, str(origin.get("coordination_scope_id", "")))
        trigger = records(state).triggers.get(str(origin.get("message_id", "")))
        if (
            state.config.source_kind != origin.get("source_kind", "feishu")
            or not trigger
            or not trigger.authorized
            or trigger.actor_id != str(actor.user_id)
            or trigger.origin.get("source_id") != origin.get("source_id")
            or trigger.origin.get("project_grant_id") != origin.get("project_grant_id")
        ):
            raise CoordinationError("CODE_SOURCE_ACCESS_REVOKED")
        await self._authorize_trigger(actor.workspace_id, state.config, trigger)
        ticket = records(state).tickets.get(str(origin.get("coordination_ticket_id", "")))
        if not ticket or ticket.mandate.actor_id != str(actor.user_id):
            raise CoordinationError("MANDATE_REVOKED")
        # 历史是所有已交付输入的派生副本；只校验原请求会漏掉已撤权或过期的后续消息。
        for identity in {item.trigger_id for item in ticket.inputs if item.trigger_id}:
            source = records(state).triggers.get(identity)
            if not source or not source.authorized:
                raise CoordinationError("SOURCE_REVOKED")
            await self._authorize_trigger(actor.workspace_id, state.config, source)
        return state.config

    async def reconcile(self, workspace_id: UUID, scope_id: str) -> None:
        """将原生 Run 的终态或等待状态投影为 Worker 报告，成功仍以原生交付验收为准。

        以 Run、状态和事件序号幂等对账；只处理当前未结束安排，不修改其他模块的数据。
        """
        state = await self.authorized_state(workspace_id, scope_id)
        for ticket in state.tickets:
            origin = records(state).triggers.get(ticket.origin_id)
            if not origin or not origin.authorized:
                continue
            if not ticket.assignments or ticket.ended_by == "human":
                continue
            assignment = ticket.assignments[-1]
            if not assignment.run_id or assignment.ended_at:
                continue
            actor = RunActor(UUID(ticket.mandate.actor_id), workspace_id)
            detail = await self.runs.access_metadata(actor, UUID(assignment.run_id))
            # 先绑定已确认的交付，再投影 ready；不能先结束任务、下轮才发现它还在等待评审。
            if self.delivery and self.code_events:
                references = await self.delivery.review_references(actor, UUID(assignment.run_id))
                if references:

                    def bind_reviews(
                        engine: Engine,
                        current: State,
                        identity: str = ticket.id,
                        captured: tuple[ReviewReference, ...] = tuple(references),
                    ) -> None:
                        active = engine.ticket(identity)
                        if active.ended_by == "human":
                            return
                        for reference in captured:
                            thread = "code:" + key(
                                str(workspace_id), reference.scope.repository_id, reference.remote_id
                            )
                            engine.bind_review(identity, thread, str(reference.run_id), str(reference.record_id))

                    await self.store.transact(workspace_id, scope_id, bind_reviews)
            if not detail or detail.run.status in {"QUEUED", "RUNNING"}:
                continue
            report_id = f"{assignment.run_id}:{detail.run.status}:{detail.run.last_sequence}"
            checkpoint_key = f"reconciled:{assignment.run_id}"
            if state.checkpoints.get(checkpoint_key) == report_id:
                continue
            status = detail.run.status
            # 原生输出保存在 Run；协调报告有独立大小上限，长结果不能阻塞终态对账。
            summary = detail.run.output_text or f"运行状态 {status}，请在 Weora Console 处理该运行。"
            if len(summary) > 16000:
                summary = summary[:15800] + f"\n（摘要已截断，完整结果见 Weora Run {assignment.run_id}。）"
            failure_code = detail.run.failure_code or status
            report = Report(
                "waiting" if status == "SUCCEEDED" else "failed" if status in {"FAILED", "CANCELLED"} else "blocked",
                summary,
                datetime.now(UTC).isoformat(),
                human_action=(
                    f"{RUN_FAILURE_REASONS.get(failure_code, '任务执行失败')}（{failure_code}）。"
                    "请在原话题 @机器人回复重试。平台会先核对已执行的操作。"
                    if status == "FAILED" else None if status in {"SUCCEEDED", "CANCELLED"}
                    else f"任务需要人类处理：{status}。运行：{assignment.run_id}"
                ),
                verified=status == "SUCCEEDED",
            )
            proposed = state.checkpoints.get(f"report:{assignment.run_id}")
            if status == "SUCCEEDED" and isinstance(proposed, dict) and proposed.get("state") != "progress":
                report = TypeAdapter(Report).validate_python(dict(proposed, at=report.at, verified=True))
            elif status == "SUCCEEDED" and isinstance(proposed, dict) and proposed.get("human_action"):
                report = TypeAdapter(Report).validate_python(dict(proposed, state="waiting", at=report.at))

            def save_report(
                engine: Engine,
                current: State,
                *,
                revision_key: str = checkpoint_key,
                revision: str = report_id,
                ticket_id: str = ticket.id,
                assignment_id: str = assignment.id,
                expected_run: str | None = assignment.run_id,
                snapshot: Report = report,
            ) -> None:
                # 模型回合成功不是目标完成；只有明确 ready 且原生验收通过才结束 Ticket。
                if current.checkpoints.get(revision_key) != revision:
                    active = engine.ticket(ticket_id)
                    current_assignment = active.assignments[-1]
                    if (
                        active.ended_by == "human" or current_assignment.ended_at
                        or current_assignment.id != assignment_id or current_assignment.run_id != expected_run
                    ):
                        return
                    run_id = current_assignment.run_id
                    session_ids = {engine.ticket(ticket_id).session_id} | {
                        key("subagent-session", str(run_id), item.id)
                        for item in current.activities
                        if item.run_id == run_id and item.kind == "subagent"
                    }
                    for session in current.sessions:
                        if session.id in session_ids:
                            session.model_retry = None
                    for activity in current.activities:
                        if activity.run_id == run_id and activity.state == "running":
                            activity.state = "unknown"
                    engine.report(ticket_id, assignment_id, snapshot)
                    current.checkpoints[revision_key] = revision

            await self.store.transact(
                workspace_id,
                scope_id,
                save_report,
            )

    async def poll_code_events(self, workspace_id: UUID, scope_id: str, effect: Effect | None = None) -> None:
        """逐线程补偿代码事件；来源授权、持久游标、失败隔离由代码源用例处理。"""
        if self.delivery and self.code_events:
            await CodePolling(self.store, self.authorized_state, self.delivery, self.code_events).poll(
                workspace_id, scope_id, effect
            )

    async def deliver(self, workspace_id: UUID, scope_id: str, effect: Effect) -> bool:
        """原子领取待发回复，再调用飞书并保存回执；未知发送不自动重放。

        授权读取之后仍在事务内复核租约、暂停和当前问题，避免旧执行者领取待发消息或旧问题。
        返回 True 只表示本次实际发送并保存了远端回执；并发重复或撤销回复返回 False。
        """
        state = await self.authorized_state(workspace_id, scope_id)
        reply = records(state).replies.get(str(effect.data["reply_id"]))
        if not reply or state.config.paused or not state.config.enabled:
            return False
        if reply.state == "unknown":
            if effect.owner is not None and reply.sending_owner == effect.owner:
                # 同一有效尝试正在发送；重复调用安静退出。不同 owner 看到 unknown 才进入崩溃对账。
                state.require_effect_lease(effect, datetime.now(UTC).isoformat())
                return False
            # 进程可能在真实发送后、保存回执前退出；恢复时通知主会话对账，不能静默结算成完成。
            raise CoordinationError("CHANNEL_RECONCILIATION_REQUIRED")
        if reply.state != "pending":
            return False
        trigger = records(state).triggers.get(reply.trigger_id)
        if not trigger or not trigger.authorized:
            raise CoordinationError("REPLY_ACCESS_REVOKED")
        code_identity = None
        connection: Json = {}
        if state.config.source_kind == "code_hosting":
            if not self.repository_intake:
                raise CoordinationError("CODE_SOURCE_NOT_CONFIGURED")
            code_identity = await self.repository_intake.authorize(workspace_id, state.config, trigger, reply=True)
        else:
            _, connection, _ = await self.context.authorize_channel(
                ContextActor(UUID(trigger.actor_id), workspace_id), UUID(state.config.source_id), trigger.actor_open_id
            )

        image_keys: list[str] = []
        if not code_identity and reply.purpose == "result" and reply.ticket_id and self.delivery:
            ticket = records(state).tickets.get(reply.ticket_id)
            assignment = next(
                (item for item in ticket.assignments if key("result", ticket.id, item.id) == reply.id), None
            ) if ticket else None
            if ticket and assignment and assignment.run_id:
                images = await self.delivery.reply_screenshots(
                    RunActor(UUID(ticket.mandate.actor_id), workspace_id),
                    UUID(assignment.run_id),
                    artifact_ids=tuple(reply.artifact_ids),
                    report=assignment.report.summary if assignment.report else reply.text,
                )
                # 上传图片本身不发布群消息，失败可安全重试；发送消息前才进入 unknown。
                for image in images:
                    content = base64.b64decode(image.body or "")
                    image_keys.append(await self.context.reader.upload_image(connection, content))
                await self.authorized_state(workspace_id, scope_id)
                await self.context.authorize_channel(
                    ContextActor(UUID(trigger.actor_id), workspace_id), UUID(state.config.source_id),
                    trigger.actor_open_id,
                )

        # 发送前先标记 unknown。硬退出时也不能把可能已发送的回复当 pending 重发。
        def sending(_: Engine, current: State) -> str | None:
            if effect.owner is not None:
                current.require_effect_lease(effect, datetime.now(UTC).isoformat())
            if current.config.paused or not current.config.enabled:
                raise DeferredEffect("PROCESSING_PAUSED")
            saved = records(current).replies.get(reply.id)
            if not saved or saved.state != "pending":
                return None
            current_trigger = records(current).triggers.get(reply.trigger_id)
            if not current_trigger or not current_trigger.authorized:
                raise CoordinationError("REPLY_ACCESS_REVOKED")
            ticket = records(current).tickets.get(saved.ticket_id or "")
            if saved.purpose == "attention" and (
                not ticket
                or ticket.attention != saved.text
                or saved.id != key("attention", ticket.attention_id or "")
            ):
                return None
            if (
                current.config.source_kind == "feishu" and saved.purpose == "ack"
                and ticket and ticket.origin_id == saved.trigger_id
            ):
                if ticket.ended_at:
                    saved.state = "withdrawn"
                    records(current).replies.save(saved)
                    return None
                # 已有原生任务号时附上；新任务只有在首回确认后才创建。
                if ticket.task_id:
                    number = f"\n\n任务 #{ticket.task_id[:8].upper()}"
                    saved.text = f"{saved.text[:240 - len(number)]}{number}"
            saved.state = "unknown"
            saved.sending_owner = effect.owner
            return saved.text

        text = await self.store.transact(workspace_id, scope_id, sending)
        if text is None:
            return False
        if code_identity and self.repository_intake:
            remote_id = await self.repository_intake.reply(code_identity, trigger, text, reply.id)
        else:
            try:
                remote_id = await self.context.reader.reply(
                    connection, str(trigger.origin["message_id"]), text,
                    str(uuid5(NAMESPACE_URL, f"{workspace_id}:{scope_id}:{reply.id}")), image_keys=tuple(image_keys),
                )
            except ReplyNotSent:
                def reset(_: Engine, current: State) -> None:
                    if effect.owner is not None:
                        current.require_effect_lease(effect, datetime.now(UTC).isoformat())
                    saved = records(current).replies.get(reply.id)
                    if saved and saved.state == "unknown" and saved.sending_owner == effect.owner:
                        saved.state, saved.sending_owner = "pending", None

                await self.store.transact(workspace_id, scope_id, reset)
                raise

        def sent(_: Engine, current: State) -> None:
            saved = records(current).replies.get(reply.id)
            if saved:
                saved.state, saved.remote_message_id = "sent", remote_id
                ticket = records(current).tickets.get(saved.ticket_id or "")
                if ticket and ticket.assignments and saved.id == key("result", ticket.id, ticket.assignments[-1].id):
                    # 当前结果已获得远端回执，解除此前图片准备失败的告警。
                    ticket.delivery_error = None

        await self.store.transact(workspace_id, scope_id, sent)
        return True

    async def effect_failed(self, workspace_id: UUID, scope_id: str, effect: Effect, *, code: str) -> None:
        """失败也进入管理事实；渠道未知绝不能用 Worker 重试偷偷变成第二次发送。"""

        def failed(engine: Engine, current: State) -> None:
            if effect.kind == "reply":
                reply = records(current).replies.get(str(effect.data["reply_id"]))
                if reply and reply.ticket_id and reply.state in {"pending", "unknown"}:
                    ticket = records(current).tickets.get(reply.ticket_id)
                    if ticket:
                        unknown = reply.state == "unknown"
                        ticket.delivery_error = "DELIVERY_RESULT_UNKNOWN" if unknown else "DELIVERY_PREPARATION_FAILED"
                        engine.record_notice(
                            "ticket.delivery_failed",
                            effect.id,
                            {"ticket_id": ticket.id, "reply_id": reply.id,
                             "code": code, "delivery_state": ticket.delivery_error},
                        )
                        if (
                            current.config.source_kind == "feishu" and reply.purpose == "ack"
                            and reply.trigger_id == ticket.origin_id and not ticket.ended_at
                            and ticket.assignments and not any(item.run_id for item in ticket.assignments)
                            and not ticket.assignments[-1].report
                        ):
                            reason = FEISHU_REPLY_ERRORS.get(code, "飞书未能确认首回发送")
                            next_step = (
                                "请管理员核对原话题是否收到首回。"
                                if unknown else "请管理员检查飞书连接，恢复后可在原话题 @机器人重试。"
                            )
                            prefix = "接单回复发送结果未知" if unknown else "接单回复发送失败"
                            summary = f"{prefix}，Worker 未启动。{reason}（{code}）。{next_step}"
                            engine.report(ticket.id, ticket.assignments[-1].id, Report("failed", summary, engine.now()))
            elif effect.kind == "worker":
                ticket = records(current).tickets.get(str(effect.data["ticket_id"]))
                if (
                    ticket and ticket.assignments and not ticket.ended_by
                    and ticket.assignments[-1].id == effect.data["assignment_id"]
                    and not ticket.assignments[-1].ended_at
                ):
                    reason = RUN_FAILURE_REASONS.get(code, "任务未能启动")
                    message = f"{reason}（{code}）。请在原话题 @机器人回复重试。"
                    engine.report(
                        ticket.id,
                        ticket.assignments[-1].id,
                        Report(
                            "failed", message, engine.now(),
                            human_action=message if current.config.source_kind == "feishu" else None,
                        ),
                    )

        await self.store.transact(workspace_id, scope_id, failed)
