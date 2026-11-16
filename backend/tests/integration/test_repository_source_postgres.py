"""独立仓库来源在真实 PostgreSQL 中创建原生 Task，并持续保存已订阅 PR 的自动事实。"""

from collections.abc import AsyncGenerator, Callable
from inspect import unwrap
from typing import cast

import pytest

from tests.api.test_runs import RunsHarness
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as context_fixture
from tests.coordination.test_repository_discussions import (
    test_owned_external_pull_keeps_ci_cursor_full_note_and_waits_for_human as subscription_scenario,
)
from tests.coordination.test_repository_source import (
    test_repository_intake_preserves_managed_delivery_on_same_task as native_source_scenario,
)
from tests.integration.test_approval_recovery_postgres import PostgresHarness
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, PI_RPC_RUNTIME

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.mark.parametrize("scenario", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME, "ci", "blocked_ci"])
async def test_repository_source_keeps_native_origin_and_subscriptions(
    postgres_runs: PostgresHarness, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    # 复用同一业务验收用例，仅替换权威数据库，避免两个测试各自维护不同的行为规则。
    fixture = cast(Callable[[RunsHarness], AsyncGenerator[ContextRuns, None]], unwrap(context_fixture))
    context = fixture(postgres_runs.runs)
    harness = await anext(context)
    try:
        if scenario in (PI_RPC_RUNTIME, OMP_RPC_RUNTIME):
            await native_source_scenario(harness, scenario)
        else:
            await subscription_scenario(harness, monkeypatch, scenario == "blocked_ci")
    finally:
        await context.aclose()
