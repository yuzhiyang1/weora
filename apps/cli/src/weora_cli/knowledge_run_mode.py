"""运行模式由原生事件或启动环境确定。"""

import os
from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from weora_cli.commands import JsonValue

RunMode = Literal["attended", "unattended"]
TASK_ENV = "WEORA_UNATTENDED_TASK_ID"


def configured_mode() -> RunMode:
    configured = os.environ.get("WEORA_RUN_MODE")
    if configured == "attended":
        return "attended"
    if configured == "unattended":
        return "unattended"
    ci = os.environ.get("CI", "").lower()
    return "unattended" if ci not in ("", "0", "false", "no") else "attended"


def task_identifier() -> str | None:
    value = os.environ.get(TASK_ENV)
    return str(UUID(value)) if value else None


def is_unattended(runtime: str, event: Mapping[str, JsonValue]) -> bool:
    return (
        configured_mode() == "unattended" or event.get("is_headless") is True
        or runtime == "claude-code" and os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith(("sdk", "headless"))
    )
