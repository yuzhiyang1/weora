"""原生会话复用任务权限、固定仓库快照与现有平台证据接口。"""

from uuid import UUID, uuid5

from weora.bootstrap.settings import AppSettings
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.projects.domain import ProjectActor, ProjectError, RepositoryArchive
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.execution import RunLease, RuntimeFailure, RuntimeSession
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, ToolCaller
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.application.service import RunService
from weora.modules.runs.domain.errors import RunError
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.native_rpc import NATIVE_ARCHIVE_LIMIT, NativeCodingRuntime, NativeSandbox


def native_runtime(
    settings: AppSettings,
    lease: RunLease,
    runs: RunService,
    projects: ProjectService,
    executions: SqlAlchemyExecutionStore,
    delivery: DeliveryService,
    key: str,
    session: RuntimeSession | None,
    tool: ToolCaller,
) -> NativeCodingRuntime:
    run = lease.run
    actor = RunActor(run.created_by, run.workspace_id)
    project_actor = ProjectActor(run.created_by, run.workspace_id)
    if not settings.native_rpc_image:
        raise RuntimeFailure("NATIVE_RUNTIME_NOT_AUTHORIZED")

    async def check_access() -> bool:
        if not await executions.owns_lease(lease):
            return False
        try:
            # get 复核群来源、派生正文、协作关系及成员权限，不能仅放开 audience 条件。
            detail = await runs.get(actor=actor, workspace_id=actor.workspace_id, run_id=run.id)
        except RunError:
            return False
        return not detail.task.project_context or await projects.authorize_snapshot(
            project_actor, detail.task.project_context
        )

    async def archives() -> dict[UUID, RepositoryArchive]:
        detail = await runs.access_metadata(actor, run.id)
        if not detail or not await check_access():
            raise RuntimeFailure("PROJECT_ACCESS_REVOKED")
        if not detail.task.project_context:
            return {}
        snapshot = detail.task.project_context
        repositories = snapshot.get("repositories")
        if not isinstance(repositories, list) or not repositories:
            raise RuntimeFailure("PROJECT_REPOSITORY_NOT_PREPARED")
        # 每个任务选用同引擎最早的原生 Run 作固定提交来源，之后不再次解析远端 HEAD。
        prepared: dict[UUID, RepositoryArchive] = {}
        try:
            anchor = await runs.native_session_anchor(actor, run.id)
            remaining = NATIVE_ARCHIVE_LIMIT
            for item in repositories:
                if remaining <= 0:
                    raise RuntimeFailure("NATIVE_REPOSITORY_TOO_LARGE")
                repository = UUID(str(item["id"]))
                # 保留全部选定仓库；逐仓只导出剩余总额，不能先产生超量临时文件再裁掉仓库。
                prepared[repository] = await projects.prepare_archive(
                    project_actor, anchor, snapshot, repository, max_bytes=remaining,
                )
                remaining -= prepared[repository].path.stat().st_size
                if remaining < 0:
                    raise RuntimeFailure("NATIVE_REPOSITORY_TOO_LARGE")
            return prepared
        except BaseException as error:
            for archive in prepared.values():
                archive.path.unlink(missing_ok=True)
            if isinstance(error, (ProjectError, RunError)):
                raise RuntimeFailure(error.code) from None
            raise

    sandbox_id = run.task_id
    if session and session.isolated_workspace:
        sandbox_id = uuid5(run.task_id, "child:" + session.id)
    if session and session.native_history_epoch:
        sandbox_id = uuid5(sandbox_id, "history:" + session.native_history_epoch)

    async def diff(repository: UUID, revision: str, body: str, truncated: bool) -> None:
        # 取消可保留已发生的本地提交前改动。撤权或失去租约时不再公开产物。
        if not await check_access():
            return
        if not body and not truncated:
            return
        if session and session.isolated_workspace:
            # 子工作区的 Diff 不能被误读为主工作区已应用、已复测的交付证据。
            body = (
                f"子会话隔离工作区：{sandbox_id}\n"
                "以下变更尚未合并到任务主工作区；父会话必须检查、应用并在主工作区重新测试。\n\n"
                + body
            )
        try:
            await delivery.record_native_diff(actor, run.id, repository, revision, body, truncated)
        except DeliveryError as error:
            raise RuntimeFailure(error.code) from None

    async def progress(event: str) -> None:
        await executions.native_progress(lease, event)

    sandbox = NativeSandbox(
        (settings.sandbox_cli_executable, *settings.sandbox_cli_args),
        settings.native_rpc_image,
        run.workspace_id,
        sandbox_id,
        read_only=run.recovery_read_only or bool(session and session.read_only),
        memory_mb=max(1024, settings.project_sandbox_resources.memory_mb),
        cpus=settings.project_sandbox_resources.cpus,
        pids=settings.project_sandbox_resources.pids,
        temporary_mb=settings.project_sandbox_resources.workspace_mb,
        extensions=(
            settings.native_rpc_omp_extensions
            if run.execution_profile and run.execution_profile.runtime_id == OMP_RPC_RUNTIME
            else settings.native_rpc_pi_extensions
        ),
    )
    return NativeCodingRuntime(
        sandbox,
        key=key,
        session=session,
        call=tool,
        load_repositories=archives,
        check_access=check_access,
        progress=progress,
        diff=diff,
    )
