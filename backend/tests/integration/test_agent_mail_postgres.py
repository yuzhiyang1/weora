"""使用真实 PostgreSQL 验证并发重试不会重复发送。"""

import pytest

from tests.api.test_agent_mail import exercise_direct
from tests.integration.test_approval_recovery_postgres import PostgresHarness
from tests.integration.test_approval_recovery_postgres import postgres_runs as postgres_runs

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_postgres_direct_mail_concurrent_retry(postgres_runs: PostgresHarness) -> None:
    await exercise_direct(postgres_runs.runs, concurrent=True)
