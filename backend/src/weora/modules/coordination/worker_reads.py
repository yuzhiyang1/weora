"""受管 Worker 的有界事实读取；长消息通过本任务内引用分段读取，不能改用任意来源 ID。"""

from dataclasses import asdict

from weora.modules.coordination.domain import CoordinationError, Json, State
from weora.modules.coordination.records import records


def worker_facts(state: State, ticket_id: str, arguments: Json) -> Json:
    """模型历史不充当读取授权；调用前由会话复核 ACL，引用在此重新限定到本 Ticket。"""
    ticket = records(state).tickets.get(ticket_id)
    if not ticket:
        raise CoordinationError("TICKET_NOT_FOUND")
    resource, offset = str(arguments.get("resource", "ticket")), int(str(arguments.get("offset", 0)))
    identity = str(arguments.get("id", ""))
    if resource in {"input", "root"}:
        item = next((item for item in ticket.inputs if item.id == identity or item.trigger_id == identity), None)
        trigger = records(state).triggers.get(item.trigger_id) if item and item.trigger_id else None
        if resource == "root":
            trigger = records(state).triggers.get(ticket.origin_id)
        if (resource != "root" and not item) or (trigger and not trigger.authorized):
            raise CoordinationError("EVIDENCE_NOT_DELIVERED")
        text = (
            str(trigger.origin.get("root_text", ""))
            if resource == "root" and trigger
            else (trigger.text if trigger else (item.text or "") if item else "")
        )
        return {
            "ok": True,
            "id": identity or ticket.origin_id,
            "peer_evidence": bool(resource == "input" and item and item.peer_evidence),
            "text": text[offset : offset + 4000],
            "chars": len(text),
            "next_offset": offset + 4000 if len(text) > offset + 4000 else None,
        }
    if resource != "ticket":
        raise CoordinationError("EVIDENCE_NOT_DELIVERED")
    inputs: list[Json] = []
    for item in ticket.inputs[offset : offset + 8]:
        trigger = records(state).triggers.get(item.trigger_id) if item.trigger_id else None
        if trigger and not trigger.authorized:
            continue
        text = trigger.text if trigger else item.text or ""
        inputs.append(
            {
                "id": item.id,
                "trigger_id": item.trigger_id,
                "kind": item.kind,
                "human": item.human,
                "context_only": item.context_only,
                "peer_evidence": item.peer_evidence,
                "text": text[:1200],
                "chars": len(text),
                "next_offset": 1200 if len(text) > 1200 else None,
                "read_at": item.read_at,
            }
        )
    assignment = asdict(ticket.assignments[-1]) if ticket.assignments else None
    if assignment and isinstance(assignment.get("report"), dict):
        assignment["report"] = dict(assignment["report"], summary=str(assignment["report"].get("summary", ""))[:4000])
    return {
        "ok": True,
        "ticket": {
            "id": ticket.id,
            "title": ticket.title,
            "mandate": asdict(ticket.mandate),
            "task_id": ticket.task_id,
            "outcome": ticket.outcome,
            "attention": ticket.attention,
            "assignment": assignment,
            "follows": [asdict(item) for item in ticket.follows[:50]],
        },
        "inputs": inputs,
        "next_offset": offset + 8 if len(ticket.inputs) > offset + 8 else None,
    }
