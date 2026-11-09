"""平台存活探针。

存活探针只证明 API 进程能够处理请求，不检查数据库或 Redis。
依赖检查会在基础设施真正接入后由独立的就绪探针负责。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text

from weora import __version__

router = APIRouter(prefix="/health", tags=["System"])


class LiveHealthResponse(BaseModel):
    """稳定的进程存活响应。"""

    status: Literal["ok"] = "ok"
    service: str = "weora-api"
    version: str = __version__


class ReadyHealthResponse(BaseModel):
    """所有已启用权威基础设施可用时返回的就绪状态。"""

    status: Literal["ready"] = "ready"
    database: Literal["ok"] = "ok"
    object_storage: Literal["ok", "disabled"]


@router.get("/live", response_model=LiveHealthResponse)
def get_live_health() -> LiveHealthResponse:
    """返回 API 进程身份，不访问外部基础设施。"""
    return LiveHealthResponse()


@router.get("/ready", response_model=ReadyHealthResponse)
async def get_ready_health(request: Request) -> ReadyHealthResponse:
    """只有权威数据库和已启用对象存储可访问时才接收业务流量。"""
    try:
        async with request.app.state.db_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "DATABASE_UNAVAILABLE", "message": "数据库暂不可用"},
        ) from exc

    object_storage = request.app.state.object_storage
    if object_storage is None:
        return ReadyHealthResponse(object_storage="disabled")
    try:
        await object_storage.probe()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "OBJECT_STORAGE_UNAVAILABLE", "message": "对象存储暂不可用"},
        ) from exc
    return ReadyHealthResponse(object_storage="ok")
