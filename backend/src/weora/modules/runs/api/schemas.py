"""运行 HTTP 契约，受众和发起人由平台注入而非模型或调用方指定。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from weora.modules.runs.application.ports import RunDetail, RunStatus


class CreateRunRequest(BaseModel):
    """只接收明确的已发布版本和本次目标；拒绝身份、受众或凭据注入。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    agent_version: str = Field(min_length=1, max_length=64)
    instruction: str = Field(min_length=1, max_length=16000)
    project_id: UUID | None = None
    repository_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("agent_version", "instruction")
    @classmethod
    def trim_nonempty(cls, value: str) -> str:
        """拒绝全空白目标，同时让前后空白不影响重复提交判断。"""
        if not value.strip():
            raise ValueError("内容不能为空")
        return value.strip()


class ContinueTaskRequest(BaseModel):
    """只追加本次跟进，版本、身份、受众和原审批均不能由请求覆盖。"""

    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=16000)
    mode: Literal["continue", "steer"] = "continue"

    @field_validator("instruction")
    @classmethod
    def trim_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("跟进内容不能为空")
        return value.strip()


class RunSummaryResponse(BaseModel):
    """私有运行摘要，不公开提交键、目标正文或凭据。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    agent_version_id: UUID
    agent_name: str
    agent_version: str
    task_title: str | None = None
    status: RunStatus
    last_sequence: int
    created_at: datetime
    updated_at: datetime


class ExecutionProfileResponse(BaseModel):
    """固定的非敏感执行配置，不公开 Key、子进程路径或租约凭证。"""

    model_config = ConfigDict(from_attributes=True)

    runtime_id: str
    provider: str
    model_id: str
    max_tokens: int | None
    timeout_seconds: int
    execution_timeout_seconds: int | None = None


class RunDetailResponse(RunSummaryResponse):
    """正文和真实模型输出只交付发起人；租约及凭据不公开。"""

    instruction: str
    audience: str = "private"
    output_text: str | None = None
    failure_code: str | None = None
    cancellation_requested_at: datetime | None = None
    execution_profile: ExecutionProfileResponse | None = None
    recovery_read_only: bool = False
    context_cutoff: datetime
    project_context: dict[str, object] | None = None

    @classmethod
    def from_detail(cls, detail: RunDetail) -> "RunDetailResponse":
        summary = RunSummaryResponse.model_validate(detail.run)
        summary.task_title = summary.task_title or detail.task.display_title
        return cls(
            **summary.model_dump(),
            instruction=detail.task.instruction,
            audience=detail.task.audience,
            output_text=detail.run.output_text,
            failure_code=detail.run.failure_code,
            cancellation_requested_at=detail.run.cancellation_requested_at,
            recovery_read_only=detail.run.recovery_read_only,
            project_context=detail.task.project_context,
            context_cutoff=datetime.fromisoformat(str(detail.task.origin_metadata["created_at"]))
            if detail.task.origin_metadata and detail.task.origin_metadata.get("created_at")
            else detail.run.created_at,
            execution_profile=ExecutionProfileResponse.model_validate(detail.run.execution_profile)
            if detail.run.execution_profile is not None
            else None,
        )


class RunListResponse(BaseModel):
    """稳定游标翻页；存在下一页时返回最后一条已交付记录的 ID。"""

    items: list[RunSummaryResponse]
    next_before_id: UUID | None


class RunEventResponse(BaseModel):
    """只有事实类型、顺序、时间和追踪 ID，不含目标正文。"""

    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    sequence: int
    event_type: str
    request_id: str
    occurred_at: datetime


class RunEventsResponse(BaseModel):
    """持久事件补读页面，next_after 是最后一条实际交付的序号。"""

    items: list[RunEventResponse]
    next_after: int
    latest_sequence: int
    has_more: bool
