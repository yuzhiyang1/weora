"""将上游 Ticket 常驻目录适配为 Weora 的有期限工作快照，新 Run 不继承旧审批。"""

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from weora.modules.delivery.application.ports import DeliveryAccess, DeliveryStore, WorkspaceSandbox
from weora.modules.delivery.domain.models import ArtifactRecord, DeliveryError, PlanScope
from weora.modules.runs.application.ports import RunActor


class WorkspaceContinuity:
    """通过 Runs 应用查询确定同 Task 的前序 Run；快照仍使用既有产物权限和过期清理。"""

    def __init__(
        self,
        store: DeliveryStore,
        access: DeliveryAccess,
        sandbox: WorkspaceSandbox,
        history: Callable[[RunActor, UUID], Awaitable[tuple[UUID, ...]]],
        *,
        retention_days: int,
    ) -> None:
        self.store, self.access, self.sandbox, self.history = store, access, sandbox, history
        self.retention_days = retention_days
        self.restored: set[UUID] = set()

    async def capture(self, actor: RunActor, run_id: UUID, scope: PlanScope) -> None:
        """清理容器前只读保存批准范围内的最终文件；失败不保存貌似完整的半份快照。"""
        if not self.sandbox.owns(run_id):
            return
        await self.access.authorize(actor, run_id)
        changes = await self.sandbox.export_workspace(run_id, scope)
        body = json.dumps(
            {"repository_id": scope.repository_id, "base_revision": scope.base_revision, "changes": changes},
            ensure_ascii=False,
            sort_keys=True,
        )
        if len(body.encode()) > 1500000:
            raise DeliveryError("WORKSPACE_LIMIT", "工作区快照超过限制，不能清理后声称可恢复")
        digest = hashlib.sha256(body.encode()).hexdigest()
        snapshots = [
            item for item in await self.store.artifacts(actor.workspace_id, run_id) if item.kind == "workspace"
        ]
        if snapshots and snapshots[-1].sha256 == digest:
            return
        now = datetime.now(UTC)
        await self.store.add_artifact(
            ArtifactRecord(
                uuid4(),
                run_id,
                actor.workspace_id,
                "workspace",
                "SAVED",
                body,
                now,
                now + timedelta(days=self.retention_days),
                digest,
            )
        )

    async def restore(
        self,
        actor: RunActor,
        run_id: UUID,
        scope: PlanScope,
        guard: Callable[[Awaitable[None]], Awaitable[None]],
    ) -> None:
        """恢复最新可读的前序工作；仓库、提交、路径和本轮 file.write 批准必须同时匹配。"""
        if run_id in self.restored:
            return
        # 审批等待会清理容器但继续同一个 Run；先恢复本轮检查点，再查 Task 的前序运行。
        candidates = (run_id,) + tuple(reversed(await self.history(actor, run_id)))
        for previous in dict.fromkeys(candidates):
            await self.access.authorize(actor, previous)
            snapshots = [
                item for item in await self.store.artifacts(actor.workspace_id, previous) if item.kind == "workspace"
            ]
            if not snapshots:
                continue
            item = snapshots[-1]
            if item.expires_at <= datetime.now(UTC) or not item.body:
                raise DeliveryError("CONTEXT_EXPIRED", "前序工作快照已过期，不能凭旧历史恢复")
            if hashlib.sha256(item.body.encode()).hexdigest() != item.sha256:
                raise DeliveryError("WORKSPACE_CORRUPT", "工作快照校验失败")
            try:
                data = json.loads(item.body)
                changes = data["changes"]
                if (
                    not isinstance(data, dict)
                    or set(data) != {"repository_id", "base_revision", "changes"}
                    or not isinstance(changes, list)
                    or len(changes) > 500
                    or any(
                        not isinstance(change, dict)
                        or set(change) - {"path", "content", "mode"}
                        or not isinstance(change.get("path"), str)
                        or "content" not in change
                        or not isinstance(change.get("content"), (str, type(None)))
                        or change.get("mode") not in {None, "100644", "100755"}
                        for change in changes
                    )
                ):
                    raise ValueError
                total = 0
                paths: set[str] = set()
                for change in changes:
                    if change["path"] in paths:
                        raise ValueError
                    paths.add(change["path"])
                    raw = base64.b64decode(change["content"], validate=True) if change["content"] is not None else b""
                    total += len(raw)
                    if len(raw) > 262144 or total > 1048576:
                        raise ValueError
            except (ValueError, KeyError, TypeError):
                raise DeliveryError("WORKSPACE_CORRUPT", "工作快照格式无效") from None
            if data["repository_id"] != scope.repository_id or data["base_revision"] != scope.base_revision:
                raise DeliveryError("WORKSPACE_BASE_CHANGED", "前序工作基线不同，需要显式迁移")
            changes = cast(list[dict[str, str | None]], changes)
            if changes and (
                "file.write" not in scope.actions
                or any(not scope.allows_path(str(change["path"])) for change in changes)
            ):
                raise DeliveryError("WORKSPACE_APPROVAL_REQUIRED", "本轮审批需要覆盖前序修改路径及 file.write")
            if changes:
                await guard(self.sandbox.restore_changes(run_id, scope, changes))
            self.restored.add(run_id)
            return
        self.restored.add(run_id)
