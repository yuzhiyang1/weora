"""验证 Python 适配器的隔离地址、凭据和接管顺序。"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from weora.modules.collaboration.application.tasks import TaskRecord
from weora.modules.computers.openbot import ComputerError, OpenBot
from weora.modules.computers.policy import ComputerMode, computer_mode


@pytest.mark.anyio
async def test_take_and_release_use_only_the_derived_computer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = OpenBot("http://supervisor:4300", "supervisor-secret", "master-secret", "weora")
    identity = bot.computer_id(uuid4(), uuid4(), uuid4())
    calls: list[tuple[str, dict[str, object] | None]] = []
    responses: list[dict[str, object]] = [
        {"holder": "bot"},
        {"holder": "bot", "request": {"status": "waiting", "id": "take-1"}},
        {"holder": "human"},
        {"holder": "human", "request": {"status": "taken", "id": "take-1"}},
        {"holder": "bot"},
    ]

    async def request(
        url: str, token: str, body: dict[str, object] | None = None, *, deadline_seconds: int = 12
    ) -> dict[str, object]:
        assert url.startswith(f"http://weora-computer-{identity}:4100/")
        assert len(token) == 64 and token not in {"supervisor-secret", "master-secret"}
        calls.append((url.rsplit(":4100", 1)[1], body))
        return responses.pop(0)

    monkeypatch.setattr(bot, "_request", request)
    await bot.control(identity, "take")
    await bot.control(identity, "release")
    assert calls == [
        ("/control", None),
        ("/control/request", {"reason": "Weora 用户请求接管桌面"}),
        ("/control/take", {"requestId": "take-1"}),
        ("/control", None),
        ("/control/release", {"requestId": "take-1"}),
    ]


def test_feishu_computer_requires_exact_scope_and_real_assignment() -> None:
    task = TaskRecord(uuid4(), uuid4(), uuid4(), "验收", datetime.now(UTC))
    scope_id = "a" * 64
    scopes: dict[str, ComputerMode] = {scope_id: "observe"}
    origin: dict[str, object] = {
        "coordination_scope_id": scope_id,
        "coordination_ticket_id": "ticket",
        "coordination_assignment_id": "assignment",
    }
    group = replace(task, audience="group", origin="feishu", origin_metadata=origin)
    assert computer_mode(task, scopes) == "test"
    assert computer_mode(group, {}) is None
    assert computer_mode(group, scopes) == "observe"
    assert computer_mode(replace(group, origin="code_hosting"), scopes) is None
    assert computer_mode(replace(group, origin_metadata={**origin, "coordination_status_only": True}), scopes) is None
    assert computer_mode(replace(group, origin_metadata={"coordination_scope_id": scope_id}), scopes) is None


def test_computer_id_is_stable_and_scoped_to_private_run() -> None:
    workspace, actor, run = uuid4(), uuid4(), uuid4()
    computer_id: Callable[..., str] = OpenBot.computer_id
    original = computer_id(workspace, actor, run)
    assert computer_id(workspace, actor, run) == original
    assert computer_id(uuid4(), actor, run) != original
    assert computer_id(workspace, uuid4(), run) != original
    assert computer_id(workspace, actor, uuid4()) != original


@pytest.mark.anyio
async def test_unexpected_control_response_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = OpenBot("http://supervisor:4300", "supervisor-secret", "master-secret", "weora")

    async def call(*_args: object) -> dict[str, object]:
        return {"holder": "human", "request": {"id": 12}}

    handler: Callable[..., Awaitable[dict[str, object]]] = call
    monkeypatch.setattr(bot, "call", handler)
    with pytest.raises(ComputerError, match="没有可以切换的接管请求"):
        await bot.control("run-local", "release")
