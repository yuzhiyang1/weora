"""Pi Worker 会话端口。业务状态、身份与租约仍由 Weora 后端持有。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict
from functools import partial
from typing import Literal, cast

from jsonschema import Draft202012Validator

from weora.modules.coordination.activities import RunActivities
from weora.modules.coordination.domain import CoordinationError, Json, Report, State
from weora.modules.coordination.engine import Engine, key
from weora.modules.coordination.records import records
from weora.modules.coordination.service import CoordinationService
from weora.modules.coordination.tools import ID, TEXT, USER_REPLY_STYLE, object_schema
from weora.modules.coordination.worker_children import ChildContext, WorkerChildren
from weora.modules.coordination.worker_reads import worker_facts
from weora.modules.coordination.worker_sessions import WorkerBinding, WorkerSession
from weora.modules.runs.application.execution import RunLease, RuntimeFailure, RuntimeInput
from weora.modules.runs.application.harness import HarnessFactory
from weora.modules.runs.application.ports import ExecutionProfile, RunActor
from weora.modules.runs.application.session_delivery import deliver_session
from weora.modules.tools.application.gateway import ToolSpec

ACTIVITY_TOOLS = (
    ToolSpec(
        "orchestrator_activity_read",
        "读取本次运行的后台命令或子会话状态和结果。",
        object_schema({"activity_id": ID}, "activity_id"),
    ),
    ToolSpec(
        "orchestrator_activity_cancel",
        "取消本次运行的后台命令或子会话，并等待清理。",
        object_schema({"activity_id": ID}, "activity_id"),
        "write",
    ),
    ToolSpec(
        "orchestrator_delegate",
        "委派独立子会话协助当前任务；沿用本次工具审批，不能批准计划或接管其他任务。",
        object_schema({"instruction": TEXT}, "instruction"),
        "write",
    ),
    ToolSpec(
        "orchestrator_activity_message",
        "向仍在运行的子会话补充信息，在下一模型步骤读取；不扩大执行权限。",
        object_schema({"activity_id": ID, "text": TEXT}, "activity_id", "text"),
        "write",
    ),
)

WORKER_TOOLS = ACTIVITY_TOOLS + (
    ToolSpec(
        "orchestrator_code_read",
        "读取本任务已交付的完整代码托管事件，不能指定其他任务或任意 URL。",
        object_schema({"event_id": ID, "offset": {"type": "integer", "minimum": 0, "maximum": 65536}}, "event_id"),
    ),
    ToolSpec(
        "orchestrator_read",
        "读取本任务及输入，长正文按 id 和 offset 分段；root 读取原讨论主题。",
        object_schema(
            {
                "resource": {"enum": ["ticket", "input", "root", "deliveries"]},
                "id": ID,
                "offset": {"type": "integer", "minimum": 0, "maximum": 65536},
            }
        ),
    ),
    ToolSpec(
        "orchestrator_ack",
        "确认指定人类请求或跟进，按 trigger_id 幂等回复一次。",
        object_schema({"text": TEXT, "trigger_id": ID}, "text"),
        "write",
    ),
    ToolSpec(
        "orchestrator_report",
        "报告进度、完成、阻塞或问题。ready 须经 Weora 交付验收后才生效。",
        object_schema(
            {
                "state": {"enum": ["progress", "ready", "blocked", "failed", "dropped", "waiting"]},
                "summary": TEXT,
                "human_action": {"type": ["string", "null"], "maxLength": 4000},
                "question_trigger_id": ID,
                "answers": ID,
            },
            "state",
            "summary",
        ),
        "write",
    ),
    ToolSpec(
        "orchestrator_trigger_handback",
        "消息不属于本任务时交还主 Agent，每消息只能一次。",
        object_schema({"trigger_id": ID, "reason": TEXT}, "trigger_id", "reason"),
        "write",
    ),
    ToolSpec(
        "orchestrator_thread_follow",
        "订阅当前来源已知线程，只作为上下文，不获得其请求授权。",
        object_schema({"thread": ID}, "thread"),
        "write",
    ),
    ToolSpec(
        "orchestrator_thread_unrelated",
        "退订无关的上下文线程，不能退订原始任务线程。",
        object_schema({"thread": ID}, "thread"),
        "write",
    ),
)

# 这些要求决定何时可以交付。措辞和排版由模型根据问题选择。
WORKER_DELIVERY_GUIDANCE = """现有证据已能回答全部或一部分问题时，用 ready 把能确认的结论写进 summary，
同时说明尚未核实的范围。对当前结论没有影响的资料缺口，不需要先向用户追问。
用户要方案时，按已知约束交付可评审的方案；暂未决定的选型、参数或实现细节可以作为待定项随方案交付。
只有缺少继续所必需的事实、决定或权限时，才请求用户处理。若已确认部分结论，也在发给用户的 human_action 里说清楚。"""

WORKER_PROMPT = f"""\n你是主 Agent 分配的 Weora Worker，负责本任务，不接管其他任务。
先读取本任务输入；原始用户输入、后续人类纠偏、平台事实备注和上下文分别对待。
不相关的新任务调用 trigger_handback，不能自行接单；订阅仅提供上下文，不扩大权限。
peer_evidence=true 是同话题其他成员的复现线索。页面名写错、补充操作步骤或证据时，在原任务里只读核实；
证据不等于事实，报告实际观察结果。无关消息交回，不新建任务；普通闲聊无需回应。
如果需要变更任务目标、扩大资料或执行权限，报告 waiting 并填写 human_action，说明变更内容并请原发起人确认。
即使原发起人回复，也只有明确确认才可按新目标提出计划；执行仍须通过原有工具授权和审批。
使用 orchestrator_report 及时报告 progress、ready、blocked、failed、waiting，回答用户的问题时填写 answers。
每轮（包括续接）结束前必须调用 orchestrator_report，历史报告只属于历史 Run。
ready 的 summary 可能直接发回原话题，只写可向原话题受众公开的内容。
{WORKER_DELIVERY_GUIDANCE}
{USER_REPLY_STYLE}
仅回复普通文本不会完成 Ticket；不要因为上轮报告过 ready 就省略本轮报告。
输入有 chars/next_offset 时是不完整预览，使用 orchestrator_read(resource=input,id,offset) 读必要正文；
原讨论主题可用 resource=root 分段读取，不能把缺失的正文当成不存在的要求。
需要人类时填写 human_action；ready 不得同时提出问题。完成报告须由平台验证现有交付证据后才生效。
如果任务需要核对代码而尚未绑定仓库，报告 blocked。直接说明尚未绑定仓库、已经核对了哪些资料，以及需要绑定的仓库或分支。
请用项目名或仓库名说明下一步，避免让发起人查找内部编号。
浏览器属于本用户的同一 Task，后续 Run 会复用登录态和页面。续接先 snapshot/read 核对现状，不能重放开始考试等写操作。
登录先 browser.credentials 获取凭据引用，再 fill_credential 填入指定字段；填充后取新快照，不用明文 type 传密码。
没有授权凭据、遇到验证码或需要人类判断时，用 browser.request_human 并报告 blocked、人类操作路径和继续方式。
静态标题用 browser.read 核对；快照未列出文字不代表页面未渲染，缺少请求也不能擅自认定前端原因。
结果必须区分实际观察、推断和未覆盖范围，用 browser.evidence 保存真实截图并将编号交给 Main 选择图片。
网页观察用 browser.validate_evidence 校验截图产物；browser.verify 是代码交付验证，需要代码计划及审批，不能混用。
原有计划、审批、仓库和写工具规则继续执行，过去批准不能用于新 Run。旧工具结果只作历史。
PR 反馈续接先读取 orchestrator_read(resource=deliveries) 的本 Task 分支交付引用；
plan.propose 指定 source_delivery_id，重新申请 git.commit/git.push，批准后用 delivery.branch.push 续写原分支。
平台会冻结分支与旧 SHA，漂移时不得覆盖；已有 PR 不重复创建，也不合并。
status_only 输入只报告 progress/waiting，不报告 ready/dropped，不实施修改。禁止绕过工具网关、审批或沙箱。
长命令超过五秒返回 activity_id；可继续阅读、接收新消息，并用 activity_read/cancel 管理。
可用 delegate 委派子会话协助；活动结果作为平台事实返回，不代表新的人类授权。
有后台活动时不得报告最终完成；需要人类处理时平台会停止后代并释放执行资源。
"""


class CoordinatedRuntime:
    def __init__(
        self,
        service: CoordinationService,
        lease: RunLease,
        *,
        runtime_factory: HarnessFactory,
        call_tool: Callable[[str, str, Json], Awaitable[Json]],
        authorize: Callable[[], Awaitable[bool]],
    ) -> None:
        self.service, self.lease = service, lease
        self.runtime_factory, self.call_tool = runtime_factory, call_tool
        self.authorize = authorize

    async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
        """为受管理的原生 Run 恢复 Worker 会话并接入新输入、回执和报告。

        每次工具与检查点均复核原生 fencing 和当前安排；ready 先暂存，待原生运行验收后才对外生效。
        """
        actor = RunActor(self.lease.run.created_by, self.lease.run.workspace_id)
        detail = await self.service.runs.access_metadata(actor, self.lease.run.id)
        origin = (detail.task.origin_metadata or {}) if detail else {}
        scope_id, ticket_id = origin.get("coordination_scope_id"), origin.get("coordination_ticket_id")
        if not scope_id or not ticket_id:
            return await self.runtime_factory(None, self.call_tool).run(payload, profile)
        workspace_id = actor.workspace_id
        scope_id, ticket_id = str(scope_id), str(ticket_id)
        status_only = origin.get("coordination_status_only") is True
        owner = f"{self.lease.owner}:{self.lease.fencing_token}"
        state = await self.service.authorized_state(workspace_id, scope_id)
        ticket = records(state).tickets.get(ticket_id)
        if not ticket or ticket.ended_by == "human" or not ticket.assignments:
            raise RuntimeFailure("ACCESS_REVOKED")
        assignment_id = ticket.assignments[-1].id
        session_id = ticket.session_id
        initial = cast(list[str], state.checkpoints.get(f"inputs:{self.lease.run.id}", []))

        journal = WorkerSession(
            self.service,
            WorkerBinding(self.lease, scope_id, ticket_id, assignment_id, session_id, tuple(initial)),
            self.authorize,
        )
        session = await journal.open(profile.model_id, profile.runtime_id)
        fresh, check, pull, status = journal.fresh, journal.check, journal.pull, journal.status

        activities = RunActivities(str(self.lease.run.id), owner, journal.save_activity)
        activities.require_clean_recovery(state.activities)
        write_lock = asyncio.Lock()

        async def gateway_call(identity: str, name: str, arguments: Json) -> Json:
            """子会话与父会话共享原生网关；写动作串行，防止同时修改同一沙箱。"""
            declared = next((item for item in payload.tools if item.name == name), None)
            if not declared:
                return {"ok": False, "code": "TOOL_NOT_DECLARED"}
            if declared.effect == "read":
                return await self.call_tool(identity, name, arguments)
            async with write_lock:
                current = await fresh()
                active = records(current).tickets.get(ticket_id)
                # 测试站导航仍受原 Run 的站点白名单限制；证据不能触发点击提交、改码或新计划。
                if active and Engine.peer_evidence_only(active) and name != "browser.navigate":
                    return {"ok": False, "code": "PEER_EVIDENCE_REQUIRES_OWNER_CONFIRMATION"}
                return await self.call_tool(identity, name, arguments)

        children = WorkerChildren(
            self.service.store,
            ChildContext(workspace_id, scope_id, str(self.lease.run.id), owner, payload, profile),
            activities,
            fresh,
            check,
            gateway_call,
            self.runtime_factory,
            ACTIVITY_TOOLS,
        )

        async def invoke(call_id: str, name: str, arguments: Json) -> Json:
            current = await fresh()
            spec = next((item for item in WORKER_TOOLS if item.name == name), None)
            if not spec:
                declared = next((item for item in payload.tools if item.name == name), None)
                if status_only and (not declared or declared.effect != "read"):
                    return {"ok": False, "code": "STATUS_ONLY_READ_ONLY"}
                if name in {"sandbox.command", "sandbox.pi", "sandbox.omp"} and not status_only:
                    return await activities.start(
                        key(str(self.lease.run.id), call_id),
                        "command",
                        lambda: gateway_call(call_id, name, arguments),
                    )
                return await gateway_call(call_id, name, arguments)
            if not Draft202012Validator(spec.input_schema).is_valid(arguments):
                return {"ok": False, "code": "INVALID_TOOL_PARAMETERS"}
            if name == "orchestrator_code_read":
                return await self.service.read_code_event(
                    workspace_id, scope_id, ticket_id, str(arguments["event_id"]), int(str(arguments.get("offset", 0)))
                )
            if name == "orchestrator_read" and arguments.get("resource") == "deliveries":
                if not self.service.delivery:
                    return {"ok": False, "code": "DELIVERY_NOT_CONFIGURED"}
                return {
                    "ok": True,
                    "branches": await self.service.delivery.continuation_branches(actor, self.lease.run.id),
                }
            if name == "orchestrator_activity_read":
                return activities.describe(str(arguments["activity_id"]))
            if name in {"orchestrator_activity_cancel", "orchestrator_delegate", "orchestrator_activity_message"}:
                if status_only:
                    return {"ok": False, "code": "STATUS_ONLY_READ_ONLY"}
                active = records(current).tickets.get(ticket_id)
                if active and Engine.peer_evidence_only(active):
                    return {"ok": False, "code": "PEER_EVIDENCE_REQUIRES_OWNER_CONFIRMATION"}
                if name == "orchestrator_activity_cancel":
                    return await activities.cancel(str(arguments["activity_id"]))
                if name == "orchestrator_activity_message":
                    return await children.message(call_id, str(arguments["activity_id"]), str(arguments["text"]))
                identity = key(str(self.lease.run.id), call_id)
                return await activities.start(
                    identity,
                    "subagent",
                    lambda: children.delegate(identity, str(arguments["instruction"])),
                    foreground_seconds=0,
                )

            def invoke_coordination(engine: Engine, current: State) -> Json:
                controller = engine
                check(current)
                active = controller.ticket(ticket_id)
                if name == "orchestrator_read":
                    return worker_facts(current, ticket_id, arguments)
                if name == "orchestrator_ack":
                    return {
                        "ok": True,
                        "reply": asdict(
                            controller.reply(
                                str(arguments.get("trigger_id", active.origin_id)),
                                "ack",
                                str(arguments["text"]),
                                ticket_id=ticket_id,
                            )
                        ),
                    }
                if name == "orchestrator_trigger_handback":
                    return {
                        "ok": True,
                        "queued": controller.handback(
                            ticket_id, str(arguments["trigger_id"]), str(arguments["reason"])
                        ),
                    }
                if name == "orchestrator_thread_follow":
                    controller.follow(ticket_id, str(arguments["thread"]))
                elif name == "orchestrator_thread_unrelated":
                    controller.unsubscribe(ticket_id, str(arguments["thread"]))
                elif name == "orchestrator_report":
                    if status_only and arguments.get("state") not in {"progress", "waiting"}:
                        raise CoordinationError("STATUS_ONLY_REPORT_REQUIRED")
                    report = Report(
                        state=cast(
                            Literal["progress", "ready", "blocked", "failed", "dropped", "waiting"], arguments["state"]
                        ),
                        summary=str(arguments["summary"]),
                        at=controller.now(),
                        human_action=cast(str | None, arguments.get("human_action")),
                        question_trigger_id=cast(str | None, arguments.get("question_trigger_id")),
                        answers=cast(str | None, arguments.get("answers")),
                        human_action_supplied="human_action" in arguments,
                    )
                    current.checkpoints[f"report:{self.lease.run.id}"] = asdict(report)
                    if report.state in {"ready", "dropped"}:
                        if activities.running:
                            raise CoordinationError("BACKGROUND_WORK_RUNNING")
                        if report.human_action or not report.summary.strip():
                            raise CoordinationError("REPORT_QUESTION_CONFLICT")
                        return {"ok": True, "status": "pending_run_validation"}
                    controller.report(ticket_id, assignment_id, report)
                return {"ok": True}

            try:
                return await self.service.store.transact(workspace_id, scope_id, invoke_coordination)
            except CoordinationError as error:
                return {"ok": False, "code": error.code}

        runtime = self.runtime_factory(session, invoke)
        try:
            instruction = payload.instruction
            while True:
                text = await deliver_session(
                    partial(
                        runtime.run,
                        RuntimeInput(
                            payload.system_prompt + WORKER_PROMPT,
                            instruction,
                            tuple(item for item in payload.tools if not status_only or item.effect == "read")
                            + WORKER_TOOLS,
                        ),
                        profile,
                    ),
                    status,
                )
                # 与上游一致，空闲回合有后台活动时保留槽位；新消息或活动结果重新进入同一会话。
                deadline = asyncio.get_running_loop().time() + 1800
                while True:
                    activities.require_settled_records()
                    current = await fresh()
                    active = records(current).tickets.get(ticket_id)
                    assert active is not None
                    if active.attention or Engine.waits_for_person(active):
                        return text
                    completed = [
                        item
                        for item in current.activities
                        if item.run_id == str(self.lease.run.id) and item.state != "running" and not item.read
                    ]
                    if completed or await pull():
                        session.initial_input_ids = [f"activity:{item.id}" for item in completed]
                        instruction = "平台活动事实（不增加权限），请读取后继续当前任务：" + json.dumps(
                            [
                                {"activity_id": item.id, "state": item.state, "result": str(item.result)[:12000]}
                                for item in completed
                            ],
                            ensure_ascii=False,
                        )
                        break
                    if not activities.running:
                        return text
                    if asyncio.get_running_loop().time() >= deadline:
                        await activities.close()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(activities.changed.wait(), timeout=0.2)
                    activities.changed.clear()
        except CoordinationError as error:
            raise RuntimeFailure(error.code) from None
        finally:
            # Weora 的人工等待和 Run 结束均释放执行资源；不保留脱离审批/租约的服务进程。
            await activities.close()
