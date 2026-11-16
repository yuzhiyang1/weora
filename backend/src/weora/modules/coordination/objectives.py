"""用户目标引用与模型路由摘要分开，来源保持在已授权的协调记录中。"""

from weora.modules.coordination.domain import Input, Repository, Ticket, Trigger


def objective_inputs(ticket: Ticket, triggers: Repository[Trigger]) -> list[tuple[Input, Trigger]]:
    """原发起人的任务及纠偏构成目标；同伴证据和自动备注不能新增要求。"""
    result: list[tuple[Input, Trigger]] = []
    for item in ticket.inputs:
        trigger = triggers.get(item.trigger_id) if item.trigger_id else None
        if (
            trigger is not None
            and trigger.authorized
            and trigger.actor_id == ticket.mandate.actor_id
            and not item.context_only
            and not item.peer_evidence
            and item.control_action != "stop"
            and (item.human or trigger.id in ticket.origin_ids)
        ):
            result.append((item, trigger))
    return result


def record_body_read(item: Input, start: int, end: int) -> None:
    """合并真正返回给会话的正文区间；只读最后一页不能冒充全文已读。"""
    if start < 0 or end <= start:
        return
    merged: list[tuple[int, int]] = []
    for left, right in sorted([*item.body_read_ranges, (start, end)]):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    item.body_read_ranges = merged


def fully_read(item: Input, trigger: Trigger) -> bool:
    """读取回执与全文覆盖分开；原文后部要求必须实际进入过会话。"""
    return not trigger.text or bool(
        item.body_read_ranges and item.body_read_ranges[0][0] == 0
        and item.body_read_ranges[0][1] >= len(trigger.text)
    )
