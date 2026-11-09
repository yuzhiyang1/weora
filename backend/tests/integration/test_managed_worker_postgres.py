"""真实 PostgreSQL 行锁下并发投递子会话指令，模型端口仍使用脚本夹具。"""

from collections.abc import AsyncGenerator, Callable
from inspect import unwrap
from typing import cast

import pytest

from tests.api.test_runs import RunsHarness
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as context_fixture
from tests.coordination.test_managed_worker import (
    test_child_steering_completion_keeps_run_slot_and_handoff_cancels_descendants as managed_scenario,
)
from tests.integration.test_approval_recovery_postgres import PostgresHarness
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.mark.parametrize("handoff", [False, True])
async def test_actual_row_locks_preserve_child_input_and_results(
    postgres_runs: PostgresHarness, monkeypatch: pytest.MonkeyPatch, handoff: bool
) -> None:
    # pytest 的装饰器不公开原函数类型；边界处恢复这个夹具唯一的输入与产出契约。
    fixture = cast(Callable[[RunsHarness], AsyncGenerator[ContextRuns, None]], unwrap(context_fixture))
    context = fixture(postgres_runs.runs)
    harness = await anext(context)
    try:
        await managed_scenario(harness, monkeypatch, handoff)
    finally:
        await context.aclose()
