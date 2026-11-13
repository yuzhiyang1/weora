"""FastAPI 应用工厂。

应用工厂集中完成框架装配，业务模块不能依赖这里的全局应用实例。
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from weora import __version__
from weora.bootstrap.console_settings import git_identity, model_profile
from weora.bootstrap.execution_services import configure_execution_services
from weora.bootstrap.harness import omp_configured
from weora.bootstrap.health import router as health_router
from weora.bootstrap.legacy_code_sources import LegacyCodeSources
from weora.bootstrap.object_storage import create_object_storage
from weora.bootstrap.run_profile import execution_profile
from weora.bootstrap.settings import AppSettings, get_settings
from weora.modules.agents.api.routes import router as agents_router
from weora.modules.agents.application.runtime import AgentRuntimeContextAssembler
from weora.modules.agents.application.service import AgentRegistryService
from weora.modules.agents.infrastructure.skill_catalog import SkillsModuleVersionCatalog
from weora.modules.agents.infrastructure.unit_of_work import SqlAlchemyAgentsUnitOfWork
from weora.modules.collaboration.agent_mail_api import router as agent_mail_router
from weora.modules.collaboration.api import router as tasks_router
from weora.modules.collaboration.application.inbox import InboxService
from weora.modules.collaboration.application.tasks import TaskRecord
from weora.modules.collaboration.inbox_api import router as inbox_router
from weora.modules.collaboration.infrastructure.inbox import InboxStore
from weora.modules.computers.api import router as computer_router
from weora.modules.computers.credential_api import router as computer_credential_router
from weora.modules.computers.openbot import OpenBot
from weora.modules.console_settings.api import router as settings_router
from weora.modules.console_settings.domain import ModelPreferences, SettingsActor, SettingsError
from weora.modules.console_settings.service import ConsoleSettingsService
from weora.modules.console_settings.store import SettingsStore
from weora.modules.context.api import router as context_router
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextError, ContextRunScope, Json
from weora.modules.context.feishu import FernetSecrets
from weora.modules.coordination.api import router as coordination_router
from weora.modules.coordination.domain import CoordinationError
from weora.modules.coordination.repository_source import RepositoryIntake
from weora.modules.coordination.service import CoordinationService
from weora.modules.coordination.store import SqlCoordinationStore
from weora.modules.delivery.api.routes import router as delivery_router
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.git import ConfiguredCredentials
from weora.modules.identity.api.routes import router as identity_router
from weora.modules.identity.api.workspace_routes import router as workspace_identity_router
from weora.modules.identity.application.invitations import InviteSigner
from weora.modules.identity.application.service import IdentityService
from weora.modules.identity.infrastructure.security import Argon2PasswordHasher, SecureTokenManager
from weora.modules.identity.infrastructure.unit_of_work import SqlAlchemyIdentityUnitOfWork
from weora.modules.projects.api import router as projects_router
from weora.modules.projects.api import source_router
from weora.modules.projects.code_sources import CodeSourceService
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.git_reader import GitRepositoryReader
from weora.modules.projects.repository_sync import RepositorySync
from weora.modules.projects.service import ProjectService
from weora.modules.runs.api.routes import router as runs_router
from weora.modules.runs.application.harness import OMP_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.application.task_collaboration import TaskCollaborationService
from weora.modules.runs.infrastructure.agent_catalog import AgentsModuleVersionCatalog
from weora.modules.runs.infrastructure.models import RunModel
from weora.modules.runs.infrastructure.module_access import RunContentGuard
from weora.modules.runs.infrastructure.project_catalog import ProjectsModuleCatalog
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork
from weora.modules.runs.infrastructure.watch import RunWatch
from weora.modules.skills.api.distribution_routes import router as skill_distribution_router
from weora.modules.skills.api.routes import router as skills_router
from weora.modules.skills.application.service import SkillRegistryService
from weora.modules.skills.infrastructure.unit_of_work import SqlAlchemySkillsUnitOfWork
from weora.shared.api.request_id import RequestIdMiddleware
from weora.shared.infrastructure.database import create_database
from weora.shared.infrastructure.postgres_watch import PostgresWatch
from weora.shared.object_storage import ObjectStoragePort


def create_app(
    settings: AppSettings | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    object_storage_factory: Callable[[AppSettings], ObjectStoragePort | None] = create_object_storage,
) -> FastAPI:
    """创建一个隔离的 FastAPI 应用实例，便于测试和多进程启动。"""
    resolved_settings = settings or get_settings()
    owned_engine = None
    if session_factory is None:
        owned_engine, session_factory = create_database(resolved_settings.database_url)
    object_storage = object_storage_factory(resolved_settings)
    run_watch = RunWatch()
    agent_mail_watch = PostgresWatch("weora_agent_mail_changed")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """统一释放对象存储和数据库连接池。"""
        async def stop_listener() -> None:
            for listener in listeners:
                listener.cancel()
            await asyncio.gather(*listeners, return_exceptions=True)

        listeners = (
            [asyncio.create_task(watch.listen(owned_engine)) for watch in (run_watch, agent_mail_watch)]
            if owned_engine is not None else []
        )
        try:
            yield
        finally:
            # 任一外部资源关闭失败，其他凭据、HTTP 与连接池仍必须继续释放。
            async with AsyncExitStack() as cleanup:
                if owned_engine is not None:
                    cleanup.push_async_callback(owned_engine.dispose)
                if object_storage is not None:
                    cleanup.push_async_callback(object_storage.aclose)
                if app.state.github_tokens is not None:
                    cleanup.push_async_callback(app.state.github_tokens.aclose)
                if app.state.context_reader is not None:
                    cleanup.push_async_callback(app.state.context_reader.aclose)
                cleanup.push_async_callback(stop_listener)

    app = FastAPI(
        title=resolved_settings.app_name,
        summary="Enterprise Human-Agent collaboration platform",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.run_watch = run_watch

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        """422 不回显输入与 Pydantic ctx，保护只写密钥及工具正文。"""
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(item["loc"]), "type": item["type"], "msg": "请求参数无效，请检查字段格式与长度"}
                    for item in error.errors()
                ]
            },
        )

    app.state.db_session_factory = session_factory
    app.state.agent_mail_watch = agent_mail_watch
    app.state.object_storage = object_storage
    # TODO(上线前): 关闭并删除开发阶段的密码绕过，只保留正式认证流程。
    development_auth_bypass_enabled = (
        resolved_settings.environment == "development" and resolved_settings.development_auth_bypass_enabled
    )
    app.state.identity_service = IdentityService(
        uow_factory=lambda: SqlAlchemyIdentityUnitOfWork(session_factory),
        password_hasher=Argon2PasswordHasher(),
        token_manager=SecureTokenManager(),
        password_verification_enabled=not development_auth_bypass_enabled,
        registration_enabled=resolved_settings.registration_enabled,
        session_ttl=timedelta(days=resolved_settings.session_ttl_days),
        login_max_failures=resolved_settings.login_max_failures,
        login_failure_window=timedelta(minutes=resolved_settings.login_failure_window_minutes),
        invite_signer=InviteSigner(
            resolved_settings.workspace_invite_key.get_secret_value().encode()
            if resolved_settings.workspace_invite_key else None
        ),
    )
    skill_registry_service = SkillRegistryService(
        uow_factory=lambda: SqlAlchemySkillsUnitOfWork(session_factory),
        object_storage=object_storage,
    )
    app.state.skill_registry_service = skill_registry_service
    app.state.agent_registry_service = AgentRegistryService(
        uow_factory=lambda: SqlAlchemyAgentsUnitOfWork(session_factory),
        skill_catalog=SkillsModuleVersionCatalog(skill_registry_service),
        runtime_context_assembler=AgentRuntimeContextAssembler(skill_registry_service.read_version_archive),
    )
    guard = RunContentGuard(app.state.identity_service)

    class ProjectMemberships:
        async def role(self, actor: ProjectActor) -> str | None:
            principal = await app.state.identity_service.get_active_workspace_principal(
                user_id=actor.user_id, workspace_id=actor.workspace_id
            )
            return principal.role if principal else None

    project_cipher = (
        FernetSecrets(resolved_settings.context_encryption_key.get_secret_value())
        if resolved_settings.context_encryption_key
        else None
    )

    class SettingsMemberships:
        async def role(self, actor: SettingsActor) -> str | None:
            principal = await app.state.identity_service.get_active_workspace_principal(
                user_id=actor.user_id, workspace_id=actor.workspace_id
            )
            return principal.role if principal else None

    app.state.console_settings_service = ConsoleSettingsService(
        SettingsStore(session_factory),
        SettingsMemberships(),
        project_cipher,
        ModelPreferences(
            model_id=resolved_settings.pi_model or "deepseek-chat",
            max_tokens=resolved_settings.run_model_max_tokens,
            timeout_seconds=resolved_settings.run_model_timeout_seconds,
            execution_timeout_seconds=resolved_settings.run_execution_timeout_seconds,
        ),
        resolved_settings.deepseek_api_key.get_secret_value() if resolved_settings.deepseek_api_key else "",
        resolved_settings.run_runtime != "disabled",
        omp_available=omp_configured(resolved_settings),
    )

    @app.exception_handler(SettingsError)
    async def settings_error(_: Request, error: SettingsError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status, content={"detail": {"code": error.code, "message": error.message}}
        )

    project_reader = (
        GitRepositoryReader(
            Path(resolved_settings.repository_cache_path),
            private_origins=resolved_settings.repository_private_origins,
            ca_files=resolved_settings.repository_tls_ca_files,
            ssh_credentials=ConfiguredCredentials(resolved_settings.hosting_credentials),
            max_bytes=resolved_settings.repository_snapshot_max_bytes,
            sync_timeout_seconds=resolved_settings.repository_sync_timeout_seconds,
            sync_max_bytes=resolved_settings.repository_sync_max_bytes,
        )
        if resolved_settings.repository_cache_path
        else None
    )
    projects = ProjectService(session_factory, ProjectMemberships(), project_cipher, project_reader)
    if project_reader is not None and resolved_settings.repository_sync_enabled:
        projects.repository_sync = RepositorySync(
            session_factory, project_reader, projects.repository_source,
            timeout_seconds=resolved_settings.repository_sync_timeout_seconds,
            interval_seconds=resolved_settings.repository_sync_interval_seconds,
            max_age_seconds=resolved_settings.repository_sync_max_age_seconds,
        )
    app.state.project_service = projects
    app.state.code_source_service = CodeSourceService(session_factory, ProjectMemberships(), project_cipher)
    if resolved_settings.sandbox_image:
        app.state.project_service.build_profiles["default"] = ("基础构建环境", resolved_settings.sandbox_image)
    if resolved_settings.project_sandbox_image:
        app.state.project_service.build_profiles["project"] = (
            "项目专用构建环境",
            resolved_settings.project_sandbox_image,
        )
    app.state.legacy_code_sources = LegacyCodeSources(
        resolved_settings, app.state.code_source_service, app.state.project_service
    )
    project_catalog = ProjectsModuleCatalog(app.state.project_service)
    guard.projects = project_catalog
    base_profile = execution_profile(resolved_settings)

    async def current_profile(actor: RunActor) -> ExecutionProfile | None:
        if base_profile is None:
            return None
        service = app.state.console_settings_service
        config = await service.resolve_model(actor.workspace_id)
        profile = model_profile(config, base_profile)
        selected, _ = await service.selected_harness(actor.workspace_id)
        if selected == "oh-my-pi":
            if not omp_configured(resolved_settings) or profile.provider != "deepseek" or not config.api_key:
                raise SettingsError("HARNESS_UNAVAILABLE", "Oh My Pi 当前不可用，请检查执行内核和模型设置", 409)
            return replace(profile, runtime_id=OMP_RUNTIME)
        return profile

    async def current_git_identity(actor: RunActor, agent_name: str) -> dict[str, object]:
        return await git_identity(app.state.console_settings_service, app.state.identity_service, actor, agent_name)

    async def workspace_member_name(workspace_id: UUID, user_id: UUID) -> str | None:
        member = await app.state.identity_service.get_active_workspace_principal(
            workspace_id=workspace_id, user_id=user_id,
        )
        return member.display_name if member else None

    agent_catalog = AgentsModuleVersionCatalog(app.state.agent_registry_service)

    async def historical_title(task: TaskRecord) -> str | None:
        coordinator = app.state.coordination_service
        return await coordinator.historical_task_title(task) if coordinator is not None else None

    app.state.run_service = RunService(
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(session_factory),
        agent_catalog=agent_catalog,
        execution_profile=base_profile,
        profile_provider=current_profile if base_profile is not None else None,
        git_identity_provider=current_git_identity,
        content_authorizer=guard,
        projects=project_catalog,
        channel_project_authorizer=guard.channel_project_allowed,
        historical_title=historical_title,
    )
    app.state.task_collaboration_service = TaskCollaborationService(
        lambda: SqlAlchemyRunsUnitOfWork(session_factory), workspace_member_name, agent_catalog,
        current_profile, project_catalog, current_git_identity,
    )
    app.state.openbot = None
    if resolved_settings.computer_supervisor_url:
        supervisor_token = resolved_settings.computer_supervisor_token
        master_token = resolved_settings.computer_master_token
        assert supervisor_token is not None and master_token is not None
        app.state.openbot = OpenBot(
            resolved_settings.computer_supervisor_url,
            supervisor_token.get_secret_value(),
            master_token.get_secret_value(),
            "weora",
        )
    configure_execution_services(app, resolved_settings, guard)
    app.state.inbox_service = InboxService(
        InboxStore(session_factory, select(RunModel.id, RunModel.workspace_id, RunModel.task_id).subquery("run_tasks")),
        app.state.task_collaboration_service,
        app.state.run_service,
        app.state.delivery_service,
    )
    context = app.state.context_service
    app.state.coordination_service = (
        CoordinationService(
            SqlCoordinationStore(session_factory, context.cipher),
            context,
            app.state.run_service,
            app.state.agent_registry_service,
            app.state.project_service,
            app.state.delivery_service,
            app.state.code_event_reader,
        )
        if context
        else None
    )
    app.include_router(coordination_router, prefix=resolved_settings.api_prefix)

    coordinator = app.state.coordination_service
    if context and coordinator:
        coordinator.repository_intake = RepositoryIntake(
            context, app.state.project_service, app.state.repository_identity
        )
        coordinator.repository_intake.legacy_options = app.state.repository_options
        coordinator.repository_intake.reply_identity = app.state.repository_reply_identity

        async def authorize_external(scope: ContextRunScope) -> None:
            """通用上下文闸门使用原生 Task 的实际来源，绝不从模型工具参数取得用户身份。"""
            detail = await app.state.run_service.access_metadata(
                RunActor(scope.actor.user_id, scope.actor.workspace_id), scope.run_id
            )
            if detail is None:
                raise ContextError("SOURCE_REVOKED")
            origin = detail.task.origin_metadata or {}
            if not origin.get("coordination_scope_id"):
                return
            try:
                await coordinator.authorize_runtime_origin(
                    RunActor(scope.actor.user_id, scope.actor.workspace_id), origin
                )
            except (CoordinationError, ProjectError, DeliveryError) as error:
                raise ContextError(error.code) from None

        async def code_project_allowed(actor: RunActor, origin: Json, snapshot: Json) -> bool:
            """仓库群发授权来自管理员来源配置；原生创建只允许该项目内已选的仓库。"""
            try:
                config = await coordinator.authorize_code_origin(actor, origin)
                selected = snapshot.get("repositories", [])
                return (
                    snapshot.get("project_id") == config.project_id
                    and isinstance(selected, list)
                    and {item["id"] for item in selected if isinstance(item, dict)} == set(config.repository_ids)
                )
            except (CoordinationError, ProjectError, DeliveryError):
                return False

        context.run_authorizer = authorize_external
        guard.code_project_authorizer = code_project_allowed

    async def channel_status(scope: ContextRunScope) -> Json:
        if scope.source_kind == "code_hosting":
            await context.authorize_derived(scope)
            result: Json = {"inbox": None, "deliveries": [], "source_kind": "code_hosting"}
        else:
            result = await FeishuChannels(context).run_status(scope)
        if app.state.coordination_service:
            deliveries = result["deliveries"]
            assert isinstance(deliveries, list)
            deliveries.extend(await app.state.coordination_service.channel_deliveries(scope))
        return result

    app.state.channel_status_factory = channel_status if context else None

    # CORS 属于 HTTP 边界策略，集中放在装配层，避免领域模块感知浏览器细节。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router, prefix=resolved_settings.api_prefix)
    app.include_router(identity_router, prefix=resolved_settings.api_prefix)
    app.include_router(workspace_identity_router, prefix=resolved_settings.api_prefix)
    app.include_router(skills_router, prefix=resolved_settings.api_prefix)
    app.include_router(skill_distribution_router, prefix=resolved_settings.api_prefix)
    app.include_router(agents_router, prefix=resolved_settings.api_prefix)
    app.include_router(runs_router, prefix=resolved_settings.api_prefix)
    app.include_router(tasks_router, prefix=resolved_settings.api_prefix)
    app.include_router(agent_mail_router, prefix=resolved_settings.api_prefix)
    app.include_router(inbox_router, prefix=resolved_settings.api_prefix)
    app.include_router(computer_router, prefix=resolved_settings.api_prefix)
    app.include_router(computer_credential_router, prefix=resolved_settings.api_prefix)
    app.include_router(context_router, prefix=resolved_settings.api_prefix)
    app.include_router(delivery_router, prefix=resolved_settings.api_prefix)
    app.include_router(projects_router, prefix=resolved_settings.api_prefix)
    app.include_router(source_router, prefix=resolved_settings.api_prefix)
    app.include_router(settings_router, prefix=resolved_settings.api_prefix)
    return app
