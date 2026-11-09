"""私有运行的电脑接口。桌面帧按请求鉴权，不进入模型上下文或工具日志。"""

import logging
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from weora.modules.computers.openbot import ComputerError, OpenBot
from weora.modules.computers.policy import computer_mode
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/workspaces/{workspace_id}/runs/{run_id}/computer", tags=["Computer"])


class DesktopInput(BaseModel):
    """桌面输入只接受有界坐标和单键操作。"""

    model_config = ConfigDict(extra="forbid")
    control_id: str = Field(min_length=1, max_length=80)
    x: int | None = Field(default=None, ge=0, lt=1280)
    y: int | None = Field(default=None, ge=0, lt=800)
    button: Literal[1, 3, 4, 5] = 1
    clicks: Literal[1, 2] = 1
    key: str | None = Field(default=None, min_length=1, max_length=24)


async def _access(
    request: Request, principal: PrincipalRecord, workspace_id: UUID, run_id: UUID
) -> tuple[OpenBot | None, str]:
    actor = RunActor(principal.user_id, principal.workspace_id)
    try:
        detail = await cast(RunService, request.app.state.run_service).get(
            actor=actor, workspace_id=workspace_id, run_id=run_id
        )
    except RunError:
        raise HTTPException(404, detail="运行不存在或不可访问") from None
    # 只读话题的账号与页面证据由 Worker 获取，人工控制仅对明确开放测试的范围生效。
    if computer_mode(detail.task, request.app.state.settings.computer_feishu_scopes) != "test":
        return None, ""
    identity = OpenBot.computer_id(workspace_id, actor.user_id, detail.task.id)
    return cast(OpenBot | None, request.app.state.openbot), identity


def _http_error(error: ComputerError) -> HTTPException:
    return HTTPException(error.status, detail={"code": error.code, "message": error.message})


@router.get("")
async def status(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    bot, identity = await _access(request, principal, workspace_id, run_id)
    if bot is None:
        return {"state": "disabled"}
    try:
        if await bot.state(identity) != "running":
            return {"state": "stopped"}
        control = await bot.call(identity, "/control")
        current = control.get("request")
        return {
            "state": "running",
            "holder": control.get("holder"),
            "transitioning": control.get("transitioning"),
            "control_id": current.get("id") if isinstance(current, dict) else None,
        }
    except ComputerError as error:
        raise _http_error(error) from None


@router.get("/screen")
async def screen(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    bot, identity = await _access(request, principal, workspace_id, run_id)
    if bot is None:
        raise HTTPException(404, detail="虚拟电脑未启用")
    try:
        return await bot.call(identity, "/desktop/screen")
    except ComputerError as error:
        raise _http_error(error) from None


@router.post("/input", status_code=204)
async def desktop_input(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    payload: DesktopInput,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> None:
    bot, identity = await _access(request, principal, workspace_id, run_id)
    if bot is None:
        raise HTTPException(404, detail="虚拟电脑未启用")
    if (payload.x is None) != (payload.y is None) or (payload.key is None) == (payload.x is None):
        raise HTTPException(422, detail="请输入一组坐标或一个按键")
    try:
        await bot.call(identity, "/desktop/input", payload.model_dump(exclude_none=True))
    except ComputerError as error:
        raise _http_error(error) from None


@router.post("/{action}", status_code=204)
async def change(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    action: Literal["start", "stop", "take", "release"],
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> None:
    bot, identity = await _access(request, principal, workspace_id, run_id)
    if bot is None:
        raise HTTPException(404, detail="虚拟电脑未启用")
    try:
        if action in {"start", "stop"}:
            verb = "ensure" if action == "start" else "stop"
            await bot.supervisor(f"/computers/{identity}/{verb}", {})
        else:
            await bot.control(identity, action)
    except ComputerError as error:
        raise _http_error(error) from None
    # 审计生命周期和接管事实，不记录画面、按键内容或内部凭据。
    logger.info(
        "computer action=%s workspace_id=%s run_id=%s user_id=%s", action, workspace_id, run_id, principal.user_id
    )
