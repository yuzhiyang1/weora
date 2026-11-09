"""Agent Registry HTTP 请求与响应模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from weora.modules.agents.application.ports import (
    AgentDraftRecord,
    AgentRecord,
    AgentSkillBindingRecord,
    AgentVersionRecord,
)


class AgentSkillBindingResponse(BaseModel):
    """面向管理端展示的不可变 SkillVersion 绑定。"""

    skill_version_id: UUID
    skill_id: UUID
    skill_slug: str
    skill_display_name: str
    version: str
    content_sha256: str

    @classmethod
    def from_record(cls, binding: AgentSkillBindingRecord) -> "AgentSkillBindingResponse":
        """隐藏对象存储键，只公开管理端需要的版本身份。"""
        return cls(
            skill_version_id=binding.skill_version_id,
            skill_id=binding.skill_id,
            skill_slug=binding.skill_slug,
            skill_display_name=binding.skill_display_name,
            version=binding.version,
            content_sha256=binding.content_sha256,
        )


class AgentDraftResponse(BaseModel):
    """Agent 当前可变草稿。"""

    system_prompt: str
    skills: list[AgentSkillBindingResponse]
    updated_at: datetime

    @classmethod
    def from_record(cls, draft: AgentDraftRecord) -> "AgentDraftResponse":
        return cls(
            system_prompt=draft.system_prompt,
            skills=[AgentSkillBindingResponse.from_record(item) for item in draft.skills],
            updated_at=draft.updated_at,
        )


class AgentDetailResponse(BaseModel):
    """Agent 资产详情与当前草稿。"""

    id: UUID
    display_name: str
    description: str
    status: str
    latest_published_version: str | None
    draft: AgentDraftResponse | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, agent: AgentRecord) -> "AgentDetailResponse":
        return cls(
            id=agent.id,
            display_name=agent.display_name,
            description=agent.description,
            status=agent.status,
            latest_published_version=agent.latest_published_version,
            draft=AgentDraftResponse.from_record(agent.draft) if agent.draft else None,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
        )


class AgentSummaryResponse(BaseModel):
    """Agent 资产列表摘要。"""

    id: UUID
    display_name: str
    description: str
    status: str
    latest_published_version: str | None
    updated_at: datetime

    @classmethod
    def from_record(cls, agent: AgentRecord) -> "AgentSummaryResponse":
        return cls(
            id=agent.id,
            display_name=agent.display_name,
            description=agent.description,
            status=agent.status,
            latest_published_version=agent.latest_published_version,
            updated_at=agent.updated_at,
        )


class AgentListResponse(BaseModel):
    """当前工作空间的 Agent 列表。"""

    items: list[AgentSummaryResponse]


class AgentVersionResponse(BaseModel):
    """不可变 Agent 版本定义。"""

    id: UUID
    version: str
    changelog: str
    display_name: str
    description: str
    system_prompt: str
    skills: list[AgentSkillBindingResponse]
    published_by: UUID
    published_at: datetime

    @classmethod
    def from_record(cls, version: AgentVersionRecord) -> "AgentVersionResponse":
        return cls(
            id=version.id,
            version=version.version,
            changelog=version.changelog,
            display_name=version.display_name,
            description=version.description,
            system_prompt=version.system_prompt,
            skills=[AgentSkillBindingResponse.from_record(item) for item in version.skills],
            published_by=version.published_by,
            published_at=version.published_at,
        )


class AgentVersionListResponse(BaseModel):
    """Agent 的全部不可变发布版本。"""

    items: list[AgentVersionResponse]


class CreateAgentRequest(BaseModel):
    """创建 Agent 资产与首份草稿。"""

    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2_000)
    system_prompt: str = Field(min_length=1, max_length=30_000)
    skill_version_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("display_name", "description", "system_prompt", mode="before")
    @classmethod
    def reject_blank_text(cls, value: object) -> object:
        """统一去除边缘空白，并拒绝只含空白字符的 Agent 配置。"""
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("不能为空")
            return normalized
        return value


class UpdateAgentDraftRequest(BaseModel):
    """替换 Agent 当前草稿配置。"""

    system_prompt: str = Field(min_length=1, max_length=30_000)
    skill_version_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("system_prompt", mode="before")
    @classmethod
    def reject_blank_prompt(cls, value: object) -> object:
        """草稿更新不能把 System Prompt 清空为纯空白。"""
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("不能为空")
            return normalized
        return value


class PublishAgentRequest(BaseModel):
    """从当前草稿发布不可变 Agent 版本。"""

    version: str = Field(min_length=1, max_length=64)
    changelog: str = Field(min_length=1, max_length=2_000)

    @field_validator("version", "changelog", mode="before")
    @classmethod
    def reject_blank_publish_fields(cls, value: object) -> object:
        """版本号和变更说明不能只包含空白字符。"""
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("不能为空")
            return normalized
        return value


class ErrorResponse(BaseModel):
    """统一错误响应外层结构。"""

    detail: dict[str, str]
