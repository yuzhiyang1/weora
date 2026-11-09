"""在真实 PostgreSQL 上验证跨用户事务与行锁。"""

import pytest

from tests.api.test_task_collaboration import (
    test_shared_task_handoff_isolation_and_concurrent_start as check_concurrent_handoff,
)
from tests.api.test_task_collaboration import (
    test_stop_handoff_waits_for_worker_and_rejects_external_members as check_stop_handoff,
)
from tests.api.test_task_review_inbox import (
    test_approval_inbox_follows_private_run_and_plan_version as check_approval_inbox,
)
from tests.api.test_task_review_inbox import (
    test_archive_paginates_tasks_and_restores_the_entire_group as check_archive_groups,
)
from tests.api.test_task_review_inbox import (
    test_member_submits_review_owner_returns_and_accepts as check_review_inbox,
)
from tests.integration.test_approval_recovery_postgres import PostgresHarness
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_postgres_cross_member_handoff(postgres_runs: PostgresHarness) -> None:
    await check_concurrent_handoff(postgres_runs.runs)


async def test_postgres_stop_handoff(postgres_runs: PostgresHarness) -> None:
    await check_stop_handoff(postgres_runs.runs)


async def test_postgres_review_inbox(postgres_runs: PostgresHarness) -> None:
    await check_review_inbox(postgres_runs.runs)


async def test_postgres_approval_inbox(postgres_runs: PostgresHarness) -> None:
    await check_approval_inbox(postgres_runs.runs)


async def test_postgres_archive_groups(postgres_runs: PostgresHarness) -> None:
    await check_archive_groups(postgres_runs.runs)
