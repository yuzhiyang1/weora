"""只有认证的人类写接口可以审批；模型工具表中没有 decision。"""

from dataclasses import asdict
from typing import Annotated, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import Decision, DeliveryError, PlanScope
from weora.modules.identity.api.dependencies import current_principal, write_principal
from weora.modules.identity.application.ports import PrincipalRecord
from weora.modules.runs.application.ports import RunActor

router = APIRouter(prefix="/workspaces/{workspace_id}/runs/{run_id}", tags=["Delivery"])


class ScopeRequest(BaseModel):
    """字段表达审批范围，不允许携带凭据、命令身份或宿主路径。"""

    model_config = ConfigDict(extra="forbid")
    repository_id: str = Field(min_length=1, max_length=100)
    base_revision: str = Field(min_length=40, max_length=64)
    paths: list[str] = Field(min_length=1, max_length=100)
    commands: list[list[str]] = Field(default_factory=list, max_length=30)
    actions: list[str] = Field(min_length=1, max_length=10)
    remote_id: str | None = Field(default=None, max_length=100)
    target_branch: str | None = Field(default=None, max_length=120)
    source_delivery_id: UUID | None = None

    def to_scope(self) -> PlanScope:
        return PlanScope(
            self.repository_id,
            self.base_revision,
            tuple(self.paths),
            tuple(tuple(x) for x in self.commands),
            tuple(self.actions),
            self.remote_id,
            self.target_branch,
            source_delivery_id=str(self.source_delivery_id) if self.source_delivery_id else None,
        )


class ProposePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: ScopeRequest
    summary: str = Field(min_length=1, max_length=12000)
    risk: str = Field(min_length=1, max_length=4000)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Decision
    expires_in_seconds: int = Field(default=3600, ge=60, le=86400)
    reason: str | None = Field(default=None, max_length=4000)


def _service(request: Request) -> DeliveryService:
    service = getattr(request.app.state, "delivery_service", None)
    if service is None:
        raise HTTPException(503, detail={"code": "DELIVERY_NOT_CONFIGURED", "message": "研发交付服务未配置"})
    return cast(DeliveryService, service)


def _actor(principal: PrincipalRecord, workspace_id: UUID) -> RunActor:
    if principal.workspace_id != workspace_id:
        raise HTTPException(404, detail={"code": "RUN_NOT_FOUND", "message": "运行不存在或不可访问"})
    return RunActor(principal.user_id, workspace_id)


def _error(exc: DeliveryError) -> NoReturn:
    code = 404 if exc.code in {"RUN_NOT_FOUND", "PERMISSION_DENIED", "SOURCE_REVOKED"} else 409
    raise HTTPException(code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/plan")
async def get_plan(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    try:
        plan = await _service(request).get_plan(_actor(principal, workspace_id), run_id)
        return {"plan": jsonable_encoder(asdict(plan)) if plan else None}
    except DeliveryError as exc:
        _error(exc)


@router.post("/plan")
async def propose_plan(
    workspace_id: UUID,
    run_id: UUID,
    payload: ProposePlanRequest,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        plan = await _service(request).propose(
            _actor(principal, workspace_id), run_id, payload.scope.to_scope(), payload.summary, payload.risk
        )
        return {"plan": jsonable_encoder(asdict(plan))}
    except DeliveryError as exc:
        _error(exc)


@router.post("/plan/{plan_id}/decision")
async def decide_plan(
    workspace_id: UUID,
    run_id: UUID,
    plan_id: UUID,
    payload: DecisionRequest,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    try:
        plan = await _service(request).decide(
            _actor(principal, workspace_id),
            run_id,
            plan_id,
            payload.decision,
            payload.expires_in_seconds,
            payload.reason,
        )
        return {"plan": jsonable_encoder(asdict(plan))}
    except DeliveryError as exc:
        _error(exc)


@router.get("/artifacts")
async def get_artifacts(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    try:
        items = await _service(request).artifacts(_actor(principal, workspace_id), run_id)
        return {"items": jsonable_encoder([asdict(item) for item in items])}
    except DeliveryError as exc:
        _error(exc)


@router.get("/deliveries")
async def get_deliveries(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(current_principal)],
) -> dict[str, object]:
    try:
        items = await _service(request).deliveries(_actor(principal, workspace_id), run_id)
        return {"items": jsonable_encoder([asdict(item) for item in items])}
    except DeliveryError as exc:
        _error(exc)


@router.post("/deliveries/reconcile")
async def reconcile_delivery(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Annotated[PrincipalRecord, Depends(write_principal)],
) -> dict[str, object]:
    """显式核对只执行远端 GET，不需要批准新的写操作。"""
    try:
        item = await _service(request).reconcile_pr(_actor(principal, workspace_id), run_id)
        return {"delivery": jsonable_encoder(asdict(item)) if item else None}
    except DeliveryError as exc:
        _error(exc)
