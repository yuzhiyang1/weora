"""Skill Registry HTTP 路由。"""

from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response

from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.skills.api.schemas import (
    ErrorResponse,
    PublishSkillRequest,
    SkillDetailResponse,
    SkillDraftResponse,
    SkillEventListResponse,
    SkillEventResponse,
    SkillListResponse,
    SkillSummaryResponse,
    SkillVersionListResponse,
    SkillVersionResponse,
    UpdateSkillRequest,
)
from weora.modules.skills.application.service import SkillActor, SkillRegistryService
from weora.modules.skills.domain.errors import (
    SkillArchivedError,
    SkillArchiveInvalidError,
    SkillArchiveUnsafePathError,
    SkillCursorInvalidError,
    SkillDraftNotReadyError,
    SkillManifestInvalidError,
    SkillManifestMissingError,
    SkillNotFoundError,
    SkillObjectMissingError,
    SkillPackageTooLargeError,
    SkillPermissionDeniedError,
    SkillRegistryError,
    SkillRootAmbiguousError,
    SkillSlugConflictError,
    SkillStorageUnavailableError,
    SkillVersionConflictError,
    SkillVersionInvalidError,
    WorkspaceAccessDeniedError,
)
from weora.shared.api.request_id import get_request_id

router = APIRouter(prefix="/workspaces/{workspace_id}/skills", tags=["Skills"])
_MAX_UPLOAD_READ_BYTES = 50 * 1024 * 1024 + 1


def _service(request: Request) -> SkillRegistryService:
    return cast(SkillRegistryService, request.app.state.skill_registry_service)


def _actor(principal: PrincipalRecord) -> SkillActor:
    return SkillActor(
        user_id=principal.user_id,
        workspace_id=principal.workspace_id,
        role=principal.role,
    )


def _raise_http_error(error: SkillRegistryError) -> NoReturn:
    """把稳定领域错误映射为不泄漏内部信息的 HTTP 状态。"""
    if isinstance(error, (WorkspaceAccessDeniedError, SkillNotFoundError)):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, SkillPermissionDeniedError):
        http_status = status.HTTP_403_FORBIDDEN
    elif isinstance(error, (SkillSlugConflictError, SkillVersionConflictError, SkillArchivedError)):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(error, SkillStorageUnavailableError):
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(
        error,
        (
            SkillPackageTooLargeError,
            SkillArchiveInvalidError,
            SkillArchiveUnsafePathError,
            SkillRootAmbiguousError,
            SkillManifestMissingError,
            SkillManifestInvalidError,
            SkillVersionInvalidError,
            SkillDraftNotReadyError,
            SkillCursorInvalidError,
        ),
    ):
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif isinstance(error, SkillObjectMissingError):
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        http_status = status.HTTP_400_BAD_REQUEST
    raise HTTPException(
        status_code=http_status,
        detail={"code": error.code, "message": error.message},
    ) from error


@router.post(
    "",
    response_model=SkillDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def create_skill(
    workspace_id: UUID,
    request: Request,
    package: Annotated[UploadFile, File(description=".zip 或 .skill 格式的完整 Skill 目录")],
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> SkillDetailResponse:
    """上传、校验并创建一个带初始草稿的 Skill。"""
    package_name = package.filename or "skill.skill"
    if not package_name.lower().endswith((".zip", ".skill")):
        _raise_http_error(SkillArchiveInvalidError())
    archive_bytes = await package.read(_MAX_UPLOAD_READ_BYTES)
    try:
        skill = await _service(request).create_skill(
            actor=_actor(principal),
            workspace_id=workspace_id,
            package_name=package_name,
            archive_bytes=archive_bytes,
            request_id=get_request_id() or "unknown",
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillDetailResponse.from_record(skill)


@router.get(
    "",
    response_model=SkillListResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def list_skills(
    workspace_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    search: Annotated[str | None, Query(max_length=120)] = None,
    skill_status: Annotated[
        str | None,
        Query(alias="status", pattern="^(active|archived)$"),
    ] = None,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> SkillListResponse:
    """列出当前工作空间中的 Skill 资产。"""
    try:
        page = await _service(request).list_skills(
            actor=_actor(principal),
            workspace_id=workspace_id,
            limit=limit,
            search=search,
            status=skill_status,
            cursor=cursor,
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillListResponse(
        items=[SkillSummaryResponse.from_record(skill) for skill in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{skill_id}", response_model=SkillDetailResponse)
async def get_skill(
    workspace_id: UUID,
    skill_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> SkillDetailResponse:
    """获取 Skill 元数据、当前草稿和最新发布版本摘要。"""
    try:
        skill = await _service(request).get_skill(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillDetailResponse.from_record(skill)


@router.patch("/{skill_id}", response_model=SkillDetailResponse)
async def update_skill(
    workspace_id: UUID,
    skill_id: UUID,
    payload: UpdateSkillRequest,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> SkillDetailResponse:
    """修改列表展示名称和描述，不改变包内清单。"""
    try:
        skill = await _service(request).update_metadata(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
            display_name=payload.display_name,
            description=payload.description,
            request_id=get_request_id() or "unknown",
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillDetailResponse.from_record(skill)


@router.post("/{skill_id}/draft", response_model=SkillDraftResponse)
async def replace_skill_draft(
    workspace_id: UUID,
    skill_id: UUID,
    request: Request,
    package: Annotated[UploadFile, File(description="替换当前草稿的完整 Skill 包")],
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> SkillDraftResponse:
    """校验并替换当前草稿，历史发布版本不发生变化。"""
    package_name = package.filename or "skill.skill"
    if not package_name.lower().endswith((".zip", ".skill")):
        _raise_http_error(SkillArchiveInvalidError())
    try:
        draft = await _service(request).replace_draft(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
            package_name=package_name,
            archive_bytes=await package.read(_MAX_UPLOAD_READ_BYTES),
            request_id=get_request_id() or "unknown",
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillDraftResponse.from_record(draft)


@router.get("/{skill_id}/draft", response_model=SkillDraftResponse)
async def get_skill_draft(
    workspace_id: UUID,
    skill_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> SkillDraftResponse:
    """获取当前草稿的包校验报告。"""
    try:
        skill = await _service(request).get_skill(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
        )
        if skill.draft is None:
            raise SkillDraftNotReadyError
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillDraftResponse.from_record(skill.draft)


@router.post(
    "/{skill_id}/publish",
    response_model=SkillVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_skill(
    workspace_id: UUID,
    skill_id: UUID,
    payload: PublishSkillRequest,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> SkillVersionResponse:
    """以当前草稿发布一个不可变 SemVer 版本。"""
    try:
        version = await _service(request).publish(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
            version=payload.version,
            changelog=payload.changelog,
            request_id=get_request_id() or "unknown",
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillVersionResponse.from_record(version)


@router.get("/{skill_id}/versions", response_model=SkillVersionListResponse)
async def list_skill_versions(
    workspace_id: UUID,
    skill_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> SkillVersionListResponse:
    """获取 Skill 的全部不可变发布版本。"""
    try:
        versions = await _service(request).list_versions(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillVersionListResponse(items=[SkillVersionResponse.from_record(item) for item in versions])


@router.get("/{skill_id}/versions/{version}", response_model=SkillVersionResponse)
async def get_skill_version(
    workspace_id: UUID,
    skill_id: UUID,
    version: str,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> SkillVersionResponse:
    """获取一个不可变版本的完整校验快照。"""
    try:
        record = await _service(request).get_version(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
            version=version,
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillVersionResponse.from_record(record)


@router.get("/{skill_id}/versions/{version}/download")
async def download_skill_version(
    workspace_id: UUID,
    skill_id: UUID,
    version: str,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> Response:
    """完成权限和摘要校验后下载发布包。"""
    try:
        download = await _service(request).download_version(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
            version=version,
            request_id=get_request_id() or "unknown",
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)

    return Response(
        download.archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{download.filename}"',
            "Content-Length": str(download.package_size),
            "X-Content-SHA256": download.package_sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _change_archive_status(
    *,
    workspace_id: UUID,
    skill_id: UUID,
    archived: bool,
    request: Request,
    principal: PrincipalRecord,
) -> SkillDetailResponse:
    """复用归档与恢复的鉴权、错误和响应映射。"""
    try:
        skill = await _service(request).set_archived(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
            archived=archived,
            request_id=get_request_id() or "unknown",
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillDetailResponse.from_record(skill)


@router.post("/{skill_id}/archive", response_model=SkillDetailResponse)
async def archive_skill(
    workspace_id: UUID,
    skill_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> SkillDetailResponse:
    """从活跃资产列表中归档 Skill。"""
    return await _change_archive_status(
        workspace_id=workspace_id,
        skill_id=skill_id,
        archived=True,
        request=request,
        principal=principal,
    )


@router.post("/{skill_id}/restore", response_model=SkillDetailResponse)
async def restore_skill(
    workspace_id: UUID,
    skill_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> SkillDetailResponse:
    """把已归档 Skill 恢复为活跃资产。"""
    return await _change_archive_status(
        workspace_id=workspace_id,
        skill_id=skill_id,
        archived=False,
        request=request,
        principal=principal,
    )


@router.get("/{skill_id}/events", response_model=SkillEventListResponse)
async def list_skill_events(
    workspace_id: UUID,
    skill_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> SkillEventListResponse:
    """获取 Skill 创建、发布、归档等关键活动。"""
    try:
        events = await _service(request).list_events(
            actor=_actor(principal),
            workspace_id=workspace_id,
            skill_id=skill_id,
        )
    except SkillRegistryError as exc:
        _raise_http_error(exc)
    return SkillEventListResponse(items=[SkillEventResponse.from_record(item) for item in events])
