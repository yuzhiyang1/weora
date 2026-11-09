"""工作空间代码源只管理 MR API 身份；Git 身份继续由项目仓库独立管理。"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from weora.modules.projects.domain import ProjectActor, ProjectError, ProjectMembers, ProjectSecrets, git_address
from weora.modules.projects.persistence import CodeSourceModel, ProjectModel, ProjectRepositoryModel
from weora.shared.infrastructure.database import SessionFactory


def codeup_origin(value: str) -> str:
    """固定官方 HTTPS API 主机，防止把保存的令牌发送到任意用户地址。"""
    if not re.fullmatch(r"https://(?:[a-z0-9-]+\.)?openapi-rdc\.aliyuncs\.com", value):
        raise ProjectError("INVALID_CODE_SOURCE", "请选择云效官方 API 地址，不接受自定义主机、路径或端口")
    return value


@dataclass(frozen=True)
class CodeSourceIdentity:
    """仅供后台装配使用的短期身份；普通响应、模型与日志不能读取令牌。"""

    id: UUID
    version: int
    api_origin: str
    organization: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class LegacySourceBinding:
    """部署装配层提供的白名单映射；客户端不能传令牌、文件路径或 API 目标。"""

    fingerprint: str
    repository_id: UUID
    clone_url: str
    target_branches: tuple[str, ...]
    api_origin: str
    organization: str
    platform_repository: str

    def matches(self, row: dict[str, object]) -> bool:
        return (
            row["id"] == str(self.repository_id)
            and row["provider"] == "codeup"
            and git_address(str(row["git_url"])) == git_address(self.clone_url)
            and row["default_branch"] in self.target_branches
        )


def source_view(row: CodeSourceModel, manage: bool) -> dict[str, object]:
    return {
        "id": str(row.id),
        "name": row.name,
        "provider": row.provider,
        "api_origin": row.api_origin,
        "organization": row.organization,
        "enabled": row.enabled,
        "version": row.version,
        "can_manage": manage,
        "has_credential": row.credential_ciphertext is not None,
        "verification_status": "not_checked",
        "updated_at": row.updated_at.isoformat(),
    }


class CodeSourceService:
    def __init__(self, sessions: SessionFactory, members: ProjectMembers, secrets: ProjectSecrets | None) -> None:
        self.sessions, self.members, self.secrets = sessions, members, secrets

    async def role(self, actor: ProjectActor) -> str:
        role = await self.members.role(actor)
        if role is None:
            raise ProjectError("PROJECT_NOT_FOUND", "代码源不存在或无权访问")
        return role

    async def require_import_admin(self, actor: ProjectActor) -> None:
        if await self.role(actor) not in {"owner", "admin"}:
            raise ProjectError("PROJECT_FORBIDDEN", "只有工作空间管理员可以接入部署级 API 身份")

    async def adopt_repository(
        self, actor: ProjectActor, binding: LegacySourceBinding, repository_version: int, token: str
    ) -> dict[str, object]:
        try:
            return await self._adopt_repository(actor, binding, repository_version, token)
        except IntegrityError:
            raise ProjectError("PROJECT_CHANGED", "配置被并发接入，请刷新后重试") from None

    async def _adopt_repository(
        self, actor: ProjectActor, binding: LegacySourceBinding, repository_version: int, token: str
    ) -> dict[str, object]:
        """一次事务接入身份并关联原仓库；保留 Git 密码、构建配置，不覆盖已轮换的身份。"""
        await self.require_import_admin(actor)
        codeup_origin(binding.api_origin)
        if not self.secrets or not token or len(token) > 8000 or any(c.isspace() for c in token):
            raise ProjectError("PROJECT_SECRETS_UNAVAILABLE", "部署级 API 身份无法安全接入")
        source_id = uuid5(actor.workspace_id, "legacy-code-source:" + binding.fingerprint)
        now = datetime.now(UTC)
        async with self.sessions() as session:
            repo = await session.scalar(
                select(ProjectRepositoryModel)
                .where(
                    ProjectRepositoryModel.id == binding.repository_id,
                    ProjectRepositoryModel.workspace_id == actor.workspace_id,
                )
                .with_for_update()
            )
            project = await session.get(ProjectModel, repo.project_id) if repo else None
            if not repo or not project or project.workspace_id != actor.workspace_id:
                raise ProjectError("PROJECT_NOT_FOUND", "仓库不存在或无权接入")
            if (
                repo.status != "active"
                or project.status != "active"
                or not binding.matches(
                    {
                        "id": str(repo.id),
                        "provider": repo.provider,
                        "git_url": repo.git_url,
                        "default_branch": repo.default_branch,
                    }
                )
            ):
                raise ProjectError("PROJECT_CHANGED", "仓库与部署连接不匹配，请刷新后重试")
            source = await session.get(CodeSourceModel, source_id)
            if source and (
                not source.enabled
                or not source.credential_ciphertext
                or source.organization != binding.organization
                or source.api_origin != binding.api_origin
            ):
                raise ProjectError("CODE_SOURCE_CHANGED", "已接入身份已变更或停用，不会用旧部署配置覆盖")
            if repo.code_source_id == source_id and repo.platform_repository == binding.platform_repository and source:
                return {"source": source_view(source, True), "repository_id": str(repo.id), "already_imported": True}
            if repo.code_source_id or repo.version != repository_version:
                raise ProjectError("PROJECT_CHANGED", "仓库配置已变化，不会覆盖已有 MR 关联")
            if source is None:
                source = CodeSourceModel(
                    id=source_id,
                    workspace_id=actor.workspace_id,
                    created_by=actor.user_id,
                    name="云效 · " + binding.organization,
                    provider="codeup",
                    api_origin=binding.api_origin,
                    organization=binding.organization,
                    enabled=True,
                    credential_ciphertext=self.secrets.encrypt(token),
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(source)
                await session.flush()
            # CAS 兼容没有行锁的测试数据库；并发冲突回滚整次接入，不留下孤立身份。
            result = await session.execute(
                update(ProjectRepositoryModel)
                .where(
                    ProjectRepositoryModel.id == repo.id,
                    ProjectRepositoryModel.workspace_id == actor.workspace_id,
                    ProjectRepositoryModel.version == repository_version,
                )
                .values(
                    code_source_id=source_id,
                    platform_repository=binding.platform_repository,
                    version=repository_version + 1,
                    connection_version=repo.connection_version + 1,
                    connection_check=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise ProjectError("PROJECT_CHANGED", "仓库已被并发修改，请刷新后重试")
            await session.execute(
                update(ProjectModel)
                .where(ProjectModel.id == repo.project_id)
                .values(version=ProjectModel.version + 1, updated_at=now)
            )
            await session.commit()
            return {"source": source_view(source, True), "repository_id": str(repo.id), "already_imported": False}

    async def list(self, actor: ProjectActor) -> dict[str, object]:
        role = await self.role(actor)
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(CodeSourceModel)
                    .where(CodeSourceModel.workspace_id == actor.workspace_id)
                    .order_by(CodeSourceModel.created_at)
                    .limit(100)
                )
            ).all()
            return {
                "items": [
                    source_view(row, role in {"owner", "admin"} or row.created_by == actor.user_id) for row in rows
                ]
            }

    async def save(
        self,
        actor: ProjectActor,
        values: dict[str, object],
        *,
        token: str | None,
        source_id: UUID | None = None,
        version: int | None = None,
    ) -> dict[str, object]:
        await self.role(actor)
        values = dict(values)
        values["api_origin"] = codeup_origin(str(values["api_origin"]))
        if token is not None:
            if token and (len(token) > 8000 or any(c.isspace() for c in token)):
                raise ProjectError("INVALID_CODE_SOURCE", "API 令牌格式错误")
            if self.secrets is None:
                raise ProjectError("PROJECT_SECRETS_UNAVAILABLE", "未配置加密密钥，不能保存 API 身份")
            values["credential_ciphertext"] = self.secrets.encrypt(token) if token else None
        now = datetime.now(UTC)
        async with self.sessions() as session:
            if source_id is None:
                if not token:
                    raise ProjectError("INVALID_CODE_SOURCE", "首次创建代码源必须填写 MR API 令牌")
                row = CodeSourceModel(
                    id=uuid4(),
                    workspace_id=actor.workspace_id,
                    created_by=actor.user_id,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    **values,
                )
                session.add(row)
                await session.commit()
                return source_view(row, True)
            row = await self._row(session, actor, source_id)
            role = await self.role(actor)
            if role not in {"owner", "admin"} and row.created_by != actor.user_id:
                raise ProjectError("PROJECT_FORBIDDEN", "只有连接创建者或管理员可以修改代码源")
            # 改变目标时不能把旧身份带到新组织；必须重新明确提供令牌。
            if (row.organization != values["organization"] or row.api_origin != values["api_origin"]) and not token:
                raise ProjectError("INVALID_CODE_SOURCE", "切换组织或 API 地址必须重新填写令牌")
            result = await session.execute(
                update(CodeSourceModel)
                .where(
                    CodeSourceModel.id == source_id,
                    CodeSourceModel.workspace_id == actor.workspace_id,
                    CodeSourceModel.version == version,
                )
                .values(**values, version=(version or 0) + 1, updated_at=now)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise ProjectError("PROJECT_CHANGED", "代码源已被修改，请刷新后重试")
            await session.commit()
            await session.refresh(row)
            return source_view(row, True)

    async def _row(self, session: AsyncSession, actor: ProjectActor, source_id: UUID) -> CodeSourceModel:
        row = await session.scalar(
            select(CodeSourceModel)
            .where(CodeSourceModel.id == source_id, CodeSourceModel.workspace_id == actor.workspace_id)
            .with_for_update()
        )
        if row is None:
            raise ProjectError("PROJECT_NOT_FOUND", "代码源不存在或无权访问")
        return row

    async def identity(self, actor: ProjectActor, source_id: UUID, version: int) -> CodeSourceIdentity:
        await self.role(actor)
        async with self.sessions() as session:
            row = await self._row(session, actor, source_id)
            if not row.enabled or row.version != version or not row.credential_ciphertext or not self.secrets:
                raise ProjectError("CODE_SOURCE_CHANGED", "MR API 连接已变化或停用，请重新提案和审批")
            return CodeSourceIdentity(
                row.id,
                row.version,
                codeup_origin(row.api_origin),
                row.organization,
                self.secrets.decrypt(row.credential_ciphertext),
            )
