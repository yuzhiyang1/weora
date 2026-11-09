"""持久工具预算、去重、撤权与只读路径边界。"""

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from tests.api.test_runs import RunsHarness
from weora.modules.runs.application.ports import RunActor
from weora.modules.tools.application.gateway import ToolGateway, ToolScope, ToolSpec
from weora.modules.tools.infrastructure.repository import READ_FILE, ReadOnlyRepository
from weora.modules.tools.infrastructure.store import SqlAlchemyToolStore

pytest_plugins = ["tests.api.test_runs"]


class Access:
    active = True

    async def authorize(self, scope: ToolScope, *, effect: str) -> bool:
        return self.active


@pytest.mark.anyio
async def test_persistent_calls_budget_duplicates_conflicts_and_revoked_cache(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    scope = ToolScope(UUID(created.json()["id"]), RunActor(UUID(runs.user_id), UUID(runs.workspace_id)), "owner", 1)
    store, access = SqlAlchemyToolStore(runs.sessions), Access()
    gateway = ToolGateway(store, access, max_calls=2)
    calls = 0

    async def handler(scope: ToolScope, arguments: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True, "content": "事实"}

    gateway.register(READ_FILE, handler)
    first = await gateway.invoke(scope, "one", READ_FILE.name, {"path": "README.md"})
    assert first["ok"] is True
    assert await gateway.invoke(scope, "one", READ_FILE.name, {"path": "README.md"}) == first
    assert calls == 1
    assert "TOOL_CALL_CONFLICT" in str(await gateway.invoke(scope, "one", READ_FILE.name, {"path": "other.md"}))
    assert "TOOL_ARGUMENTS_INVALID" in str(
        await gateway.invoke(scope, "bad", READ_FILE.name, {"path": "a", "actor": "admin"})
    )
    assert "TOOL_NOT_ALLOWED" in str(await gateway.invoke(scope, "bad", "bash", {}))
    await gateway.invoke(scope, "two", READ_FILE.name, {"path": "README.md"})
    assert "TOOL_BUDGET_EXCEEDED" in str(await gateway.invoke(scope, "three", READ_FILE.name, {"path": "README.md"}))
    access.active = False
    assert "ACCESS_REVOKED" in str(await gateway.invoke(scope, "one", READ_FILE.name, {"path": "README.md"}))
    assert len(await store.list_calls(scope)) == 2


@pytest.mark.anyio
async def test_tool_timeout_preserves_unknown_write_and_never_reexecutes(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    scope = ToolScope(UUID(created.json()["id"]), RunActor(UUID(runs.user_id), UUID(runs.workspace_id)), "owner", 1)
    gateway = ToolGateway(SqlAlchemyToolStore(runs.sessions), Access())

    async def slow(scope: ToolScope, arguments: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(1)
        return {"ok": True}

    gateway.register(
        ToolSpec("test.write", "测试未知写结果", {"type": "object", "additionalProperties": False}, "write", 0.01), slow
    )
    assert "TOOL_TIMEOUT" in str(await gateway.invoke(scope, "timeout", "test.write", {}))
    assert "TOOL_RECOVERY_REQUIRED" in str(await gateway.invoke(scope, "timeout", "test.write", {}))


@pytest.mark.anyio
async def test_readonly_repository_rejects_escapes_and_supports_utf8(tmp_path: Path) -> None:
    # 夹具通过 pytest 创建文件；产品代码从不写被读取仓库。
    from uuid import uuid4

    (tmp_path / "README.md").write_text("中文说明", encoding="utf-8")
    repository = ReadOnlyRepository(tmp_path)
    scope = ToolScope(uuid4(), RunActor(uuid4(), uuid4()), "owner", 1)
    assert (await repository.read(scope, {"path": "README.md"}))["content"] == "中文说明"
    for path in ("../secret", "/etc/passwd", "C:/secret", "a\\secret", ".git/config"):
        assert "REPOSITORY_PATH_DENIED" in str(await repository.read(scope, {"path": path}))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path", [".ENV", ".Env.local", ".NPMRC", ".PYPIRC", "ID_RSA", "ID_ED25519", ".GIT/config", ".env ", ".git./config"]
)
async def test_repository_denies_windows_sensitive_filename_aliases(tmp_path: Path, path: str) -> None:
    """即使文件不存在也必须先拒绝，不能依赖宿主机的大小写和尾随字符规则。"""
    from uuid import uuid4

    # 假凭据只在临时目录中使用；回归用例不接触开发者自己的 .env。
    (tmp_path / ".env").write_text("DUMMY_NOT_A_SECRET=fixture", encoding="utf-8")
    scope = ToolScope(uuid4(), RunActor(uuid4(), uuid4()), "owner", 1)
    result = await ReadOnlyRepository(tmp_path).read(scope, {"path": path})
    assert result == {"ok": False, "error": {"code": "REPOSITORY_PATH_DENIED"}}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("filename", "content", "code"),
    [
        ("missing.md", None, "REPOSITORY_FILE_UNAVAILABLE"),
        ("binary.dat", b"\xff\xfe", "REPOSITORY_FILE_UNAVAILABLE"),
        ("oversized.md", b"x" * 65537, "REPOSITORY_FILE_TOO_LARGE"),
    ],
    ids=["missing-file", "invalid-utf8", "over-64k"],
)
async def test_repository_unavailable_and_size_boundaries(
    tmp_path: Path, filename: str, content: bytes | None, code: str
) -> None:
    from uuid import uuid4

    if content is not None:
        (tmp_path / filename).write_bytes(content)
    scope = ToolScope(uuid4(), RunActor(uuid4(), uuid4()), "owner", 1)
    assert await ReadOnlyRepository(tmp_path).read(scope, {"path": filename}) == {"ok": False, "error": {"code": code}}


@pytest.mark.anyio
async def test_repository_denial_is_persisted_as_failed_tool_fact(runs: RunsHarness, tmp_path: Path) -> None:
    """文件适配器的拒绝要进入真实 Gateway/存储，不能只由模型口头解释。"""
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    scope = ToolScope(UUID(created.json()["id"]), RunActor(UUID(runs.user_id), UUID(runs.workspace_id)), "owner", 1)
    store = SqlAlchemyToolStore(runs.sessions)
    gateway = ToolGateway(store, Access())
    gateway.register(READ_FILE, ReadOnlyRepository(tmp_path).read)
    for index, path in enumerate(("../outside.md", ".ENV", "https://example.com/README.md")):
        result = await gateway.invoke(scope, f"denied-{index}", READ_FILE.name, {"path": path})
        assert result == {"ok": False, "error": {"code": "REPOSITORY_PATH_DENIED"}}
    calls = await store.list_calls(scope)
    assert len(calls) == 3
    assert all(call.status == "FAILED" and call.error_code == "REPOSITORY_PATH_DENIED" for call in calls)
    assert all(call.result == {"ok": False, "error": {"code": "REPOSITORY_PATH_DENIED"}} for call in calls)
