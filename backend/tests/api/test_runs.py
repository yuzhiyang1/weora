"""公开 HTTP 验证私有运行、并发幂等与事务回滚，不使用真实模型或开发数据库。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.support.database import enable_sqlite_foreign_keys
from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.application import create_app
from weora.modules.collaboration.infrastructure.tasks import TaskModel
from weora.modules.computers.openbot import OpenBot
from weora.modules.identity.infrastructure.models import WorkspaceMembershipModel
from weora.modules.runs.infrastructure.models import RunDispatchModel, RunEventModel, RunModel, RunStepModel
from weora.shared.infrastructure.database import Base


@dataclass(frozen=True, slots=True)
class RunsHarness:
    """独立文件数据库支持多连接竞争；新建应用仍复用同一测试数据库。"""

    client: AsyncClient
    app: FastAPI
    sessions: async_sessionmaker[AsyncSession]
    workspace_id: str
    user_id: str
    agent_id: str
    version_id: str

    @property
    def path(self) -> str:
        return f"/api/v1/workspaces/{self.workspace_id}/runs"

    def payload(self, instruction: str = "请解释登录失败可能有哪些原因。") -> dict[str, str]:
        return {"agent_id": self.agent_id, "agent_version": "0.1.0", "instruction": instruction}

    def headers(self, key: str = "request-1") -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies["weora_csrf"], "Idempotency-Key": key}


@pytest.fixture
async def runs(tmp_path: Path) -> AsyncIterator[RunsHarness]:
    """开启 SQLite 外键约束，避免测试把不存在的父记录当作合法写入。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}", connect_args={"timeout": 10})

    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        # 并发领取同时伴随只读鉴权；WAL 避免 SQLite 回滚日志的读写锁互等冒充 PostgreSQL 租约失败。
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app(
        IsolatedAppSettings(environment="test", session_cookie_secure=False),
        session_factory=sessions,
        object_storage_factory=lambda _: None,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registration = await client.post(
            "/api/v1/auth/register",
            json={
                "display_name": "运行发起人",
                "email": "run-owner@example.com",
                "password": "correct horse battery staple",
                "workspace_name": "运行测试组",
            },
        )
        assert registration.status_code == 201, registration.text
        identity = registration.json()
        workspace_id = identity["workspace"]["id"]
        agent = await client.post(
            f"/api/v1/workspaces/{workspace_id}/agents",
            json={
                "display_name": "研发助手",
                "description": "只用于运行测试",
                "system_prompt": "你是研发助手。",
                "skill_version_ids": [],
            },
            headers={"X-CSRF-Token": client.cookies["weora_csrf"]},
        )
        assert agent.status_code == 201, agent.text
        agent_id = agent.json()["id"]
        version = await client.post(
            f"/api/v1/workspaces/{workspace_id}/agents/{agent_id}/publish",
            json={
                "version": "0.1.0",
                "changelog": "首个测试发布版本",
            },
            headers={"X-CSRF-Token": client.cookies["weora_csrf"]},
        )
        assert version.status_code == 201, version.text
        yield RunsHarness(client, app, sessions, workspace_id, identity["user"]["id"], agent_id, version.json()["id"])
    await engine.dispose()


@pytest.mark.anyio
async def test_computer_routes_reuse_run_access_and_csrf(runs: RunsHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert created.status_code == 201
    run_id = created.json()["id"]
    path = f"{runs.path}/{run_id}/computer"
    assert (await runs.client.get(path)).json() == {"state": "disabled"}

    bot = OpenBot("http://supervisor:4300", "supervisor-test", "master-test", "weora")
    runs.app.state.openbot = bot
    identity = OpenBot.computer_id(UUID(runs.workspace_id), UUID(runs.user_id), UUID(created.json()["task_id"]))
    calls: list[tuple[str, dict[str, object] | None]] = []

    async def supervisor(endpoint: str, body: dict[str, object] | None = None) -> dict[str, object]:
        calls.append((endpoint, body))
        return {"computers": []}

    monkeypatch.setattr(bot, "supervisor", supervisor)
    assert (await runs.client.get(path)).json() == {"state": "stopped"}
    assert (await runs.client.post(path + "/start", json={})).status_code == 403
    other_workspace = path.replace(runs.workspace_id, str(uuid4()))
    assert (await runs.client.get(other_workspace)).status_code == 404
    assert (await runs.client.post(path + "/start", json={}, headers=runs.headers())).status_code == 204
    assert calls == [("/computers", None), (f"/computers/{identity}/ensure", {})]


async def table_counts(runs: RunsHarness) -> list[int]:
    """确认五类记录要么全部写入，要么全部回滚。"""
    async with runs.sessions() as session:
        return [
            int(await session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (
                TaskModel,
                RunModel,
                RunStepModel,
                RunEventModel,
                RunDispatchModel,
            )
        ]


@pytest.mark.anyio
async def test_create_persists_fixed_version_step_event_and_pending_dispatch(runs: RunsHarness) -> None:
    response = await runs.client.post(
        runs.path,
        json=runs.payload(),
        headers={
            **runs.headers(),
            "X-Request-ID": "run-create-test",
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "QUEUED"
    assert data["audience"] == "private"
    assert data["agent_version_id"] == runs.version_id
    assert data["last_sequence"] == 1
    assert data["instruction"] == runs.payload()["instruction"]
    assert "idempotency_key" not in data
    assert "output" not in data
    assert await table_counts(runs) == [1, 1, 1, 1, 1]
    async with runs.sessions() as session:
        assert await session.scalar(select(RunDispatchModel.status)) == "PENDING"
        assert await session.scalar(select(RunStepModel.status)) == "PENDING"
    events = (await runs.client.get(f"{runs.path}/{data['id']}/events")).json()
    assert events["items"][0]["event_type"] == "run.queued"
    assert events["items"][0]["request_id"] == "run-create-test"
    assert "instruction" not in str(events)


@pytest.mark.anyio
async def test_submission_replay_conflict_and_draft_changes(runs: RunsHarness) -> None:
    first = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    replay = await runs.client.post(
        runs.path, json=runs.payload("  请解释登录失败可能有哪些原因。  "), headers=runs.headers()
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()
    conflict = await runs.client.post(runs.path, json=runs.payload("另一项任务"), headers=runs.headers())
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "RUN_IDEMPOTENCY_CONFLICT"
    await runs.client.patch(
        f"/api/v1/workspaces/{runs.workspace_id}/agents/{runs.agent_id}/draft",
        json={"system_prompt": "修改后的草稿", "skill_version_ids": []},
        headers=runs.headers(),
    )
    detail = await runs.client.get(f"{runs.path}/{first.json()['id']}")
    assert detail.json()["agent_version_id"] == runs.version_id
    assert await table_counts(runs) == [1, 1, 1, 1, 1]


@pytest.mark.anyio
async def test_concurrent_same_key_creates_only_one_task_and_run(runs: RunsHarness) -> None:
    responses = await asyncio.gather(
        *(runs.client.post(runs.path, json=runs.payload(), headers=runs.headers()) for _ in range(6))
    )
    assert sorted(item.status_code for item in responses) == [200, 200, 200, 200, 200, 201]
    assert len({item.json()["id"] for item in responses}) == 1
    assert await table_counts(runs) == [1, 1, 1, 1, 1]


@pytest.mark.anyio
async def test_concurrent_key_with_different_payload_is_conflict(runs: RunsHarness) -> None:
    responses = await asyncio.gather(
        *(runs.client.post(runs.path, json=runs.payload(text), headers=runs.headers()) for text in ("目标 A", "目标 B"))
    )
    assert sorted(item.status_code for item in responses) == [201, 409]
    assert await table_counts(runs) == [1, 1, 1, 1, 1]


@pytest.mark.anyio
async def test_creation_rollback_includes_task_when_outbox_write_fails(runs: RunsHarness) -> None:
    def fail_dispatch(_: object, __: object, ___: object) -> None:
        raise RuntimeError("injected dispatch storage failure")

    event.listen(RunDispatchModel, "before_insert", fail_dispatch)
    try:
        with pytest.raises(RuntimeError, match="injected dispatch storage failure"):
            await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    finally:
        event.remove(RunDispatchModel, "before_insert", fail_dispatch)
    assert await table_counts(runs) == [0, 0, 0, 0, 0]
    retry = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert retry.status_code == 201


@pytest.mark.anyio
async def test_cancel_is_atomic_idempotent_and_events_resume_by_sequence(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    path = f"{runs.path}/{created.json()['id']}"
    cancelled = await runs.client.post(f"{path}/cancel", headers=runs.headers())
    repeated = await runs.client.post(f"{path}/cancel", headers=runs.headers())
    assert cancelled.status_code == repeated.status_code == 200
    assert cancelled.json() == repeated.json()
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["last_sequence"] == 2
    async with runs.sessions() as session:
        assert await session.scalar(select(RunDispatchModel.status)) == "CANCELLED"
        assert await session.scalar(select(RunStepModel.status)) == "CANCELLED"
    page = (await runs.client.get(f"{path}/events?limit=1")).json()
    assert [item["sequence"] for item in page["items"]] == [1]
    assert page["has_more"] is True
    next_page = (await runs.client.get(f"{path}/events?after={page['next_after']}&limit=1")).json()
    assert [item["sequence"] for item in next_page["items"]] == [2]
    assert next_page["items"][0]["event_type"] == "run.cancelled"
    assert next_page["has_more"] is False
    assert (await runs.client.get(f"{path}/events?after=2")).json()["items"] == []
    replay = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert replay.status_code == 200
    assert replay.json()["status"] == "CANCELLED"
    assert await table_counts(runs) == [1, 1, 1, 2, 1]


@pytest.mark.anyio
async def test_cancellation_event_failure_rolls_back_state_and_dispatch(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    path = f"{runs.path}/{created.json()['id']}"

    def fail_event(_: object, __: object, target: RunEventModel) -> None:
        if target.sequence == 2:
            raise RuntimeError("injected cancellation event failure")

    event.listen(RunEventModel, "before_insert", fail_event)
    try:
        with pytest.raises(RuntimeError, match="injected cancellation event failure"):
            await runs.client.post(f"{path}/cancel", headers=runs.headers())
    finally:
        event.remove(RunEventModel, "before_insert", fail_event)
    assert (await runs.client.get(path)).json()["status"] == "QUEUED"
    assert await table_counts(runs) == [1, 1, 1, 1, 1]
    async with runs.sessions() as session:
        assert await session.scalar(select(RunDispatchModel.status)) == "PENDING"
        assert await session.scalar(select(RunStepModel.status)) == "PENDING"


@pytest.mark.anyio
async def test_cancellation_never_claims_running_process_has_stopped(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    async with runs.sessions() as session:
        await session.execute(update(RunModel).values(status="RUNNING"))
        await session.commit()
    response = await runs.client.post(f"{runs.path}/{created.json()['id']}/cancel", headers=runs.headers())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUN_CANCELLATION_CONFLICT"


@pytest.mark.anyio
async def test_private_runs_are_hidden_from_other_workspace_and_same_workspace_admin(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    path = f"{runs.path}/{created.json()['id']}"
    async with AsyncClient(transport=ASGITransport(app=runs.app), base_url="http://test") as other:
        identity = await other.post(
            "/api/v1/auth/register",
            json={
                "display_name": "另一成员",
                "email": "other-owner@example.com",
                "password": "correct horse battery staple",
                "workspace_name": "另一工作空间",
            },
        )
        assert identity.status_code == 201
        other_user = UUID(identity.json()["user"]["id"])
        other_workspace = identity.json()["workspace"]["id"]
        for endpoint in (path, f"{path}/events", runs.path):
            assert (await other.get(endpoint)).status_code == 404
        assert (
            await other.post(f"{path}/cancel", headers={"X-CSRF-Token": other.cookies["weora_csrf"]})
        ).status_code == 404
        own_path = f"/api/v1/workspaces/{other_workspace}/runs/{created.json()['id']}"
        assert (await other.get(own_path)).status_code == 404
        # 管理员身份只管理组织资产，不隐式继承其他成员的私有任务正文。
        async with runs.sessions() as session:
            await session.execute(
                update(WorkspaceMembershipModel)
                .where(
                    WorkspaceMembershipModel.user_id == other_user,
                )
                .values(workspace_id=UUID(runs.workspace_id), role="admin")
            )
            await session.commit()
        assert (await other.get(path)).status_code == 404
        assert (await other.get(f"{path}/events")).status_code == 404
        assert (await other.get(runs.path)).json()["items"] == []
        assert (await other.get(f"{runs.path}?before_id={created.json()['id']}")).status_code == 404
        private_run = await other.post(
            runs.path,
            json=runs.payload(),
            headers={
                "X-CSRF-Token": other.cookies["weora_csrf"],
                "Idempotency-Key": "request-1",
            },
        )
        assert private_run.status_code == 201
        assert private_run.json()["id"] != created.json()["id"]


@pytest.mark.anyio
async def test_stable_list_pagination_and_new_app_readback(runs: RunsHarness) -> None:
    created_ids: set[str] = set()
    for index in range(3):
        response = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers(f"request-{index}"))
        assert response.status_code == 201
        created_ids.add(response.json()["id"])
    first = (await runs.client.get(f"{runs.path}?limit=2")).json()
    second = (await runs.client.get(f"{runs.path}?limit=2&before_id={first['next_before_id']}")).json()
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert second["next_before_id"] is None
    assert {item["id"] for page in (first, second) for item in page["items"]} == created_ids
    assert all("instruction" not in item for item in first["items"])
    # 重建应用工厂，证明运行和事件不是保存在旧应用的进程内状态中。
    fresh = create_app(
        IsolatedAppSettings(environment="test", session_cookie_secure=False),
        session_factory=runs.sessions,
        object_storage_factory=lambda _: None,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fresh), base_url="http://test", cookies=runs.client.cookies
    ) as client:
        run_id = first["items"][0]["id"]
        assert (await client.get(f"{runs.path}/{run_id}")).json()["id"] == run_id
        assert (await client.get(f"{runs.path}/{run_id}/events")).json()["latest_sequence"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"agent_version": "unpublished"},
        {"agent_id": str(uuid4())},
    ],
)
async def test_unpublished_and_unknown_versions_cannot_create_runs(runs: RunsHarness, change: dict[str, str]) -> None:
    response = await runs.client.post(runs.path, json={**runs.payload(), **change}, headers=runs.headers())
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_VERSION_NOT_FOUND"
    assert await table_counts(runs) == [0, 0, 0, 0, 0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"instruction": "   "},
        {"instruction": "a" * 16001},
        {"agent_version": " "},
        {"created_by": str(uuid4())},
        {"audience": "public"},
        {"api_key": "not-allowed"},
    ],
)
async def test_invalid_input_and_platform_owned_fields_are_rejected(runs: RunsHarness, change: dict[str, str]) -> None:
    response = await runs.client.post(runs.path, json={**runs.payload(), **change}, headers=runs.headers())
    assert response.status_code == 422
    assert await table_counts(runs) == [0, 0, 0, 0, 0]


@pytest.mark.anyio
async def test_auth_csrf_missing_key_and_invalid_pagination(runs: RunsHarness) -> None:
    async with AsyncClient(transport=ASGITransport(app=runs.app), base_url="http://test") as guest:
        assert (await guest.get(runs.path)).status_code == 401
        assert (await guest.post(runs.path, json=runs.payload(), headers={"Idempotency-Key": "x"})).status_code == 401
    assert (await runs.client.post(runs.path, json=runs.payload(), headers={"Idempotency-Key": "x"})).status_code == 403
    assert (
        await runs.client.post(
            runs.path,
            json=runs.payload(),
            headers={
                "X-CSRF-Token": runs.client.cookies["weora_csrf"],
            },
        )
    ).status_code == 422
    for key in ("contains space", "a" * 129):
        assert (await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers(key))).status_code == 422
    for query in ("limit=0", "limit=101", "before_id=invalid"):
        assert (await runs.client.get(f"{runs.path}?{query}")).status_code == 422
    assert await table_counts(runs) == [0, 0, 0, 0, 0]


@pytest.mark.anyio
async def test_disabled_membership_cannot_read_or_cancel_run(runs: RunsHarness) -> None:
    created: Response = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    path = f"{runs.path}/{created.json()['id']}"
    async with runs.sessions() as session:
        await session.execute(
            update(WorkspaceMembershipModel)
            .where(
                WorkspaceMembershipModel.user_id == UUID(runs.user_id),
            )
            .values(status="inactive")
        )
        await session.commit()
    assert (await runs.client.get(path)).status_code == 401
    assert (await runs.client.get(f"{path}/events")).status_code == 401
    # 沿用当前认证依赖：读请求为 401，写请求的会话/CSRF 联合校验失败为 403。
    assert (await runs.client.post(f"{path}/cancel", headers=runs.headers())).status_code == 403


@pytest.mark.anyio
async def test_concurrent_cancels_write_only_one_terminal_event(runs: RunsHarness) -> None:
    """重复按钮和并发请求均不能重复分配终态序号。"""
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    path = f"{runs.path}/{created.json()['id']}"
    responses = await asyncio.gather(*(runs.client.post(f"{path}/cancel", headers=runs.headers()) for _ in range(6)))
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["status"] == "CANCELLED" for response in responses)
    assert all(response.json()["last_sequence"] == 2 for response in responses)
    assert await table_counts(runs) == [1, 1, 1, 2, 1]


@pytest.mark.anyio
async def test_new_agent_version_cannot_reuse_old_request_key(runs: RunsHarness) -> None:
    """固定版本属于请求语义的一部分，不能同键偷偷换版本。"""
    first = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    response = await runs.client.post(
        f"/api/v1/workspaces/{runs.workspace_id}/agents/{runs.agent_id}/publish",
        json={"version": "0.2.0", "changelog": "下一版"},
        headers=runs.headers(),
    )
    assert response.status_code == 201
    conflict = await runs.client.post(
        runs.path,
        json={**runs.payload(), "agent_version": "0.2.0"},
        headers=runs.headers(),
    )
    assert conflict.status_code == 409
    assert (await runs.client.get(f"{runs.path}/{first.json()['id']}")).json()["agent_version_id"] == runs.version_id
    assert await table_counts(runs) == [1, 1, 1, 1, 1]


@pytest.mark.anyio
async def test_same_timestamp_pages_do_not_skip_runs(runs: RunsHarness) -> None:
    """时间相同以 ID 排序补足游标，避免同批记录漏读或重复。"""
    for index in range(3):
        await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers(f"same-time-{index}"))
    async with runs.sessions() as session:
        first = await session.scalar(select(RunModel).limit(1))
        assert first is not None
        await session.execute(update(RunModel).values(created_at=first.created_at))
        await session.commit()
    ids: list[str] = []
    before: str | None = None
    for _ in range(3):
        response = await runs.client.get(
            runs.path,
            params={
                "limit": "1",
                **({"before_id": before} if before else {}),
            },
        )
        assert response.status_code == 200
        page = response.json()
        ids.extend(item["id"] for item in page["items"])
        before = page["next_before_id"]
    assert before is None
    assert len(ids) == len(set(ids)) == 3


@pytest.mark.anyio
async def test_database_prevents_second_active_run_for_same_task(runs: RunsHarness) -> None:
    """不只依赖 API 顺序检查，数据库也拒绝同目标两个活跃执行。"""
    await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    async with runs.sessions() as session:
        original = await session.scalar(select(RunModel))
        assert original is not None
        values = {column.name: getattr(original, column.name) for column in RunModel.__table__.columns}
        values.update(id=uuid4(), idempotency_key="different-key")
        session.add(RunModel(**values))
        with pytest.raises(IntegrityError, match="runs.task_id"):
            await session.commit()
        await session.rollback()
    assert await table_counts(runs) == [1, 1, 1, 1, 1]
