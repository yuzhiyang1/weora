"""调用者、权限、预算和持久化由平台决定，模型只提供工具名与业务参数。"""

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from jsonschema import Draft202012Validator

from weora.modules.runs.application.ports import RunActor
from weora.shared.runtime_limits import MAX_TOOL_CALLS


@dataclass(frozen=True, slots=True)
class ToolScope:
    """来自 Worker 的可信运行范围；不能从模型参数恢复。"""

    run_id: UUID
    actor: RunActor
    lease_owner: str
    fencing_token: int
    role: str = "console"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, object]
    effect: str = "read"
    timeout_seconds: float = 10


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    call_id: str
    name: str
    status: str
    arguments: dict[str, object]
    result: dict[str, object] | None
    error_code: str | None
    created_at: datetime


class ToolAccess(Protocol):
    async def authorize(self, scope: ToolScope, *, effect: str) -> bool: ...


class ToolStore(Protocol):
    async def reserve(
        self,
        scope: ToolScope,
        call_id: str,
        name: str,
        fingerprint: str,
        arguments: dict[str, object],
        *,
        max_calls: int,
    ) -> tuple[str, dict[str, object] | None]: ...

    async def finish(
        self, scope: ToolScope, call_id: str, *, result: dict[str, object], status: str, error_code: str | None
    ) -> None: ...

    async def list_calls(self, scope: ToolScope) -> list[ToolInvocation]: ...


ToolHandler = Callable[[ToolScope, dict[str, object]], Awaitable[dict[str, object]]]


def failure(code: str) -> dict[str, object]:
    """错误也是事实结果，不能把拒绝、超时解释成没有资料。"""
    return {"ok": False, "error": {"code": code}}


class ToolGateway:
    def __init__(self, store: ToolStore, access: ToolAccess, *, max_calls: int = MAX_TOOL_CALLS) -> None:
        self._store = store
        self._access = access
        self._max_calls = max_calls
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        """只允许装配层注册，运行中不能借工具结果安装新工具。"""
        if spec.name in self._tools or not re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", spec.name):
            raise ValueError("工具名称重复或无效")
        Draft202012Validator.check_schema(spec.input_schema)
        self._tools[spec.name] = (spec, handler)

    def specifications(self) -> tuple[ToolSpec, ...]:
        return tuple(item[0] for item in self._tools.values())

    async def invoke(
        self, scope: ToolScope, call_id: str, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        """同 ID 异参数拒绝；未知写结果不重放；撤权后不能读取缓存结果。"""
        definition = self._tools.get(name)
        if definition is None:
            return failure("TOOL_NOT_ALLOWED")
        spec, handler = definition
        if scope.role == "recovery" and spec.effect != "read":
            return failure("RECOVERY_READ_ONLY")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", call_id):
            return failure("TOOL_CALL_ID_INVALID")
        if not await self._access.authorize(scope, effect=spec.effect):
            return failure("ACCESS_REVOKED")
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 65536 or not Draft202012Validator(spec.input_schema).is_valid(arguments):
            return failure("TOOL_ARGUMENTS_INVALID")
        digest = hashlib.sha256((name + "\n" + encoded).encode()).hexdigest()
        disposition, previous = await self._store.reserve(
            scope, call_id, name, digest, arguments, max_calls=self._max_calls
        )
        if disposition == "CACHED":
            return previous or failure("TOOL_RESULT_EXPIRED")
        if disposition != "NEW":
            return failure(disposition)
        status, code = "SUCCEEDED", None
        try:
            async with asyncio.timeout(spec.timeout_seconds):
                result = await handler(scope, arguments)
            if len(json.dumps(result, ensure_ascii=False).encode()) > 262144:
                result, status, code = failure("TOOL_OUTPUT_LIMIT"), "FAILED", "TOOL_OUTPUT_LIMIT"
            if spec.effect != "control" and not await self._access.authorize(scope, effect=spec.effect):
                result, status, code = failure("ACCESS_REVOKED"), "FAILED", "ACCESS_REVOKED"
            if (
                result.get("ok") is False or (result.get("status") == "unavailable" and result.get("error"))
            ) and status == "SUCCEEDED":
                status = "FAILED"
                error = result.get("error")
                code = str(error.get("code", "TOOL_FAILED"))[:64] if isinstance(error, dict) else "TOOL_FAILED"
        except TimeoutError:
            result, status, code = (
                failure("TOOL_TIMEOUT"),
                "UNKNOWN" if spec.effect != "read" else "FAILED",
                "TOOL_TIMEOUT",
            )
        except asyncio.CancelledError:
            # 取消不能假定外部副作用没有发生，留下 UNKNOWN 供显式对账。
            await self._store.finish(
                scope, call_id, result=failure("TOOL_INTERRUPTED"), status="UNKNOWN", error_code="TOOL_INTERRUPTED"
            )
            raise
        except Exception:
            # 不记录 SDK/文件错误原文，避免路径、正文或凭据进入日志。
            result, status, code = (
                failure("TOOL_FAILED"),
                "UNKNOWN" if spec.effect != "read" else "FAILED",
                "TOOL_FAILED",
            )
        await self._store.finish(scope, call_id, result=result, status=status, error_code=code)
        return result
