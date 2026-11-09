"""跨模块权限与外部副作用均从这里注入。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from weora.modules.delivery.domain.models import ArtifactRecord, PlanRecord, PlanScope, RemoteRecord
from weora.modules.runs.application.ports import RunActor


class DeliveryAccess(Protocol):
    async def authorize(
        self, actor: RunActor, run_id: UUID, *, write: bool = False, fencing_token: int | None = None
    ) -> None:
        """验证成员、私有 Run、来源实时授权；写工具还需当前有效租约。"""
        ...

    async def plan_changed(self, actor: RunActor, run_id: UUID, state: str) -> None:
        """跨模块更新等待、拒绝或恢复状态，不让交付模块直接修改 Run 表。"""
        ...


class DeliveryStore(Protocol):
    async def latest_plan(self, workspace_id: UUID, run_id: UUID) -> PlanRecord | None: ...
    async def get_plan(self, workspace_id: UUID, plan_id: UUID) -> PlanRecord | None: ...
    async def save_plan(self, plan: PlanRecord, *, expected_version: int, expected_status: str | None) -> bool:
        """原子版本/状态 CAS；插入新版本与撤销旧批准必须同事务。"""
        ...

    async def add_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord: ...
    async def artifacts(self, workspace_id: UUID, run_id: UUID) -> list[ArtifactRecord]: ...
    async def purge_expired(self, now: datetime) -> int: ...
    async def purge_runs(self, workspace_id: UUID, run_ids: tuple[UUID, ...]) -> int: ...
    async def remotes(self, workspace_id: UUID, run_id: UUID) -> list[RemoteRecord]: ...
    async def remote_by_id(self, workspace_id: UUID, record_id: UUID) -> RemoteRecord | None: ...
    async def get_remote(self, workspace_id: UUID, run_id: UUID, key: str) -> RemoteRecord | None: ...
    async def claim_remote(self, record: RemoteRecord) -> bool: ...
    async def finish_remote(self, record: RemoteRecord, *, expected_status: str) -> bool: ...


@dataclass(frozen=True)
class SandboxResult:
    status: str
    output: str
    exit_code: int | None = None


class Sandbox(Protocol):
    async def prepare(self, run_id: UUID, scope: PlanScope) -> None: ...
    async def command(self, run_id: UUID, argv: tuple[str, ...]) -> SandboxResult: ...
    async def write(self, run_id: UUID, path: str, content: str) -> SandboxResult: ...
    async def diff(self, run_id: UUID, scope: PlanScope) -> SandboxResult: ...
    async def cleanup(self, run_id: UUID) -> None: ...


class WorkspaceSandbox(Protocol):
    """工作文件快照与模型历史分开；只操作当前实例拥有的隔离环境。"""

    def owns(self, run_id: UUID) -> bool: ...
    async def export_workspace(self, run_id: UUID, scope: PlanScope) -> list[dict[str, str | None]]: ...
    async def restore_changes(self, run_id: UUID, scope: PlanScope, changes: list[dict[str, str | None]]) -> None: ...


@dataclass(frozen=True)
class ReviewReference:
    """已成功创建的评审及冻结范围；后台轮询不接受模型 URL 或任意仓库标识。"""

    run_id: UUID
    record_id: UUID
    scope: PlanScope
    remote_id: str
    url: str


@dataclass(frozen=True)
class BrowserResult:
    status: str
    report: str
    screenshot_base64: str | None = None


@dataclass(frozen=True)
class BrowserEvidence:
    """一次验证的两类持久记录；验证记录ID不能冒充截图ID，没有图片时截图为None。"""

    verification: ArtifactRecord
    screenshot: ArtifactRecord | None = None


class BrowserVerifier(Protocol):
    async def verify(self, run_id: UUID, path: str, expected_text: str) -> BrowserResult: ...


@dataclass(frozen=True)
class RemoteResult:
    remote_id: str
    url: str
    # PR 创建器确认的实际源分支与提交；缺失时不能猜测可续写的旧提交。
    branch_name: str | None = None
    branch_revision: str | None = None


class DraftPullRequests(Protocol):
    async def create(
        self, run_id: UUID, scope: PlanScope, business_key: str, title: str, body: str
    ) -> RemoteResult: ...
    async def reconcile(self, scope: PlanScope, business_key: str) -> RemoteResult | None: ...
