"""Fake 只用于状态与错误测试，不作为真实隔离执行证据。"""

import asyncio
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from weora.modules.delivery.application.ports import BrowserResult, RemoteResult, SandboxResult
from weora.modules.delivery.domain.models import ArtifactRecord, DeliveryError, PlanRecord, PlanScope, RemoteRecord
from weora.modules.runs.application.ports import RunActor


class MemoryStore:
    def __init__(self) -> None:
        self.plans: list[PlanRecord] = []
        self.evidence: list[ArtifactRecord] = []
        self.remote_records: dict[str, RemoteRecord] = {}

    async def latest_plan(self, workspace_id: UUID, run_id: UUID) -> PlanRecord | None:
        return next((p for p in reversed(self.plans) if p.workspace_id == workspace_id and p.run_id == run_id), None)

    async def save_plan(self, plan: PlanRecord, *, expected_version: int, expected_status: str | None) -> bool:
        previous = await self.latest_plan(plan.workspace_id, plan.run_id)
        if previous and (previous.version != expected_version or previous.status != expected_status):
            return False
        if not previous and expected_version:
            return False
        if previous:
            self.plans[self.plans.index(previous)] = (
                plan if plan.version == expected_version else replace(previous, status="SUPERSEDED")
            )

        if not previous or plan.version != expected_version:
            self.plans.append(plan)
        return True

    async def get_plan(self, workspace_id: UUID, plan_id: UUID) -> PlanRecord | None:
        return next((p for p in self.plans if p.workspace_id == workspace_id and p.id == plan_id), None)

    async def add_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        previous = max((item.sequence for item in self.evidence if item.run_id == artifact.run_id), default=0)
        artifact = replace(artifact, sequence=previous + 1)
        self.evidence.append(artifact)
        return artifact

    async def artifacts(self, workspace_id: UUID, run_id: UUID) -> list[ArtifactRecord]:
        return [item for item in self.evidence if item.workspace_id == workspace_id and item.run_id == run_id]

    async def purge_expired(self, now: datetime) -> int:
        count = sum(1 for item in self.evidence if item.expires_at <= now and item.body is not None)
        self.evidence = [replace(item, body=None) if item.expires_at <= now else item for item in self.evidence]
        return count

    async def purge_runs(self, workspace_id: UUID, run_ids: tuple[UUID, ...]) -> int:
        matched = [item for item in self.evidence if item.workspace_id == workspace_id and item.run_id in run_ids]
        self.evidence = [
            replace(item, body=None, command=(), status="EXPIRED") if item in matched else item
            for item in self.evidence
        ]
        return len(matched)

    async def get_remote(self, workspace_id: UUID, run_id: UUID, key: str) -> RemoteRecord | None:
        item = self.remote_records.get(key)
        return item if item and item.workspace_id == workspace_id and item.run_id == run_id else None

    async def remote_by_id(self, workspace_id: UUID, record_id: UUID) -> RemoteRecord | None:
        return next(
            (r for r in self.remote_records.values() if r.workspace_id == workspace_id and r.id == record_id), None
        )

    async def claim_remote(self, record: RemoteRecord) -> bool:
        if record.business_key in self.remote_records:
            return False
        self.remote_records[record.business_key] = record
        return True

    async def finish_remote(self, record: RemoteRecord, *, expected_status: str) -> bool:
        previous = self.remote_records[record.business_key]
        if previous.status != expected_status:
            return False
        self.remote_records[record.business_key] = record
        return True

    async def remotes(self, workspace_id: UUID, run_id: UUID) -> list[RemoteRecord]:
        return [r for r in self.remote_records.values() if r.workspace_id == workspace_id and r.run_id == run_id]


class Access:
    def __init__(self, actor: RunActor, run_id: UUID) -> None:
        self.actor = actor
        self.run_id = run_id
        self.allowed = True
        self.fence = 7
        self.states: list[str] = []

    async def authorize(
        self, actor: RunActor, run_id: UUID, *, write: bool = False, fencing_token: int | None = None
    ) -> None:
        if not self.allowed or actor != self.actor or run_id != self.run_id:
            raise DeliveryError("PERMISSION_DENIED")
        if write and fencing_token != self.fence:
            raise DeliveryError("LEASE_LOST")

    async def plan_changed(self, actor: RunActor, run_id: UUID, state: str) -> None:
        self.states.append(state)


class Sandbox:
    def __init__(self) -> None:
        self.calls = 0
        self.cleanups = 0
        self.result = SandboxResult("PASSED", "2 tests passed", 0)
        self.block = False
        self.started = asyncio.Event()
        self.diff_output = "+ fixed"
        self.command_changes_diff: str | None = None

    async def prepare(self, run_id: UUID, scope: PlanScope) -> None:
        pass

    async def command(self, run_id: UUID, argv: tuple[str, ...]) -> SandboxResult:
        self.calls += 1
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.command_changes_diff is not None:
            self.diff_output = self.command_changes_diff
        return self.result

    async def write(self, run_id: UUID, path: str, content: str) -> SandboxResult:
        self.calls += 1
        return SandboxResult("PASSED", "写入成功", 0)

    async def diff(self, run_id: UUID, scope: PlanScope) -> SandboxResult:
        return SandboxResult("PASSED", self.diff_output, 0)

    async def cleanup(self, run_id: UUID) -> None:
        self.cleanups += 1


class Browser:
    def __init__(self) -> None:
        self.result = BrowserResult("NOT_CONFIGURED", "未配置浏览器")

    async def verify(self, run_id: UUID, path: str, expected_text: str) -> BrowserResult:
        return self.result


class Remote:
    def __init__(self) -> None:
        self.calls = 0
        self.unknown = False
        self.found: RemoteResult | None = None

    async def create(self, run_id: UUID, scope: PlanScope, business_key: str, title: str, body: str) -> RemoteResult:
        self.calls += 1
        if self.unknown:
            raise TimeoutError
        return RemoteResult("42", "https://github.com/example/test/pull/42")

    async def reconcile(self, scope: PlanScope, business_key: str) -> RemoteResult | None:
        return self.found
