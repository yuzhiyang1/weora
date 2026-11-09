"""来源正文到期后删除派生副本；先关闭执行权，再依次清理各模块。"""

from typing import cast
from uuid import UUID

from fastapi import FastAPI

from weora.modules.delivery.application.service import DeliveryService
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork
from weora.modules.tools.infrastructure.store import SqlAlchemyToolStore
from weora.shared.infrastructure.database import SessionFactory


async def purge_derived_content(app: FastAPI, workspace_id: UUID, run_ids: set[UUID]) -> None:
    """失败可重复补偿；清理前访问控制已拒绝过期正文，不能等清理任务才撤权。"""
    if not run_ids:
        return
    executions = cast(SqlAlchemyExecutionStore, app.state.execution_store)
    await executions.purge_content(workspace_id, run_ids)
    await cast(SqlAlchemyToolStore, app.state.tool_store).purge_runs(workspace_id, run_ids)
    await cast(DeliveryService, app.state.delivery_service).purge_runs(workspace_id, tuple(run_ids))
    tasks = await executions.task_ids(workspace_id, run_ids)
    sessions = cast(SessionFactory, app.state.db_session_factory)
    async with SqlAlchemyRunsUnitOfWork(sessions) as uow:
        await uow.tasks.purge_content(workspace_id, tasks)
        await uow.commit()
