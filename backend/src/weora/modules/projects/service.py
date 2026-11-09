"""开发者维护自己的团队项目；共享资料不隐式共享凭据或赋予执行权限。"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.projects.domain import (
    ProjectActor,
    ProjectError,
    ProjectMembers,
    ProjectSecrets,
    RepositoryArchive,
    RepositoryArchiveExporter,
    RepositoryDirectoryReader,
    RepositoryReader,
    RepositorySource,
    git_address,
)
from weora.modules.projects.persistence import CodeSourceModel, ProjectModel, ProjectRepositoryModel, RunRepositoryModel
from weora.shared.infrastructure.database import SessionFactory


def project_view(row: ProjectModel, can_manage: bool) -> dict[str, object]:
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "architecture": row.architecture,
        "conventions": row.conventions,
        "status": row.status,
        "version": row.version,
        "can_manage": can_manage,
        "updated_at": row.updated_at.isoformat(),
    }


def repository_view(row: ProjectRepositoryModel, can_manage: bool) -> dict[str, object]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "purpose": row.purpose,
        "provider": row.provider,
        "git_url": git_address(row.git_url),
        "default_branch": row.default_branch,
        "technology": row.technology,
        "development_notes": row.development_notes,
        "status": row.status,
        "version": row.version,
        "has_credential": row.credential_ciphertext is not None if can_manage else False,
        "credential_username": row.credential_username if can_manage else "",
        "connection_version": row.connection_version,
        "connection_check": row.connection_check,
        "connection_status": (row.connection_check or {}).get("status", "registered"),
        "execution_status": "not_prepared",
        "code_source_id": str(row.code_source_id) if row.code_source_id else None,
        "platform_repository": row.platform_repository,
        "build_config": row.build_config,
    }


class ProjectService:
    def __init__(
        self,
        sessions: SessionFactory,
        members: ProjectMembers,
        secrets: ProjectSecrets | None = None,
        reader: RepositoryReader | None = None,
    ) -> None:
        self.sessions, self.members, self.secrets = sessions, members, secrets
        self.reader = reader
        # 管理员在装配层登记镜像，普通用户只能选择 ID，不能输入宿主路径或任意镜像。
        self.build_profiles: dict[str, tuple[str, str]] = {}

    async def _role(self, actor: ProjectActor) -> str:
        role = await self.members.role(actor)
        if role is None:
            raise ProjectError("PROJECT_NOT_FOUND", "项目不存在或无权访问")
        return role

    async def _project(self, session: AsyncSession, actor: ProjectActor, project_id: UUID) -> ProjectModel:
        row = await session.scalar(
            # 项目作为聚合锁：仓库增删状态与资料快照必须看到同一个版本。
            select(ProjectModel)
            .where(ProjectModel.id == project_id, ProjectModel.workspace_id == actor.workspace_id)
            .with_for_update()
        )
        if row is None:
            raise ProjectError("PROJECT_NOT_FOUND", "项目不存在或无权访问")
        return row

    @staticmethod
    def _manage(actor: ProjectActor, role: str, row: ProjectModel) -> bool:
        return role in {"owner", "admin"} or row.created_by == actor.user_id

    async def list_projects(self, actor: ProjectActor) -> dict[str, object]:
        role = await self._role(actor)
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ProjectModel)
                    .where(ProjectModel.workspace_id == actor.workspace_id)
                    .order_by(ProjectModel.updated_at.desc())
                    .limit(200)
                )
            ).all()
            return {"items": [project_view(row, self._manage(actor, role, row)) for row in rows]}

    async def detail(self, actor: ProjectActor, project_id: UUID) -> dict[str, object]:
        role = await self._role(actor)
        async with self.sessions() as session:
            row = await self._project(session, actor, project_id)
            manage = self._manage(actor, role, row)
            repos = (
                await session.scalars(
                    select(ProjectRepositoryModel)
                    .where(
                        ProjectRepositoryModel.project_id == project_id,
                        ProjectRepositoryModel.workspace_id == actor.workspace_id,
                    )
                    .order_by(ProjectRepositoryModel.created_at)
                )
            ).all()
            source_ids = [repo.code_source_id for repo in repos if repo.code_source_id]
            sources = (
                (
                    await session.scalars(
                        select(CodeSourceModel).where(
                            CodeSourceModel.workspace_id == actor.workspace_id, CodeSourceModel.id.in_(source_ids)
                        )
                    )
                ).all()
                if source_ids
                else []
            )
            available_sources = {
                source.id: source for source in sources if source.enabled and source.credential_ciphertext
            }

            def source_version(repo: ProjectRepositoryModel) -> int | None:
                source = available_sources.get(repo.code_source_id) if repo.code_source_id else None
                address = urlsplit(git_address(repo.git_url))
                # 组织变更后，即使是新任务也不能将旧仓库静默绑定到新组织的 API 身份。
                if (
                    source
                    and repo.provider == "codeup"
                    and address.hostname == "codeup.aliyun.com"
                    and address.path.startswith("/" + source.organization + "/")
                    and repo.platform_repository
                ):
                    return source.version
                return None

            return project_view(row, manage) | {
                "repositories": [
                    repository_view(repo, manage) | {"code_source_version": source_version(repo)} for repo in repos
                ]
            }

    async def workspace_repositories(self, actor: ProjectActor) -> dict[str, object]:
        """配置总览只读取当前工作空间，不用前端逐项目请求拼出仓库列表。"""
        role = await self._role(actor)
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ProjectRepositoryModel, ProjectModel)
                    .join(ProjectModel, ProjectRepositoryModel.project_id == ProjectModel.id)
                    .where(
                        ProjectRepositoryModel.workspace_id == actor.workspace_id,
                        ProjectModel.workspace_id == actor.workspace_id,
                    )
                    .order_by(ProjectModel.name, ProjectRepositoryModel.name)
                    .limit(201)
                )
            ).all()
            return {
                "items": [
                    repository_view(repo, self._manage(actor, role, project))
                    | {
                        "project_name": project.name,
                        "project_status": project.status,
                        "can_manage": self._manage(actor, role, project),
                    }
                    for repo, project in rows[:200]
                ],
                "truncated": len(rows) > 200,
            }

    async def create(self, actor: ProjectActor, values: dict[str, object]) -> dict[str, object]:
        await self._role(actor)
        now = datetime.now(UTC)
        row = ProjectModel(
            id=uuid4(),
            workspace_id=actor.workspace_id,
            created_by=actor.user_id,
            **values,
            status="active",
            version=1,
            created_at=now,
            updated_at=now,
        )
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
            return project_view(row, True) | {"repositories": []}

    async def edit(
        self, actor: ProjectActor, project_id: UUID, version: int, values: dict[str, object]
    ) -> dict[str, object]:
        role = await self._role(actor)
        async with self.sessions() as session:
            row = await self._project(session, actor, project_id)
            if not self._manage(actor, role, row):
                raise ProjectError("PROJECT_FORBIDDEN", "只有项目创建者或工作空间管理员可以修改")
            result = await session.execute(
                update(ProjectModel)
                .where(
                    ProjectModel.id == project_id,
                    ProjectModel.version == version,
                    ProjectModel.workspace_id == actor.workspace_id,
                )
                .values(**values, version=version + 1, updated_at=datetime.now(UTC))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise ProjectError("PROJECT_CHANGED", "项目已被修改，请刷新后重试")
            await session.commit()
        return await self.detail(actor, project_id)

    async def save_repository(
        self,
        actor: ProjectActor,
        project_id: UUID,
        values: dict[str, object],
        *,
        repository_id: UUID | None = None,
        version: int | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        role = await self._role(actor)
        values = dict(values, git_url=git_address(str(values["git_url"])))
        if token is not None:
            if self.secrets is None:
                raise ProjectError("PROJECT_SECRETS_UNAVAILABLE", "尚未配置平台加密密钥，不能保存凭据")
            values["credential_ciphertext"] = self.secrets.encrypt(token) if token else None
        now = datetime.now(UTC)
        async with self.sessions() as session:
            project = await self._project(session, actor, project_id)
            if not self._manage(actor, role, project):
                raise ProjectError("PROJECT_FORBIDDEN", "只有项目创建者或工作空间管理员可以配置仓库")
            if project.status != "active":
                raise ProjectError("PROJECT_ARCHIVED", "项目已归档，请先恢复")
            build = values.get("build_config")
            if isinstance(build, dict) and build.get("profile") not in self.build_profiles:
                raise ProjectError("BUILD_PROFILE_UNAVAILABLE", "所选构建环境未由管理员配置")
            source_id = values.get("code_source_id")
            if source_id is not None:
                source = await session.scalar(
                    select(CodeSourceModel)
                    .where(CodeSourceModel.id == source_id, CodeSourceModel.workspace_id == actor.workspace_id)
                    .with_for_update()
                )
                address = urlsplit(str(values["git_url"]))
                if (
                    source is None
                    or not source.enabled
                    or not source.credential_ciphertext
                    or values.get("provider") != "codeup"
                    or address.hostname != "codeup.aliyun.com"
                    or not address.path.startswith("/" + source.organization + "/")
                    or not values.get("platform_repository")
                ):
                    raise ProjectError("INVALID_CODE_SOURCE", "请选择同组织的活动云效连接，并填写平台仓库数字 ID")
            elif values.get("platform_repository"):
                raise ProjectError("INVALID_CODE_SOURCE", "平台仓库 ID 必须同时选择 MR API 连接")
            # 旧库可能保存云效网页地址；等价的 .git 地址也属于同仓，聚合锁内先做规范化判重。
            registered = (
                await session.execute(
                    select(ProjectRepositoryModel.id, ProjectRepositoryModel.git_url).where(
                        ProjectRepositoryModel.project_id == project_id,
                        ProjectRepositoryModel.workspace_id == actor.workspace_id,
                    )
                )
            ).all()
            if any(key != repository_id and git_address(url) == values["git_url"] for key, url in registered):
                raise ProjectError("REPOSITORY_EXISTS", "该项目已登记这个 Git 地址")
            if repository_id is None:
                count = await session.scalar(
                    select(func.count())
                    .select_from(ProjectRepositoryModel)
                    .where(ProjectRepositoryModel.project_id == project_id)
                )
                if (count or 0) >= 20:
                    raise ProjectError("PROJECT_LIMIT", "一个项目最多登记 20 个仓库")
                row = ProjectRepositoryModel(
                    id=uuid4(),
                    project_id=project_id,
                    workspace_id=actor.workspace_id,
                    created_by=actor.user_id,
                    **values,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                previous = await session.get(ProjectRepositoryModel, repository_id)
                if previous is None or previous.project_id != project_id or previous.workspace_id != actor.workspace_id:
                    raise ProjectError("PROJECT_CHANGED", "仓库不存在或已被修改")
                if (
                    git_address(previous.git_url) != values["git_url"]
                    or previous.provider != values["provider"]
                    or previous.default_branch != values["default_branch"]
                    or previous.credential_username != values.get("credential_username", previous.credential_username)
                    or token is not None
                    or previous.code_source_id != values.get("code_source_id")
                    or previous.platform_repository != values.get("platform_repository", "")
                    or previous.build_config != values.get("build_config")
                ):
                    values["connection_version"] = previous.connection_version + 1
                    values["connection_check"] = None
                    # 换仓库地址不能携带旧令牌请求新目标；必须明确提供新凭据。
                    if git_address(previous.git_url) != values["git_url"] and token is None:
                        values["credential_ciphertext"] = None
                try:
                    result = await session.execute(
                        update(ProjectRepositoryModel)
                        .where(
                            ProjectRepositoryModel.id == repository_id,
                            ProjectRepositoryModel.project_id == project_id,
                            ProjectRepositoryModel.workspace_id == actor.workspace_id,
                            ProjectRepositoryModel.version == version,
                        )
                        .values(**values, version=(version or 0) + 1, updated_at=now)
                    )
                except IntegrityError:
                    raise ProjectError("REPOSITORY_EXISTS", "该项目已登记这个 Git 地址") from None
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    raise ProjectError("PROJECT_CHANGED", "仓库不存在或已被修改，请刷新后重试")
            # 仓库变动也推进项目版本，形成可见的项目资料快照边界。
            # 新增仓库的唯一冲突统一留在 commit 处理，不能在这里隐式 flush 成为 500。
            with session.no_autoflush:
                await session.execute(
                    update(ProjectModel)
                    .where(ProjectModel.id == project_id)
                    .values(version=ProjectModel.version + 1, updated_at=now)
                )
            try:
                await session.commit()
            except IntegrityError:
                raise ProjectError("REPOSITORY_EXISTS", "该项目已登记这个 Git 地址") from None
        return await self.detail(actor, project_id)

    async def snapshot(
        self, actor: ProjectActor, project_id: UUID, repository_ids: tuple[UUID, ...]
    ) -> dict[str, object]:
        detail = await self.detail(actor, project_id)
        if detail["status"] != "active" or not repository_ids or len(set(repository_ids)) != len(repository_ids):
            raise ProjectError("PROJECT_SELECTION_INVALID", "请选择活动项目内至少一个不重复的仓库")
        repos = detail["repositories"]
        assert isinstance(repos, list)
        selected = [repo for repo in repos if UUID(repo["id"]) in repository_ids and repo["status"] == "active"]
        if len(selected) != len(repository_ids):
            raise ProjectError("PROJECT_SELECTION_INVALID", "仓库不属于当前项目或已停用")
        if any(repo.get("code_source_id") and repo.get("code_source_version") is None for repo in selected):
            raise ProjectError("CODE_SOURCE_CHANGED", "仓库关联的 MR API 身份已失效，请重新配置或取消关联")
        # 严格白名单：凭据、作者身份、未选仓库及默认执行仓库都不进入模型快照。
        public_repos = [
            {
                key: repo[key]
                for key in (
                    "id",
                    "name",
                    "purpose",
                    "provider",
                    "git_url",
                    "default_branch",
                    "technology",
                    "development_notes",
                    "version",
                    "execution_status",
                    "connection_version",
                    "code_source_id",
                    "code_source_version",
                    "platform_repository",
                    "build_config",
                )
            }
            for repo in selected
        ]
        snapshot = {
            "project_id": detail["id"],
            "name": detail["name"],
            "version": detail["version"],
            "description": detail["description"],
            "architecture": detail["architecture"],
            "conventions": detail["conventions"],
            "repositories": public_repos,
        }
        for repo in public_repos:
            build = repo.get("build_config")
            if isinstance(build, dict):
                configured = self.build_profiles.get(str(build["profile"]))
                if configured is None:
                    raise ProjectError("BUILD_PROFILE_UNAVAILABLE", "构建环境已下线，请重新配置仓库")
                repo["build_profile_fingerprint"] = hashlib.sha256(configured[1].encode()).hexdigest()
        if self.reader is not None:
            snapshot["read_protocol"] = "git-objects-v1"
            for repo in public_repos:
                repo["execution_status"] = "pending_readonly"
        if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 48000:
            raise ProjectError("PROJECT_CONTEXT_TOO_LARGE", "项目资料超过本次上下文预算，请精简或减少仓库")
        return snapshot

    async def authorize_snapshot(self, actor: ProjectActor, snapshot: dict[str, object]) -> bool:
        try:
            detail = await self.detail(actor, UUID(str(snapshot["project_id"])))
            repos = detail["repositories"]
            selected = snapshot["repositories"]
            assert isinstance(repos, list) and isinstance(selected, list)
            active = {repo["id"]: repo for repo in repos if repo["status"] == "active"}
            return detail["status"] == "active" and all(
                repo["id"] in active
                and (
                    repo.get("connection_version") is None
                    or repo.get("connection_version") == active[repo["id"]]["connection_version"]
                )
                and (
                    not repo.get("code_source_id")
                    or (
                        repo.get("code_source_version") is not None
                        and repo.get("code_source_version") == active[repo["id"]].get("code_source_version")
                    )
                )
                for repo in selected
            )
        except (ProjectError, ValueError, KeyError):
            return False

    async def _source(
        self, actor: ProjectActor, project_id: UUID, repository_id: UUID, expected_version: int
    ) -> RepositorySource:
        await self._role(actor)
        async with self.sessions() as session:
            project = await self._project(session, actor, project_id)
            row = await session.get(ProjectRepositoryModel, repository_id)
            if (
                project.status != "active"
                or row is None
                or row.project_id != project_id
                or row.workspace_id != actor.workspace_id
                or row.status != "active"
                or row.connection_version != expected_version
            ):
                raise ProjectError("PROJECT_ACCESS_REVOKED", "项目或仓库不再可读")
            if row.credential_ciphertext and self.secrets is None:
                raise ProjectError("PROJECT_SECRETS_UNAVAILABLE", "连接凭据不可用")
            token = (
                self.secrets.decrypt(row.credential_ciphertext) if self.secrets and row.credential_ciphertext else ""
            )
            return RepositorySource(
                actor.workspace_id, row.id, git_address(row.git_url), row.default_branch, row.credential_username, token
            )

    async def check_connection(
        self, actor: ProjectActor, project_id: UUID, repository_id: UUID, version: int
    ) -> dict[str, object]:
        role = await self._role(actor)
        async with self.sessions() as session:
            project = await self._project(session, actor, project_id)
            if not self._manage(actor, role, project):
                raise ProjectError("PROJECT_FORBIDDEN", "只有项目创建者或管理员可以校验连接")
            repo = await session.get(ProjectRepositoryModel, repository_id)
            if repo is None or repo.project_id != project_id or repo.version != version:
                raise ProjectError("PROJECT_CHANGED", "仓库资料已变化，请刷新")
            connection_version = repo.connection_version
        if self.reader is None:
            raise ProjectError("REPOSITORY_READ_DISABLED", "管理员尚未配置只读克隆存储")
        source = await self._source(actor, project_id, repository_id, connection_version)
        result: dict[str, object] = {
            "checked_at": datetime.now(UTC).isoformat(),
            "connection_version": connection_version,
        }
        try:
            result |= {"status": "connected", "revision": await self.reader.check(source)}
        except ProjectError as error:
            result |= {"status": "failed", "error_code": error.code, "message": error.message}
        role = await self._role(actor)
        async with self.sessions() as session:
            project = await self._project(session, actor, project_id)
            if project.status != "active" or not self._manage(actor, role, project):
                raise ProjectError("PROJECT_ACCESS_REVOKED", "项目授权已变化")
            changed = await session.execute(
                update(ProjectRepositoryModel)
                .where(
                    ProjectRepositoryModel.id == repository_id,
                    ProjectRepositoryModel.project_id == project_id,
                    ProjectRepositoryModel.connection_version == connection_version,
                    ProjectRepositoryModel.status == "active",
                )
                .values(connection_check=result)
            )
            if changed.rowcount != 1:  # type: ignore[attr-defined]
                raise ProjectError("PROJECT_CHANGED", "校验期间连接已变化，请刷新重试")
            await session.commit()
        return result

    async def run_repositories(self, actor: ProjectActor, run_id: UUID) -> list[dict[str, object]]:
        await self._role(actor)
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(RunRepositoryModel)
                    .where(
                        RunRepositoryModel.run_id == run_id,
                        RunRepositoryModel.workspace_id == actor.workspace_id,
                    )
                    .order_by(RunRepositoryModel.repository_id)
                )
            ).all()
            return [
                {
                    "repository_id": str(row.repository_id),
                    "revision": row.revision,
                    "status": row.status,
                    "error_code": row.error_code,
                }
                for row in rows
            ]

    async def prepare(self, actor: ProjectActor, run_id: UUID, snapshot: dict[str, object]) -> list[dict[str, object]]:
        if snapshot.get("read_protocol") != "git-objects-v1":
            return []  # 历史资料型任务不自动获得新的网络权限。
        if self.reader is None:
            raise ProjectError("REPOSITORY_READ_DISABLED", "只读克隆未配置")
        selected = snapshot["repositories"]
        assert isinstance(selected, list)
        for repo in selected:
            if not await self.authorize_snapshot(actor, snapshot):
                raise ProjectError("PROJECT_ACCESS_REVOKED", "项目或连接授权已变化")
            repository_id = UUID(repo["id"])
            key = (run_id, repository_id)
            source = await self._source(
                actor, UUID(str(snapshot["project_id"])), repository_id, repo["connection_version"]
            )
            async with self.sessions() as session:
                existing = await session.get(RunRepositoryModel, key)
            if existing is None:
                # 首次解析成功的提交成为权威；恢复时不再跟随移动的默认分支。
                revision = await self.reader.check(source)
                async with self.sessions() as session:
                    session.add(
                        RunRepositoryModel(
                            run_id=run_id,
                            repository_id=repository_id,
                            workspace_id=actor.workspace_id,
                            connection_version=repo["connection_version"],
                            revision=revision,
                            status="PREPARING",
                            cache_id=None,
                            error_code=None,
                            created_at=datetime.now(UTC),
                        )
                    )
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                async with self.sessions() as session:
                    existing = await session.get(RunRepositoryModel, key)
            assert existing is not None
            if existing.status == "READY":
                continue
            try:
                cache_id = await self.reader.clone(source, existing.revision)
            except ProjectError as error:
                async with self.sessions() as session:
                    await session.execute(
                        update(RunRepositoryModel)
                        .where(
                            RunRepositoryModel.run_id == run_id,
                            RunRepositoryModel.repository_id == repository_id,
                            RunRepositoryModel.cache_id.is_(None),
                        )
                        .values(status="FAILED", error_code=error.code)
                    )
                    await session.commit()
                raise
            try:
                if not await self.authorize_snapshot(actor, snapshot):
                    raise ProjectError("PROJECT_ACCESS_REVOKED", "克隆期间授权已变化")
                async with self.sessions() as session:
                    changed = await session.execute(
                        update(RunRepositoryModel)
                        .where(
                            RunRepositoryModel.run_id == run_id,
                            RunRepositoryModel.repository_id == repository_id,
                            RunRepositoryModel.cache_id.is_(None),
                            RunRepositoryModel.workspace_id == actor.workspace_id,
                        )
                        .values(cache_id=cache_id, status="READY", error_code=None)
                    )
                    await session.commit()
                    kept = changed.rowcount == 1  # type: ignore[attr-defined]
                if kept:
                    continue
            except BaseException:
                await self.reader.discard(cache_id)
                raise
            await self.reader.discard(cache_id)  # 并发落后者只清理自己的克隆，不覆盖赢家。
        return await self.run_repositories(actor, run_id)

    async def assessment_read(
        self,
        actor: ProjectActor,
        project_id: UUID,
        repository_ids: tuple[UUID, ...],
        repository_id: UUID,
        path: str,
        *,
        revision: str | None = None,
        directory: bool = False,
    ) -> dict[str, object]:
        """接单前的受限只读端口；不创建假 Run，不暴露 URL/凭据，读取完立即回收临时克隆。"""
        snapshot = await self.snapshot(actor, project_id, repository_ids)
        repositories = cast(list[dict[str, object]], snapshot["repositories"])
        selected = next((item for item in repositories if item["id"] == str(repository_id)), None)
        if not selected or self.reader is None:
            raise ProjectError("REPOSITORY_NOT_SELECTED", "评估只能读取当前选中的可读仓库")
        source = await self._source(actor, project_id, repository_id, int(str(selected["connection_version"])))
        fixed_revision = revision or await self.reader.check(source)
        cache = await self.reader.clone(source, fixed_revision)
        try:
            if not await self.authorize_snapshot(actor, snapshot):
                raise ProjectError("PROJECT_ACCESS_REVOKED", "读取前项目授权已变化")
            if directory:
                if not isinstance(self.reader, RepositoryDirectoryReader):
                    raise ProjectError("REPOSITORY_DIRECTORY_UNAVAILABLE", "当前只读适配器不支持目录")
                result = await self.reader.directory(cache, fixed_revision, path)
            else:
                result = await self.reader.read(cache, fixed_revision, path)
            if not await self.authorize_snapshot(actor, snapshot):
                raise ProjectError("PROJECT_ACCESS_REVOKED", "读取期间项目授权已变化")
            return result | {"revision": fixed_revision, "repository_id": str(repository_id)}
        finally:
            await self.reader.discard(cache)

    async def read_file(
        self, actor: ProjectActor, run_id: UUID, snapshot: dict[str, object], repository_id: UUID, path: str
    ) -> dict[str, object]:
        if not await self.authorize_snapshot(actor, snapshot):
            raise ProjectError("PROJECT_ACCESS_REVOKED", "项目或仓库授权已变化")
        selected = snapshot["repositories"]
        assert isinstance(selected, list)
        if str(repository_id) not in {repo["id"] for repo in selected}:
            raise ProjectError("REPOSITORY_NOT_SELECTED", "仓库不在本次任务范围内")
        async with self.sessions() as session:
            row = await session.get(RunRepositoryModel, (run_id, repository_id))
            if (
                row is None
                or row.workspace_id != actor.workspace_id
                or row.status != "READY"
                or row.cache_id is None
                or self.reader is None
            ):
                raise ProjectError("PROJECT_REPOSITORY_NOT_PREPARED", "本次仓库固定提交尚未准备完成")
            result = await self.reader.read(row.cache_id, row.revision, path)
        if not await self.authorize_snapshot(actor, snapshot):
            raise ProjectError("PROJECT_ACCESS_REVOKED", "读取期间授权已变化")
        return result | {"repository_id": str(repository_id)}

    async def validate_revision(
        self, actor: ProjectActor, run_id: UUID, snapshot: dict[str, object], repository_id: UUID, revision: str
    ) -> UUID:
        """审批与每次执行都验证选中集合和固定提交，不因缓存存在跳过撤权检查。"""
        if snapshot.get("read_protocol") != "git-objects-v1" or not await self.authorize_snapshot(actor, snapshot):
            raise ProjectError("PROJECT_ACCESS_REVOKED", "项目、连接或仓库授权已变化")
        selected = snapshot["repositories"]
        assert isinstance(selected, list)
        if str(repository_id) not in {r["id"] for r in selected}:
            raise ProjectError("REPOSITORY_NOT_SELECTED", "执行仓库不在本次选择范围内")
        async with self.sessions() as session:
            row = await session.get(RunRepositoryModel, (run_id, repository_id))
            if row is None or row.workspace_id != actor.workspace_id or row.status != "READY" or row.cache_id is None:
                raise ProjectError("PROJECT_REPOSITORY_NOT_PREPARED", "执行仓库尚未准备固定提交")
            if row.revision != revision:
                raise ProjectError("REPOSITORY_REVISION_MISMATCH", "计划基准与本次固定提交不一致")
            return row.cache_id

    async def export_archive(
        self, actor: ProjectActor, run_id: UUID, snapshot: dict[str, object], repository_id: UUID, revision: str
    ) -> RepositoryArchive:
        """归档能力由可信装配器注入，不把宿主路径当作模型工具参数。"""
        cache_id = await self.validate_revision(actor, run_id, snapshot, repository_id, revision)
        # 读取器端口保持只读能力；仅具体 Git 适配器提供后台归档方法。
        if not hasattr(self.reader, "archive"):
            raise ProjectError("REPOSITORY_ARCHIVE_UNAVAILABLE", "当前仓库适配器不支持沙箱快照")
        archive = await cast(RepositoryArchiveExporter, self.reader).archive(cache_id, revision)
        assert isinstance(archive, RepositoryArchive)
        try:
            await self.validate_revision(actor, run_id, snapshot, repository_id, revision)
        except BaseException:
            # 撤权窗口内也回收本次生成的临时正文，不能等到后续沙箱接收者清理。
            await asyncio.to_thread(archive.path.unlink, missing_ok=True)
            raise
        return archive

    async def execution_connection(
        self, actor: ProjectActor, run_id: UUID, snapshot: dict[str, object], repository_id: UUID, revision: str
    ) -> RepositorySource:
        """执行代理短时取凭据；只允许固定任务中仍有效的连接，返回值不能写入事件。"""
        await self.validate_revision(actor, run_id, snapshot, repository_id, revision)
        selected = snapshot["repositories"]
        assert isinstance(selected, list)
        repo = next(r for r in selected if r["id"] == str(repository_id))
        return await self._source(actor, UUID(str(snapshot["project_id"])), repository_id, repo["connection_version"])
