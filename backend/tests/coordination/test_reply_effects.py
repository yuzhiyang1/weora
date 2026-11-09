"""执行入口区分上传准备失败与消息发送未知，不能重放可能已发出的消息。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.coordination.test_engine import setup
from weora.bootstrap import coordination
from weora.modules.context.domain import ContextError
from weora.modules.coordination.domain import Effect, Reply


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sending", "attempts", "code", "retry", "unknown"),
    [
        (False, 1, "PROVIDER_UNAVAILABLE", True, False),
        (False, 3, "PROVIDER_UNAVAILABLE", False, False),
        (False, 1, "PERMISSION_DENIED", False, False),
        (True, 1, "PROVIDER_UNAVAILABLE", False, True),
    ],
)
async def test_reply_effect_retries_only_before_message_send(
    monkeypatch: pytest.MonkeyPatch, sending: bool, attempts: int, code: str, retry: bool, unknown: bool
) -> None:
    _, state = setup()
    state.replies.append(Reply("reply", "trigger", "result", "结论", "now", state="unknown" if sending else "pending"))
    effect = Effect("effect", "reply", "reply", {"reply_id": "reply"}, attempts=attempts, owner="worker")
    store = SimpleNamespace(
        claim=AsyncMock(return_value=effect), snapshot=AsyncMock(return_value=state),
        settle=AsyncMock(), defer=AsyncMock(),
    )
    coordinator = SimpleNamespace(
        store=store, deliver=AsyncMock(side_effect=ContextError(code, "测试失败")), effect_failed=AsyncMock()
    )

    @asynccontextmanager
    async def lifespan(app: object) -> AsyncIterator[None]:
        yield

    app = SimpleNamespace(
        state=SimpleNamespace(coordination_service=coordinator, settings=SimpleNamespace(run_runtime="disabled")),
        router=SimpleNamespace(lifespan_context=lifespan),
    )
    monkeypatch.setattr(coordination, "create_app", lambda: app)
    assert not await coordination.execute(uuid4(), "scope", "effect")
    assert store.defer.await_count == int(retry)
    assert store.settle.await_count == int(not retry)
    assert coordinator.effect_failed.await_count == int(not retry)
    if not retry:
        assert store.settle.call_args.kwargs == {"error": code, "unknown": unknown}
