"""电脑权限由私有 Run 或部署明确登记的飞书话题授予。"""

from typing import Literal

from weora.modules.collaboration.application.tasks import TaskRecord

ComputerMode = Literal["observe", "test"]


def computer_mode(task: TaskRecord, feishu_scopes: dict[str, ComputerMode]) -> ComputerMode | None:
    if task.audience == "private" and task.origin == "console":
        return "test"
    origin = task.origin_metadata or {}
    scope_id = origin.get("coordination_scope_id")
    if (
        task.audience != "group"
        or task.origin != "feishu"
        or not isinstance(scope_id, str)
        or not origin.get("coordination_ticket_id")
        or not origin.get("coordination_assignment_id")
        or origin.get("coordination_status_only") is True
    ):
        return None
    return feishu_scopes.get(scope_id)
