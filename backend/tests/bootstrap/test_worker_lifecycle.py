"""实际 Worker 编排入口使用隔离数据库与 Fake Runtime，覆盖 finally 容器清理。"""

from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr

from tests.api.test_run_execution import PROFILE, configured
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.delivery.fakes import Sandbox
from weora.bootstrap import harness, worker
from weora.bootstrap.run_profile import execution_profile
from weora.bootstrap.settings import AppSettings
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile

worker_runs = configured
RUNTIME_ENTRY = str(Path(__file__).resolve())


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_worker_releases_owned_sandbox_on_success_or_model_failure(
    worker_runs: RunsHarness,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    runs = worker_runs
    settings = cast(AppSettings, runs.app.state.settings).model_copy(
        update={
            "run_runtime": "pi",
            "pi_model": PROFILE.model_id,
            "deepseek_api_key": SecretStr("not-real-key"),
            "pi_runtime_entry": RUNTIME_ENTRY,
            "run_model_max_tokens": PROFILE.max_tokens,
            "run_model_timeout_seconds": PROFILE.timeout_seconds,
        }
    )
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    # 提交快照和 Worker 使用同一份新配置，旧快照的默认时限另由恢复测试覆盖。
    runs.app.state.run_service._execution_profile = execution_profile(settings)
    monkeypatch.setattr(worker, "create_app", lambda _: runs.app)
    cleaned: list[UUID] = []

    class OwnedSandbox(Sandbox):
        async def cleanup(self, run_id: UUID) -> None:
            cleaned.append(run_id)

    class Runtime:
        def __init__(self, **_: object) -> None:
            pass

        async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
            if fail:
                raise RuntimeFailure("MODEL_FAILED")
            return "仅用于平台生命周期测试的回答。"

    cast(DeliveryService, runs.app.state.delivery_service).sandbox = OwnedSandbox()
    monkeypatch.setattr(harness, "PiTextRuntime", Runtime)
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    run_id = UUID(created.json()["id"])
    assert await worker._execute(UUID(runs.workspace_id), run_id) is True
    detail = await runs.client.get(f"{runs.path}/{run_id}")
    assert detail.json()["status"] == ("FAILED" if fail else "SUCCEEDED")
    assert cleaned == [run_id]
