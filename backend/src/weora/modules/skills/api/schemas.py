"""Skill Registry HTTP 请求与响应模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from weora.modules.skills.application.ports import (
    SkillDraftRecord,
    SkillEventRecord,
    SkillRecord,
    SkillVersionRecord,
)


class SkillFileResponse(BaseModel):
    """Skill 包中文件的完整性元数据。"""

    path: str
    size: int
    media_type: str
    sha256: str


class SkillWarningResponse(BaseModel):
    """包校验发现的可定位风险。"""

    code: str
    message: str
    path: str | None
    blocks_publish: bool


class SkillCompatibilityResponse(BaseModel):
    """一个 Runtime 目标的静态兼容性结论。"""

    target: str
    status: str
    message: str


class SkillDraftResponse(BaseModel):
    """当前 Skill 草稿的校验结果。"""

    package_name: str
    package_size: int
    package_sha256: str
    content_sha256: str
    files: list[SkillFileResponse]
    compatibility: list[SkillCompatibilityResponse]
    warnings: list[SkillWarningResponse]
    updated_at: datetime

    @classmethod
    def from_record(cls, draft: SkillDraftRecord) -> "SkillDraftResponse":
        """把应用层记录转换为稳定 HTTP 契约。"""
        return cls(
            package_name=draft.package_name,
            package_size=draft.package_size,
            package_sha256=draft.package_sha256,
            content_sha256=draft.content_sha256,
            files=[
                SkillFileResponse(
                    path=item.path,
                    size=item.size,
                    media_type=item.media_type,
                    sha256=item.sha256,
                )
                for item in draft.files
            ],
            compatibility=[
                SkillCompatibilityResponse(
                    target=item.target,
                    status=item.status,
                    message=item.message,
                )
                for item in draft.compatibility
            ],
            warnings=[
                SkillWarningResponse(
                    code=item.code,
                    message=item.message,
                    path=item.path,
                    blocks_publish=item.blocks_publish,
                )
                for item in draft.warnings
            ],
            updated_at=draft.updated_at,
        )


class SkillDetailResponse(BaseModel):
    """Skill 详情与当前草稿。"""

    id: UUID
    slug: str
    display_name: str
    description: str
    status: str
    latest_published_version: str | None
    draft: SkillDraftResponse | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, skill: SkillRecord) -> "SkillDetailResponse":
        """把应用层聚合转换为详情响应。"""
        return cls(
            id=skill.id,
            slug=skill.slug,
            display_name=skill.display_name,
            description=skill.description,
            status=skill.status,
            latest_published_version=skill.latest_published_version,
            draft=SkillDraftResponse.from_record(skill.draft) if skill.draft else None,
            created_at=skill.created_at,
            updated_at=skill.updated_at,
        )


class SkillSummaryResponse(BaseModel):
    """Skills 资产列表中的轻量摘要。"""

    id: UUID
    slug: str
    display_name: str
    description: str
    status: str
    latest_published_version: str | None
    updated_at: datetime

    @classmethod
    def from_record(cls, skill: SkillRecord) -> "SkillSummaryResponse":
        """只公开列表展示所需字段。"""
        return cls(
            id=skill.id,
            slug=skill.slug,
            display_name=skill.display_name,
            description=skill.description,
            status=skill.status,
            latest_published_version=skill.latest_published_version,
            updated_at=skill.updated_at,
        )


class SkillListResponse(BaseModel):
    """使用不透明游标翻页的 Skill 列表响应。"""

    items: list[SkillSummaryResponse]
    next_cursor: str | None


class UpdateSkillRequest(BaseModel):
    """工作空间展示元数据更新请求。"""

    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2_000)


class PublishSkillRequest(BaseModel):
    """从当前草稿发布不可变版本的请求。"""

    version: str = Field(min_length=1, max_length=64)
    changelog: str = Field(min_length=1, max_length=2_000)


class SkillVersionResponse(BaseModel):
    """不可变 Skill 版本的校验与发布信息。"""

    id: UUID
    version: str
    changelog: str
    package_name: str
    package_size: int
    package_sha256: str
    content_sha256: str
    files: list[SkillFileResponse]
    compatibility: list[SkillCompatibilityResponse]
    warnings: list[SkillWarningResponse]
    published_by: UUID
    published_at: datetime

    @classmethod
    def from_record(cls, version: SkillVersionRecord) -> "SkillVersionResponse":
        """把应用层不可变版本转换为公开响应。"""
        return cls(
            id=version.id,
            version=version.version,
            changelog=version.changelog,
            package_name=version.package_name,
            package_size=version.package_size,
            package_sha256=version.package_sha256,
            content_sha256=version.content_sha256,
            files=[
                SkillFileResponse(
                    path=item.path,
                    size=item.size,
                    media_type=item.media_type,
                    sha256=item.sha256,
                )
                for item in version.files
            ],
            compatibility=[
                SkillCompatibilityResponse(
                    target=item.target,
                    status=item.status,
                    message=item.message,
                )
                for item in version.compatibility
            ],
            warnings=[
                SkillWarningResponse(
                    code=item.code,
                    message=item.message,
                    path=item.path,
                    blocks_publish=item.blocks_publish,
                )
                for item in version.warnings
            ],
            published_by=version.published_by,
            published_at=version.published_at,
        )


class SkillVersionListResponse(BaseModel):
    """指定 Skill 的全部发布版本。"""

    items: list[SkillVersionResponse]


class SkillEventResponse(BaseModel):
    """详情活动流中的单个审计事件。"""

    id: UUID
    event_type: str
    actor_id: UUID
    request_id: str
    data: dict[str, object]
    occurred_at: datetime

    @classmethod
    def from_record(cls, event: SkillEventRecord) -> "SkillEventResponse":
        """把内部事件快照转换为公开活动记录。"""
        return cls(
            id=event.id,
            event_type=event.event_type,
            actor_id=event.actor_id,
            request_id=event.request_id,
            data=event.data,
            occurred_at=event.occurred_at,
        )


class SkillEventListResponse(BaseModel):
    """指定 Skill 的关键活动记录。"""

    items: list[SkillEventResponse]


class ErrorResponse(BaseModel):
    """统一错误响应外层结构。"""

    detail: dict[str, str]
