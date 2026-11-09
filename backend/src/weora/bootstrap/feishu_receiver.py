"""专用飞书长连接进程：ACK 前提交 Inbox，不在接收回调内执行模型。"""

import argparse
import asyncio
from typing import cast
from uuid import UUID

from weora.bootstrap.application import create_app
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import ContextError, Json
from weora.modules.context.receiver import run_long_connection
from weora.modules.context.service import ContextService
from weora.modules.context.telemetry import ReceiverTelemetry


async def _connection(workspace_id: UUID, connection_id: UUID) -> tuple[Json, str]:
    app = create_app()
    async with app.router.lifespan_context(app):
        context = cast(ContextService | None, app.state.context_service)
        if context is None:
            raise ContextError("CONTEXT_NOT_CONFIGURED", "请先安全配置上下文加密与签名密钥")
        connection = await context.store.get(workspace_id, "connection", str(connection_id))
        if not connection or not connection.get("enabled"):
            raise ContextError("CONNECTION_NOT_READY", "飞书连接未启用")
        # 只把静态加密配置带入运输进程；每个实际事件仍从数据库复核启用状态。
        return connection, app.state.settings.context_encryption_key.get_secret_value()


async def _persist(workspace_id: UUID, connection_id: UUID, payload: Json) -> Json:
    app = create_app()
    async with app.router.lifespan_context(app):
        context = cast(ContextService | None, app.state.context_service)
        if context is None:
            raise ContextError("CONTEXT_NOT_CONFIGURED")
        coordinator = app.state.coordination_service
        receipt = await FeishuChannels(context, coordinator.candidate_admission if coordinator else None).receive(
            workspace_id, connection_id, payload
        )
        if receipt.get("status") == "persisted" and coordinator:
            from weora.bootstrap.coordination_notify import notify

            notify()
        return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="Weora 飞书可靠长连接接收进程")
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--connection", type=UUID, required=True)
    arguments = parser.parse_args()
    from weora.modules.context.feishu import FernetSecrets

    try:
        connection, key = asyncio.run(_connection(arguments.workspace, arguments.connection))

        async def persist(payload: Json) -> Json:
            return await _persist(arguments.workspace, arguments.connection, payload)

        async def persist_status(status: Json) -> None:
            app = create_app()
            async with app.router.lifespan_context(app):
                context = cast(ContextService, app.state.context_service)
                data = dict(
                    status, connection_id=str(arguments.connection), connection_version=connection.get("version")
                )
                await context.store.change(
                    arguments.workspace,
                    "receiver",
                    str(arguments.connection) + ":" + str(status["instance_id"]),
                    lambda _old: data,
                )

        run_long_connection(connection, FernetSecrets(key), persist, telemetry=ReceiverTelemetry(persist_status))
    except ContextError as error:
        raise SystemExit(error.code) from None


if __name__ == "__main__":
    main()
