"""设置 HTTP 契约：只写秘密、管理员写入和并发版本保护。"""

import re
from dataclasses import asdict
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator, model_validator

from weora.modules.console_settings.domain import GitPreferences, ModelPreferences, SettingsActor
from weora.modules.console_settings.model_catalog import ModelBudget
from weora.modules.console_settings.service import ConsoleSettingsService
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.runs.application.harness import HarnessId

router = APIRouter(prefix="/workspaces/{workspace_id}/settings", tags=["Console settings"])
ProviderId = Literal["deepseek", "openai", "anthropic", "google", "moonshotai-cn"]


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=0)


class HarnessInput(VersionInput):
    harness: HarnessId


class GitInput(VersionInput):
    name_mode: Literal["actor_agent", "actor", "environment", "manual"]
    email_mode: Literal["actor", "environment", "manual"]
    manual_name: str = Field(default="", max_length=160)
    manual_email: str = Field(default="", max_length=255)
    co_authored_by: bool

    @model_validator(mode="after")
    def validate_identity(self) -> "GitInput":
        """禁止换行和 trailer 注入；手动模式必须有可用身份。"""
        if re.search(r"[\r\n\x00<>]", self.manual_name + self.manual_email):
            raise ValueError("提交身份不能包含控制字符或尖括号")
        if self.name_mode == "manual" and not self.manual_name.strip():
            raise ValueError("请填写提交名称")
        if self.email_mode == "manual":
            from pydantic import TypeAdapter

            TypeAdapter(EmailStr).validate_python(self.manual_email)
        return self


class ModelInput(VersionInput):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    provider: ProviderId = "deepseek"
    model_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    max_tokens: int = Field(ge=1, le=16384)
    timeout_seconds: int = Field(ge=1, le=600)
    execution_timeout_seconds: int = Field(ge=1, le=3600)
    api_key: SecretStr | None = None

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value and (len(value.get_secret_value()) > 4096 or any(c.isspace() for c in value.get_secret_value())):
            raise ValueError("API Key 格式无效")
        return value


class ConnectionInput(VersionInput):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    provider: ProviderId
    model_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    api_key: SecretStr | None = None
    make_primary: bool = False

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr | None) -> SecretStr | None:
        return ModelInput.validate_key(value)


class BudgetInput(VersionInput):
    # null 表示采用保存时的部署默认值；仍校验显式填写的预算边界。
    max_tokens: int | None = Field(ge=1, le=16384)
    timeout_seconds: int | None = Field(ge=1, le=600)
    execution_timeout_seconds: int | None = Field(ge=1, le=3600)


def _service(request: Request) -> ConsoleSettingsService:
    return cast(ConsoleSettingsService, request.app.state.console_settings_service)


def _actor(workspace_id: UUID, principal: PrincipalRecord) -> SettingsActor:
    """不能通过路径切换到当前认证上下文以外的团队。"""
    if workspace_id != principal.workspace_id:
        raise HTTPException(403, detail={"code": "SETTINGS_FORBIDDEN", "message": "无权访问此团队设置"})
    return SettingsActor(principal.user_id, workspace_id)


@router.get("/harness")
async def get_harness(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    return await _service(request).harness(_actor(workspace_id, principal))


@router.put("/harness")
async def put_harness(
    workspace_id: UUID,
    payload: HarnessInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    return await _service(request).save_harness(_actor(workspace_id, principal), payload.revision, payload.harness)


@router.get("/git")
async def get_git(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    values, revision = await _service(request).git(_actor(workspace_id, principal))
    return asdict(values) | {"revision": revision}


@router.put("/git")
async def put_git(
    workspace_id: UUID,
    payload: GitInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    values = GitPreferences(
        payload.name_mode,
        payload.email_mode,
        payload.manual_name.strip(),
        payload.manual_email.strip(),
        payload.co_authored_by,
    )
    revision = await _service(request).save_git(_actor(workspace_id, principal), payload.revision, values)
    return asdict(values) | {"revision": revision}


@router.get("/model")
async def get_model(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    return await _service(request).model(_actor(workspace_id, principal))


@router.put("/model")
async def put_model(
    workspace_id: UUID,
    payload: ModelInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    values = ModelPreferences(
        name=payload.name.strip(),
        provider=payload.provider,
        model_id=payload.model_id,
        max_tokens=payload.max_tokens,
        timeout_seconds=payload.timeout_seconds,
        execution_timeout_seconds=payload.execution_timeout_seconds,
    )
    return await _service(request).save_model(
        _actor(workspace_id, principal),
        payload.revision,
        values,
        payload.api_key.get_secret_value() if payload.api_key is not None else None,
    )


@router.post("/model/check")
async def check_model(
    workspace_id: UUID,
    payload: VersionInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    return await _service(request).check_model(_actor(workspace_id, principal), payload.revision)


@router.get("/model-connections")
async def get_connections(
    workspace_id: UUID, request: Request, principal: Annotated[PrincipalRecord, Depends(current_principal)]
) -> dict[str, object]:
    return await _service(request).model_connections(_actor(workspace_id, principal))


@router.post("/model-connections")
async def add_connection(
    workspace_id: UUID,
    payload: ConnectionInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    return await _service(request).save_connection(
        _actor(workspace_id, principal),
        payload.revision,
        payload.name.strip(),
        payload.provider,
        payload.model_id,
        payload.api_key.get_secret_value() if payload.api_key is not None else None,
        make_primary=payload.make_primary,
    )


@router.put("/model-connections/{connection_id}")
async def edit_connection(
    workspace_id: UUID,
    connection_id: str,
    payload: ConnectionInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    return await _service(request).save_connection(
        _actor(workspace_id, principal),
        payload.revision,
        payload.name.strip(),
        payload.provider,
        payload.model_id,
        payload.api_key.get_secret_value() if payload.api_key is not None else None,
        identifier=connection_id,
        make_primary=payload.make_primary,
    )


@router.post("/model-connections/{connection_id}/primary")
async def select_connection(
    workspace_id: UUID,
    connection_id: str,
    payload: VersionInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    return await _service(request).select_connection(_actor(workspace_id, principal), payload.revision, connection_id)


@router.post("/model-connections/{connection_id}/remove")
async def remove_connection(
    workspace_id: UUID,
    connection_id: str,
    payload: VersionInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    return await _service(request).remove_connection(_actor(workspace_id, principal), payload.revision, connection_id)


@router.post("/model-connections/{connection_id}/check")
async def check_connection(
    workspace_id: UUID,
    connection_id: str,
    payload: VersionInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    return await _service(request).check_model(_actor(workspace_id, principal), payload.revision, connection_id)


@router.put("/model-budget")
async def save_budget(
    workspace_id: UUID,
    payload: BudgetInput,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    return await _service(request).save_model_budget(
        _actor(workspace_id, principal),
        payload.revision,
        ModelBudget(payload.max_tokens, payload.timeout_seconds, payload.execution_timeout_seconds),
    )
