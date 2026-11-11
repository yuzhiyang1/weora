"""Super-agent 状态机移植。

参考 59e6c405 的 core/{intake,evals,tickets,assignments,replies}.ts。
所有方法必须在调用方的同一事务中执行；模型、队列和渠道 IO 均不在事务内。
"""

import hashlib
import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from weora.modules.context.replies import TASK_ACCEPTANCE_REPLY
from weora.modules.coordination.domain import (
    Assignment,
    Clock,
    Config,
    CoordinationError,
    Decision,
    Evaluation,
    Follow,
    Input,
    Json,
    Mandate,
    Notice,
    Records,
    Reply,
    Report,
    Thread,
    Ticket,
    Trigger,
)
from weora.modules.coordination.policy import assessment_outcome, gate, policy_version, revalidate


def key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def later(at: str, seconds: float) -> str:
    return (datetime.fromisoformat(at) + timedelta(seconds=seconds)).isoformat()


def task_reference(text: str) -> tuple[str, str] | None:
    """识别消息开头的任务编号；保留格式错误的编号请求，避免落入新建任务流程。"""
    match = re.match(r"[#＃]([^\s:：]*)[\s:：]*(.*)\Z", text.lstrip(), re.DOTALL)
    return (match[1].lower(), match[2].strip()) if match else None


class Engine:
    def __init__(self, records: Records, config: Config, now: Clock) -> None:
        self.r, self.config, self.now = records, config, now

    def record_notice(self, kind: str, identity: str, data: Json, *, quiet: bool = False) -> None:
        """幂等保存协调事实；非静默事实同时唤醒主会话，静默进度仅供后续查询。"""
        identity = key(kind, identity)
        if self.r.notices.exists(identity):
            return
        self.r.notices.save(Notice(identity, kind, self.now(), data, quiet))
        if not quiet:
            self.r.effects.enqueue("main", self.config.id, {"scope_id": self.config.id, "notice_id": identity})

    def _thread(self, identity: str) -> Thread:
        return self.r.threads.get(identity) or Thread(identity, self.now(), self.now())

    def ticket(self, identity: str) -> Ticket:
        """取得当前事务内的任务记录，缺失时拒绝继续修改或生成回复。"""
        ticket = self.r.tickets.get(identity)
        if ticket is None:
            raise CoordinationError("TICKET_NOT_FOUND")
        return ticket

    def _task_reference(self, trigger: Trigger) -> tuple[str, str] | None:
        if self.config.source_kind == "feishu" and trigger.human and trigger.addressed:
            return task_reference(trigger.text)
        return None

    def owners(self, trigger: Trigger) -> list[str]:
        # 对照 core/model.ts followupOwners：人工重排、评估中与已读的结束任务不再占有跟进。
        if not trigger.human or trigger.requeued:
            return []
        thread = self.r.threads.get(trigger.threads[0])
        evaluation = self.r.evaluations.get(thread.evaluation_id) if thread and thread.evaluation_id else None
        if evaluation and trigger.id in evaluation.trigger_ids and evaluation.state == "pending":
            return []
        owners = []
        for identity in trigger.fanout:
            if identity in trigger.handbacks or identity in trigger.context_only:
                continue
            ticket = self.r.tickets.get(identity)
            if ticket and (
                not ticket.ended_at
                or (
                    ticket.ended_by != "human"
                    and any(item.trigger_id == trigger.id and item.id in ticket.undelivered for item in ticket.inputs)
                )
            ):
                owners.append(identity)
        return owners

    def receive(self, trigger: Trigger) -> bool:
        """先保存原始处置，再去重、订阅扇出和候选排队；返回值决定是否唤醒。"""
        if self.r.triggers.exists(trigger.id):
            return False
        if (
            trigger.workspace_id != self.config.workspace_id
            or trigger.scope_id != self.config.id
            or not trigger.threads
        ):
            raise CoordinationError("SOURCE_MISMATCH")
        reference = self._task_reference(trigger)
        if reference is not None:
            return self._receive_task_reference(trigger, *reference)
        admission = gate(self.config, trigger)
        subscribed: dict[str, bool] = {}
        for thread_id in trigger.threads:
            thread = self._thread(thread_id)
            thread.last_at = max(thread.last_at, trigger.at)
            for identity in thread.subscriptions:
                context_only = identity in thread.context_only
                subscribed[identity] = subscribed.get(identity, True) and context_only
            self.r.threads.save(thread)
        if not trigger.authorized or not self.config.enabled:
            trigger.filtered = "source_or_actor_revoked"
        elif not admission.admitted and not subscribed:
            trigger.filtered = admission.reason
        self.r.triggers.save(trigger)
        if trigger.filtered:
            return False
        # 只认任务自己的话题订阅。群级来源或旁支上下文不能把别人的消息挂到任务上。
        own_threads = [self.ticket(identity) for identity, context_only in subscribed.items() if not context_only]
        if (
            self.config.source_kind == "feishu" and trigger.human and own_threads
            and not any(ticket.mandate.actor_id == trigger.actor_id for ticket in own_threads)
        ):
            return self._receive_peer_evidence(trigger, own_threads)
        for identity, context_only in subscribed.items():
            ticket = self.ticket(identity)
            if ticket.ended_by == "human":
                continue
            origin = self.r.triggers.get(ticket.origin_id)
            if not origin or not origin.authorized:
                continue
            # 订阅不是新授权：不同发起人需要走主 Agent 路由，不能借线程继承他人的工具权限。
            if trigger.actor_id != ticket.mandate.actor_id:
                continue
            follow_admission = gate(self.config, trigger, participating=True)
            if not context_only and not follow_admission.admitted:
                continue
            self._input(ticket, trigger, context_only=context_only)
        self.r.triggers.save(trigger)
        if trigger.human and self.owners(trigger):
            self.record_notice("trigger.followup", trigger.id, {"trigger_id": trigger.id})
            return True
        if admission.admitted:
            self._queue(trigger)
            return True
        return bool(trigger.fanout)

    def _receive_peer_evidence(self, trigger: Trigger, tickets: list[Ticket]) -> bool:
        """话题里的跨人回复只作为原任务证据，包含无权变更或误发的新请求。"""
        trigger.peer_evidence = True
        if len(tickets) > 1:
            quote = trigger.origin.get("quote_id")
            tickets = [ticket for ticket in tickets if quote in ticket.origin_ids]
        if len(tickets) != 1:
            trigger.filtered = "peer_topic_ambiguous"
        else:
            ticket = tickets[0]
            origin = self.r.triggers.get(ticket.origin_id)
            if ticket.ended_by == "human" or not origin or not revalidate(
                self.config, ticket.mandate, authorized=origin.authorized,
            ):
                trigger.filtered = "peer_task_unavailable"
            else:
                self._input(ticket, trigger)
        self.r.triggers.save(trigger)
        return bool(trigger.fanout)

    def _receive_task_reference(self, trigger: Trigger, number: str, instruction: str) -> bool:
        """把编号请求只交给一个现有 Task，或通过 Outbox 回复发送者。"""
        self.r.triggers.save(trigger)
        if not trigger.authorized or not self.config.enabled:
            return False

        valid = re.fullmatch(r"[0-9a-f]{8}", number) is not None
        if len(number) == 36:
            try:
                valid = str(UUID(number)) == number
            except ValueError:
                valid = False
        if not valid or not instruction:
            self.reply(trigger.id, "answer", "请按「@Weora #任务编号 补充说明」发送，编号可从任务列表复制。")
            return True

        admission = gate(self.config, trigger)
        matches = [item for item in self.r.tickets.list() if item.task_id and item.task_id.startswith(number)]
        if admission.strength == "explicit" and len(matches) == 1:
            ticket = matches[0]
            origin = self.r.triggers.get(ticket.origin_id)
            if (
                ticket.mandate.actor_id == trigger.actor_id
                and ticket.ended_by != "human"
                and any(assignment.run_id for assignment in ticket.assignments)
                and origin is not None
                and (
                    not origin.expires_at
                    or datetime.fromisoformat(origin.expires_at) > datetime.fromisoformat(self.now())
                )
                and revalidate(self.config, ticket.mandate, authorized=origin.authorized)
            ):
                # 编号只指定这条消息的去向，避免把整个群聊或其他任务的话题订阅进来。
                self._input(ticket, trigger)
                self.r.triggers.save(trigger)
                self.reply(trigger.id, "ack", f"已将补充内容加入任务 #{number.upper()}。")
                return True

        self.reply(trigger.id, "answer", f"找不到可续接的任务 #{number.upper()}。请核对编号，或回到原任务话题继续。")
        return True

    def _queue(self, trigger: Trigger) -> None:
        if trigger.peer_evidence:
            return
        thread = self._thread(trigger.threads[0])
        if trigger.id not in thread.pending:
            thread.pending.append(trigger.id)
        thread.due = later(self.now(), self.config.debounce_ms / 1000)
        self.r.threads.save(thread)

    def _input(self, ticket: Ticket, trigger: Trigger, *, context_only: bool = False) -> None:
        if any(item.trigger_id == trigger.id for item in ticket.inputs):
            return
        item = Input(
            key(ticket.id, trigger.id),
            "trigger",
            self.now(),
            trigger_id=trigger.id,
            human=trigger.human,
            context_only=context_only,
            peer_evidence=trigger.peer_evidence,
        )
        ticket.inputs.append(item)
        if trigger.human or not self.waits_for_person(ticket):
            ticket.undelivered.append(item.id)
        trigger.fanout.append(ticket.id)
        if context_only:
            trigger.context_only.append(ticket.id)
        ticket.updated_at = self.now()
        if trigger.human and not context_only and ticket.ended_by == "executor" and (
            not item.peer_evidence or not (ticket.attention or self.waits_for_person(ticket))
        ):
            ticket.ended_at = ticket.ended_by = ticket.outcome = None
            ticket.assignments.append(Assignment(key("assignment", ticket.id, trigger.id), self.now()))
        self.r.tickets.save(ticket)
        self._wake(
            ticket,
            human=trigger.human
            and not context_only
            and not item.peer_evidence
            and (trigger.id not in ticket.origin_ids or ticket.mandate.strength == "explicit"),
        )

    def _wake(self, ticket: Ticket, *, human: bool = False) -> None:
        if self.config.paused or ticket.ended_by == "human" or not ticket.assignments:
            return
        assignment = ticket.assignments[-1]
        if assignment.ended_at:
            return
        if self.config.source_kind == "feishu" and assignment.report and assignment.report.state == "failed":
            # 跟进交给主会话识别是否明确重试；不能同时唤醒失败安排和新安排。
            return
        if ticket.attention and not human:
            return
        if self.waits_for_person(ticket) and not human:
            return
        if not revalidate(self.config, ticket.mandate, authorized=True):
            self.record_notice("ticket.policy_blocked", ticket.id, {"ticket_id": ticket.id})
            return
        self.r.effects.enqueue(
            "worker",
            ticket.id,
            {
                "ticket_id": ticket.id,
                "assignment_id": assignment.id,
                "input_count": len(ticket.inputs),
                "status_request": ticket.status_requests,
            },
            priority=1 if human else 0,
        )

    @staticmethod
    def waits_for_person(ticket: Ticket) -> bool:
        """对应上游 waitsForPerson：阻塞后的自动事实不能替代人类决定。"""
        if not ticket.assignments:
            return False
        assignment = ticket.assignments[-1]
        if not assignment.report or assignment.report.state not in {"blocked", "failed"}:
            return False
        if assignment.report_input_count is not None:
            later_inputs = ticket.inputs[assignment.report_input_count :]
        else:
            # 旧状态没有输入序号，沿用上游时间屏障；新报告统一保存平台时间和输入序号。
            barrier = assignment.idle_at or assignment.report.at
            later_inputs = [item for item in ticket.inputs if item.at > barrier]
        return not any(item.human and not item.context_only and not item.peer_evidence for item in later_inputs)

    @staticmethod
    def peer_evidence_only(ticket: Ticket) -> bool:
        """最近一条人类输入是他人证据时，本轮只核实事实；发起人的输入仍走原授权和审批。"""
        return next(
            (item.peer_evidence for item in reversed(ticket.inputs) if item.human and not item.context_only), False,
        )

    def pass_due(self) -> list[Evaluation]:
        """两秒兜底检查；只有已投递的评估计算超时，暂停期间冻结期限。"""
        candidates: list[Evaluation] = []
        version, now = policy_version(self.config), self.now()
        for thread in self.r.threads.list():
            evaluation = self.r.evaluations.get(thread.evaluation_id) if thread.evaluation_id else None
            if evaluation and evaluation.state == "pending":
                if self.config.paused:
                    evaluation.paused_at = evaluation.paused_at or now
                elif evaluation.paused_at:
                    if evaluation.deadline:
                        duration = (
                            datetime.fromisoformat(now) - datetime.fromisoformat(evaluation.paused_at)
                        ).total_seconds()
                        evaluation.deadline = later(evaluation.deadline, duration)
                    evaluation.paused_at = None
                elif evaluation.policy_version != version:
                    self._reassess(thread, evaluation)
                    evaluation = None
                elif evaluation.delivered_at and evaluation.deadline and evaluation.deadline <= now:
                    thread.expired += 1
                    if thread.expired > 2:
                        self._finish(thread, evaluation, "unavailable", "timeout")
                    else:
                        self._reassess(thread, evaluation)
                    evaluation = None
                if evaluation:
                    self.r.evaluations.save(evaluation)
                    candidates.append(evaluation)
            if (
                not self.config.paused
                and not thread.evaluation_id
                and thread.pending
                and (not thread.due or thread.due <= now)
            ):
                evaluation = self._start(thread)
                if evaluation:
                    candidates.append(evaluation)
            self.r.threads.save(thread)
        if not self.config.paused:
            for ticket in self.r.tickets.list():
                if ticket.ended_by == "executor" and not ticket.attention and not self.waits_for_person(ticket):
                    self._resume_pending_peer_evidence(ticket)
                self._wake(ticket)
        return candidates

    def _resume_pending_peer_evidence(self, ticket: Ticket) -> None:
        """收尾窗口刚到的证据留在同一任务，待原 Run 结束再开下一轮。"""
        for item in ticket.inputs:
            if not item.peer_evidence or item.id not in ticket.undelivered or not item.trigger_id:
                continue
            trigger = self.r.triggers.get(item.trigger_id)
            origin = self.r.triggers.get(ticket.origin_id)
            if trigger and trigger.authorized and ticket.id not in trigger.handbacks and origin and revalidate(
                self.config, ticket.mandate, authorized=origin.authorized,
            ):
                ticket.ended_at = ticket.ended_by = ticket.outcome = None
                ticket.assignments.append(Assignment(key("assignment", ticket.id, trigger.id), self.now()))
                self.r.tickets.save(ticket)
                return

    def _start(self, thread: Thread) -> Evaluation | None:
        pending = [self.r.triggers.get(identity) for identity in thread.pending]
        candidates = []
        accepted = {
            identity
            for decision in self.r.decisions.list()
            if decision.thread_key == thread.id and decision.kind == "accept"
            for identity in decision.trigger_ids
        }
        for trigger in pending:
            if trigger is None or trigger.peer_evidence or self.owners(trigger) or trigger.id in accepted:
                continue
            # 发起人接单前到达的回复可能已排进评估；已有关联任务时改交原 Worker。
            if self.config.source_kind == "feishu" and trigger.human:
                tickets = [
                    self.ticket(identity) for identity in thread.subscriptions if identity not in thread.context_only
                ]
                if tickets and not any(ticket.mandate.actor_id == trigger.actor_id for ticket in tickets):
                    self._receive_peer_evidence(trigger, tickets)
                    continue
            participating = any(
                identity not in trigger.context_only
                and (ticket := self.r.tickets.get(identity))
                and ticket.ended_by != "human"
                and ticket.mandate.actor_id == trigger.actor_id
                for identity in trigger.fanout
            )
            admission = gate(self.config, trigger, participating=participating)
            if admission.admitted:
                candidates.append((trigger, admission))
        thread.pending = []
        if not candidates:
            return None
        _, (primary, admitted) = max(
            enumerate(candidates), key=lambda row: (row[1][1].strength == "explicit", row[1][0].at, row[0])
        )
        # Weora 的 Task 有不可变发起人；同线程不同人的请求分开评估，不能共享一个人的执行授权。
        thread.pending = [item.id for item, _ in candidates if item.actor_id != primary.actor_id]
        candidates = [(item, value) for item, value in candidates if item.actor_id == primary.actor_id]
        identity = key("eval", thread.id, policy_version(self.config), self.now(), *(item.id for item, _ in candidates))
        evaluation = Evaluation(
            identity,
            thread.id,
            [item.id for item, _ in candidates],
            primary.id,
            admitted.strength,
            primary.actor_id,
            admitted.basis or {},
            policy_version(self.config),
            self.now(),
        )
        thread.evaluation_id = identity
        self.r.evaluations.save(evaluation)
        for trigger, _ in candidates:
            trigger.eval_id = identity
            self.r.triggers.save(trigger)
        self.record_notice("eval.started", identity, {"eval_id": identity})
        return evaluation

    def delivered(self, eval_id: str) -> None:
        evaluation = self.pending_evaluation(eval_id)
        if not evaluation.delivered_at:
            evaluation.delivered_at = self.now()
            evaluation.deadline = later(self.now(), self.config.eval_timeout_seconds)
            self.r.evaluations.save(evaluation)

    def read_eval(self, eval_id: str) -> Evaluation:
        evaluation = self.pending_evaluation(eval_id)
        evaluation.context_read = True
        self.r.evaluations.save(evaluation)
        return evaluation

    def pending_evaluation(self, identity: str) -> Evaluation:
        """只返回当前策略下未结束的评估；恢复暂停期限后仍须检查是否已经超时。"""
        evaluation = self.r.evaluations.get(identity)
        if not evaluation or evaluation.state != "pending":
            raise CoordinationError("EVAL_FINISHED")
        if evaluation.policy_version != policy_version(self.config):
            raise CoordinationError("POLICY_CHANGED")
        if self.config.paused or not self.config.enabled:
            raise CoordinationError("PROCESSING_PAUSED")
        if evaluation.paused_at:
            if evaluation.deadline:
                duration = (
                    datetime.fromisoformat(self.now()) - datetime.fromisoformat(evaluation.paused_at)
                ).total_seconds()
                evaluation.deadline = later(evaluation.deadline, duration)
            evaluation.paused_at = None
            self.r.evaluations.save(evaluation)
        if evaluation.delivered_at and evaluation.deadline and evaluation.deadline <= self.now():
            raise CoordinationError("EVAL_EXPIRED")
        return evaluation

    def _reassess(self, thread: Thread, evaluation: Evaluation) -> None:
        evaluation.state = "superseded"
        self.r.evaluations.save(evaluation)
        thread.evaluation_id = None
        thread.pending = list(dict.fromkeys(evaluation.trigger_ids + thread.pending))
        # 评估复用主会话而无独立 assessment 效果；superseded 状态拒绝旧决策，不取消共享会话。

    def _finish(
        self,
        thread: Thread,
        evaluation: Evaluation,
        kind: str,
        reason: str,
        *,
        audit: Json | None = None,
        ticket_id: str | None = None,
    ) -> Decision:
        decision = Decision(
            key("decision", evaluation.id),
            evaluation.id,
            thread.id,
            evaluation.trigger_ids,
            evaluation.policy_version,
            self.now(),
            kind,
            reason,
            evaluation.strength,
            audit or {},
            ticket_id,
        )
        self.r.decisions.save(decision)
        for identity in evaluation.trigger_ids:
            trigger = self.r.triggers.get(identity)
            if trigger:
                trigger.decision_id = decision.id
                self.r.triggers.save(trigger)
        evaluation.state = "decided"
        self.r.evaluations.save(evaluation)
        if thread.evaluation_id == evaluation.id:
            thread.evaluation_id = None
            thread.expired = 0
        thread.pending = [identity for identity in thread.pending if identity not in evaluation.trigger_ids]
        self.r.threads.save(thread)
        return decision

    def decide(self, eval_id: str, result: Json, *, route: bool = False) -> Decision:
        """验证本轮判断并原子保存决定、接单依据、订阅、输入及派发效果。

        推断请求还须完整规则和方向证据；显式路由可续接或停止同一作者的任务。
        本方法不创建原生 Run、不调用外部系统，异常由调用方事务整体回滚。
        """
        # 先验证评估仍有效且已读取上下文，避免旧模型轮次提交过时授权。
        evaluation = self.pending_evaluation(eval_id)
        if not evaluation.context_read:
            raise CoordinationError("CONTEXT_READ_REQUIRED")
        if route != (evaluation.strength == "explicit"):
            raise CoordinationError("WRONG_DECISION_TOOL")
        trigger = self.r.triggers.get(evaluation.primary)
        if not trigger or not trigger.authorized:
            raise CoordinationError("ACCESS_REVOKED")
        kind, reason = str(result.get("decision", "")), str(result.get("reason", "")).strip()
        audit: Json = {}
        rules: tuple[str, ...] = ()
        if not route:
            kind, reason, audit, rules = assessment_outcome(self.config, result, evaluation.strength)
        elif kind not in {"accept", "attach", "stop", "ignore"} or not reason:
            raise CoordinationError("DECISION_INVALID")
        thread = self._thread(evaluation.thread_key)
        if (
            any(self.owners(item) for identity in evaluation.trigger_ids if (item := self.r.triggers.get(identity)))
            and kind != "stop"
        ):
            raise CoordinationError("LEFT_TO_WORKER")
        ticket_id: str | None = None
        if kind == "accept":
            # 接单冻结执行范围与 Worker 版本，再把同批输入及订阅交给该任务。
            title, scope = result.get("title"), result.get("scope")
            if (
                not isinstance(title, str)
                or not title.strip()
                or len(title) > 200
                or not isinstance(scope, str)
                or not scope.strip()
                or len(scope) > 16000
            ):
                raise CoordinationError("TICKET_SCOPE_REQUIRED")
            mandate = Mandate(
                strength=evaluation.strength,
                policy_version=evaluation.policy_version,
                actor_id=trigger.actor_id,
                actor_open_id=trigger.actor_open_id,
                scope=scope,
                basis=evaluation.basis,
                rules=rules,
                worker_agent_id=self.config.worker_agent_id,
                worker_agent_version=self.config.worker_agent_version,
                worker_model_id=self.config.worker_model_id,
                project_id=self.config.project_id,
                repository_ids=self.config.repository_ids,
                project_grant_id=self.config.project_grant_id,
                route_key=self.config.route_key,
            )
            if not revalidate(self.config, mandate, authorized=trigger.authorized):
                raise CoordinationError("MANDATE_REVOKED")
            ticket_id = key("ticket", evaluation.id)
            ticket = Ticket(
                id=ticket_id,
                title=title,
                decision_id=key("decision", evaluation.id),
                mandate=mandate,
                origin_id=trigger.id,
                origin_ids=list(evaluation.trigger_ids),
                created_at=self.now(),
                updated_at=self.now(),
                session_id=key("worker-session", ticket_id),
                assignments=[Assignment(key("assignment", ticket_id, "0"), self.now())],
            )
            for identity in evaluation.trigger_ids:
                item = self.r.triggers.get(identity)
                if item:
                    for thread_id in item.threads:
                        self._follow(ticket, thread_id, own=True)
                    self._input(ticket, item)
                    self.r.triggers.save(item)
            self.r.tickets.save(ticket)
            acknowledgement = result.get("acknowledgement")
            if self.config.source_kind == "feishu" and trigger.human:
                acknowledgement = acknowledgement.strip() if isinstance(acknowledgement, str) else ""
                if not acknowledgement or len(acknowledgement) > 240:
                    acknowledgement = TASK_ACCEPTANCE_REPLY
            if isinstance(acknowledgement, str) and acknowledgement.strip():
                self.reply(trigger.id, "ack", acknowledgement, ticket_id=ticket.id)
            self.record_notice("ticket.accepted", ticket_id, {"ticket_id": ticket_id})
        elif kind in {"attach", "stop"}:
            # 跨任务路由仍按发起人隔离；停止与跟进不能继承其他人的任务权限。
            ticket = self.ticket(str(result.get("ticket_id", "")))
            if ticket.mandate.actor_id != trigger.actor_id:
                raise CoordinationError("ACTOR_MISMATCH")
            ticket_id = ticket.id
            if kind == "stop":
                self.stop(ticket.id, trigger.id)
            else:
                if ticket.ended_by == "human":
                    raise CoordinationError("HUMAN_STOPPED")
                for identity in evaluation.trigger_ids:
                    item = self.r.triggers.get(identity)
                    if item:
                        for thread_id in item.threads:
                            self._follow(ticket, thread_id, own=True)
                        self._input(ticket, item)
                        self.r.triggers.save(item)
        return self._finish(thread, evaluation, kind, reason, audit=audit, ticket_id=ticket_id)

    def route(self, trigger_id: str, result: Json) -> Decision:
        """显式请求的直接路由，也允许主会话停止 Worker 正在处理的跟进。"""
        trigger = self.r.triggers.get(trigger_id)
        if not trigger:
            raise CoordinationError("TRIGGER_NOT_FOUND")
        if trigger.peer_evidence:
            raise CoordinationError("PEER_EVIDENCE_ONLY")
        if self._task_reference(trigger) is not None:
            raise CoordinationError("TASK_REFERENCE_ALREADY_HANDLED")
        if trigger.eval_id:
            current = self.r.evaluations.get(trigger.eval_id)
            if current and current.state == "pending":
                self.read_eval(current.id)
                return self.decide(current.id, result, route=True)
        admitted = gate(self.config, trigger, participating=bool(self.owners(trigger)))
        if not admitted.admitted or admitted.strength != "explicit":
            raise CoordinationError("EXPLICIT_REQUEST_REQUIRED")
        if result.get("decision") == "accept":
            previous = next(
                (item for item in self.r.decisions.list() if item.kind == "accept" and trigger.id in item.trigger_ids),
                None,
            )
            if previous:
                return previous
        identity = key(
            "direct-eval",
            trigger.id,
            policy_version(self.config),
            str(result.get("decision")),
            str(result.get("ticket_id", "")),
        )
        previous_eval = self.r.evaluations.get(identity)
        if previous_eval and previous_eval.state == "decided":
            previous = self.r.decisions.get(key("decision", identity))
            if previous:
                return previous
        evaluation = Evaluation(
            identity,
            trigger.threads[0],
            [trigger.id],
            trigger.id,
            "explicit",
            trigger.actor_id,
            admitted.basis or {},
            policy_version(self.config),
            self.now(),
            context_read=True,
        )
        self.r.evaluations.save(evaluation)
        return self.decide(identity, result, route=True)

    def _follow(self, ticket: Ticket, thread_id: str, *, own: bool = False) -> None:
        thread = self._thread(thread_id)
        if ticket.id not in thread.subscriptions:
            thread.subscriptions.append(ticket.id)
        if own and ticket.id in thread.context_only:
            thread.context_only.remove(ticket.id)
        elif not own and ticket.id not in thread.context_only:
            thread.context_only.append(ticket.id)
        existing = next((item for item in ticket.follows if item.thread == thread_id), None)
        if existing:
            existing.own = existing.own or own
        else:
            ticket.follows.append(Follow(thread_id, own))
        self.r.threads.save(thread)

    def follow(self, ticket_id: str, thread_id: str) -> None:
        ticket = self.ticket(ticket_id)
        # 只能订阅平台已授权接收的线程；工具不能凭空构造外部群。
        if not self.r.threads.exists(thread_id):
            raise CoordinationError("THREAD_NOT_FOUND")
        source = next(
            (
                (owner, follow)
                for owner in self.r.tickets.list()
                for follow in owner.follows
                if follow.thread == thread_id and follow.lifecycle
            ),
            None,
        )
        if source and source[0].mandate.actor_id != ticket.mandate.actor_id:
            raise CoordinationError("THREAD_ACTOR_MISMATCH")
        self._follow(ticket, thread_id)
        if source:
            current = next(item for item in ticket.follows if item.thread == thread_id)
            ticket.follows[ticket.follows.index(current)] = replace(source[1], own=current.own)
        self.r.tickets.save(ticket)

    def bind_review(self, ticket_id: str, thread_id: str, run_id: str, review_id: str) -> None:
        """由平台交付事实绑定本任务的评审；模型不能通过外部 URL 把其他仓库挂为自身交付。"""
        ticket = self.ticket(ticket_id)
        for other in self.r.tickets.list():
            if (
                other.id != ticket_id
                and not other.ended_at
                and any(item.thread == thread_id and item.lifecycle and item.own for item in other.follows)
            ):
                raise CoordinationError("REVIEW_ALREADY_OWNED")
        self._follow(ticket, thread_id, own=True)
        follow = next(item for item in ticket.follows if item.thread == thread_id)
        follow.lifecycle, follow.review_run_id, follow.review_id = True, run_id, review_id
        self.r.tickets.save(ticket)

    @staticmethod
    def follows_done(ticket: Ticket) -> str | None:
        """对应上游 followsDone：有自己的生命周期线程时以自身线程为准，否则等全部关闭。"""
        follows = [item for item in ticket.follows if item.lifecycle]
        own = [item for item in follows if item.own]
        waiting = own or follows
        if not waiting or any(not item.closed_at for item in waiting):
            return None
        return max(waiting, key=lambda item: item.closed_at or "").outcome

    def observe_review(
        self,
        ticket_id: str,
        thread_id: str,
        outcome: Literal["open", "closed", "merged"],
        at: str,
        *,
        closed_at: str | None = None,
    ) -> None:
        """保存可信源的生命周期；关闭不吞掉未处理的人类输入，重开不自动恢复已结束任务。"""
        ticket = self.ticket(ticket_id)
        follow = next((item for item in ticket.follows if item.thread == thread_id and item.lifecycle), None)
        if not follow or (follow.observed_at and at < follow.observed_at):
            return
        follow.observed_at = at
        follow.closed_at, follow.outcome = (None, None) if outcome == "open" else (closed_at or at, outcome)
        self.r.tickets.save(ticket)
        result = self.follows_done(ticket)
        if not result or ticket.ended_at or not ticket.assignments:
            return
        latest = ticket.assignments[-1]
        pending_human = any(
            item.human and not item.context_only and (item.id in ticket.undelivered or not item.read_at)
            for item in ticket.inputs
        )
        # 一个仍在处理的安排不能被落后到达的 close/merge 抢先结束。
        if pending_human or not latest.report or latest.report.state == "progress":
            return
        if latest.report.state != "ready" and (not closed_at or latest.created_at > closed_at):
            # 轮询时间不是关闭时间；缺少可信关闭时刻或关闭前已恢复新工作时，等 Worker 明确报告。
            return
        latest.ended_at, latest.end_reason = self.now(), result
        ticket.ended_at, ticket.ended_by, ticket.outcome = self.now(), "executor", result
        self._release_untaken(ticket)
        self.r.effects.cancel("worker", ticket.id)
        if latest.run_id:
            self.r.effects.enqueue("cancel", latest.run_id, {"ticket_id": ticket.id, "run_id": latest.run_id})
        self.r.tickets.save(ticket)
        self.record_notice("ticket.ended", key(ticket.id, thread_id, at), {"ticket_id": ticket.id, "outcome": result})

    def unsubscribe(self, ticket_id: str, thread_id: str) -> None:
        """Worker 只能移除旁支订阅，不能把自己的任务来源判为无关。"""
        ticket = self.ticket(ticket_id)
        if any(item.thread == thread_id and item.own for item in ticket.follows):
            raise CoordinationError("OWN_THREAD_REQUIRED")
        thread = self._thread(thread_id)
        thread.subscriptions = [item for item in thread.subscriptions if item != ticket_id]
        thread.context_only = [item for item in thread.context_only if item != ticket_id]
        ticket.follows = [item for item in ticket.follows if item.thread != thread_id]
        self.r.threads.save(thread)
        self.r.tickets.save(ticket)

    def unsubscribe_thread(self, thread_id: str, trigger_id: str, ticket_id: str | None = None) -> list[str]:
        """按显式人类指令取消其任务的线程订阅，允许原线程，保留其他发起人的订阅。"""
        trigger = self.r.triggers.get(trigger_id)
        if not trigger or not trigger.human or not trigger.authorized:
            raise CoordinationError("EXPLICIT_UNSUBSCRIBE_REQUIRED")
        admission = gate(self.config, trigger, participating=True)
        if not admission.admitted or admission.strength != "explicit":
            raise CoordinationError("EXPLICIT_UNSUBSCRIBE_REQUIRED")
        thread = self.r.threads.get(thread_id)
        if not thread:
            raise CoordinationError("THREAD_NOT_FOUND")

        removed = []
        for identity in list(thread.subscriptions):
            ticket = self.ticket(identity)
            if ticket.mandate.actor_id != trigger.actor_id or (ticket_id and identity != ticket_id):
                continue
            thread.subscriptions.remove(identity)
            thread.context_only = [item for item in thread.context_only if item != identity]
            ticket.follows = [item for item in ticket.follows if item.thread != thread_id]
            self.r.tickets.save(ticket)
            removed.append(identity)
        self.r.threads.save(thread)
        self.record_notice("thread.unsubscribed", key(thread_id, trigger_id), {"thread": thread_id, "tickets": removed})
        return removed

    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool:
        ticket, trigger = self.ticket(ticket_id), self.r.triggers.get(trigger_id)
        if not trigger or not any(item.trigger_id == trigger_id for item in ticket.inputs):
            raise CoordinationError("INPUT_NOT_REACHED")
        if ticket_id in trigger.handbacks:
            raise CoordinationError("ALREADY_HANDED_BACK")
        if not reason.strip():
            raise CoordinationError("REASON_REQUIRED")
        trigger.handbacks[ticket_id] = reason
        self.r.triggers.save(trigger)
        if trigger.peer_evidence or self._task_reference(trigger) is not None:
            return False
        if not self.owners(trigger):
            self._queue(trigger)
            return True
        return False

    def requeue_trigger(self, trigger_id: str) -> None:
        """重排仍可读且不再由 Worker 占有的候选，不允许从活跃任务抢走跟进消息。"""
        trigger = self.r.triggers.get(trigger_id)
        if not trigger or not trigger.authorized or self.owners(trigger):
            raise CoordinationError("LEFT_TO_WORKER")
        if trigger.peer_evidence:
            raise CoordinationError("PEER_EVIDENCE_ONLY")
        if self._task_reference(trigger) is not None:
            raise CoordinationError("TASK_REFERENCE_ALREADY_HANDLED")
        self._queue(trigger)

    def note(self, ticket_id: str, text: str, identity: str) -> None:
        ticket = self.ticket(ticket_id)
        if not text.strip() or len(text) > 4000:
            raise CoordinationError("NOTE_INVALID")
        input_id = key(ticket_id, "note", identity)
        if not any(item.id == input_id for item in ticket.inputs):
            ticket.inputs.append(Input(input_id, "note", self.now(), text=text))
            if not self.waits_for_person(ticket):
                ticket.undelivered.append(input_id)
            self.r.tickets.save(ticket)
            self._wake(ticket)

    def status(self, ticket_id: str) -> None:
        ticket = self.ticket(ticket_id)
        if (
            ticket.ended_at
            or not ticket.assignments
            or not ticket.assignments[-1].idle_at
            or not ticket.assignments[-1].report
            or ticket.assignments[-1].report.state != "waiting"
        ):
            raise CoordinationError("WORKER_NOT_IDLE")
        ticket.assignments[-1].status_only = True
        ticket.status_requests += 1
        self.r.tickets.save(ticket)
        self._wake(ticket, human=True)

    def stop(self, ticket_id: str, trigger_id: str) -> None:
        ticket, trigger = self.ticket(ticket_id), self.r.triggers.get(trigger_id)
        if not trigger or not trigger.human or trigger.actor_id != ticket.mandate.actor_id:
            raise CoordinationError("EXPLICIT_STOP_REQUIRED")
        participating = any(item.trigger_id == trigger_id and not item.context_only for item in ticket.inputs)
        admission = gate(self.config, trigger, participating=participating)
        if not admission.admitted or admission.strength != "explicit":
            raise CoordinationError("EXPLICIT_STOP_REQUIRED")
        ticket.ended_at, ticket.ended_by, ticket.outcome = self.now(), "human", "stopped"
        if ticket.assignments:
            assignment = ticket.assignments[-1]
            assignment.ended_at, assignment.end_reason = self.now(), "human_stopped"
            if assignment.run_id:
                self.r.effects.enqueue("cancel", assignment.id, {"ticket_id": ticket.id, "run_id": assignment.run_id})
        self.r.effects.cancel("worker", ticket.id)
        self._release_untaken(ticket, stopping_trigger=trigger_id)
        self.r.tickets.save(ticket)
        self.record_notice("ticket.stopped", ticket.id, {"ticket_id": ticket.id})

    def retry(self, ticket_id: str, trigger_id: str) -> None:
        ticket, trigger = self.ticket(ticket_id), self.r.triggers.get(trigger_id)
        if ticket.delivery_error == "DELIVERY_RESULT_UNKNOWN":
            raise CoordinationError("CHANNEL_RECONCILIATION_REQUIRED")
        if ticket.ended_by == "human":
            raise CoordinationError("HUMAN_STOPPED")
        if not trigger or not trigger.human or trigger.actor_id != ticket.mandate.actor_id:
            raise CoordinationError("EXPLICIT_RETRY_REQUIRED")
        previous = ticket.assignments[-1]
        participating = any(item.trigger_id == trigger_id and not item.context_only for item in ticket.inputs)
        admission = gate(self.config, trigger, participating=participating)
        if not admission.admitted or admission.strength != "explicit" or trigger.at < previous.created_at:
            raise CoordinationError("NEW_HUMAN_RETRY_REQUIRED")
        if not ticket.delivery_error and (not previous.report or previous.report.state not in {"blocked", "failed"}):
            raise CoordinationError("NOT_RETRYABLE")
        if self.config.source_kind == "feishu" and not any(item.run_id for item in ticket.assignments):
            ack = self.r.replies.get(key("ack", ticket.origin_id))
            if ack and ack.state != "sent" and (
                ack.state != "pending" or not self.r.effects.retry_blocked_reply(ack.id, self.now())
            ):
                raise CoordinationError("FEISHU_ACK_NOT_RETRYABLE")
        previous.ended_at, previous.end_reason = self.now(), "retried"
        ticket.assignments.append(Assignment(key("assignment", ticket.id, trigger.id), self.now()))
        ticket.ended_at = ticket.ended_by = ticket.outcome = None
        ticket.delivery_error = None
        # 只重投未读输入，未知写入由 Runs 的恢复/审批机制处理，不能在这里复制写命令。
        for item in ticket.inputs:
            if not item.read_at:
                if item.delivered_to == previous.id and item.id not in ticket.undelivered:
                    ticket.undelivered.append(item.id)
                item.delivered_to = item.delivered_at = None
                item.status = "pending"
        self.r.tickets.save(ticket)
        self._wake(ticket, human=True)

    def report(self, ticket_id: str, assignment_id: str, report: Report) -> None:
        """保存当前安排的报告，合并人类问题并撤销旧提问，按通知规则唤醒主会话。

        ready 必须具备原生验收标记；若完成窗口中仍有未读人类输入，保留新安排继续处理。
        """
        ticket = self.ticket(ticket_id)
        if not ticket.assignments or ticket.assignments[-1].id != assignment_id or ticket.ended_by == "human":
            raise CoordinationError("STALE_ASSIGNMENT")
        assignment = ticket.assignments[-1]
        if assignment.ended_at:
            raise CoordinationError("ASSIGNMENT_ENDED")
        if not report.summary.strip() or len(report.summary) > 16000:
            raise CoordinationError("REPORT_INVALID")
        if report.state not in {"progress", "ready", "blocked", "failed", "dropped", "waiting"}:
            raise CoordinationError("REPORT_INVALID")
        if report.human_action and (report.state == "ready" or report.answers):
            raise CoordinationError("REPORT_QUESTION_CONFLICT")
        if report.question_trigger_id and not report.human_action:
            raise CoordinationError("QUESTION_TRIGGER_WITHOUT_QUESTION")
        question_from_peer = False
        if report.question_trigger_id or report.answers:
            identity = report.question_trigger_id or report.answers
            evidence = next((item for item in ticket.inputs if item.trigger_id == identity), None)
            if not evidence or not evidence.human or evidence.context_only or not evidence.read_at:
                raise CoordinationError("HUMAN_INPUT_NOT_READ")
            question_from_peer = bool(report.question_trigger_id and evidence.peer_evidence)
        if report.state == "ready" and not report.verified:
            # Weora 的交付验收比上游更严格：Worker 自报 ready 不能跳过代码计划/测试证据。
            raise CoordinationError("COMPLETION_NOT_VERIFIED")
        assignment.report = report
        assignment.report_input_count = len(ticket.inputs)
        assignment.idle_at = None if report.state == "progress" else self.now()
        ticket.updated_at = self.now()
        previous_attention_id = ticket.attention_id
        ask_author = question_from_peer or self.peer_evidence_only(ticket)
        attention_target = ticket.origin_id if ask_author else report.question_trigger_id or ticket.origin_id
        notify_feishu_failure = self.config.source_kind == "feishu" and report.state == "failed"
        # 同一次失败对账只通知一次；重试后的新 Run 即使报同一个错，也要再告诉用户。
        failure_attention_id = (
            key("feishu-failure", assignment.id, assignment.run_id or "dispatch")
            if notify_feishu_failure else None
        )
        if report.human_action and (
            report.human_action != ticket.attention or (ask_author and attention_target != ticket.attention_trigger_id)
            or (failure_attention_id is not None and failure_attention_id != ticket.attention_id)
        ):
            ticket.attention, ticket.attention_id = (
                report.human_action,
                failure_attention_id or key(assignment.id, report.at, report.human_action),
            )
            ticket.attention_trigger_id = attention_target
        elif report.state == "ready" or (report.human_action_supplied and report.human_action is None):
            ticket.attention = ticket.attention_id = ticket.attention_trigger_id = None
        if previous_attention_id and previous_attention_id != ticket.attention_id:
            previous_reply = self.r.replies.get(key("attention", previous_attention_id))
            if previous_reply and previous_reply.state == "pending":
                previous_reply.state = "withdrawn"
                self.r.replies.save(previous_reply)
                self.r.effects.cancel("reply", previous_reply.id)
        if report.answers and report.answers not in ticket.answers:
            ticket.answers.append(report.answers)
        if report.answers:
            answered = self.r.triggers.get(report.answers)
            if answered and ticket.id not in answered.acknowledged:
                answered.acknowledged.append(ticket.id)
                self.r.triggers.save(answered)
        if report.state in {"blocked", "failed"}:
            automatic = {item.id for item in ticket.inputs if item.kind == "note" or not item.human}
            ticket.undelivered = [identity for identity in ticket.undelivered if identity not in automatic]
        follows = [item for item in ticket.follows if item.lifecycle]
        outcome = self.follows_done(ticket)
        if report.state == "dropped" or (report.state == "ready" and (not follows or outcome)):
            result = outcome or report.state
            assignment.ended_at, assignment.end_reason = self.now(), result
            ticket.ended_at, ticket.ended_by, ticket.outcome = self.now(), "executor", result
            self._release_untaken(ticket)
        self.r.tickets.save(ticket)
        if report.human_action and (ask_author or notify_feishu_failure):
            origin = self.r.triggers.get(ticket.origin_id)
            if origin and origin.authorized and ticket.attention_trigger_id == origin.id:
                # 向原发起人的消息请求确认，不能把补充人当作批准人。
                self.reply(origin.id, "attention", report.human_action, ticket_id=ticket.id)
        self.record_notice(
            "ticket.report",
            key(assignment.id, report.at),
            {"ticket_id": ticket.id, "state": report.state},
            quiet=report.state == "progress" and not report.human_action,
        )

    def _release_untaken(self, ticket: Ticket, *, stopping_trigger: str | None = None) -> None:
        """结束时释放未接住的输入；仅把无其他所有者、未确认且未决定的人类消息送回评估。"""
        until = max(
            [assignment.idle_at or "" for assignment in ticket.assignments]
            + [assignment.report.at for assignment in ticket.assignments if assignment.report],
            default="",
        )
        waiting = set(ticket.undelivered)
        ticket.undelivered.clear()
        for item in ticket.inputs:
            if item.peer_evidence:
                trigger = self.r.triggers.get(item.trigger_id) if item.trigger_id else None
                if (
                    ticket.ended_by != "human" and trigger and trigger.authorized and ticket.id not in trigger.handbacks
                    and not item.read_at and (item.id in waiting or item.delivered_to)
                ):
                    # 已投递但未进入检查点的证据也需要随下一轮重投。
                    item.delivered_to = item.delivered_at = None
                    item.status = "pending"
                    ticket.undelivered.append(item.id)
                continue
            if not item.trigger_id or (item.id not in waiting and item.read_at and item.read_at <= until):
                continue
            trigger = self.r.triggers.get(item.trigger_id)
            if not trigger:
                continue
            trigger.fanout = [identity for identity in trigger.fanout if identity != ticket.id]
            self.r.triggers.save(trigger)
            decision = self.r.decisions.get(trigger.decision_id) if trigger.decision_id else None
            settled = decision and decision.kind not in {"stop", "unavailable"}
            if (
                trigger.human
                and trigger.authorized
                and not item.context_only
                and ticket.id not in trigger.handbacks
                and not trigger.acknowledged
                and not settled
                and trigger.id != stopping_trigger
                and not self.owners(trigger)
            ):
                self._queue(trigger)

    def mark_input(self, ticket_id: str, assignment_id: str, input_id: str, *, read: bool = False) -> None:
        """记录当前安排的投递或已读回执；禁止把旧安排的输入重新记账到新安排。"""
        ticket = self.ticket(ticket_id)
        if not ticket.assignments or ticket.assignments[-1].id != assignment_id or ticket.ended_by == "human":
            raise CoordinationError("STALE_ASSIGNMENT")
        item = next((item for item in ticket.inputs if item.id == input_id), None)
        if not item:
            raise CoordinationError("INPUT_NOT_FOUND")
        if item.delivered_to and item.delivered_to != assignment_id:
            raise CoordinationError("STALE_ASSIGNMENT")
        item.delivered_to, item.delivered_at = assignment_id, item.delivered_at or self.now()
        item.status = "read" if read else "delivered"
        if read:
            item.read_at = item.read_at or self.now()
        ticket.undelivered = [identity for identity in ticket.undelivered if identity != input_id]
        self.r.tickets.save(ticket)

    def reply(
        self, trigger_id: str, purpose: str, text: str, *,
        ticket_id: str | None = None, artifact_ids: tuple[str, ...] = (),
    ) -> Reply:
        """校验回复目的、受众与任务状态后幂等创建 Outbox；不在领域事务中调用飞书。

        结果须对应已验收安排，提问须匹配当前问题，回答权限只消费一次；相同键重试保留原文。
        """
        trigger = self.r.triggers.get(trigger_id)
        if not trigger or not trigger.authorized or not text.strip() or len(text) > 16000:
            raise CoordinationError("REPLY_INVALID")
        if artifact_ids and (purpose != "result" or len(artifact_ids) > 2):
            raise CoordinationError("REPLY_INVALID")
        if self.config.source_kind == "feishu" and purpose in {"ack", "result", "answer"} and len(text) > 240:
            raise CoordinationError("REPLY_TOO_LONG")
        try:
            artifact_ids = tuple(str(UUID(value)) for value in artifact_ids)
        except ValueError:
            raise CoordinationError("REPLY_INVALID") from None
        if trigger.origin.get("source_kind") == "code_hosting" and trigger.origin.get("kind") == "ci":
            raise CoordinationError("REPLY_UNAVAILABLE")
        ticket = (
            self.ticket(ticket_id)
            if ticket_id
            else next(
                (
                    self.ticket(identity)
                    for identity in trigger.fanout
                    if identity not in trigger.context_only and identity not in trigger.handbacks
                ),
                None,
            )
        )
        if purpose == "result" and ticket:
            origin = self.r.triggers.get(ticket.origin_id)
            if not origin or not origin.authorized:
                raise CoordinationError("REPLY_TARGET_MISMATCH")
            trigger, trigger_id = origin, origin.id
        if ticket and (
            trigger_id not in ticket.origin_ids and not any(item.trigger_id == trigger_id for item in ticket.inputs)
        ):
            raise CoordinationError("REPLY_TARGET_MISMATCH")
        if purpose == "ack":
            if (
                not ticket
                or not trigger.human
                or ticket.id in trigger.context_only
                or ticket.id not in trigger.fanout
                or ticket.ended_at
            ):
                raise CoordinationError("ACK_NOT_REACHED")
            identity = key("ack", trigger_id)
        elif purpose == "result":
            if not ticket or ticket.outcome not in {"ready", "dropped", "merged", "closed"}:
                raise CoordinationError("RESULT_NOT_READY")
            identity = key("result", ticket.id, ticket.assignments[-1].id)
        elif purpose == "attention":
            if (
                not ticket
                or not ticket.attention
                or text != ticket.attention
                or trigger_id != ticket.attention_trigger_id
            ):
                raise CoordinationError("ATTENTION_CHANGED")
            identity = key("attention", ticket.attention_id or "")
        elif purpose == "answer":
            identity = key("answer", trigger_id, text)
            if not trigger.human or (
                ticket and trigger_id not in ticket.answers and not self.r.replies.exists(identity)
            ):
                raise CoordinationError("ANSWER_NOT_REPORTED")
        else:
            raise CoordinationError("REPLY_PURPOSE_INVALID")
        previous = self.r.replies.get(identity)
        if previous:
            return previous
        reply = Reply(
            identity, trigger_id, purpose, text, self.now(), ticket.id if ticket else None,
            artifact_ids=list(artifact_ids),
        )
        self.r.replies.save(reply)
        if purpose == "answer" and ticket and trigger_id in ticket.answers:
            ticket.answers.remove(trigger_id)
            self.r.tickets.save(ticket)
        if purpose == "ack" and ticket and ticket.id not in trigger.acknowledged:
            trigger.acknowledged.append(ticket.id)
            self.r.triggers.save(trigger)
        # 首回优先派发；Worker 还须在派发事务中核对飞书回执，队列优先级本身不保证顺序。
        first_reply = (
            self.config.source_kind == "feishu"
            and purpose == "ack"
            and ticket is not None
            and ticket.origin_id == trigger_id
        )
        self.r.effects.enqueue("reply", identity, {"reply_id": identity}, priority=2 if first_reply else 0)
        return reply
