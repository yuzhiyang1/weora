"""测试凭据由用户通过受保护的配置接口登记，不经飞书消息或 Agent 工具传明文。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from weora.modules.computers.credentials import ComputerCredentials
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.runs.application.ports import RunActor

router = APIRouter(prefix="/workspaces/{workspace_id}/computer-credentials", tags=["Computer"])


class CredentialInput(BaseModel):
    """凭据仅允许填入用户明确登记的精确站点和测试话题。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    origin: str = Field(min_length=1, max_length=2048)
    scope_id: str = Field(default="", pattern=r"^([a-f0-9]{64})?$")
    label: str = Field(min_length=1, max_length=80)
    username: SecretStr = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=1024)


def service(request: Request, principal: PrincipalRecord, workspace_id: UUID) -> ComputerCredentials:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404, detail="工作空间不可访问")
    result = cast(ComputerCredentials | None, request.app.state.computer_credentials)
    if result is None:
        raise HTTPException(503, detail="电脑凭据存储尚未配置")
    return result


@router.post("", status_code=201)
async def create(
    workspace_id: UUID,
    request: Request,
    payload: CredentialInput,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, str]:
    credentials = service(request, principal, workspace_id)
    settings = request.app.state.settings
    if payload.origin not in settings.computer_browser_origins or (
        payload.scope_id and settings.computer_feishu_scopes.get(payload.scope_id) != "test"
    ):
        raise HTTPException(403, detail="该站点或话题未开放测试凭据")
    identity = await credentials.create(
        RunActor(principal.user_id, workspace_id),
        origin=payload.origin,
        scope_id=payload.scope_id,
        label=payload.label,
        username=payload.username.get_secret_value(),
        password=payload.password.get_secret_value(),
    )
    return {"id": str(identity)}


@router.get("")
async def available(
    workspace_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
    scope_id: str = "",
) -> list[dict[str, object]]:
    actor = RunActor(principal.user_id, workspace_id)
    return await service(request, principal, workspace_id).available(actor, scope_id)


@router.delete("/{credential_id}", status_code=204)
async def revoke(
    workspace_id: UUID,
    credential_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> None:
    await service(request, principal, workspace_id).revoke(RunActor(principal.user_id, workspace_id), credential_id)
