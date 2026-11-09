"""原会话回报只返回当前 Run 的元数据，继续执行撤权和过期检查。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest

from tests.context.test_context import inbound, prepare_channels, setup
from weora.modules.context.domain import ContextActor, ContextError, Json


@pytest.mark.parametrize("revoke", [None, "source", "binding", "expiry", "run"])
def test_run_channel_status_is_scoped_redacted_and_reauthorized(revoke: str | None) -> None:
    async def scenario() -> None:
        f = await setup()
        channels = await prepare_channels(f)
        await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
        inbox = await channels.receive(f.actor.workspace_id, f.connection, inbound())

        async def submit(actor: ContextActor, payload: Json) -> str:
            return str(f.scope.run_id)

        await channels.process(f.actor.workspace_id, str(inbox["inbox_id"]), submit)
        key = f"{f.connection}:om_trigger"
        await f.store.change(f.actor.workspace_id, "inbox", key,
                             lambda old: dict(old or {}, ingress="manual_history"))
        scope = replace(f.scope, audience="group", source_id=f.source, origin_open_id="ou_user")
        await channels.enqueue_delivery(scope, str(inbox["inbox_id"]), "never-return-answer", "run-result:test")
        await channels.deliver(scope, "run-result:test")
        await f.store.change(f.actor.workspace_id, "delivery", "foreign",
                             lambda _: {"run_id": str(uuid4()), "text": "foreign-secret"})
        if revoke == "source":
            await f.service.set_grant(f.actor, f.source, f.actor.user_id, False)
        elif revoke == "binding":
            await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id, False)
        elif revoke == "expiry":
            expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            await f.store.change(f.actor.workspace_id, "inbox", key,
                                 lambda old: dict(old or {}, expires_at=expired_at))
        elif revoke == "run":
            f.access.denied_runs.add(f.scope.run_id)
        if revoke:
            with pytest.raises(ContextError):
                await channels.run_status(scope)
            return
        result = await channels.run_status(scope)
        received = cast(Json, result["inbox"])
        deliveries = cast(list[Json], result["deliveries"])
        assert received["ingress"] == "manual_history"
        assert received["message_id"] == "om_trigger"
        assert "instruction" not in received and "open_id" not in received
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "SENT" and deliveries[0]["remote_message_id"] == "om_reply"
        assert "text" not in deliveries[0] and "sha256" not in deliveries[0]
        assert "never-return" not in str(result) and "foreign-secret" not in str(result)
        assert await channels.run_status(f.scope) == {"inbox": None, "deliveries": []}

    asyncio.run(scenario())
