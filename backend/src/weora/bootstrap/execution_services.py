"""第一版执行能力装配；外部系统未配置时安全禁用，不选择机器上的其他凭据。"""

from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import aiohttp
from fastapi import FastAPI

from weora.bootstrap.project_execution import ProjectExecution
from weora.bootstrap.settings import AppSettings
from weora.modules.collaboration.infrastructure.inbox import sync_plan_inbox
from weora.modules.computers.credentials import ComputerCredentials
from weora.modules.computers.tools import register_computer_tools
from weora.modules.context.agent_cli import FEISHU_AGENT_CLI, FeishuAgentCli
from weora.modules.context.agent_cli_skills import FEISHU_CLI_SKILL, read_feishu_cli_skill
from weora.modules.context.domain import ContextActor, ContextError, Json, Message, MessagePage
from weora.modules.context.feishu import FeishuHttpReader, FernetSecrets
from weora.modules.context.feishu_cli import FeishuCliProcess, FeishuCliReader
from weora.modules.context.service import ContextService
from weora.modules.context.store import SqlContextStore
from weora.modules.context.tools import ContextTools
from weora.modules.coordination.domain import Config, CoordinationError
from weora.modules.coordination.hosting_http import HostedReviewEvents, HostedReviewIdentity
from weora.modules.delivery.application.code_hosting import RepositoryConnection, RepositoryDelivery, RepositoryRegistry
from weora.modules.delivery.application.ports import DraftPullRequests, ReviewReference
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.application.tools import register_delivery_tools
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.browser import SandboxBrowserVerifier
from weora.modules.delivery.infrastructure.codeup import CodeupReviews
from weora.modules.delivery.infrastructure.codeup_existing_review import CodeupExistingReviews
from weora.modules.delivery.infrastructure.docker import (
    DockerConfig,
    DockerResources,
    DockerSandbox,
    RepositorySnapshot,
)
from weora.modules.delivery.infrastructure.git import CliGitPublisher, ConfiguredCredentials
from weora.modules.delivery.infrastructure.github import GitHubDraftPullRequests, GitHubRemote, UnconfiguredPullRequests
from weora.modules.delivery.infrastructure.github_tokens import GitHubAppTokens
from weora.modules.delivery.infrastructure.persistence import SqlAlchemyDeliveryStore
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.module_access import (
    ContextModuleAccess,
    DeliveryModuleAccess,
    GatewayModuleAccess,
    RunContentGuard,
)
from weora.modules.tools.application.gateway import ToolGateway, ToolScope, ToolSpec, failure
from weora.modules.tools.infrastructure.repository import (
    FIND_FILES,
    LIST_DIRECTORY,
    READ_FILE,
    SEARCH_TEXT,
    ReadOnlyRepository,
)
from weora.modules.tools.infrastructure.store import SqlAlchemyToolStore
from weora.shared.infrastructure.database import SessionFactory


class ManagedFeishuReader:
    """延迟创建 HTTP Session，让同步 create_app 和真实 Worker 共用相同装配。"""

    def __init__(self, cipher: FernetSecrets, cli: FeishuCliProcess | None = None) -> None:
        self.cipher = cipher
        self.cli = cli
        self.session: aiohttp.ClientSession | None = None
        self.reader: FeishuHttpReader | None = None

    def _reader(self) -> FeishuHttpReader:
        if self.reader is None:
            self.session = aiohttp.ClientSession(trust_env=False)
            self.reader = (
                FeishuCliReader(self.session, self.cipher, self.cli)
                if self.cli is not None
                else FeishuHttpReader(self.session, self.cipher)
            )
        return self.reader

    async def probe(self, connection: Json, chat_id: str | None = None) -> Json:
        return await self._reader().probe(connection, chat_id)

    async def messages(
        self,
        connection: Json,
        *,
        chat_id: str,
        start: datetime,
        end: datetime,
        page_token: str | None,
        thread_id: str | None = None,
        descending: bool = False,
    ) -> MessagePage:
        return await self._reader().messages(
            connection,
            chat_id=chat_id,
            start=start,
            end=end,
            page_token=page_token,
            thread_id=thread_id,
            descending=descending,
        )

    async def message(self, connection: Json, message_id: str) -> Message:
        return await self._reader().message(connection, message_id)

    async def upload_image(self, connection: Json, png: bytes) -> str:
        """图片和消息使用同一应用凭据，不读取本机个人身份。"""
        return await self._reader().upload_image(connection, png)

    async def reply(
        self, connection: Json, message_id: str, text: str, business_key: str, *, image_keys: tuple[str, ...] = ()
    ) -> str:
        return await self._reader().reply(connection, message_id, text, business_key, image_keys=image_keys)

    async def aclose(self) -> None:
        if self.session is not None:
            await self.session.close()


def configure_execution_services(app: FastAPI, settings: AppSettings, guard: RunContentGuard) -> None:
    sessions = cast(SessionFactory, app.state.db_session_factory)
    executions = SqlAlchemyExecutionStore(sessions)
    context_access = ContextModuleAccess(app.state.identity_service, app.state.run_service)
    context_access.projects = getattr(guard, "projects", None)
    context: ContextService | None = None
    app.state.context_reader = None
    app.state.computer_credentials = None
    if settings.context_encryption_key and settings.context_cursor_key:
        cipher = FernetSecrets(settings.context_encryption_key.get_secret_value())
        cli = (
            FeishuCliProcess(settings.feishu_cli_executable or "", timeout=settings.feishu_cli_timeout_seconds)
            if settings.feishu_reader_backend == "cli"
            else None
        )
        reader = ManagedFeishuReader(cipher, cli)
        context = ContextService(
            SqlContextStore(sessions),
            reader,
            cipher,
            settings.context_cursor_key.get_secret_value().encode(),
            context_access,
            retention_days=settings.context_retention_days,
        )
        app.state.context_reader = reader
        app.state.computer_credentials = ComputerCredentials(sessions, cipher)
    app.state.context_service = guard.context = context
    context_access.context = context
    app.state.context_scope_factory = context_access.scope
    app.state.execution_store = executions
    app.state.context_access = context_access

    sandbox_config: DockerConfig | None = None
    if settings.sandbox_image:
        sandbox_config = DockerConfig(
            settings.sandbox_image,
            {
                settings.sandbox_repository_id or "": RepositorySnapshot(
                    Path(settings.sandbox_repository_archive or ""),
                    settings.sandbox_repository_revision or "",
                    settings.sandbox_repository_sha256 or "",
                )
            },
            executable=settings.sandbox_cli_executable,
            executable_args=settings.sandbox_cli_args,
            deployment_id=str(settings.sandbox_workspace_id),
            image_resources=(
                {settings.project_sandbox_image: DockerResources(**settings.project_sandbox_resources.model_dump())}
                if settings.project_sandbox_image
                else {}
            ),
        )
        sandbox_config.validate()
    sandbox = DockerSandbox(sandbox_config)
    remote: DraftPullRequests = UnconfiguredPullRequests()
    entries: dict[RepositoryConnection, DraftPullRequests] = {}
    app.state.github_tokens = None
    if settings.github_app_id and settings.github_installation_id and settings.github_private_key:
        if not settings.github_remote_id or not settings.github_repository or not settings.github_target_branch:
            raise ValueError("GitHub App 必须同时配置获准远端、仓库与目标分支")
        remotes = {
            settings.github_remote_id: GitHubRemote(settings.github_repository, (settings.github_target_branch,))
        }
        tokens = GitHubAppTokens(
            settings.github_app_id,
            settings.github_installation_id,
            settings.github_private_key.get_secret_value(),
            remotes,
        )
        remote = GitHubDraftPullRequests(remotes, tokens, sandbox)
        app.state.github_tokens = tokens
        if settings.sandbox_workspace_id is None or not settings.sandbox_repository_id:
            raise ValueError("代码托管连接必须指定执行工作空间与仓库")
        connection = RepositoryConnection(
            settings.sandbox_workspace_id,
            settings.github_remote_id,
            "legacy-github-v1",
            "github",
            settings.sandbox_repository_id,
            settings.github_repository,
            f"https://github.com/{settings.github_repository}.git",
            "https://api.github.com",
            f"https://github.com/{settings.github_repository}",
            f"github-app:{settings.github_app_id}:installation:{settings.github_installation_id}",
            (settings.github_target_branch,),
        )
        entries[connection] = remote
    credentials = ConfiguredCredentials(settings.hosting_credentials, settings.hosting_api_token_files)
    git = CliGitPublisher(sandbox, credentials)
    for configured in settings.hosting_connections:
        connection = configured.connection()
        # 当前首个插件是云效；将来增加 GitLab 只改此装配处和对应 Adapter。
        entries[connection] = RepositoryDelivery(connection, git, CodeupReviews(credentials))
    registry = RepositoryRegistry(entries) if entries else None
    if registry is not None:
        remote = registry
    project_execution = ProjectExecution(
        app.state.project_service,
        app.state.run_service,
        sandbox,
        enabled=settings.project_sandbox_enabled,
        legacy_bindings=registry,
        image=settings.project_sandbox_image,
        review_connections=tuple(item.connection() for item in settings.hosting_connections),
        review_provider=CodeupExistingReviews(credentials),
        code_sources=app.state.code_source_service,
    )

    async def workspace_history(actor: RunActor, run_id: UUID) -> tuple[UUID, ...]:
        """Runs 应用接口保证同 Task 和同发起人，不读取其他模块的 ORM。"""
        return await cast(RunService, app.state.run_service).execution_predecessors(actor, run_id)

    delivery = DeliveryService(
        SqlAlchemyDeliveryStore(sessions, sync_plan_inbox),
        DeliveryModuleAccess(
            context_access,
            executions,
            context,
            execution_workspace_id=settings.sandbox_workspace_id,
            project_sandbox_enabled=settings.project_sandbox_enabled,
        ),
        sandbox,
        SandboxBrowserVerifier(sandbox, service_port=settings.sandbox_browser_port),
        remote,
        retention_days=settings.context_retention_days,
        remote_bindings=registry,
        validate_scope=project_execution.validate,
        prepare_sandbox=project_execution.prepare,
        bind_scope=project_execution.bind,
        push_branch=project_execution.push,
        update_branch=project_execution.update,
        create_review=project_execution.review,
        reconcile_review=project_execution.reconcile_review,
        workspace_sandbox=sandbox,
        workspace_history=workspace_history,
    )
    app.state.delivery_service = delivery

    async def review_identity(actor: RunActor, reference: ReviewReference) -> HostedReviewIdentity:
        """每次轮询重新解析项目授权和冻结的连接，不根据评审链接推测 API 身份。"""
        detail = await cast(RunService, app.state.run_service).access_metadata(actor, reference.run_id)
        if not detail:
            raise DeliveryError("RUN_NOT_FOUND")
        if detail.task.project_context:
            connection, token = await project_execution.review_identity(actor, reference)
        else:
            found = next(
                (
                    item
                    for item in entries
                    if item.workspace_id == actor.workspace_id
                    and item.repository_id == reference.scope.repository_id
                    and item.fingerprint == reference.scope.remote_binding
                ),
                None,
            )
            if not found:
                raise DeliveryError("REMOTE_BINDING_CHANGED")
            connection = found
            if connection.provider == "github":
                if not app.state.github_tokens:
                    raise DeliveryError("CODE_SOURCE_CREDENTIAL_UNAVAILABLE")
                token = (await app.state.github_tokens.observe(connection.remote_id)).value
            else:
                token = credentials.get(connection.credential_ref).api_token
        if connection.provider not in {"github", "codeup"}:
            raise DeliveryError("CAPABILITY_UNSUPPORTED")
        return HostedReviewIdentity(
            cast(Literal["github", "codeup"], connection.provider),
            connection.api_origin,
            connection.platform_repository,
            connection.web_url,
            token,
            connection.organization,
        )

    app.state.code_event_reader = HostedReviewEvents(review_identity)

    async def repository_identity(
        actor: RunActor, config: Config, *, reply: bool = False
    ) -> tuple[HostedReviewIdentity, str]:
        """独立仓库入口只使用登记连接；读取代码讨论不依赖执行镜像，也不缓存个人令牌。"""
        import hashlib
        import json

        source = config.repository_source
        if source is None:
            raise CoordinationError("CODE_SOURCE_CONFIG_REQUIRED")
        snapshot = source.project_snapshot
        if snapshot:
            projects = cast(ProjectService, app.state.project_service)
            if not await projects.authorize_snapshot(ProjectActor(actor.user_id, actor.workspace_id), snapshot):
                raise CoordinationError("PROJECT_ACCESS_REVOKED")
            repos = cast(list[Json], snapshot["repositories"])
            repo = next((item for item in repos if item["id"] == source.repository_id), None)
            if not repo:
                raise CoordinationError("REPOSITORY_NOT_SELECTED")
            if repo.get("code_source_id"):
                api = await app.state.code_source_service.identity(
                    ProjectActor(actor.user_id, actor.workspace_id),
                    UUID(str(repo["code_source_id"])),
                    int(str(repo["code_source_version"])),
                )
                web_url = str(repo["git_url"]).removesuffix(".git")
                identity = HostedReviewIdentity(
                    "codeup", api.api_origin, str(repo["platform_repository"]), web_url, api.token, api.organization
                )
                fingerprint = hashlib.sha256(
                    json.dumps(
                        [
                            repo["id"],
                            repo["connection_version"],
                            str(api.id),
                            api.version,
                            api.api_origin,
                            api.organization,
                            repo["platform_repository"],
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                if source.observe_issues or source.observe_ci:
                    raise CoordinationError("CODE_SOURCE_CAPABILITY_UNSUPPORTED")
                return identity, fingerprint
        connection = next(
            (
                item
                for item in entries
                if item.workspace_id == actor.workspace_id
                and item.repository_id == source.repository_id
                and item.remote_id == source.remote_id
            ),
            None,
        )
        if not connection or connection.provider not in {"github", "codeup"}:
            raise CoordinationError("CODE_SOURCE_NOT_CONFIGURED")
        if snapshot:
            selected = next(
                item for item in cast(list[Json], snapshot["repositories"]) if item["id"] == source.repository_id
            )
            if str(selected["git_url"]).removesuffix(".git") != connection.clone_url.removesuffix(".git"):
                raise CoordinationError("REMOTE_BINDING_CHANGED")
        if connection.provider == "github":
            if not app.state.github_tokens:
                raise CoordinationError("CODE_SOURCE_CREDENTIAL_UNAVAILABLE")
            issuer = app.state.github_tokens.comments if reply else app.state.github_tokens.observe
            token = (await issuer(connection.remote_id)).value
        else:
            if source.observe_issues or source.observe_ci:
                raise CoordinationError("CODE_SOURCE_CAPABILITY_UNSUPPORTED")
            token = credentials.get(connection.credential_ref).api_token
        return HostedReviewIdentity(
            cast(Literal["github", "codeup"], connection.provider),
            connection.api_origin,
            connection.platform_repository,
            connection.web_url,
            token,
            connection.organization,
        ), connection.fingerprint

    app.state.repository_identity = repository_identity

    async def repository_reply_identity(actor: RunActor, config: Config) -> tuple[HostedReviewIdentity, str]:
        return await repository_identity(actor, config, reply=True)

    app.state.repository_reply_identity = repository_reply_identity

    async def repository_options(actor: ContextActor) -> list[Json]:
        """部署连接只公开同工作空间中的目标选择，令牌引用及网络地址不进入面板。"""
        return [
            {
                "repository_id": item.repository_id,
                "remote_id": item.remote_id,
                "project_id": None,
                "provider": item.provider,
                "label": item.repository_id,
            }
            for item in entries
            if item.workspace_id == actor.workspace_id and item.provider in {"github", "codeup"}
        ]

    app.state.repository_options = repository_options
    app.state.sandbox = sandbox
    tool_store = SqlAlchemyToolStore(sessions)
    gateway_access = GatewayModuleAccess(context_access, executions, context)
    gateway = ToolGateway(tool_store, gateway_access)
    repository = ReadOnlyRepository(
        Path(settings.readonly_repository_path) if settings.readonly_repository_path else None
    )

    async def read_repository(
        scope: ToolScope, arguments: Json, *, operation: Literal["read", "list", "find", "search"] = "read"
    ) -> Json:
        detail = await app.state.run_service.access_metadata(scope.actor, scope.run_id)
        if detail and detail.task.project_context:
            if detail.task.project_context.get("read_protocol") != "git-objects-v1":
                return failure("PROJECT_REPOSITORY_NOT_PREPARED")
            raw_id = arguments.get("repository_id")
            if not isinstance(raw_id, str):
                return failure("REPOSITORY_SELECTION_REQUIRED")
            try:
                return await cast(ProjectService, app.state.project_service).read_file(
                    ProjectActor(scope.actor.user_id, scope.actor.workspace_id),
                    scope.run_id,
                    detail.task.project_context,
                    UUID(raw_id),
                    str(arguments.get("path", "")),
                    operation=operation,
                    term=str(arguments.get("query", arguments.get("pattern", ""))),
                )
            except ValueError:
                return failure("TOOL_ARGUMENTS_INVALID")
            except ProjectError as error:
                return failure(error.code)
        if operation != "read" or scope.actor.workspace_id != settings.readonly_repository_workspace_id:
            return failure("REPOSITORY_NOT_CONFIGURED")
        return await repository.read(scope, arguments)

    gateway.register(READ_FILE, read_repository)
    discovery_tools: tuple[tuple[ToolSpec, Literal["list", "find", "search"]], ...] = (
        (LIST_DIRECTORY, "list"), (FIND_FILES, "find"), (SEARCH_TEXT, "search")
    )
    for spec, operation in discovery_tools:
        gateway.register(spec, partial(read_repository, operation=operation))
    if context is not None:
        context_tools = ContextTools(context)
        for tool in context_tools.specs:

            async def handler(scope: ToolScope, arguments: Json, *, name: str = tool.name) -> Json:
                current = await context_access.scope(
                    # 身份仅来自 Worker 构造的 ToolScope，模型无法注入。
                    ContextActor(scope.actor.user_id, scope.actor.workspace_id),
                    scope.run_id,
                )
                return await context_tools.execute(current, name, arguments)

            gateway.register(ToolSpec(tool.name, tool.description, tool.input_schema), handler)
        if settings.feishu_agent_cli_executable:
            agent_cli = FeishuAgentCli(settings.feishu_agent_cli_executable)

            async def execute_feishu_cli(scope: ToolScope, arguments: Json) -> Json:
                try:
                    current = await context_access.scope(
                        ContextActor(scope.actor.user_id, scope.actor.workspace_id), scope.run_id
                    )
                    if current.audience != "group" or current.source_kind != "feishu" or current.source_id is None:
                        return failure("FEISHU_RUN_HAS_NO_BOUND_BOT")
                    _, connection, _ = await context._authorize(current, current.source_id)
                    return await agent_cli.execute(
                        arguments, str(connection["app_id"]), lambda: reader._reader()._token(connection)
                    )
                except ContextError as error:
                    return failure(error.code)

            gateway.register(FEISHU_AGENT_CLI, execute_feishu_cli)
            gateway.register(FEISHU_CLI_SKILL, read_feishu_cli_skill)
    register_delivery_tools(gateway, delivery, native_tools=settings.sandbox_native_tools_enabled)
    if app.state.openbot is not None and settings.computer_browser_origins:
        register_computer_tools(
            gateway,
            gateway_access,
            app.state.run_service,
            app.state.openbot,
            settings.computer_browser_origins,
            settings.computer_feishu_scopes,
            delivery,
            app.state.computer_credentials,
        )
    app.state.tool_store = tool_store
    app.state.tool_gateway = gateway
