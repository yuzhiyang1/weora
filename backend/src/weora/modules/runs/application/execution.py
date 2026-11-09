"""受租约约束的单次执行，不依赖队列、Pi 或具体模型实现。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID

from weora.modules.runs.application.ports import ExecutionProfile, RunActor, RunRecord
from weora.modules.tools.application.gateway import ToolSpec


@dataclass(frozen=True, slots=True)
class RunLease:
    """Worker 领取凭证；所有后续写入必须同时匹配 owner、token 和有效期。"""

    run: RunRecord
    owner: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class RuntimeInput:
    """仅含固定静态提示和本次目标，不携带宿主机路径或供应商会话类型。"""

    system_prompt: str
    instruction: str
    tools: tuple[ToolSpec, ...] = ()


@dataclass
class RuntimeSession:
    """中立会话控制端口；模型循环不拥有业务授权或持久化权威。"""

    id: str
    messages: list[dict[str, object]] = field(default_factory=list)
    pull: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None
    accepted: Callable[[str], Awaitable[None]] | None = None
    checkpoint: Callable[[list[dict[str, object]], list[str]], Awaitable[None]] | None = None
    initial_input_ids: list[str] = field(default_factory=list)
    status: Callable[[dict[str, object] | None], Awaitable[None]] | None = None


class RuntimeFailure(Exception):
    """安全错误码；原始 SDK/HTTP 异常不进入事件、日志或 API。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TextRuntime(Protocol):
    """取消该 await 必须等待本地受控进程退出，才能返回给 Worker。"""

    async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str: ...


class ExecutionAccess(Protocol):
    """复用身份模块检查活动成员，装配固定版本的已校验材料。"""

    async def is_active(self, actor: RunActor) -> bool: ...

    async def load_input(self, run: RunRecord) -> RuntimeInput: ...


class ExecutionStore(Protocol):
    """每个方法各自原子提交；调用模型期间不持有数据库事务。"""

    async def claim(self, *, workspace_id: UUID, run_id: UUID, owner: str, lease_seconds: int) -> RunLease | None: ...

    async def renew(self, lease: RunLease, *, lease_seconds: int) -> bool: ...

    async def finish(
        self,
        lease: RunLease,
        *,
        status: Literal["SUCCEEDED", "FAILED", "CANCELLED"],
        output: str | None = None,
        failure_code: str | None = None,
    ) -> bool: ...


class RunExecutor:
    """不重放未知执行；失去租约时取消本地 Runtime，且不能再提交成功。"""

    def __init__(
        self,
        *,
        store: ExecutionStore,
        access: ExecutionAccess,
        runtime: TextRuntime,
        profile: ExecutionProfile,
        lease_seconds: int = 30,
        heartbeat_seconds: float = 5,
        runtime_factory: Callable[[RunLease], TextRuntime] | None = None,
        validate_output: Callable[[RunLease, str], Awaitable[str]] | None = None,
        before_finish: Callable[[RunLease], Awaitable[None]] | None = None,
        profile_resolver: Callable[[RunLease], Awaitable[ExecutionProfile]] | None = None,
    ) -> None:
        if not 0 < heartbeat_seconds < lease_seconds / 2:
            raise ValueError("心跳间隔必须小于租约一半")
        self._store = store
        self._access = access
        self._runtime = runtime
        self._profile = profile
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._runtime_factory = runtime_factory
        self._validate_output = validate_output
        self._before_finish = before_finish
        self._profile_resolver = profile_resolver

    async def execute(self, *, workspace_id: UUID, run_id: UUID, owner: str) -> bool:
        """重复队列消息只有一个领取者执行；False 表示未领取或无权提交。"""
        lease = await self._store.claim(
            workspace_id=workspace_id,
            run_id=run_id,
            owner=owner,
            lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return False
        task: asyncio.Task[str] | None = None
        actor = RunActor(lease.run.created_by, lease.run.workspace_id)
        profile = self._profile
        try:
            if not await self._access.is_active(actor):
                raise RuntimeFailure("ACCESS_REVOKED")
            if self._profile_resolver:
                profile = await self._profile_resolver(lease)
            if lease.run.execution_profile != profile:
                raise RuntimeFailure("MODEL_CONFIGURATION_MISMATCH")

            async def invoke() -> str:
                # 克隆也属于受租约保护的准备阶段；等待材料时同样心跳续约和响应取消。
                payload = await self._access.load_input(lease.run)
                if not await self._store.renew(lease, lease_seconds=self._lease_seconds):
                    raise RuntimeFailure("ACCESS_REVOKED")
                runtime = self._runtime_factory(lease) if self._runtime_factory else self._runtime
                return await runtime.run(payload, profile)

            task = asyncio.create_task(invoke())
            finish_status: Literal["SUCCEEDED", "CANCELLED"] = "SUCCEEDED"
            output: str | None = None
            async with asyncio.timeout(profile.wall_timeout_seconds):
                while True:
                    done, _ = await asyncio.wait({task}, timeout=self._heartbeat_seconds)
                    if not await self._access.is_active(actor):
                        raise RuntimeFailure("ACCESS_REVOKED")
                    if done:
                        output = task.result()
                        if not output.strip():
                            raise RuntimeFailure("EMPTY_RESPONSE")
                        if len(output) > 65536:
                            raise RuntimeFailure("MODEL_OUTPUT_LIMIT")
                        if self._validate_output:
                            output = await self._validate_output(lease, output)
                        break
                    if not await self._store.renew(lease, lease_seconds=self._lease_seconds):
                        # 先等受控本地进程退出，再记录取消；过期 lease 的 finish 会被拒绝。
                        await self._stop(task)
                        finish_status = "CANCELLED"
                        break
            # 执行期限覆盖模型与工具，不中断终态提交：事务已提交后超时不能再改报失败。
            return await self._finish(lease, status=finish_status, output=output)
        except TimeoutError:
            if task is not None:
                await self._stop(task)
            # 超时和撤权可能同时发生；先确认权限，不能把已撤权误报为模型故障。
            if not await self._access.is_active(actor):
                return await self._finish(lease, status="FAILED", failure_code="ACCESS_REVOKED")
            return await self._finish(
                lease,
                status="FAILED",
                failure_code="RUN_TIMEOUT" if profile.execution_timeout_seconds else "MODEL_TIMEOUT",
            )
        except RuntimeFailure as exc:
            if task is not None:
                await self._stop(task)
            return await self._finish(lease, status="FAILED", failure_code=exc.code)
        finally:
            # 包括 Worker 被中断和数据库失败：清理子进程，但不伪造可持久化的终态。
            if task is not None:
                await self._stop(task)

    async def _finish(
        self,
        lease: RunLease,
        *,
        status: Literal["SUCCEEDED", "FAILED", "CANCELLED"],
        output: str | None = None,
        failure_code: str | None = None,
    ) -> bool:
        """终态对外可见前保存工作；期间续租，保存失败不能报告交付成功。"""
        if self._before_finish:
            capture = asyncio.ensure_future(self._before_finish(lease))
            try:
                while not capture.done():
                    await asyncio.wait({capture}, timeout=self._heartbeat_seconds)
                    if not await self._store.renew(lease, lease_seconds=self._lease_seconds):
                        capture.cancel()
                        await asyncio.gather(capture, return_exceptions=True)
                        return False
                await capture
            except RuntimeFailure as error:
                status, output, failure_code = "FAILED", None, error.code
            except asyncio.CancelledError:
                capture.cancel()
                await asyncio.gather(capture, return_exceptions=True)
                raise
            finally:
                if not capture.done():
                    capture.cancel()
                    await asyncio.gather(capture, return_exceptions=True)
        return await self._store.finish(lease, status=status, output=output, failure_code=failure_code)

    @staticmethod
    async def _stop(task: asyncio.Task[str]) -> None:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, RuntimeFailure):
            await task
