"""真实无网络容器的跨 Run 文件恢复；只创建随机命名容器，使用已配置的本地测试快照。"""

import json
import os
from collections.abc import Awaitable
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from tests.delivery.fakes import MemoryStore
from tests.delivery.test_workspaces import TaskAccess
from weora.bootstrap.settings import AppSettings
from weora.modules.delivery.application.workspaces import WorkspaceContinuity
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.docker import DockerConfig, DockerSandbox, RepositorySnapshot
from weora.modules.runs.application.ports import RunActor

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_actual_docker_preserves_bytes_deletion_mode_and_requires_new_write_scope() -> None:
    if os.environ.get("WEORA_TEST_DOCKER") != "1":
        pytest.skip("真实 Docker 验收需要显式 WEORA_TEST_DOCKER=1")
    settings = AppSettings()
    assert settings.sandbox_image and settings.sandbox_repository_archive and settings.sandbox_repository_id
    config = DockerConfig(
        settings.sandbox_image,
        {
            settings.sandbox_repository_id: RepositorySnapshot(
                Path(settings.sandbox_repository_archive),
                settings.sandbox_repository_revision or "",
                settings.sandbox_repository_sha256 or "",
            )
        },
        executable=settings.sandbox_cli_executable,
        executable_args=settings.sandbox_cli_args,
        deployment_id="workspace-parity-test-" + uuid4().hex[:12],
    )
    sandbox = DockerSandbox(config)
    actor, first, second = RunActor(uuid4(), uuid4()), uuid4(), uuid4()
    scope = PlanScope(
        settings.sandbox_repository_id,
        settings.sandbox_repository_revision or "",
        ("src",),
        (),
        ("file.write", "git.diff", "command.run"),
    )
    access, store = TaskAccess(actor, {first, second}), MemoryStore()

    async def history(actor: RunActor, run_id: UUID) -> tuple[UUID, ...]:
        return (first,)

    async def guard(operation: Awaitable[None]) -> None:
        await operation

    continuity = WorkspaceContinuity(store, access, sandbox, history, retention_days=7)
    try:
        await sandbox.prepare(first, scope)
        # 固定验收快照 src/calc.py 存在；删除、可执行位和 UTF-8 字节一起覆盖恢复差异。
        modified = await sandbox.command(
            first,
            (
                "python3",
                "-c",
                "import pathlib,os; p=pathlib.Path('src/continued.sh'); "
                "p.write_bytes('连续任务\\n'.encode()); os.chmod(p,0o755); pathlib.Path('src/calc.py').unlink()",
            ),
        )
        assert modified.status == "PASSED", modified.output
        await continuity.capture(actor, first, scope)
        await sandbox.cleanup(first)
        await sandbox.prepare(second, scope)
        with pytest.raises(DeliveryError, match="本轮审批"):
            await continuity.restore(actor, second, replace(scope, actions=("git.diff",)), guard)
        await continuity.restore(actor, second, scope, guard)
        checked = await sandbox.command(
            second,
            (
                "python3",
                "-c",
                "import pathlib,os,json; p=pathlib.Path('src/continued.sh'); "
                "print(json.dumps({'text':p.read_text(),'mode':oct(p.stat().st_mode&0o777),"
                "'deleted':not pathlib.Path('src/calc.py').exists()}))",
            ),
        )
        assert checked.status == "PASSED", checked.output
        assert json.loads(checked.output) == {"text": "连续任务\n", "mode": "0o755", "deleted": True}
    finally:
        await sandbox.cleanup(first)
        await sandbox.cleanup(second)
