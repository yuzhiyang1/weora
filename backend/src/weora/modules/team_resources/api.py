"""团队资源的工作空间会话和现有设备凭据入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from weora.modules.collaboration.application.agent_mail import AgentMailError
from weora.modules.collaboration.infrastructure.agent_sessions import authenticate
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.team_resources import store
from weora.modules.team_resources.domain import ResourceError, TreeFile, catalog, file_size, member_roles, validate_path

router = APIRouter(prefix="/workspaces/{workspace_id}/team-resources", tags=["Team resources"])
Bearer = Annotated[str | None, Header()]
Branch = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/\-]*$")]


class TreeUpdate(BaseModel):
    base: UUID | None = None
    files: dict[str, TreeFile | None] = Field(min_length=1, max_length=512)


class CandidateUpdate(TreeUpdate):
    branch: Branch
    expected: UUID | None = None


class MergeInput(BaseModel):
    expected: UUID


class TagInput(BaseModel):
    revision: UUID


@asynccontextmanager
async def access(
    request: Request,
    workspace: UUID,
    authorization: str | None,
    *,
    write: bool = False,
    maintainer: bool = False,
) -> AsyncIterator[tuple[AsyncSession, PrincipalRecord]]:
    # Cookie 鉴权可能另借连接，先完成再打开资源事务，兼容单连接池。
    principal = None
    if authorization is None:
        principal = (
            await write_principal(request, request.headers.get("x-csrf-token"))
            if write
            else await current_principal(request)
        )
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.db_session_factory)
    async with factory() as db, db.begin():
        try:
            if principal is None:
                _, principal = await authenticate(db, workspace, authorization)
            if principal.workspace_id != workspace or (maintainer and principal.role not in {"owner", "admin"}):
                raise ResourceError("TEAM_RESOURCE_FORBIDDEN", "当前成员没有此团队源的操作权限。", 403)
            yield db, principal
        except (AgentMailError, ResourceError) as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


def snapshot_view(row: store.Revision | None, principal: PrincipalRecord) -> dict[str, object]:
    files = row.files if row else {}
    return {
        "revision": row.id if row else None,
        "files": files,
        **catalog(files),
        "member_roles": member_roles(files, str(principal.user_id), principal.email),
    }


def candidate_view(row: store.Change) -> dict[str, object]:
    return {
        "id": row.id,
        "branch": row.branch,
        "author_id": row.author_id,
        "base": row.base_id,
        "revision": row.revision_id,
        "merged_revision": row.merged_id,
    }


def diff_content(content: TreeFile | None, path: str) -> str | None:
    return f"二进制文件 · {file_size(content, path)} 字节" if isinstance(content, dict) else content


def check_update(payload: TreeUpdate) -> None:
    for path in payload.files:
        validate_path(path)


@router.get("/snapshot")
async def snapshot(
    workspace_id: UUID,
    request: Request,
    authorization: Bearer = None,
    ref: Annotated[str, Query(min_length=1, max_length=120)] = "main",
) -> dict[str, object]:
    async with access(request, workspace_id, authorization) as (db, principal):
        return snapshot_view(await store.resolve(db, workspace_id, ref), principal)


@router.get("/changes")
async def changes(workspace_id: UUID, request: Request, authorization: Bearer = None) -> dict[str, object]:
    async with access(request, workspace_id, authorization) as (db, _):
        rows = await db.scalars(
            select(store.Change)
            .join(store.Revision, store.Revision.id == store.Change.revision_id)
            .where(store.Change.workspace_id == workspace_id, store.Change.merged_id.is_(None))
            .order_by(store.Revision.created_at.desc(), store.Change.id)
            .limit(100),
        )
        return {"items": [candidate_view(row) for row in rows]}


@router.get("/changes/{change_id}")
async def change_detail(
    workspace_id: UUID,
    change_id: UUID,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with access(request, workspace_id, authorization) as (db, _):
        row = await db.scalar(
            select(store.Change).where(store.Change.workspace_id == workspace_id, store.Change.id == change_id)
        )
        if row is None:
            raise ResourceError("RESOURCE_CHANGE_NOT_FOUND", "找不到合并请求。", 404)
        original = await store.revision(db, workspace_id, row.base_id)
        candidate = await store.revision(db, workspace_id, row.revision_id)
        before, after = original.files if original else {}, candidate.files if candidate else {}
        diff = [
            {"path": path, "before": diff_content(before.get(path), path), "after": diff_content(after.get(path), path)}
            for path in sorted(before.keys() | after.keys())
            if before.get(path) != after.get(path)
        ]
        return {**candidate_view(row), "changes": diff}


@router.post("/changes", status_code=201)
async def propose(
    workspace_id: UUID,
    payload: CandidateUpdate,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with access(request, workspace_id, authorization, write=True) as (db, principal):
        check_update(payload)
        if payload.branch == "main":
            raise ResourceError("RESOURCE_BRANCH_UNAVAILABLE", "main 是默认发布引用。")
        return candidate_view(
            await store.propose(
                db, workspace_id, principal.user_id, payload.branch, payload.base, payload.expected, payload.files
            )
        )


@router.post("/changes/{change_id}/merge")
async def merge(
    workspace_id: UUID,
    change_id: UUID,
    payload: MergeInput,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with access(request, workspace_id, authorization, write=True, maintainer=True) as (db, principal):
        return candidate_view(await store.merge(db, workspace_id, principal.user_id, change_id, payload.expected))


@router.put("/default")
async def write_default(
    workspace_id: UUID,
    payload: TreeUpdate,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with access(request, workspace_id, authorization, write=True, maintainer=True) as (db, principal):
        check_update(payload)
        return snapshot_view(
            await store.write(db, workspace_id, principal.user_id, payload.base, payload.files), principal
        )


@router.put("/refs/tags/{name:path}")
async def tag_revision(
    workspace_id: UUID,
    name: Branch,
    payload: TagInput,
    request: Request,
    authorization: Bearer = None,
) -> dict[str, object]:
    async with access(request, workspace_id, authorization, write=True, maintainer=True) as (db, principal):
        if name == "main":
            raise ResourceError("RESOURCE_BRANCH_UNAVAILABLE", "main 是默认发布引用。")
        tag = await store.create_tag(db, workspace_id, principal.user_id, name, payload.revision)
        return {"name": tag.name, "revision": tag.revision_id}
