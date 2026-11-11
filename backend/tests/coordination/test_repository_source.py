"""独立仓库接单的真实 HTTP/SQL/Task 链路；代码平台是明确的只读夹具，不发送外部消息。"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_engine import result
from weora.modules.context.domain import Json
from weora.modules.coordination.domain import Config, CoordinationError, State
from weora.modules.coordination.hosting_http import HostedReviewIdentity
from weora.modules.coordination.polling import compensate
from weora.modules.coordination.repository_http import RepositoryEvents
from weora.modules.coordination.repository_source import RepositoryMessage, RepositoryPoll
from weora.modules.coordination.service import CoordinationService
from weora.modules.runs.application.execution import RuntimeInput, TextRuntime
from weora.modules.runs.application.ports import ExecutionProfile, RunActor, RunDetail
from weora.modules.runs.domain.errors import RunNotFoundError
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.pi_runtime import PiSession


class CodeReader(RepositoryEvents):
    def __init__(self) -> None:
        self.messages: tuple[RepositoryMessage, ...] = ()
        self.replies: list[tuple[str, str, str]] = []
        self.fail_send = False

    async def poll(self, identity: HostedReviewIdentity, source: object, cursor: Json) -> RepositoryPoll:
        return RepositoryPoll(self.messages, {"since": datetime.now(UTC).isoformat()})

    async def reply(self, identity: HostedReviewIdentity, number: str, text: str, business_key: str) -> str:
        self.replies.append((number, text, business_key))
        if self.fail_send:
            raise CoordinationError("CODE_SOURCE_UNAVAILABLE")
        return "comment-1"


async def configure(h: ContextRuns) -> tuple[CoordinationService, str, CodeReader, Json]:
    coordinator = cast(CoordinationService, h.app.state.coordination_service)
    coordinator.runs._execution_profile = ExecutionProfile(
        "pi-agent-core@1.3.0", "deepseek", "deployment-model", 1024, 30
    )
    assert coordinator.repository_intake

    async def identity(actor: RunActor, config: Config) -> tuple[HostedReviewIdentity, str]:
        assert actor.workspace_id == h.actor.workspace_id
        return HostedReviewIdentity(
            "github",
            "https://api.github.com",
            "team/configurable-repo",
            "https://github.com/team/configurable-repo",
            "test-only",
        ), "frozen-connection"

    coordinator.repository_intake.identity = identity
    coordinator.repository_intake.reply_identity = identity
    reader = CodeReader()
    coordinator.repository_intake.reader = reader
    payload: Json = {
        "repository_id": "configured-repo",
        "remote_id": "configured-remote",
        "agent_login": "weora",
        "actors": {"external-user": str(h.actor.user_id)},
        "allowed_actors": ["external-user"],
        "main_agent_id": h.original.agent_id,
        "main_agent_version": "0.1.0",
        "worker_agent_id": h.original.agent_id,
        "worker_agent_version": "0.1.0",
        "rules": [{"id": "fix", "name": "修复", "instruction": "明确修复"}],
        "expected_revision": 0,
        "mode": "auto",
        "observe_issues": True,
        "observe_comments": True,
        "debounce_ms": 0,
        "main_model_id": "main-model",
        "worker_model_id": "worker-model",
    }
    response = await h.client.put(h.base + "/coordination/code-config", json=payload, headers=h.headers)
    assert response.status_code == 200, response.text
    return coordinator, response.json()["config"]["id"], reader, payload


def message(identity: str, actor: str = "external-user") -> RepositoryMessage:
    return RepositoryMessage(
        identity,
        "42",
        actor,
        True,
        datetime.now(UTC).isoformat(),
        "请修复登录失败，沿用现有交互",
        "issue",
        False,
        "https://github.com/team/configurable-repo/issues/42",
        "主题的完整背景",
    )


async def accept(h: ContextRuns, service: CoordinationService, scope_id: str) -> str:
    evaluations = await service.store.transact(h.actor.workspace_id, scope_id, lambda engine, _: engine.pass_due())
    evaluation = evaluations[0]
    evidence = await service.tool(
        h.actor.workspace_id, scope_id, "read", "orchestrator_eval_read", {"eval_id": evaluation.id}
    )
    assert evidence["ok"] is True, evidence
    answer = await service.tool(
        h.actor.workspace_id, scope_id, "decide", "orchestrator_eval_decide", dict(result(), eval_id=evaluation.id)
    )
    assert answer["ok"] is True, answer
    return str(cast(Json, answer["decision"])["ticket_id"])


@pytest.mark.anyio
async def test_repository_intake_is_not_feishu_and_continues_same_native_task(contextual: ContextRuns) -> None:
    """未 @ 仓库请求经过 Main 判断后创建原生 Run，后来评论继续同一个 Task，模型选择冻结。"""
    h = contextual
    service, scope_id, reader, _ = await configure(h)
    reader.messages = (message("issue"), message("unbound", "somebody-else"))
    assert await compensate(service, h.actor.workspace_id, scope_id) == 1
    assert await compensate(service, h.actor.workspace_id, scope_id) == 0
    ticket_id = await accept(h, service, scope_id)
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    await service.save(
        h.context_actor,
        replace(state.config, worker_model_id="future-worker-model"),
        expected_revision=state.config.revision,
    )
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    await service.launch(h.actor.workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker"))
    first = await service.store.snapshot(h.actor.workspace_id, scope_id)
    run_id = UUID(str(first.tickets[0].assignments[-1].run_id))
    detail = await service.runs.get(actor=h.actor, workspace_id=h.actor.workspace_id, run_id=run_id)
    assert detail.run.execution_profile
    assert detail.task.origin == "code_hosting" and detail.run.execution_profile.model_id == "worker-model"
    assert detail.task.origin_metadata and "root_text" not in detail.task.origin_metadata
    assert "主题的完整背景" in detail.task.instruction
    assert await h.context.store.list(h.actor.workspace_id, "inbox") == []
    assert (await h.context.store.list(h.actor.workspace_id, "external"))[0]["coordinated_run_ids"] == [str(run_id)]
    # 人类跟进在结束前到达；模型回合成功后可继续同 Task，但仍生成没有旧审批的新 Run。
    store = SqlAlchemyExecutionStore(h.original.sessions)
    lease = await store.claim(workspace_id=h.actor.workspace_id, run_id=run_id, owner="first", lease_seconds=30)
    assert lease and await store.finish(lease, status="SUCCEEDED", output="本轮已分析")
    await service.reconcile(h.actor.workspace_id, scope_id)
    reader.messages = (message("comment"),)
    await compensate(service, h.actor.workspace_id, scope_id)
    current = await service.store.snapshot(h.actor.workspace_id, scope_id)
    effect = next(item for item in current.effects if item.kind == "worker" and item.data["ticket_id"] == ticket_id)
    await service.launch(h.actor.workspace_id, scope_id, effect)
    current = await service.store.snapshot(h.actor.workspace_id, scope_id)
    second_id = UUID(str(current.tickets[0].assignments[-1].run_id))
    assert second_id != run_id
    second = await service.runs.get(actor=h.actor, workspace_id=h.actor.workspace_id, run_id=second_id)
    assert second.run.execution_profile
    assert second.task.id == detail.task.id and second.run.execution_profile.model_id == "worker-model"


@pytest.mark.anyio
async def test_source_reply_unknown_is_not_replayed_and_revoked_author_loses_body(contextual: ContextRuns) -> None:
    h = contextual
    service, scope_id, reader, _ = await configure(h)
    reader.messages = (message("issue"),)
    await compensate(service, h.actor.workspace_id, scope_id)
    reply = await service.tool(
        h.actor.workspace_id,
        scope_id,
        "clarify",
        "orchestrator_reply",
        {"trigger_id": "issue", "purpose": "answer", "text": "请确认验收"},
    )
    assert reply["ok"] is True, reply
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    effect = next(item for item in state.effects if item.kind == "reply")
    reader.fail_send = True
    with pytest.raises(CoordinationError):
        await service.deliver(h.actor.workspace_id, scope_id, effect)
    with pytest.raises(CoordinationError, match="CHANNEL_RECONCILIATION_REQUIRED"):
        await service.deliver(h.actor.workspace_id, scope_id, effect)
    assert len(reader.replies) == 1

    def revoke(_: object, state: State) -> None:
        assert state.config.repository_source
        state.config = replace(state.config, repository_source=replace(state.config.repository_source, actors={}))

    await service.store.transact(h.actor.workspace_id, scope_id, revoke)
    state = await service.authorized_state(h.actor.workspace_id, scope_id)
    assert not state.triggers[0].authorized and state.triggers[0].text == ""
    assert "root_text" not in state.triggers[0].origin and state.replies[0].text == ""


@pytest.mark.anyio
async def test_repository_config_requires_bound_members_and_revision(contextual: ContextRuns) -> None:
    h = contextual
    _, _, _, payload = await configure(h)
    conflict = await h.client.put(h.base + "/coordination/code-config", json=payload, headers=h.headers)
    assert conflict.status_code == 409
    invalid = dict(payload, expected_revision=1, allowed_actors=["unbound"])
    response = await h.client.put(h.base + "/coordination/code-config", json=invalid, headers=h.headers)
    assert response.status_code == 403 and response.json()["detail"]["code"] == "ACTOR_UNBOUND"
    no_csrf = await h.client.put(h.base + "/coordination/code-config", json=payload)
    assert no_csrf.status_code == 403


@pytest.mark.anyio
async def test_existing_main_session_keeps_its_model_after_admin_changes_defaults(contextual: ContextRuns) -> None:

    h = contextual
    service, scope_id, _, _ = await configure(h)
    models: list[str] = []

    def runtime(session: PiSession, invoke: Callable[[str, str, Json], Awaitable[Json]]) -> TextRuntime:
        class Main:
            async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
                models.append(profile.model_id)
                assert session.checkpoint
                await session.checkpoint([{"role": "user", "content": "已保存的主会话历史"}], [])
                return "当前没有新工作"

        return Main()

    profile = ExecutionProfile("pi-agent-core@1.3.0", "deepseek", "deployment-model", 1024, 30)
    for turn in range(2):
        state = await service.store.snapshot(h.actor.workspace_id, scope_id)
        pending = next(item for item in state.effects if item.kind == "main")
        effect = await service.store.claim(h.actor.workspace_id, scope_id, pending.id, f"owner-{turn}", seconds=60)
        assert effect
        await service.main(h.actor.workspace_id, scope_id, effect, runtime, profile)
        await service.store.settle(h.actor.workspace_id, scope_id, effect)
        if turn == 0:
            state = await service.store.snapshot(h.actor.workspace_id, scope_id)
            await service.save(
                h.context_actor,
                replace(state.config, main_model_id="future-main-model"),
                expected_revision=state.config.revision,
            )
    assert models == ["main-model", "main-model"]


@pytest.mark.anyio
async def test_later_message_expiry_revokes_native_history_and_all_derived_copies(contextual: ContextRuns) -> None:
    h = contextual
    service, scope_id, reader, _ = await configure(h)
    reader.messages = (message("root"), message("later"))
    await compensate(service, h.actor.workspace_id, scope_id)
    await accept(h, service, scope_id)
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    await service.launch(h.actor.workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker"))
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    run_id = UUID(str(state.tickets[0].assignments[-1].run_id))
    dependencies = await h.context.store.list(h.actor.workspace_id, "external")
    assert len(dependencies) == 2
    for row in dependencies:
        assert isinstance(row["coordinated_run_ids"], list) and str(run_id) in row["coordinated_run_ids"]

    def expire(_: object, current: State) -> None:
        next(item for item in current.triggers if item.id == "later").expires_at = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()

    await service.store.transact(h.actor.workspace_id, scope_id, expire)
    with pytest.raises(RunNotFoundError):
        await service.runs.get(actor=h.actor, workspace_id=h.actor.workspace_id, run_id=run_id)
    current = await service.authorized_state(h.actor.workspace_id, scope_id)
    assert current.triggers[0].authorized and not current.triggers[1].authorized
    assert current.tickets[0].title == "来源已失效"


@pytest.mark.anyio
async def test_revoke_after_native_commit_keeps_retention_link_for_unbound_run(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原生提交已完成但协调绑定未完成时撤权，孤立 Run 也必须进入正文清理集合。"""
    h = contextual
    service, scope_id, reader, _ = await configure(h)
    reader.messages = (message("root"),)
    await compensate(service, h.actor.workspace_id, scope_id)
    await accept(h, service, scope_id)
    original = service.runs.create
    created: list[UUID] = []

    async def create_then_revoke(
        *,
        actor: RunActor,
        workspace_id: UUID,
        agent_id: UUID,
        agent_version: str,
        instruction: str,
        idempotency_key: str,
        request_id: str,
        origin_metadata: Json | None = None,
        project_id: UUID | None = None,
        repository_ids: tuple[UUID, ...] = (),
        thread_keys: tuple[str, ...] = (),
        model_id: str | None = None,
        task_title: str | None = None,
    ) -> tuple[RunDetail, bool]:
        detail, replay = await original(
            actor=actor,
            workspace_id=workspace_id,
            agent_id=agent_id,
            agent_version=agent_version,
            instruction=instruction,
            idempotency_key=idempotency_key,
            request_id=request_id,
            origin_metadata=origin_metadata,
            project_id=project_id,
            repository_ids=repository_ids,
            thread_keys=thread_keys,
            model_id=model_id,
            task_title=task_title,
        )
        created.append(detail.run.id)

        def revoke(_: object, current: State) -> None:
            assert current.config.repository_source
            current.config = replace(
                current.config, repository_source=replace(current.config.repository_source, actors={})
            )

        await service.store.transact(h.actor.workspace_id, scope_id, revoke)
        return detail, replay

    monkeypatch.setattr(service.runs, "create", create_then_revoke)
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    with pytest.raises(CoordinationError, match="SOURCE_REVOKED"):
        await service.launch(
            h.actor.workspace_id, scope_id, next(item for item in state.effects if item.kind == "worker")
        )
    assert len(created) == 1 and created[0] in await h.context.expired_run_ids(h.actor.workspace_id)
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    assert state.tickets[0].assignments[-1].run_id is None
