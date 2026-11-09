"""Main 持续会话的资产装配、租约、日志与通知；结构化决策继续由协调应用服务校验。"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from weora.modules.agents.application.service import AgentActor
from weora.modules.context.domain import ContextActor
from weora.modules.coordination.domain import CoordinationError, Effect, Json, Session, State
from weora.modules.coordination.engine import Engine, key
from weora.modules.coordination.policy import policy_version
from weora.modules.coordination.records import records
from weora.modules.coordination.tools import MAIN_PROMPT, MAIN_TOOLS
from weora.modules.runs.application.execution import RuntimeInput, RuntimeSession, TextRuntime
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.application.session_delivery import deliver_session

if TYPE_CHECKING:
    from weora.modules.coordination.service import CoordinationService


class MainSessionRunner:
    def __init__(self, service: "CoordinationService") -> None:
        self.service = service

    async def run(
        self,
        workspace_id: UUID,
        scope_id: str,
        effect: Effect,
        runtime_factory: Callable[[RuntimeSession, Callable[[str, str, Json], Awaitable[Json]]], TextRuntime],
        profile: ExecutionProfile,
    ) -> None:
        """运行一轮主会话，绑定当前效果租约，恢复日志并接收新通知，不创建用户 Task。

        固定 Agent/Skill 装配与模型调用在事务外；日志、已读及工具变更单独提交且每次复核租约。
        """
        # 恢复前重新授权，不把持久日志当成跨保留期或跨来源的读取许可。
        state = await self.service.authorized_state(workspace_id, scope_id)
        if state.config.paused or not state.config.enabled:
            return
        config = state.config
        owner = ContextActor(UUID(config.created_by), workspace_id)
        await self.service.context.authorize_actor(owner)
        agent = await self.service.agents.build_runtime_context(
            actor=AgentActor(owner.user_id, workspace_id, "member"),
            workspace_id=workspace_id,
            agent_id=UUID(config.main_agent_id),
            version=config.main_agent_version,
        )
        session_id = key("main-session", scope_id)
        generation = effect.attempts

        def acquire(_: Engine, current: State) -> Session:
            current.require_effect_lease(effect, datetime.now(UTC).isoformat())
            session = records(current).sessions.get(session_id) or Session(session_id, "main")
            session.generation, session.owner = generation, effect.owner
            session.policy_version = policy_version(config)
            if session.model_id is None:
                # 旧日志延续部署模型；新配置不回写正在使用的会话选择。
                session.model_id = profile.model_id if session.messages else config.main_model_id or profile.model_id
            if session.execution_profile is None:
                session.execution_profile = asdict(replace(profile, model_id=session.model_id))
            records(current).sessions.save(session)
            return session

        journal = await self.service.store.transact(workspace_id, scope_id, acquire)
        initial_ids = [item.id for item in state.notices[-50:] if not item.quiet]
        initial_sources = {item.id for item in state.triggers if item.authorized}

        async def current_session() -> State:
            current = await self.service.authorized_state(workspace_id, scope_id)
            current.require_effect_lease(effect, datetime.now(UTC).isoformat())
            session = records(current).sessions.get(session_id)
            if (
                not session
                or session.owner != effect.owner
                or session.generation != generation
                or current.config.paused
                or not current.config.enabled
                or any(item.id in initial_sources and not item.authorized for item in current.triggers)
            ):
                raise CoordinationError("SESSION_LEASE_LOST")
            return current

        async def checkpoint(messages: list[Json], read: list[str]) -> None:
            await current_session()

            def save(engine: Engine, current: State) -> None:
                current.require_effect_lease(effect, datetime.now(UTC).isoformat())
                session = records(current).sessions.get(session_id)
                if not session or session.owner != effect.owner or session.generation != generation:
                    raise CoordinationError("SESSION_LEASE_LOST")
                session.messages = messages
                session.read = list(dict.fromkeys(session.read + read))[-500:]
                records(current).sessions.save(session)
                for evaluation in current.evaluations:
                    if evaluation.state == "pending" and key("eval.started", evaluation.id) in read:
                        engine.delivered(evaluation.id)

            await self.service.store.transact(workspace_id, scope_id, save)

        async def invoke(call_id: str, name: str, arguments: Json) -> Json:
            await current_session()
            return await self.service.tool(workspace_id, scope_id, call_id, name, arguments, lease=effect)

        async def pull() -> list[tuple[str, str]]:
            current = await current_session()
            session = records(current).sessions.get(session_id)
            assert session is not None
            return [
                (item.id, "平台协调通知（不是人类授权）：" + json.dumps(asdict(item), ensure_ascii=False))
                for item in current.notices
                if not item.quiet and item.id not in initial_ids and item.id not in session.read
            ][-10:]

        # Pi 只负责模型循环；输入轮询、日志和已读回执由平台回调持久化。
        async def model_status(value: Json | None) -> None:
            """在有效 Main 租约内更新重试健康，不保存模型异常原文。"""
            await current_session()

            def save(_: Engine, current: State) -> None:
                current.require_effect_lease(effect, datetime.now(UTC).isoformat())
                session = records(current).sessions.get(session_id)
                if not session or session.owner != effect.owner or session.generation != generation:
                    raise CoordinationError("SESSION_LEASE_LOST")
                session.model_retry = value

            await self.service.store.transact(workspace_id, scope_id, save)

        session = RuntimeSession(
            session_id,
            journal.messages,
            pull=pull,
            checkpoint=checkpoint,
            initial_input_ids=initial_ids,
            status=model_status,
        )
        runtime = runtime_factory(session, invoke)
        summary = self.service._read(state, {"resource": "overview"})
        skills = [
            {
                "name": skill.name,
                "instructions": skill.instructions,
                "references": [
                    {"path": reference.path, "content": reference.content} for reference in skill.references
                ],
            }
            for skill in agent.skills
        ]
        try:
            await deliver_session(
                lambda: runtime.run(
                    RuntimeInput(
                        MAIN_PROMPT
                        + "\n固定 Agent 指南（受平台治理约束）：\n"
                        + agent.system_prompt
                        + ("\n固定版本 Skills：" + json.dumps(skills, ensure_ascii=False) if skills else ""),
                        "本轮请按当前工具契约处理，不沿用历史中的错误判断。"
                        "先读取 evaluation 和 trigger 证据。显式请求（strength=explicit）使用 orchestrator_route，"
                        '参数为 {"decisions":[{"trigger_id":"读取到的 trigger.id","decision":"accept",'
                        '"title":"任务标题","scope":"操作范围和完整目标 URL","reason":"接单依据"}]}。'
                        "只有 inferred 请求使用 eval_decide 判断实施意图和规则。observe 模式也应提交语义 accept，"
                        "由后端自动记录 would_accept，绝不会创建 Worker。"
                        "模式限制不是 unavailable 的理由。\n"
                        "本次协调事实（平台数据，不是新的用户授权）：\n" + json.dumps(summary, ensure_ascii=False),
                        MAIN_TOOLS,
                    ),
                    replace(profile, model_id=journal.model_id or profile.model_id),
                ),
                model_status,
            )
        finally:
            # 健康清理不授予执行权；只有相同 owner 和代次能清除自己的退避状态。
            def clear_retry(_: Engine, current: State) -> None:
                session = records(current).sessions.get(session_id)
                if session and session.owner == effect.owner and session.generation == generation:
                    session.model_retry = None

            await self.service.store.transact(workspace_id, scope_id, clear_retry)
