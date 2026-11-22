"""在原生计划工具后发送一次召回提醒，团队策略由平台维护。"""

import argparse
import hashlib
import json
import time
from pathlib import Path
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_settings import SessionSettings, session_state_root

_API = "/settings/plan-reminder"
_DAY_SECONDS = 24 * 60 * 60
_ZH = (
    "任务已规划。需要团队知识时，先单独调用 weora-recall 检索一次，再继续执行。"
    "用户已提供充分上下文或任务只是简单局部修改时，可以跳过。"
)
_EN = (
    "Task plan detected. If team knowledge would help, invoke weora-recall once before proceeding. "
    "Skip this for trivial local changes or when the user already supplied the relevant context."
)


def observe(settings: SessionSettings, runtime: str, profile: str, session: UUID) -> str:
    """仅在自动召回与该提醒都开启时领取本地 24 小时提示。"""
    if settings.effective("recall").get("enabled") is not True:
        return ""
    policy = call(settings.client, None, _API)
    if policy.get("enabled") is not True:
        return ""
    custom = policy.get("message")
    language = policy.get("language")
    message = custom if isinstance(custom, str) else {"zh": _ZH, "en": _EN}.get(
        language if isinstance(language, str) else "", _ZH + "\n\n" + _EN,
    )
    # 会话与设备凭据分别散列。本机记录只保存提示时间。
    key = hashlib.sha256(f"{runtime}/{profile}/{session}".encode()).hexdigest()
    path = session_state_root(settings.client) / "plan-reminder" / f"{key}.json"
    with lock_file(path):
        now = int(time.time())
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            previous = state.get("hinted_at") if isinstance(state, dict) else None
        except (FileNotFoundError, ValueError):
            previous = None
        if type(previous) is int and 0 <= now - previous <= _DAY_SECONDS:
            return ""
        write_json(path, {"hinted_at": now})
    return message


def configure(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    """工作空间管理员通过 API 保存开关、语言和自定义正文。"""
    if args.mode == "status":
        if args.language or args.default_message or args.message_file:
            raise CollabError("KNOWLEDGE_PLAN_REMINDER_OPTIONS", "请用 configure 修改语言或文案。")
        return call(client, None, _API)
    changes: dict[str, JsonValue] = {}
    if args.language:
        changes["language"] = None if args.language == "default" else args.language
    if args.default_message:
        changes["message"] = None
    elif args.message_file:
        try:
            path: Path = args.message_file
            with path.open(encoding="utf-8-sig") as stream:
                message = stream.read(4001)
        except (OSError, UnicodeError) as error:
            raise CollabError("KNOWLEDGE_PLAN_REMINDER_MESSAGE", "无法读取 UTF-8 提醒正文。") from error
        if len(message) > 4000 or not message.strip():
            raise CollabError("KNOWLEDGE_PLAN_REMINDER_MESSAGE", "提醒正文须包含 1 到 4000 个字符。")
        changes["message"] = message.strip()
    if args.mode in ("enable", "disable"):
        changes["enabled"] = args.mode == "enable"
    if not changes:
        raise CollabError("KNOWLEDGE_PLAN_REMINDER_OPTIONS", "请指定要更新的语言或文案。")
    return call(client, None, _API, method="PATCH", payload=changes)
