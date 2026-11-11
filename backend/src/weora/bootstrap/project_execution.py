"""项目与交付的显式装配桥；只走应用接口，不跨模块查询 ORM 或让模型指定宿主路径。"""

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr

from weora.modules.delivery.application.code_hosting import (
    ConnectionBindings,
    PublishedBranch,
    RepositoryConnection,
)
from weora.modules.delivery.application.ports import RemoteResult, ReviewReference
from weora.modules.delivery.domain.models import DeliveryError, PlanScope, RemoteRecord
from weora.modules.delivery.infrastructure.codeup_existing_review import CodeupExistingReviews
from weora.modules.delivery.infrastructure.docker import DockerSandbox, RepositorySnapshot
from weora.modules.delivery.infrastructure.git import (
    CliGitPublisher,
    ConfiguredCredentials,
    CredentialMaterial,
    IsolatedGitRunner,
)
from weora.modules.projects.code_sources import CodeSourceService
from weora.modules.projects.domain import ProjectActor, ProjectError, RepositorySource, git_address
from weora.modules.projects.git_reader import GitRepositoryReader
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService


class ProjectExecution:
    def __init__(
        self,
        projects: ProjectService,
        runs: RunService,
        sandbox: DockerSandbox,
        *,
        enabled: bool,
        legacy_bindings: ConnectionBindings | None = None,
        image: str | None = None,
        review_connections: tuple[RepositoryConnection, ...] = (),
        review_provider: CodeupExistingReviews | None = None,
        code_sources: CodeSourceService | None = None,
    ) -> None:
        self.projects, self.runs, self.sandbox, self.enabled = projects, runs, sandbox, enabled
        self.legacy_bindings = legacy_bindings
        self.image = image
        self.review_connections, self.review_provider = review_connections, review_provider
        self.code_sources = code_sources

    async def review_identity(self, actor: RunActor, reference: ReviewReference) -> tuple[RepositoryConnection, str]:
        """只读评审沿用本任务冻结的仓库和 API 身份；不把令牌交给协调领域或模型。"""
        snapshot = await self._context(actor, reference.run_id)
        if not snapshot or not await self.projects.authorize_snapshot(
            ProjectActor(actor.user_id, actor.workspace_id), snapshot
        ):
            raise DeliveryError("PROJECT_ACCESS_REVOKED", "项目评审来源已失效")
        connection = await self._review_connection(actor, snapshot, reference.scope)
        if reference.scope.remote_binding != connection.fingerprint:
            raise DeliveryError("REMOTE_BINDING_CHANGED", "原评审连接已经变更")
        adapter = await self._review_adapter(actor, snapshot, reference.scope)
        token = adapter.credentials.get(connection.credential_ref).api_token
        return connection, token

    async def _review_connection(
        self, actor: RunActor, snapshot: dict[str, object], scope: PlanScope
    ) -> RepositoryConnection:
        project = self._connection(actor, snapshot, scope)
        repos = snapshot["repositories"]
        assert isinstance(repos, list)
        repo = next(r for r in repos if r["id"] == scope.repository_id)
        if repo.get("code_source_id"):
            if self.code_sources is None:
                raise DeliveryError("PROJECT_REVIEW_NOT_CONFIGURED", "自助代码源服务未装配，不能回退到其他 API 身份")
            try:
                identity = await self.code_sources.identity(
                    ProjectActor(actor.user_id, actor.workspace_id),
                    UUID(str(repo["code_source_id"])),
                    int(str(repo.get("code_source_version", 0))),
                )
            except (ValueError, ProjectError):
                raise DeliveryError("REMOTE_BINDING_CHANGED", "MR API 身份已变更，请重新提案和审批") from None
            return replace(
                project,
                api_origin=identity.api_origin,
                organization=identity.organization,
                platform_repository=str(repo["platform_repository"]),
                credential_ref="code-source-" + str(identity.id),
                version=project.version + ":source:" + str(identity.version),
            )
        for connection in self.review_connections:
            if (
                connection.workspace_id == actor.workspace_id
                and connection.repository_id == scope.repository_id
                and connection.remote_id == scope.remote_id
                and connection.provider == "codeup"
                and git_address(connection.clone_url) == project.clone_url
                and scope.target_branch in connection.target_branches
            ):
                return connection
        raise DeliveryError("PROJECT_REVIEW_NOT_CONFIGURED", "本项目仓库尚未登记匹配的 MR API 连接")

    async def _review_adapter(
        self, actor: RunActor, snapshot: dict[str, object], scope: PlanScope
    ) -> CodeupExistingReviews:
        """每次动作从应用端口短时读取身份，不缓存、不给模型、不给沙箱。"""
        repos = snapshot["repositories"]
        assert isinstance(repos, list)
        repo = next(r for r in repos if r["id"] == scope.repository_id)
        if repo.get("code_source_id"):
            if self.code_sources is None:
                raise DeliveryError("PROJECT_REVIEW_NOT_CONFIGURED", "自助代码源服务未装配，不能回退到其他 API 身份")
            try:
                identity = await self.code_sources.identity(
                    ProjectActor(actor.user_id, actor.workspace_id),
                    UUID(str(repo["code_source_id"])),
                    int(str(repo["code_source_version"])),
                )
            except (ValueError, ProjectError):
                raise DeliveryError("REMOTE_BINDING_CHANGED", "MR API 连接已变化") from None
            return CodeupExistingReviews(
                ConfiguredCredentials(
                    {"code-source-" + str(identity.id): SecretStr(json.dumps({"api_token": identity.token}))}
                )
            )
        if self.review_provider is None:
            raise DeliveryError("PROJECT_REVIEW_NOT_CONFIGURED", "项目 MR 适配器不可用")
        return self.review_provider

    def _image(self, snapshot: dict[str, object], scope: PlanScope) -> str:
        repos = snapshot["repositories"]
        assert isinstance(repos, list)
        repo: dict[str, object] = next((r for r in repos if r["id"] == scope.repository_id), {})
        build = repo.get("build_config")
        if isinstance(build, dict):
            profile = self.projects.build_profiles.get(str(build.get("profile")))
            if profile is None or hashlib.sha256(profile[1].encode()).hexdigest() != repo.get(
                "build_profile_fingerprint"
            ):
                raise DeliveryError("BUILD_PROFILE_CHANGED", "构建环境已变化，请重新发起任务")
            return profile[1]
        return self.image or self.sandbox._config().image

    def _connection(self, actor: RunActor, snapshot: dict[str, object], scope: PlanScope) -> RepositoryConnection:
        repos = snapshot["repositories"]
        assert isinstance(repos, list)
        repo = next((r for r in repos if r["id"] == scope.repository_id), None)
        if (
            repo is None
            or scope.remote_id != "project-" + scope.repository_id
            or scope.target_branch != repo["default_branch"]
        ):
            raise DeliveryError("REMOTE_DENIED", "远端和目标分支必须来自本次选中仓库")
        url = git_address(repo["git_url"])
        origin = "https://" + (urlsplit(url).hostname or "")
        return RepositoryConnection(
            actor.workspace_id,
            scope.remote_id,
            "v" + str(repo["connection_version"]) + ":image:" + self._image(snapshot, scope),
            repo["provider"],
            scope.repository_id,
            scope.repository_id,
            url,
            origin,
            url.removesuffix(".git"),
            "project-" + scope.repository_id + "-v" + str(repo["connection_version"]),
            (str(scope.target_branch),),
        )

    async def bind(self, actor: RunActor, run_id: UUID, scope: PlanScope) -> PlanScope:
        snapshot = await self._context(actor, run_id)
        if snapshot is None:
            return self.legacy_bindings.freeze(actor.workspace_id, scope) if self.legacy_bindings else scope
        if "review.create" in scope.actions:
            return replace(scope, remote_binding=(await self._review_connection(actor, snapshot, scope)).fingerprint)
        if {"git.push", "pr.create"} & set(scope.actions):
            return replace(scope, remote_binding=self._connection(actor, snapshot, scope).fingerprint)
        return scope

    async def _context(self, actor: RunActor, run_id: UUID) -> dict[str, object] | None:
        detail = await self.runs.access_metadata(actor, run_id)
        if detail is None:
            raise DeliveryError("RUN_NOT_FOUND", "运行不存在或不可访问")
        return detail.task.project_context

    async def validate(self, actor: RunActor, run_id: UUID, scope: PlanScope) -> None:
        snapshot = await self._context(actor, run_id)
        if snapshot is None:
            return  # 无项目任务继续走原部署快照，不改变已有验收路径。
        if not self.enabled:
            raise DeliveryError("PROJECT_REPOSITORY_NOT_PREPARED", "尚未启用项目仓库隔离执行")
        if "pr.create" in scope.actions:
            raise DeliveryError(
                "PROJECT_REVIEW_NOT_CONFIGURED", "项目 MR API 身份尚未配置，Git/SSH 密钥不能代替 API 凭据"
            )
        if "git.push" in scope.actions and scope.remote_binding != self._connection(actor, snapshot, scope).fingerprint:
            raise DeliveryError("REMOTE_BINDING_CHANGED", "项目连接已变化，需要重新提案和审批")
        if "review.create" in scope.actions and (
            scope.remote_binding != (await self._review_connection(actor, snapshot, scope)).fingerprint
        ):
            raise DeliveryError("REMOTE_BINDING_CHANGED", "普通 MR 的 API 连接已变化，请重新审批")
        try:
            await self.projects.validate_revision(
                ProjectActor(actor.user_id, actor.workspace_id),
                run_id,
                snapshot,
                UUID(scope.repository_id),
                scope.base_revision,
            )
        except ValueError:
            raise DeliveryError("REPOSITORY_NOT_SELECTED", "项目执行必须选择本次登记仓库") from None
        except ProjectError as error:
            raise DeliveryError(error.code, error.message) from None

    async def prepare(self, actor: RunActor, run_id: UUID, scope: PlanScope) -> bool:
        await self.validate(actor, run_id, scope)
        snapshot = await self._context(actor, run_id)
        if snapshot is None:
            return False
        if self.sandbox.has_bound_snapshot(run_id, scope):
            await self.sandbox.prepare(run_id, scope)
            return True
        try:
            archive = await self.projects.export_archive(
                ProjectActor(actor.user_id, actor.workspace_id),
                run_id,
                snapshot,
                UUID(scope.repository_id),
                scope.base_revision,
            )
        except ProjectError as error:
            raise DeliveryError(error.code, error.message) from None
        try:
            self.sandbox.bind_snapshot(
                run_id,
                scope,
                RepositorySnapshot(archive.path, archive.revision, archive.sha256),
                image=self._image(snapshot, scope),
            )
            await self.sandbox.prepare(run_id, scope)
            await self.validate(actor, run_id, scope)
        finally:
            # 归档是后台本次生成的临时文件，容器成功解包后不保留额外正文副本。
            await asyncio.to_thread(archive.path.unlink, missing_ok=True)
        return True

    @staticmethod
    def _published(source: RemoteRecord) -> PublishedBranch:
        # 业务键及续写来源归属由 DeliveryService 校验；这里解析已批准的不可变提交事实。
        fields = (source.remote_id or "").split(":")
        if (
            len(fields) != 3
            or fields[0] != "branch"
            or not re.fullmatch(r"weora/[0-9a-f]{24}", fields[1])
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[2])
        ):
            raise DeliveryError("BRANCH_REQUIRED", "分支交付元数据不完整")
        return PublishedBranch(fields[1], fields[2])

    async def review(
        self, actor: RunActor, run_id: UUID, scope: PlanScope, source: RemoteRecord, key: str, title: str, body: str
    ) -> RemoteResult:
        """只为数据库已确认且远端未漂移的测试分支创建 MR，完全不调用 GitPublisher。"""
        await self.validate(actor, run_id, scope)
        snapshot = await self._context(actor, run_id)
        if snapshot is None or not isinstance(self.projects.reader, GitRepositoryReader):
            raise DeliveryError("PROJECT_REVIEW_NOT_CONFIGURED", "项目 MR 适配器不可用")
        connection, branch = await self._review_connection(actor, snapshot, scope), self._published(source)
        provider = await self._review_adapter(actor, snapshot, scope)
        selected = snapshot["repositories"]
        assert isinstance(selected, list)
        if any(repo.get("code_source_id") and repo["id"] == scope.repository_id for repo in selected):
            await provider.verify_repository(connection)
        try:
            git_source = await self.projects.execution_connection(
                ProjectActor(actor.user_id, actor.workspace_id),
                run_id,
                snapshot,
                UUID(scope.repository_id),
                scope.base_revision,
            )
            before = await self.projects.reader.check(git_source)
            if await self.projects.reader.check(replace(git_source, branch=branch.name)) != branch.revision:
                raise DeliveryError("BRANCH_CHANGED", "已交付分支发生变化，需要重新测试和审批")
            result = await provider.create_existing(connection, scope, branch, key, title, body)
            if (
                await self.projects.reader.check(git_source) != before
                or await self.projects.reader.check(replace(git_source, branch=branch.name)) != branch.revision
            ):
                raise DeliveryError("BRANCH_CHANGED", "创建期间分支发生变化，MR 结果需要人工核对")
            return result
        except ProjectError as error:
            raise DeliveryError(error.code, error.message) from None

    async def reconcile_review(
        self, actor: RunActor, run_id: UUID, scope: PlanScope, source: RemoteRecord, key: str
    ) -> RemoteResult | None:
        await self.validate(actor, run_id, scope)
        snapshot = await self._context(actor, run_id)
        if snapshot is None:
            raise DeliveryError("PROJECT_REVIEW_NOT_CONFIGURED", "项目 MR 适配器不可用")
        provider = await self._review_adapter(actor, snapshot, scope)
        return await provider.reconcile_existing(
            await self._review_connection(actor, snapshot, scope), scope, self._published(source), key
        )

    async def push(self, actor: RunActor, run_id: UUID, scope: PlanScope, key: str, title: str) -> RemoteResult:
        return await self._push(actor, run_id, scope, key, title)

    async def update(
        self, actor: RunActor, run_id: UUID, scope: PlanScope, source: RemoteRecord, key: str, title: str
    ) -> RemoteResult:
        """旧交付记录与新审批共同授权续写；精确 SHA 漂移时发布器拒绝覆盖。"""
        return await self._push(actor, run_id, scope, key, title, previous=self._published(source))

    async def _push(
        self,
        actor: RunActor,
        run_id: UUID,
        scope: PlanScope,
        key: str,
        title: str,
        *,
        previous: PublishedBranch | None = None,
    ) -> RemoteResult:
        """只在受控后台发布批准 Diff；Git 密码绝不进入容器、模型或命令参数。"""
        await self.validate(actor, run_id, scope)
        snapshot = await self._context(actor, run_id)
        if snapshot is None or not isinstance(self.projects.reader, GitRepositoryReader):
            raise DeliveryError("REMOTE_DENIED", "项目分支发布器不接受默认测试仓库")
        connection = self._connection(actor, snapshot, scope)
        try:
            source = await self.projects.execution_connection(
                ProjectActor(actor.user_id, actor.workspace_id),
                run_id,
                snapshot,
                UUID(scope.repository_id),
                scope.base_revision,
            )
            before = await self.projects.reader.check(source)
            if before != scope.base_revision:
                raise DeliveryError("BASE_REVISION_CHANGED", "目标分支已前进，请刷新基准并重新审批")
            publisher = CliGitPublisher(
                self.sandbox,
                _ProjectCredentials(connection.credential_ref, source),
                _ProjectTransport(self.projects.reader, source, snapshot.get("git_identity")),
            )
            published = await publisher.publish(run_id, scope, connection, key, title, previous=previous)
            after = await self.projects.reader.check(source)
            if after != before:
                raise DeliveryError("PROTECTED_BRANCH_CHANGED", "核对期间目标分支发生变化，需要人工检查")
        except ProjectError as error:
            raise DeliveryError(error.code, error.message) from None
        return RemoteResult("branch:" + published.name + ":" + published.revision, connection.web_url)


class _ProjectCredentials:
    """只给本次连接提供短时 Git 身份，不借用个人登录态或 API 令牌。"""

    def __init__(self, reference: str, source: RepositorySource) -> None:
        self.reference, self.source = reference, source

    def get(self, reference: str) -> CredentialMaterial:
        if reference != self.reference:
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "凭据不属于本次项目连接")
        return CredentialMaterial(username=self.source.username, password=self.source.token)


class _ProjectTransport(IsolatedGitRunner):
    def __init__(self, reader: GitRepositoryReader, source: RepositorySource, identity: object = None) -> None:
        super().__init__()
        self.reader, self.source = reader, source
        self.identity = identity if isinstance(identity, dict) else {}

    async def run(
        self, cwd: Path, env: Mapping[str, str], *args: str,
        stdin: bytes | None = None, success_codes: tuple[int, ...] = (0,),
    ) -> bytes:
        if args[0] in {"fetch", "push", "ls-remote"}:
            # 复用已验收的 DNS 固定、精确认证作用域、私网/端口和 TLS 规则。
            checked = await self.reader._environment(cwd, self.source)
            env = dict(env) | checked
        if args[0] == "commit-tree" and self.identity:
            # 署名来自 Task 创建时的可信快照，不从模型工具参数或当前设置取得。
            name, email = str(self.identity.get("name", "")), str(self.identity.get("email", ""))
            if not name or not email or any(character in name + email for character in "\r\n\x00<>"):
                raise DeliveryError("GIT_IDENTITY_INVALID", "任务固定的提交身份无效")
            env = dict(env) | {
                "GIT_AUTHOR_NAME": name,
                "GIT_COMMITTER_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_EMAIL": email,
            }
            co_author = str(self.identity.get("co_author", ""))
            if co_author:
                if any(character in co_author for character in "\r\n\x00"):
                    raise DeliveryError("GIT_IDENTITY_INVALID", "协作署名无效")
                stdin = (stdin or b"").rstrip() + ("\n\nCo-authored-by: " + co_author + "\n").encode()
        return await super().run(cwd, env, *args, stdin=stdin, success_codes=success_codes)
