"""通过应用接口串联身份、运行、上下文与交付权限，不跨模块读写 ORM。"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from weora.modules.collaboration.application.tasks import TaskRecord
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextActor, ContextError, ContextRunScope
from weora.modules.context.service import ContextService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.identity.application.service import IdentityService
from weora.modules.runs.application.ports import ProjectContextCatalog, RunActor, RunRecord
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.tools.application.gateway import ToolScope


def context_scope(actor: ContextActor, run: RunRecord, task: TaskRecord) -> ContextRunScope:
    origin = task.origin_metadata or {}
    cutoff = datetime.fromisoformat(str(origin["created_at"])) if origin.get("created_at") else run.created_at
    return ContextRunScope(
        run.id,
        actor,
        "group" if task.audience == "group" else "private",
        UUID(str(origin["source_id"])) if origin.get("source_id") else None,
        cutoff,
        str(origin["open_id"]) if origin.get("open_id") else None,
        "code_hosting" if origin.get("source_kind") == "code_hosting" else "feishu",
    )


class ContextModuleAccess:
    def __init__(self, identity: IdentityService, runs: RunService) -> None:
        self.identity, self.runs = identity, runs
        self.projects: ProjectContextCatalog | None = None
        self.context: ContextService | None = None

    async def is_active(self, actor: ContextActor) -> bool:
        return (
            await self.identity.get_active_workspace_principal(user_id=actor.user_id, workspace_id=actor.workspace_id)
            is not None
        )

    async def can_manage(self, actor: ContextActor) -> bool:
        principal = await self.identity.get_active_workspace_principal(
            user_id=actor.user_id, workspace_id=actor.workspace_id
        )
        return principal is not None and principal.role in {"owner", "admin"}

    async def can_access_run(self, scope: ContextRunScope) -> bool:
        detail = await self.runs.access_metadata(RunActor(scope.actor.user_id, scope.actor.workspace_id), scope.run_id)
        if detail is None or context_scope(scope.actor, detail.run, detail.task) != scope:
            return False
        if detail.task.project_context and scope.audience == "group" and scope.source_kind == "feishu":
            if self.context is None:
                return False
            return await FeishuChannels(self.context).project_allowed(
                scope.actor, detail.task.origin_metadata or {}, detail.task.project_context
            )
        return True

    async def scope(self, actor: ContextActor, run_id: UUID) -> ContextRunScope:
        detail = await self.runs.access_metadata(RunActor(actor.user_id, actor.workspace_id), run_id)
        if detail is None or not await self.is_active(actor):
            raise ContextError("PERMISSION_DENIED", "运行不存在或不可访问")
        if detail.task.project_context and (
            self.projects is None
            or not await self.projects.authorize(
                RunActor(actor.user_id, actor.workspace_id), detail.task.project_context
            )
        ):
            raise ContextError("PROJECT_ACCESS_REVOKED", "项目已归档或选中仓库已停用")
        return context_scope(actor, detail.run, detail.task)


class DeliveryModuleAccess:
    def __init__(
        self,
        context_access: ContextModuleAccess,
        executions: SqlAlchemyExecutionStore,
        context: ContextService | None,
        *,
        execution_workspace_id: UUID | None,
        project_sandbox_enabled: bool = False,
    ) -> None:
        self.context_access, self.executions, self.context = context_access, executions, context
        self.execution_workspace_id = execution_workspace_id
        self.project_sandbox_enabled = project_sandbox_enabled

    async def authorize(
        self, actor: RunActor, run_id: UUID, *, write: bool = False, fencing_token: int | None = None
    ) -> None:
        try:
            scope = await self.context_access.scope(ContextActor(actor.user_id, actor.workspace_id), run_id)
            if self.context:
                await self.context.authorize_derived(scope)
            elif scope.audience == "group":
                raise ContextError("CREDENTIALS_UNAVAILABLE")
        except ContextError as error:
            raise DeliveryError(error.code, error.message) from None
        if write:
            detail = await self.context_access.runs.access_metadata(actor, run_id)
            if detail and detail.task.project_context and not self.project_sandbox_enabled:
                raise DeliveryError(
                    "PROJECT_REPOSITORY_NOT_PREPARED", "所选项目仓库尚未准备隔离代码副本，不会执行默认测试仓库"
                )
            if self.execution_workspace_id != actor.workspace_id:
                raise DeliveryError("REPOSITORY_DENIED", "当前工作空间没有隔离仓库授权")
            if fencing_token is None or not await self.executions.authorize_token(
                actor, run_id, fencing_token, write=True
            ):
                raise DeliveryError("LEASE_LOST", "执行租约已失效或已请求取消")

    async def plan_changed(self, actor: RunActor, run_id: UUID, state: str) -> None:
        if not await self.executions.transition(actor, run_id, state):
            raise DeliveryError("RUN_STATE_CONFLICT", "运行状态已变化，不能恢复终态或覆盖正在执行的尝试")


class GatewayModuleAccess:
    def __init__(
        self, context_access: ContextModuleAccess, executions: SqlAlchemyExecutionStore, context: ContextService | None
    ) -> None:
        self.context_access, self.executions, self.context = context_access, executions, context

    async def authorize(self, scope: ToolScope, *, effect: str) -> bool:
        try:
            current = await self.context_access.scope(
                ContextActor(scope.actor.user_id, scope.actor.workspace_id), scope.run_id
            )
            if self.context:
                await self.context.authorize_derived(current)
            elif current.audience == "group":
                return False
        except ContextError:
            return False
        return await self.executions.authorize_token(
            scope.actor, scope.run_id, scope.fencing_token, scope.lease_owner, write=effect != "read"
        )


class RunContentGuard:
    def __init__(self, identity: IdentityService | None = None) -> None:
        self.identity = identity
        self.context: ContextService | None = None
        self.projects: ProjectContextCatalog | None = None
        self.code_project_authorizer: (
            Callable[[RunActor, dict[str, object], dict[str, object]], Awaitable[bool]] | None
        ) = None

    async def channel_project_allowed(
        self, actor: RunActor, origin: dict[str, object], snapshot: dict[str, object]
    ) -> bool:
        """创建入口只接受当前管理员明确授予原群的项目选择，不信任消息正文中的 ID。"""
        if origin.get("source_kind") == "code_hosting":
            if self.code_project_authorizer is None:
                return False
            return await self.code_project_authorizer(actor, origin, snapshot)
        return self.context is not None and await FeishuChannels(self.context).project_allowed(
            ContextActor(actor.user_id, actor.workspace_id), origin, snapshot
        )

    async def __call__(self, actor: RunActor, run: RunRecord, task: TaskRecord) -> bool:
        if (
            self.identity is not None
            and await self.identity.get_active_workspace_principal(
                user_id=actor.user_id,
                workspace_id=actor.workspace_id,
            )
            is None
        ):
            return False
        if run.failure_code == "CONTEXT_EXPIRED":
            return False
        if task.project_context and (
            self.projects is None or not await self.projects.authorize(actor, task.project_context)
        ):
            return False
        scope = context_scope(ContextActor(actor.user_id, actor.workspace_id), run, task)
        if self.context is None:
            return task.audience == "private"
        try:
            await self.context.authorize_derived(scope)
            return True
        except ContextError:
            return False
