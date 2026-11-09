"""可配置仓库的独立接单入口；对照上游 github.ts 的仓库观察与按作者准入。

代码讨论使用自身来源身份，不伪造飞书收件。创建 Task 仍走原生 Runs，任何代码写入仍需审批。
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from weora.modules.context.domain import ContextActor, Json
from weora.modules.context.service import ContextService
from weora.modules.coordination.domain import Config, CoordinationError, Effect, State, Trigger
from weora.modules.coordination.engine import Engine, key, later
from weora.modules.coordination.hosting_http import HostedReviewIdentity
from weora.modules.coordination.records import records
from weora.modules.coordination.repository_discussions import RepositoryDiscussions
from weora.modules.projects.domain import ProjectActor
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.ports import RunActor

if TYPE_CHECKING:
    from weora.modules.coordination.service import CoordinationService


@dataclass(frozen=True)
class RepositoryMessage:
    id: str
    number: str
    actor: str
    human: bool
    at: str
    text: str
    kind: str
    is_pull: bool
    url: str
    root: str = ""


@dataclass(frozen=True)
class RepositoryReview:
    number: str
    outcome: Literal["open", "closed", "merged"]
    at: str
    closed_at: str | None = None


@dataclass(frozen=True)
class RepositoryPoll:
    messages: tuple[RepositoryMessage, ...]
    cursor: Json
    reviews: tuple[RepositoryReview, ...] = ()


RepositoryIdentity = Callable[[RunActor, Config], Awaitable[tuple[HostedReviewIdentity, str]]]


class RepositoryIntake:
    def __init__(self, context: ContextService, projects: ProjectService, identity: RepositoryIdentity) -> None:
        self.context, self.projects, self.identity = context, projects, identity
        # 延迟导入仅切断 HTTP DTO 的依赖环；不在模块加载时创建有状态客户端。
        from weora.modules.coordination.repository_http import RepositoryEvents

        self.reader = RepositoryEvents()
        self.legacy_options: Callable[[ContextActor], Awaitable[list[Json]]] | None = None
        self.reply_identity: RepositoryIdentity | None = None

    async def options(self, actor: ContextActor) -> list[Json]:
        """仓库候选来自项目目录和部署连接；只列公开配置，提交时仍重新验证 API 身份。"""
        result = await self.projects.workspace_repositories(ProjectActor(actor.user_id, actor.workspace_id))
        choices: list[Json] = []
        for repo in cast(list[Json], result["items"]):
            if (
                repo.get("status") != "active"
                or repo.get("project_status") != "active"
                or not repo.get("code_source_id")
            ):
                continue
            choices.append(
                {
                    "repository_id": repo["id"],
                    "project_id": repo["project_id"],
                    "remote_id": "project-" + str(repo["id"]),
                    "provider": repo["provider"],
                    "label": f"{repo['project_name']} / {repo['name']}",
                }
            )
        if self.legacy_options:
            choices.extend(await self.legacy_options(actor))
        return choices

    async def validate_config(self, actor: ContextActor, config: Config) -> Config:
        """管理员选择已登记连接和成员；冻结连接指纹，修改政策不重置启用边界。"""
        source = config.repository_source
        if not source or not source.actors or not source.agent_login.strip():
            raise CoordinationError("CODE_SOURCE_CONFIG_REQUIRED")
        for account, user_id in source.actors.items():
            if not account or len(account) > 256:
                raise CoordinationError("CODE_SOURCE_ACTOR_INVALID")
            await self.context.authorize_actor(ContextActor(UUID(user_id), actor.workspace_id))
        if not set(config.allowed_actors + config.allowed_bots) <= set(source.actors):
            raise CoordinationError("ACTOR_UNBOUND")
        snapshot = (
            await self.projects.snapshot(
                ProjectActor(actor.user_id, actor.workspace_id), UUID(config.project_id), (UUID(source.repository_id),)
            )
            if config.project_id
            else None
        )
        frozen_source = replace(source, project_snapshot=snapshot)
        candidate = replace(
            config,
            repository_ids=(source.repository_id,) if config.project_id else (),
            repository_source=frozen_source,
        )
        identity, fingerprint = await self.identity(RunActor(actor.user_id, actor.workspace_id), candidate)
        if identity.provider == "github" and len({name.casefold() for name in source.actors}) != len(source.actors):
            raise CoordinationError("CODE_SOURCE_ACTOR_DUPLICATED")
        grant = key("repository-source", source.repository_id, fingerprint, json.dumps(source.actors, sort_keys=True))
        return replace(
            candidate,
            project_grant_id=grant,
            repository_source=replace(
                frozen_source,
                connection_fingerprint=fingerprint,
                started_at=source.started_at or datetime.now(UTC).isoformat(),
            ),
        )

    async def authorize(
        self, workspace_id: UUID, config: Config, trigger: Trigger, *, reply: bool = False
    ) -> HostedReviewIdentity:
        """每次读写复核真实作者、当前成员与连接；正文有效期不能由较晚的 Run 延长。"""
        source = config.repository_source
        if (
            not config.enabled
            or not source
            or source.actors.get(trigger.actor_open_id) != trigger.actor_id
            or trigger.origin.get("source_id") != config.source_id
            or trigger.origin.get("project_grant_id") != config.project_grant_id
            or trigger.expires_at is None
            or datetime.fromisoformat(trigger.expires_at) <= datetime.now(UTC)
        ):
            raise CoordinationError("CODE_SOURCE_ACCESS_REVOKED")
        actor = RunActor(UUID(trigger.actor_id), workspace_id)
        await self.context.authorize_actor(ContextActor(actor.user_id, workspace_id))
        if source.project_snapshot and not await self.projects.authorize_snapshot(
            ProjectActor(actor.user_id, workspace_id), source.project_snapshot
        ):
            raise CoordinationError("PROJECT_ACCESS_REVOKED")
        resolver = self.reply_identity if reply and self.reply_identity else self.identity
        identity, fingerprint = await resolver(actor, config)
        if fingerprint != source.connection_fingerprint:
            raise CoordinationError("REMOTE_BINDING_CHANGED")
        return identity

    async def poll(
        self, service: "CoordinationService", workspace_id: UUID, scope_id: str, effect: Effect | None
    ) -> int:
        """先持久候选与去重，再推进游标；失败或失去效果租约都保留旧位置。"""
        state = await service.authorized_state(workspace_id, scope_id)
        source = state.config.repository_source
        if not source:
            raise CoordinationError("CODE_SOURCE_CONFIG_REQUIRED")
        actor = RunActor(UUID(state.config.created_by), workspace_id)
        identity, fingerprint = await self.identity(actor, state.config)
        if fingerprint != source.connection_fingerprint:
            raise CoordinationError("REMOTE_BINDING_CHANGED")
        cursor = dict(cast(Json, state.checkpoints.get("repository:cursor", {"since": source.started_at})))
        cursor["followed_numbers"] = sorted(
            {str(item.origin["number"]) for _, _, item in RepositoryDiscussions.bindings(state, pull_only=False)}
        )
        observed = await self.reader.poll(identity, source, cursor)
        discussions = await RepositoryDiscussions(self).read(workspace_id, state)
        await service.authorized_state(workspace_id, scope_id)
        candidates: list[Trigger] = []
        for message in observed.messages:
            actor_name = (
                next((name for name in source.actors if name.casefold() == message.actor.casefold()), message.actor)
                if identity.provider == "github"
                else message.actor
            )
            user_id = source.actors.get(actor_name)
            if not user_id:
                continue  # 运输凭据的所有者不能替未绑定的外部作者授权。
            at = datetime.fromisoformat(message.at)
            if at > datetime.now(UTC) + timedelta(minutes=5):
                raise CoordinationError("CODE_SOURCE_PROTOCOL")
            if at < datetime.now(UTC) - timedelta(days=self.context.retention_days):
                continue
            expires_at = min(
                at + timedelta(days=self.context.retention_days),
                datetime.now(UTC) + timedelta(days=self.context.retention_days),
            ).isoformat()
            origin: Json = {
                "source_kind": "code_hosting",
                "source_id": state.config.source_id,
                "coordination_scope_id": scope_id,
                "route_key": state.config.route_key,
                "project_grant_id": state.config.project_grant_id,
                "open_id": actor_name,
                "created_at": message.at,
                "message_id": message.id,
                "number": message.number,
                "is_pull": message.is_pull,
                "url": message.url,
                "kind": message.kind,
                "repository_id": source.repository_id,
                "expires_at": expires_at,
                "root_text": message.root,
            }
            trigger = Trigger(
                message.id,
                str(workspace_id),
                scope_id,
                user_id,
                actor_name,
                message.human,
                self.reader.mentions(message.text, source.agent_login),
                ["repository:" + key(scope_id, message.number)],
                message.at,
                message.text,
                origin,
                expires_at=expires_at,
            )
            await self.authorize(workspace_id, state.config, trigger)
            candidates.append(trigger)

        def commit(engine: Engine, current: State) -> int:
            if effect:
                current.require_effect_lease(effect, engine.now())
            if current.config.repository_source != source or not current.config.enabled or current.config.paused:
                raise CoordinationError("SOURCE_REVISION_CONFLICT")
            count = sum(engine.receive(item) for item in candidates)
            for review in observed.reviews:
                current.checkpoints[f"repository:review:{review.number}"] = {
                    "outcome": review.outcome,
                    "at": review.at,
                    "closed_at": review.closed_at,
                }
            self.apply_reviews(engine, current)
            RepositoryDiscussions.apply(engine, current, discussions)
            current.checkpoints["repository:cursor"] = observed.cursor
            current.checkpoints["poll:repository"] = {"next_at": later(engine.now(), 60)}
            return count

        return await service.store.transact(workspace_id, scope_id, commit)

    @staticmethod
    def apply_reviews(engine: Engine, state: State) -> None:
        """原讨论本身就是 PR 时同样跟进关闭事实；不要求它是 Weora 之前创建的交付。"""
        for ticket in state.tickets:
            origin = records(state).triggers.get(ticket.origin_id)
            if not origin or not origin.authorized or origin.origin.get("is_pull") is not True:
                continue
            fact = state.checkpoints.get(f"repository:review:{origin.origin['number']}")
            if not isinstance(fact, dict) or fact.get("outcome") not in {"open", "closed", "merged"}:
                continue
            for follow in ticket.follows:
                if follow.thread in origin.threads:
                    follow.lifecycle = True
                    engine.observe_review(
                        ticket.id,
                        follow.thread,
                        cast(Literal["open", "closed", "merged"], fact["outcome"]),
                        str(fact["at"]),
                        closed_at=cast(str | None, fact.get("closed_at")),
                    )

    @staticmethod
    def evidence(state: State, evaluation_id: str, trigger: Trigger) -> Json:
        """只交付当前来源已落库的讨论；未补读的历史明确标为缺口，不声称覆盖完整线程。"""
        evaluation = records(state).evaluations.get(evaluation_id)
        cutoff = evaluation.started_at if evaluation else trigger.at
        related = sorted(
            (
                item
                for item in state.triggers
                if item.authorized
                and item.actor_id == trigger.actor_id
                and set(item.threads) & set(trigger.threads)
                and item.at <= cutoff
            ),
            key=lambda item: (item.at, item.id),
        )
        anchor = next((i for i, item in enumerate(related) if item.id == trigger.id), 0)
        selected = [item for i, item in enumerate(related) if i < 5 or abs(i - anchor) <= 5]
        return {
            "root": trigger.origin.get("root_text", ""),
            "items": [{"id": item.id, "author_id": item.actor_open_id, "text": item.text[:1200]} for item in selected],
            "partial": True,
            "coverage": "persisted_events_since_source_enabled",
            "next_cursor": None,
            "loaded_messages": len(related),
        }

    async def reply(self, identity: HostedReviewIdentity, trigger: Trigger, text: str, business_key: str) -> str:
        """Outbox 已先保存 unknown；这里只提交一次，网络不明结果留人工对账。"""
        return await self.reader.reply(identity, str(trigger.origin["number"]), text, business_key)
