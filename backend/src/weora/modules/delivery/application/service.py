"""审批采用精确版本 CAS；所有执行与内容回读都重新鉴权。"""

import asyncio
import base64
import binascii
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from weora.modules.delivery.application.code_hosting import ConnectionBindings
from weora.modules.delivery.application.ports import (
    BrowserEvidence,
    BrowserVerifier,
    DeliveryAccess,
    DeliveryStore,
    DraftPullRequests,
    RemoteResult,
    ReviewReference,
    Sandbox,
    SandboxResult,
    WorkspaceSandbox,
)
from weora.modules.delivery.application.workspaces import WorkspaceContinuity
from weora.modules.delivery.domain.models import (
    ArtifactRecord,
    Decision,
    DeliveryError,
    PlanRecord,
    PlanScope,
    PlanStatus,
    RemoteRecord,
    ReviewNotSent,
)
from weora.modules.runs.application.ports import RunActor


class DeliveryService:
    """工具调用者不能决定身份、凭据、审批主体或有效租约。"""

    def __init__(
        self,
        store: DeliveryStore,
        access: DeliveryAccess,
        sandbox: Sandbox,
        browser: BrowserVerifier,
        remote: DraftPullRequests,
        *,
        retention_days: int = 7,
        remote_bindings: ConnectionBindings | None = None,
        validate_scope: Callable[[RunActor, UUID, PlanScope], Awaitable[None]] | None = None,
        prepare_sandbox: Callable[[RunActor, UUID, PlanScope], Awaitable[bool]] | None = None,
        bind_scope: Callable[[RunActor, UUID, PlanScope], Awaitable[PlanScope]] | None = None,
        push_branch: Callable[[RunActor, UUID, PlanScope, str, str], Awaitable[RemoteResult]] | None = None,
        update_branch: Callable[[RunActor, UUID, PlanScope, RemoteRecord, str, str], Awaitable[RemoteResult]]
        | None = None,
        create_review: Callable[[RunActor, UUID, PlanScope, RemoteRecord, str, str, str], Awaitable[RemoteResult]]
        | None = None,
        reconcile_review: Callable[[RunActor, UUID, PlanScope, RemoteRecord, str], Awaitable[RemoteResult | None]]
        | None = None,
        workspace_sandbox: WorkspaceSandbox | None = None,
        workspace_history: Callable[[RunActor, UUID], Awaitable[tuple[UUID, ...]]] | None = None,
    ) -> None:
        self.store = store
        self.access = access
        self.sandbox = sandbox
        self.browser = browser
        self.remote = remote
        self.remote_bindings = remote_bindings
        self.retention_days = min(7, max(1, retention_days))
        self.validate_scope = validate_scope
        self.prepare_sandbox = prepare_sandbox
        self.bind_scope = bind_scope
        self.push_branch = push_branch
        self.update_branch, self.workspace_history = update_branch, workspace_history
        self.review_creator, self.review_reconciler = create_review, reconcile_review
        self.prepared_scopes: dict[UUID, PlanScope] = {}
        self.workspaces = (
            WorkspaceContinuity(store, access, workspace_sandbox, workspace_history, retention_days=self.retention_days)
            if workspace_sandbox is not None and workspace_history is not None
            else None
        )

    async def get_plan(self, actor: RunActor, run_id: UUID) -> PlanRecord | None:
        await self.access.authorize(actor, run_id)
        plan = await self.store.latest_plan(actor.workspace_id, run_id)
        if plan and plan.created_at + timedelta(days=self.retention_days) <= datetime.now(UTC):
            return replace(
                plan,
                summary="[上下文已过期]",
                risk="[上下文已过期]",
                reason=None,
                status="REVOKED" if plan.status in {"PENDING", "REVISION_REQUIRED", "APPROVED"} else plan.status,
                scope=replace(plan.scope, commands=(), actions=()),
            )
        return plan

    async def propose(
        self, actor: RunActor, run_id: UUID, scope: PlanScope, summary: str, risk: str, request_id: str = "unknown"
    ) -> PlanRecord:
        """新增版本不会继承旧授权；相同未决提案重试直接返回原版本。"""
        del request_id
        await self.access.authorize(actor, run_id)
        scope.validate()
        if scope.source_delivery_id and "git.push" in scope.actions:
            source = await self._source_branch(actor, scope, run_id)
            fields = (source.remote_id or "").split(":")
            if len(fields) != 3 or not re.fullmatch(r"weora/[0-9a-f]{24}", fields[1]):
                raise DeliveryError("BRANCH_REQUIRED", "已交付分支缺少可续写的精确提交记录")
            scope = replace(scope, source_branch=fields[1], source_revision=fields[2])
            scope.validate()
        if self.bind_scope is not None:
            scope = await self.bind_scope(actor, run_id, scope)
        if self.validate_scope is not None:
            await self.validate_scope(actor, run_id, scope)
        if "review.create" in scope.actions:
            await self._source_branch(actor, scope)
        if self.bind_scope is None and self.remote_bindings is not None:
            scope = self.remote_bindings.freeze(actor.workspace_id, scope)
        if not summary.strip() or len(summary) > 12000 or not risk.strip() or len(risk) > 4000:
            raise DeliveryError("INVALID_PLAN", "计划和风险说明必须填写，且不能超过限制")
        previous = await self.store.latest_plan(actor.workspace_id, run_id)
        same_proposal = previous and previous.scope == scope and previous.summary == summary and previous.risk == risk
        still_valid = previous and (
            previous.status == "PENDING"
            or previous.status == "APPROVED"
            and previous.expires_at is not None
            and previous.expires_at > datetime.now(UTC)
        )
        if same_proposal and still_valid:
            assert previous is not None
            if previous.status == "PENDING":
                await self.access.plan_changed(actor, run_id, "WAITING_APPROVAL")
            return previous
        version = previous.version + 1 if previous else 1
        plan = PlanRecord(
            uuid4(),
            run_id,
            actor.workspace_id,
            actor.user_id,
            version,
            scope,
            summary,
            risk,
            "PENDING",
            datetime.now(UTC),
        )
        if not await self.store.save_plan(
            plan, expected_version=version - 1, expected_status=previous.status if previous else None
        ):
            raise DeliveryError("PLAN_CHANGED", "计划已经变化，请刷新后重试")
        await self.access.plan_changed(actor, run_id, "WAITING_APPROVAL")
        return plan

    async def decide(
        self,
        actor: RunActor,
        run_id: UUID,
        plan_id: UUID,
        decision: Decision,
        expires_in_seconds: int = 3600,
        reason: str | None = None,
        request_id: str = "unknown",
    ) -> PlanRecord:
        """只由认证的人类 API 调用；不注册成 Agent Tool。"""
        del request_id
        await self.access.authorize(actor, run_id)
        plan = await self.store.latest_plan(actor.workspace_id, run_id)
        if plan is None or plan.id != plan_id:
            raise DeliveryError("PLAN_CHANGED", "只能审批最新版本")
        if decision == "approve" and self.validate_scope is not None:
            await self.validate_scope(actor, run_id, plan.scope)
        if decision == "approve" and plan.scope.source_delivery_id:
            await self._source_branch(actor, plan.scope, run_id)
        if plan.created_at + timedelta(days=self.retention_days) <= datetime.now(UTC):
            raise DeliveryError("CONTEXT_EXPIRED", "计划上下文已经过期，请重新提交计划")
        target = {"approve": "APPROVED", "reject": "REJECTED", "revoke": "REVOKED"}.get(decision)
        if decision == "supplement":
            if plan.status != "PENDING":
                raise DeliveryError("INVALID_DECISION", "只能要求重拟待审批计划")
            if not reason or not reason.strip() or len(reason) > 4000:
                raise DeliveryError("INVALID_PLAN", "请填写需要补充的信息")
            # 补充记录不是 Agent 的新提案：不可批准，先唤醒只读规划，再等待新范围审批。
            changed = replace(
                plan,
                id=uuid4(),
                version=plan.version + 1,
                status="REVISION_REQUIRED",
                created_at=datetime.now(UTC),
                decided_by=actor.user_id,
                decided_at=datetime.now(UTC),
                expires_at=None,
                reason=reason,
            )
            if not await self.store.save_plan(changed, expected_version=plan.version, expected_status=plan.status):
                raise DeliveryError("PLAN_CHANGED", "计划已被其他请求处理")
            await self.access.plan_changed(actor, run_id, "QUEUED")
            return changed
        if target is None:
            raise DeliveryError("INVALID_DECISION", "不支持的审批动作")
        if plan.status == target:
            # 上次请求可能在批准持久化后、Run 状态同步前中断；重试补齐该投影。
            await self.access.plan_changed(
                actor,
                run_id,
                "QUEUED" if decision == "approve" else "FAILED" if decision == "reject" else "WAITING_APPROVAL",
            )
            return plan
        if decision == "revoke":
            if plan.status != "APPROVED":
                raise DeliveryError("INVALID_DECISION", "只能撤销已批准的计划")
        elif plan.status != "PENDING":
            raise DeliveryError("INVALID_DECISION", "该计划已处理，请刷新")
        if not 60 <= expires_in_seconds <= 86400:
            raise DeliveryError("INVALID_EXPIRY", "审批有效期须为 1 分钟到 24 小时")
        now = datetime.now(UTC)
        changed = replace(
            plan,
            status=cast(PlanStatus, target),
            decided_by=actor.user_id,
            decided_at=now,
            expires_at=now + timedelta(seconds=expires_in_seconds) if decision == "approve" else None,
            reason=reason,
        )
        if not await self.store.save_plan(changed, expected_version=plan.version, expected_status=plan.status):
            raise DeliveryError("PLAN_CHANGED", "计划已被其他请求处理")
        await self.access.plan_changed(
            actor,
            run_id,
            "QUEUED" if decision == "approve" else "FAILED" if decision == "reject" else "WAITING_APPROVAL",
        )
        return changed

    async def _approved(self, actor: RunActor, run_id: UUID, action: str, fencing_token: int | None) -> PlanRecord:
        await self.access.authorize(actor, run_id, write=True, fencing_token=fencing_token)
        plan = await self.store.latest_plan(actor.workspace_id, run_id)
        if plan is None or plan.status != "APPROVED" or not plan.expires_at:
            raise DeliveryError("APPROVAL_REQUIRED", "执行前需要人工批准最新计划")
        if plan.expires_at <= datetime.now(UTC):
            raise DeliveryError("APPROVAL_EXPIRED", "审批已过期，请重新提交并批准计划")
        if plan.created_at + timedelta(days=self.retention_days) <= datetime.now(UTC):
            raise DeliveryError("CONTEXT_EXPIRED", "计划上下文已经过期，需要重新读取来源和提交计划")
        if action not in plan.scope.actions:
            raise DeliveryError("ACTION_DENIED", "该动作不在批准范围内")
        if self.validate_scope is not None:
            await self.validate_scope(actor, run_id, plan.scope)
        return plan

    async def _prepare(
        self,
        actor: RunActor,
        run_id: UUID,
        scope: PlanScope,
        fencing_token: int | None,
    ) -> None:
        """准备本轮隔离环境并恢复同 Task 工作；恢复文件单独复核本轮审批与租约。"""
        prepared = self.prepare_sandbox is not None and await self.prepare_sandbox(actor, run_id, scope)
        if not prepared:
            await self.sandbox.prepare(run_id, scope)
        self.prepared_scopes[run_id] = scope
        if self.workspaces:

            async def guard(operation: Awaitable[None]) -> None:
                await self._controlled(actor, run_id, "file.write", fencing_token, operation)

            await self.workspaces.restore(actor, run_id, scope, guard)

    async def checkpoint_workspace(self, actor: RunActor, run_id: UUID) -> None:
        """运行结束和改变审批状态前保存工作文件，失败不能声称下一轮可无损恢复。"""
        scope = self.prepared_scopes.get(run_id)
        if self.workspaces and scope:
            await self.workspaces.capture(actor, run_id, scope)

    async def review_references(self, actor: RunActor, run_id: UUID) -> list[ReviewReference]:
        """查询本 Run 已确认创建的评审，分支推送、未知请求和其他 Task 均不能成为订阅依据。"""
        await self.access.authorize(actor, run_id)
        references = []
        for record in await self.store.remotes(actor.workspace_id, run_id):
            if record.status != "SUCCEEDED" or not record.plan_id or not record.remote_id or not record.url:
                continue
            plan = await self.store.get_plan(actor.workspace_id, record.plan_id)
            if not plan or plan.run_id != run_id or not {"pr.create", "review.create"}.intersection(plan.scope.actions):
                continue
            if not record.remote_id.isdecimal() or not (
                record.url.endswith("/pull/" + record.remote_id) or record.url.endswith("/change/" + record.remote_id)
            ):
                continue
            references.append(ReviewReference(run_id, record.id, plan.scope, record.remote_id, record.url))
        return references

    async def _evidence(
        self,
        actor: RunActor,
        run_id: UUID,
        kind: str,
        result: SandboxResult,
        command: tuple[str, ...] = (),
        *,
        plan_id: UUID | None = None,
        repository_sha256: str | None = None,
    ) -> ArtifactRecord:
        now = datetime.now(UTC)
        body = result.output[: 262144 if kind in {"screenshot", "computer_screenshot"} else 65536]
        record = ArtifactRecord(
            uuid4(),
            run_id,
            actor.workspace_id,
            kind,
            result.status,
            body,
            now,
            now + timedelta(days=self.retention_days),
            hashlib.sha256(body.encode()).hexdigest(),
            command,
            result.exit_code,
            plan_id=plan_id,
            repository_sha256=repository_sha256,
        )
        return await self.store.add_artifact(record)

    async def record_computer_screenshot(self, actor: RunActor, run_id: UUID, png: str) -> ArtifactRecord:
        """保存电脑截图，单独标识以免成为代码交付测试证据。"""
        await self.access.authorize(actor, run_id)
        return await self._evidence(actor, run_id, "computer_screenshot", SandboxResult("PASSED", png))

    @staticmethod
    def _repository_digest(result: SandboxResult) -> str | None:
        if result.status != "PASSED" or result.exit_code != 0:
            return None
        return hashlib.sha256(result.output.encode()).hexdigest()

    async def _controlled[T](
        self,
        actor: RunActor,
        run_id: UUID,
        action: str,
        fencing_token: int | None,
        operation: Awaitable[T],
    ) -> T:
        """长命令运行期间也检查撤权、租约和审批失效，不等工具返回后才阻断。"""
        task = asyncio.ensure_future(operation)
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.5)
                await self._approved(actor, run_id, action, fencing_token)
            return await task
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.cleanup(actor, run_id)
            raise

    async def command(
        self, actor: RunActor, run_id: UUID, argv: tuple[str, ...], fencing_token: int | None
    ) -> ArtifactRecord:
        plan = await self._approved(actor, run_id, "command.run", fencing_token)
        if argv not in plan.scope.commands:
            raise DeliveryError("COMMAND_DENIED", "只能执行计划逐项列出的 argv")
        await self._prepare(actor, run_id, plan.scope, fencing_token)
        # 准备环境可能耗时；进入真实命令前再次检查批准和有效租约。
        await self._approved(actor, run_id, "command.run", fencing_token)
        before = await self._controlled(
            actor, run_id, "command.run", fencing_token, self.sandbox.diff(run_id, plan.scope)
        )
        before_sha = self._repository_digest(before)
        if before_sha is None:
            raise DeliveryError("FAILED_EVIDENCE", "执行前无法核验仓库状态，不能创建验证证据")
        try:
            result = await self._controlled(
                actor, run_id, "command.run", fencing_token, self.sandbox.command(run_id, argv)
            )
        except asyncio.CancelledError:
            await self.cleanup(actor, run_id)
            raise
        await self.access.authorize(actor, run_id, write=True, fencing_token=fencing_token)
        try:
            after = await self._controlled(
                actor, run_id, "command.run", fencing_token, self.sandbox.diff(run_id, plan.scope)
            )
            after_sha = self._repository_digest(after)
        except DeliveryError:
            # 命令已发生，仍保存真实输出/退出码，但状态不可核验时不能作为通过证据。
            await self._evidence(actor, run_id, "command", result, argv, plan_id=plan.id)
            raise
        # 写脚本、格式化和生成命令可能退出 0，但不等于对最终代码完成了验证。
        state_sha = after_sha if before_sha == after_sha else None
        return await self._evidence(
            actor, run_id, "command", result, argv, plan_id=plan.id, repository_sha256=state_sha
        )

    async def write(
        self, actor: RunActor, run_id: UUID, path: str, content: str, fencing_token: int | None
    ) -> ArtifactRecord:
        plan = await self._approved(actor, run_id, "file.write", fencing_token)
        if not plan.scope.allows_path(path) or len(content.encode()) > 262144:
            raise DeliveryError("PATH_DENIED", "写入超出已批准的路径或大小")
        await self._prepare(actor, run_id, plan.scope, fencing_token)
        await self._approved(actor, run_id, "file.write", fencing_token)
        result = await self._controlled(
            actor, run_id, "file.write", fencing_token, self.sandbox.write(run_id, path, content)
        )
        await self.access.authorize(actor, run_id, write=True, fencing_token=fencing_token)
        return await self._evidence(actor, run_id, "file.write", result, plan_id=plan.id)

    async def diff(self, actor: RunActor, run_id: UUID, fencing_token: int | None) -> ArtifactRecord:
        plan = await self._approved(actor, run_id, "git.diff", fencing_token)
        await self._prepare(actor, run_id, plan.scope, fencing_token)
        result = await self._controlled(actor, run_id, "git.diff", fencing_token, self.sandbox.diff(run_id, plan.scope))
        await self.access.authorize(actor, run_id, write=True, fencing_token=fencing_token)
        return await self._evidence(
            actor, run_id, "diff", result, plan_id=plan.id, repository_sha256=self._repository_digest(result)
        )

    async def verify_browser(
        self, actor: RunActor, run_id: UUID, path: str, expected_text: str, fencing_token: int | None
    ) -> BrowserEvidence:
        plan = await self._approved(actor, run_id, "browser.verify", fencing_token)
        result = await self._controlled(
            actor, run_id, "browser.verify", fencing_token, self.browser.verify(run_id, path, expected_text)
        )
        await self.access.authorize(actor, run_id, write=True, fencing_token=fencing_token)
        artifact = await self._evidence(
            actor, run_id, "browser", SandboxResult(result.status, result.report), plan_id=plan.id
        )
        screenshot = None
        if result.screenshot_base64:
            screenshot = await self._evidence(
                actor, run_id, "screenshot", SandboxResult(result.status, result.screenshot_base64), plan_id=plan.id
            )
        return BrowserEvidence(artifact, screenshot)

    async def artifacts(self, actor: RunActor, run_id: UUID) -> list[ArtifactRecord]:
        await self.access.authorize(actor, run_id)
        now = datetime.now(UTC)
        return [
            replace(record, body=None, command=(), status="EXPIRED") if record.expires_at <= now else record
            for record in await self.store.artifacts(actor.workspace_id, run_id)
        ]

    async def reply_screenshots(
        self, actor: RunActor, run_id: UUID, *, artifact_ids: tuple[str, ...] = (), report: str = ""
    ) -> list[ArtifactRecord]:
        """只从同 Task 的可访问运行选择有效截图，模型不能指定文件或外链。"""
        await self.access.authorize(actor, run_id)
        if len(artifact_ids) > 2:
            raise DeliveryError("SCREENSHOT_UNAVAILABLE", "回复最多附两张截图")
        references = list(artifact_ids) or re.findall(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", report)
        history = await self.workspace_history(actor, run_id) if self.workspace_history and references else ()
        screenshots: dict[str, ArtifactRecord] = {}
        for previous in (run_id, *tuple(reversed(history))[:20]):
            for record in reversed(await self.artifacts(actor, previous)):
                if (
                    record.kind not in {"computer_screenshot", "screenshot"}
                    or record.status != "PASSED" or not record.body
                ):
                    continue
                if hashlib.sha256(record.body.encode()).hexdigest() != record.sha256:
                    continue
                try:
                    image = base64.b64decode(record.body, validate=True)
                except (ValueError, binascii.Error):
                    continue
                if len(image) <= 1_000_000 and image.startswith(b"\x89PNG\r\n\x1a\n"):
                    screenshots[str(record.id)] = record
        if artifact_ids and any(identity not in screenshots for identity in artifact_ids):
            raise DeliveryError("SCREENSHOT_UNAVAILABLE", "截图已失效或不属于本任务")
        selected = [screenshots[identity] for identity in dict.fromkeys(references) if identity in screenshots]
        return (selected or ([] if references else list(screenshots.values())))[:2]

    async def publish_branch(
        self, actor: RunActor, run_id: UUID, title: str, fencing_token: int | None
    ) -> RemoteRecord:
        """推送与 MR 分开记录；未知结果不自动重推，成功分支不能冒充已创建 MR。"""
        plan = await self._approved(actor, run_id, "git.push", fencing_token)
        source = await self._source_branch(actor, plan.scope, run_id) if plan.scope.source_delivery_id else None
        if "git.commit" not in plan.scope.actions or (
            self.update_branch is None if source else self.push_branch is None
        ):
            raise DeliveryError("ACTION_DENIED", "提交和推送必须分别批准且已配置发布器")
        if not title.strip() or len(title) > 200:
            raise DeliveryError("INVALID_PR", "提交标题长度错误")
        await self.validate_completion(actor, run_id, fencing_token or 0, check_remote=False)
        key = hashlib.sha256(f"{run_id}:{plan.id}:{plan.scope.digest}:branch-push".encode()).hexdigest()
        existing = await self.store.get_remote(actor.workspace_id, run_id, key)
        if existing:
            if existing.status == "SUCCEEDED":
                return existing
            raise DeliveryError("RECOVERY_REQUIRED", "上次分支交付需要核对，不会自动重推")
        now = datetime.now(UTC)
        pending = RemoteRecord(
            uuid4(),
            run_id,
            actor.workspace_id,
            key,
            "SENDING",
            1,
            now,
            now,
            remote_id="branch:pending",
            plan_id=plan.id,
        )
        if not await self.store.claim_remote(pending):
            raise DeliveryError("DELIVERY_IN_PROGRESS", "该分支交付正在处理")
        try:
            if source:
                assert self.update_branch
                publishing = self.update_branch(actor, run_id, plan.scope, source, key, title)
            else:
                assert self.push_branch
                publishing = self.push_branch(actor, run_id, plan.scope, key, title)
            result = await self._controlled(actor, run_id, "git.push", fencing_token, publishing)
            await self._approved(actor, run_id, "git.push", fencing_token)
        except (DeliveryError, TimeoutError, ConnectionError, asyncio.CancelledError):
            await self.store.finish_remote(
                replace(pending, status="UNKNOWN", updated_at=datetime.now(UTC), error_code="BRANCH_RESULT_UNKNOWN"),
                expected_status="SENDING",
            )
            await self.access.plan_changed(actor, run_id, "RECOVERY_REQUIRED")
            raise
        done = replace(
            pending, status="SUCCEEDED", remote_id=result.remote_id, url=result.url, updated_at=datetime.now(UTC)
        )
        await self.store.finish_remote(done, expected_status="SENDING")
        return done

    async def _source_branch(self, actor: RunActor, scope: PlanScope, run_id: UUID | None = None) -> RemoteRecord:
        """跨 Run 引用必须重新验证私有可见性及原仓库授权，不能凭 UUID 借用他人的分支。"""
        if not scope.source_delivery_id:
            raise DeliveryError("BRANCH_REQUIRED", "缺少已成功分支交付记录")
        source = await self.store.remote_by_id(actor.workspace_id, UUID(scope.source_delivery_id))
        if source is None:
            raise DeliveryError("BRANCH_REQUIRED", "分支交付记录不可访问")
        await self.access.authorize(actor, source.run_id)
        if "git.push" in scope.actions:
            if (
                run_id is None
                or source.run_id != run_id
                and (not self.workspace_history or source.run_id not in await self.workspace_history(actor, run_id))
            ):
                raise DeliveryError("BRANCH_TASK_MISMATCH", "续写只能引用同一 Task 的前序成功分支")
            if scope.source_branch and source.remote_id != f"branch:{scope.source_branch}:{scope.source_revision}":
                raise DeliveryError("BRANCH_CHANGED", "冻结的分支交付记录发生变化")
        original = await self.store.get_plan(actor.workspace_id, source.plan_id) if source.plan_id else None
        if (
            source.status != "SUCCEEDED"
            or not (source.remote_id or "").startswith("branch:weora/")
            or original is None
            # 后续提案会替换旧批准，但已成功交付的分支事实仍可作为新审批的引用。
            # 撤销的计划、从未批准的 SUPERSEDED 计划继续拒绝。
            or not (
                original.status == "APPROVED"
                or original.status == "SUPERSEDED"
                and original.decided_by is not None
                and original.decided_at is not None
            )
            or original.created_at + timedelta(days=self.retention_days) <= datetime.now(UTC)
        ):
            raise DeliveryError("BRANCH_REQUIRED", "原分支未确认、已撤权或证据已过期")
        fields = (source.remote_id or "").split(":")
        pr_key = hashlib.sha256(f"{source.run_id}:{original.id}:{original.scope.digest}".encode()).hexdigest()
        pr_branch_key = hashlib.sha256((pr_key + ":branch").encode()).hexdigest()
        expected_branch = (
            original.scope.source_branch
            or "weora/" + (pr_key if source.business_key == pr_branch_key else source.business_key)[:24]
        )
        if (
            len(fields) != 3
            or fields[1] != expected_branch
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[2])
        ):
            raise DeliveryError("BRANCH_REQUIRED", "分支提交与原交付业务键或批准的续写来源不匹配")
        bound_fields = ("repository_id", "base_revision", "paths", "remote_id", "target_branch")
        if any(getattr(original.scope, field) != getattr(scope, field) for field in bound_fields):
            raise DeliveryError("REMOTE_DENIED", "MR 范围必须匹配原已交付仓库、固定基准和路径")
        if self.validate_scope is not None:
            await self.validate_scope(actor, source.run_id, original.scope)
        return source

    async def continuation_branches(self, actor: RunActor, run_id: UUID) -> list[dict[str, object]]:
        """只向本 Task 提供仍可访问的前序分支引用；历史元数据缺失时不猜测 SHA。"""
        await self.access.authorize(actor, run_id)
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        history = await self.workspace_history(actor, run_id) if self.workspace_history else ()
        for previous in (run_id, *tuple(reversed(history))[:20]):
            for record in reversed(await self.store.remotes(actor.workspace_id, previous)):
                if record.status != "SUCCEEDED" or not (record.remote_id or "").startswith("branch:weora/"):
                    continue
                plan = await self.store.get_plan(actor.workspace_id, record.plan_id) if record.plan_id else None
                if not plan:
                    continue
                candidate = replace(
                    plan.scope,
                    actions=("git.commit", "git.push"),
                    source_delivery_id=str(record.id),
                    source_branch=None,
                    source_revision=None,
                )
                try:
                    await self._source_branch(actor, candidate, run_id)
                except DeliveryError:
                    continue
                fields = (record.remote_id or "").split(":")
                if len(fields) != 3 or fields[1] in seen:
                    continue
                seen.add(fields[1])
                result.append(
                    {
                        "delivery_id": str(record.id),
                        "branch": fields[1],
                        "revision": fields[2],
                        "repository_id": plan.scope.repository_id,
                        "remote_id": plan.scope.remote_id,
                        "base_revision": plan.scope.base_revision,
                        "target_branch": plan.scope.target_branch,
                        "paths": list(plan.scope.paths),
                    }
                )
        return result

    async def _record_pr_branch(self, pending: RemoteRecord, result: RemoteResult) -> None:
        """PR 成功响应中的源分支另记不可变事实，供下一 Run 精确审批续写。"""
        if result.branch_name is None and result.branch_revision is None:
            return
        if result.branch_name != "weora/" + pending.business_key[:24] or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", result.branch_revision or ""
        ):
            raise DeliveryError("RECOVERY_REQUIRED", "供应商没有返回可确认的分支提交，不猜测续写目标")
        identity = hashlib.sha256((pending.business_key + ":branch").encode()).hexdigest()
        branch = replace(
            pending,
            id=uuid5(NAMESPACE_URL, "weora-pr-branch:" + identity),
            business_key=identity,
            status="SUCCEEDED",
            remote_id=f"branch:{result.branch_name}:{result.branch_revision}",
            url=result.url,
        )
        if not await self.store.claim_remote(branch):
            existing = await self.store.remote_by_id(pending.workspace_id, branch.id)
            if not existing or existing.remote_id != branch.remote_id or existing.status != "SUCCEEDED":
                raise DeliveryError("RECOVERY_REQUIRED", "源分支记录冲突，请先只读核对")

    async def create_review(
        self, actor: RunActor, run_id: UUID, title: str, body: str, fencing_token: int | None
    ) -> RemoteRecord:
        """普通 MR 独立审批；只消费持久分支事实，不调用沙箱、提交或推送。"""
        plan = await self._approved(actor, run_id, "review.create", fencing_token)
        source = await self._source_branch(actor, plan.scope)
        if self.review_creator is None:
            raise DeliveryError("CAPABILITY_UNSUPPORTED", "尚未配置普通 MR 创建能力")
        if not title.strip() or len(title) > 200 or len(body) > 12000:
            raise DeliveryError("INVALID_REVIEW", "MR 标题或说明长度不符合限制")
        # 同一分支只能由一个业务键创建 MR；不同 Run 重试也不能制造重复评审。
        key = hashlib.sha256(f"{actor.workspace_id}:{source.id}:review".encode()).hexdigest()
        record_id = uuid5(NAMESPACE_URL, "weora-review:" + key)
        existing = await self.store.remote_by_id(actor.workspace_id, record_id)
        if existing is not None:
            if existing.run_id != run_id:
                raise DeliveryError("REVIEW_ALREADY_REQUESTED", "该分支已有 MR 请求，请回原任务核对，不重复创建")
            if existing.status == "SUCCEEDED":
                return existing
            if existing.status != "NOT_SENT":
                raise DeliveryError("RECOVERY_REQUIRED", "MR 结果尚待核对，不自动重建")
            if existing.plan_id == plan.id:
                raise DeliveryError("APPROVAL_REQUIRED", "预检未发送的请求也需要新计划审批，不自动重试")
        now = datetime.now(UTC)
        pending = RemoteRecord(
            record_id,
            run_id,
            actor.workspace_id,
            key,
            "SENDING",
            1,
            now,
            now,
            remote_id="review:pending",
            plan_id=plan.id,
        )
        if existing is not None:
            pending = replace(pending, created_at=existing.created_at, attempts=existing.attempts + 1)
            claimed = await self.store.finish_remote(pending, expected_status="NOT_SENT")
        else:
            claimed = await self.store.claim_remote(pending)
        if not claimed:
            raise DeliveryError("DELIVERY_IN_PROGRESS", "该分支已有 MR 交付请求，请核对原任务")
        try:
            result = await self._controlled(
                actor,
                run_id,
                "review.create",
                fencing_token,
                self.review_creator(actor, run_id, plan.scope, source, key, title, body),
            )
            await self._approved(actor, run_id, "review.create", fencing_token)
        except (DeliveryError, TimeoutError, ConnectionError, asyncio.CancelledError) as error:
            # POST 可能已经生效；即使响应不合法，也保留 UNKNOWN，禁止把超时当作未创建。
            await self.store.finish_remote(
                replace(
                    pending,
                    status="NOT_SENT" if isinstance(error, ReviewNotSent) else "UNKNOWN",
                    updated_at=datetime.now(UTC),
                    error_code=error.code if isinstance(error, DeliveryError) else "REVIEW_RESULT_UNKNOWN",
                ),
                expected_status="SENDING",
            )
            await self.access.plan_changed(actor, run_id, "RECOVERY_REQUIRED")
            raise
        done = replace(
            pending,
            status="SUCCEEDED",
            remote_id="review:" + result.remote_id,
            url=result.url,
            updated_at=datetime.now(UTC),
        )
        if not await self.store.finish_remote(done, expected_status="SENDING"):
            raise DeliveryError("RECOVERY_REQUIRED", "远端响应已收到，但持久状态未确认，请只读核对")
        return done

    async def create_pr(
        self, actor: RunActor, run_id: UUID, title: str, body: str, fencing_token: int | None
    ) -> RemoteRecord:
        """先占业务键再调用供应商；未知结果只能对账，不能盲目重发。"""
        plan = await self._approved(actor, run_id, "pr.create", fencing_token)
        if not {"git.commit", "git.push", "pr.create"} <= set(plan.scope.actions):
            raise DeliveryError("ACTION_DENIED", "提交、推送和草稿 PR 必须分别获批")
        if not title.strip() or len(title) > 200 or len(body) > 16000:
            raise DeliveryError("INVALID_PR", "PR 标题或说明长度错误")
        key = hashlib.sha256(f"{run_id}:{plan.id}:{plan.scope.digest}:draft-pr".encode()).hexdigest()
        existing = await self.store.get_remote(actor.workspace_id, run_id, key)
        if existing:
            if existing.status == "SUCCEEDED":
                return existing
            if existing.status in {"SENDING", "UNKNOWN", "RECOVERY_REQUIRED"}:
                found = await self.remote.reconcile(plan.scope, key)
                await self._approved(actor, run_id, "pr.create", fencing_token)
                if found:
                    done = replace(
                        existing,
                        status="SUCCEEDED",
                        remote_id=found.remote_id,
                        url=found.url,
                        updated_at=datetime.now(UTC),
                        error_code=None,
                    )
                    await self.store.finish_remote(done, expected_status=existing.status)
                    return done
                raise DeliveryError("RECOVERY_REQUIRED", "远端结果仍不确定，请先对账，不会重复创建 PR")
            raise DeliveryError("DELIVERY_FAILED", "该交付已失败，请核对原因并通过新计划明确授权重试")
        now = datetime.now(UTC)
        pending = RemoteRecord(uuid4(), run_id, actor.workspace_id, key, "SENDING", 1, now, now, plan_id=plan.id)
        if not await self.store.claim_remote(pending):
            raise DeliveryError("DELIVERY_IN_PROGRESS", "该交付正在处理，请刷新")
        remote_started = False
        try:
            await self._approved(actor, run_id, "pr.create", fencing_token)
            # 在网络副作用前检查实际 Diff 没有越过批准路径。
            diff = await self.sandbox.diff(run_id, plan.scope)
            if diff.status != "PASSED":
                raise DeliveryError("DIFF_NOT_READY", "没有可安全交付的沙箱 Diff")
            await self._evidence(actor, run_id, "diff", diff, plan_id=plan.id)
            remote_started = True
            result = await self._controlled(
                actor,
                run_id,
                "pr.create",
                fencing_token,
                self.remote.create(run_id, plan.scope, key, title, body),
            )
            await self.access.authorize(actor, run_id, write=True, fencing_token=fencing_token)
            await self._record_pr_branch(pending, result)
        except (TimeoutError, ConnectionError, asyncio.CancelledError) as error:
            uncertain = replace(pending, status="UNKNOWN", updated_at=datetime.now(UTC), error_code="REMOTE_UNKNOWN")
            await self.store.finish_remote(uncertain, expected_status="SENDING")
            if isinstance(error, asyncio.CancelledError):
                raise
            await self.access.plan_changed(actor, run_id, "RECOVERY_REQUIRED")
            raise DeliveryError("RECOVERY_REQUIRED", "远端请求结果未知，需要先核对远端") from None
        except DeliveryError as exc:
            # 调用远端之后的协议错误、撤权或租约丢失都不能证明副作用未发生。
            unknown = remote_started and exc.code not in {
                "PR_NOT_CONFIGURED",
                "REMOTE_DENIED",
                "EMPTY_DIFF",
                "CAPABILITY_UNSUPPORTED",
                "REMOTE_BINDING_CHANGED",
            }
            failed = replace(
                pending, status="UNKNOWN" if unknown else "FAILED", updated_at=datetime.now(UTC), error_code=exc.code
            )
            await self.store.finish_remote(failed, expected_status="SENDING")
            if unknown:
                await self.access.plan_changed(actor, run_id, "RECOVERY_REQUIRED")
                raise DeliveryError("RECOVERY_REQUIRED", "远端动作结果需要核对，禁止自动重放") from None
            raise
        done = replace(
            pending, status="SUCCEEDED", updated_at=datetime.now(UTC), remote_id=result.remote_id, url=result.url
        )
        await self.store.finish_remote(done, expected_status="SENDING")
        return done

    async def deliveries(self, actor: RunActor, run_id: UUID) -> list[RemoteRecord]:
        await self.access.authorize(actor, run_id)
        return await self.store.remotes(actor.workspace_id, run_id)

    async def reconcile_pr(self, actor: RunActor, run_id: UUID) -> RemoteRecord | None:
        """只读核对未知结果；无需活跃执行租约，也绝不重建提交、分支或 PR。"""
        await self.access.authorize(actor, run_id)
        records = await self.store.remotes(actor.workspace_id, run_id)
        if not records:
            return None
        record = records[-1]
        if (record.remote_id or "").startswith("branch:"):
            return record  # 分支 UNKNOWN 保留人工核对，不能拿 MR 对账器误判或重发。
        if record.status == "SUCCEEDED":
            return record
        plan = (
            await self.store.get_plan(actor.workspace_id, record.plan_id)
            if record.plan_id
            else await self.store.latest_plan(actor.workspace_id, run_id)
        )
        if plan is None or plan.run_id != run_id:
            raise DeliveryError("RECOVERY_REQUIRED", "缺少原交付计划，不能猜测远端或目标分支")
        if (record.remote_id or "").startswith("review:"):
            source = await self._source_branch(actor, plan.scope)
            if self.review_reconciler is None:
                raise DeliveryError("CAPABILITY_UNSUPPORTED", "未配置普通 MR 只读核对")
            found = await self.review_reconciler(actor, run_id, plan.scope, source, record.business_key)
            if found:
                found = replace(found, remote_id="review:" + found.remote_id)
        else:
            found = await self.remote.reconcile(plan.scope, record.business_key)
        await self.access.authorize(actor, run_id)
        updated = replace(
            record,
            status="SUCCEEDED" if found else "RECOVERY_REQUIRED",
            updated_at=datetime.now(UTC),
            remote_id=found.remote_id if found else record.remote_id,
            url=found.url if found else record.url,
            error_code=None if found else "REMOTE_UNCONFIRMED",
        )
        if not await self.store.finish_remote(updated, expected_status=record.status):
            return await self.store.get_remote(actor.workspace_id, run_id, record.business_key)
        return updated

    async def purge_runs(self, workspace_id: UUID, run_ids: tuple[UUID, ...]) -> int:
        """来源 TTL/撤权清理入口；删除派生正文并撤销批准，不等待产物自身保留期。"""
        return await self.store.purge_runs(workspace_id, run_ids)

    async def purge_expired(self) -> int:
        """后台保留审计哈希和命令元数据，清除所有产物正文副本。"""
        return await self.store.purge_expired(datetime.now(UTC))

    async def validate_completion(
        self, actor: RunActor, run_id: UUID, fencing_token: int, *, check_remote: bool = True
    ) -> None:
        """平台完成门槛：检查事实证据，不能以模型的一句“完成”代替研发交付。"""
        await self.access.authorize(actor, run_id)
        plan = await self.store.latest_plan(actor.workspace_id, run_id)
        if plan is None:
            return  # 咨询与历史查询不强加改码/测试要求。
        await self.access.authorize(actor, run_id, write=True, fencing_token=fencing_token)
        if plan.status != "APPROVED":
            raise DeliveryError("APPROVAL_REQUIRED", "研发计划未获批准，不能完成执行步骤")
        if not plan.expires_at or plan.expires_at <= datetime.now(UTC):
            raise DeliveryError("APPROVAL_EXPIRED", "审批已经过期，不能提交成功状态")
        actions = set(plan.scope.actions)
        if "review.create" in actions:
            await self._source_branch(actor, plan.scope)
            reviews = [
                item
                for item in await self.store.remotes(actor.workspace_id, run_id)
                if item.plan_id == plan.id and (item.remote_id or "").startswith("review:")
            ]
            if not reviews or reviews[-1].status != "SUCCEEDED":
                raise DeliveryError("MISSING_EVIDENCE", "普通 MR 尚未确认创建成功")
            return  # 不重新执行已完成分支的测试，也不把普通 MR 称为草稿。
        coding = "file.write" in actions
        if coding and (not {"command.run", "git.diff"} <= actions or not plan.scope.commands):
            raise DeliveryError("MISSING_EVIDENCE", "改码计划必须先明确批准测试命令与 Diff 验证")
        final_state: str | None = None
        if "git.diff" in actions:
            final_diff = await self.diff(actor, run_id, fencing_token)
            if final_diff.status != "PASSED":
                raise DeliveryError("FAILED_EVIDENCE", "真实修改 Diff 未通过范围校验")
            if coding and not (final_diff.body or "").strip():
                raise DeliveryError("MISSING_EVIDENCE", "没有通过范围校验的真实修改 Diff")
            final_state = final_diff.repository_sha256
        elif plan.scope.commands:
            current = await self._controlled(
                actor, run_id, "command.run", fencing_token, self.sandbox.diff(run_id, plan.scope)
            )
            final_state = self._repository_digest(current)
        evidence = [item for item in await self.artifacts(actor, run_id) if item.plan_id == plan.id]
        writes = [item for item in evidence if item.kind == "file.write" and item.status == "PASSED"]
        if coding and not writes:
            raise DeliveryError("MISSING_EVIDENCE", "没有实际文件修改证据")
        last_write = max((item.sequence for item in writes), default=0)
        for command in plan.scope.commands:
            matching = [
                item
                for item in evidence
                if item.kind == "command" and item.command == command and item.sequence > last_write
            ]
            if not matching:
                raise DeliveryError("MISSING_EVIDENCE", "修改后尚未运行全部已批准的验证命令")
            if matching[-1].status != "PASSED" or matching[-1].exit_code != 0:
                raise DeliveryError("FAILED_EVIDENCE", "修改后尚未通过全部已批准的验证命令")
            if not final_state or matching[-1].repository_sha256 != final_state:
                raise DeliveryError("MISSING_EVIDENCE", "命令没有绑定最终仓库状态，需要针对当前代码重新验证")
        if "browser.verify" in actions:
            checks = [item for item in evidence if item.kind == "browser" and item.sequence > last_write]
            screenshots = [
                item
                for item in evidence
                if item.kind == "screenshot" and item.sequence > last_write and item.status == "PASSED" and item.body
            ]
            if not checks or not screenshots:
                raise DeliveryError("MISSING_EVIDENCE", "浏览器验证未配置、未执行或缺少有效截图")
            if checks[-1].status != "PASSED":
                raise DeliveryError("FAILED_EVIDENCE", "浏览器验证未通过")
        if check_remote and "git.push" in actions and "pr.create" not in actions:
            branches = [
                item
                for item in await self.store.remotes(actor.workspace_id, run_id)
                if item.plan_id == plan.id and (item.remote_id or "").startswith("branch:")
            ]
            if not branches or branches[-1].status != "SUCCEEDED":
                raise DeliveryError("MISSING_EVIDENCE", "测试分支尚未确认推送成功")
        if check_remote and "pr.create" in actions:
            deliveries = [
                item
                for item in await self.store.remotes(actor.workspace_id, run_id)
                if item.plan_id == plan.id and not (item.remote_id or "").startswith("branch:")
            ]
            if not deliveries or deliveries[-1].status != "SUCCEEDED":
                raise DeliveryError("MISSING_EVIDENCE", "草稿 PR 尚未确认创建成功")

    async def cleanup(self, actor: RunActor, run_id: UUID) -> bool:
        """Worker 在终态或失效时清理自己拥有的容器；失败保留补偿证据。"""
        try:
            await self.sandbox.cleanup(run_id)
            return True
        except DeliveryError as error:
            if error.code == "SANDBOX_NOT_CONFIGURED":
                return True
            await self.record_cleanup_failure(actor, run_id)
            return False

    async def record_cleanup_failure(self, actor: RunActor, run_id: UUID) -> None:
        """供可信后台回收器记录失败，不把 Docker 错误正文或宿主路径复制进产物。"""
        await self._evidence(
            actor, run_id, "cleanup", SandboxResult("CLEANUP_REQUIRED", "隔离容器清理未确认，需后台补偿清理")
        )
