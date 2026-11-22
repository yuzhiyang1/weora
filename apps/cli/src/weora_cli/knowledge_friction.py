"""从宿主的本地会话记录提取分享提醒需要的计数。"""

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from weora_cli.commands import JsonValue
from weora_cli.knowledge_native_usage import NativeUsage

_MAX_BYTES = 50 * 1024 * 1024
_MAX_LINE = 2 * 1024 * 1024
_INTERRUPTED = "[Request interrupted by user"
_CORRECTION = re.compile(
    r"(?<!\w)(?:wrong|redo|undo|revert|mistake|instead|don't|that's not|not what)(?!\w)"
    r"|不对|不是|错了|错误|重来|重新|撤销|回退|别这样|不要"
    r"|違う|ちがう|そうじゃな|そうではな|やり直|やりなおし|間違って|間違え|勝手に|戻して",
    re.IGNORECASE,
)
_INJECTED = re.compile(r"<task-notification>|<system-reminder>|\[Request interrupted by user")
_REJECTION = ("the tool use was rejected", "doesn't want to proceed with this tool use")
_EXIT_CODE = re.compile(r"^(?:Process|Command) exited with code (-?\d+)\s*$", re.MULTILINE | re.IGNORECASE)
_INTERACTIVE_CODEX = {"cli", "vscode", "app-server", "appServer", "desktop"}


def correction_text(prompt: str) -> str:
    return _INJECTED.split(prompt, maxsplit=1)[0].strip()


def correction_prompt(prompt: str) -> bool:
    return bool(_CORRECTION.search(unicodedata.normalize("NFC", correction_text(prompt))))


@dataclass(slots=True)
class Friction:
    tools: dict[str, str] = field(default_factory=dict)
    rejected: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    interrupts: int = 0
    usage: NativeUsage | None = None

    def eligible(self, corrections: int, recall_bonus: int = 0) -> bool:
        if len(self.tools) < 15:
            return False
        error_points = next(
            (points for minimum, points in ((8, 25), (5, 18), (3, 10)) if len(self.failed) >= minimum), 0
        )
        names = set(self.tools.values())
        diversity = min(5, int(5 * len(names) / 10 + 0.5))
        score = 20 * (self.interrupts + len(self.rejected) + corrections) + error_points + diversity
        return score + recall_bonus + (5 if "Skill" in names else 0) >= 20

    def reason(self, corrections: int) -> str:
        observed = (
            (self.interrupts, "用户中断"),
            (len(self.rejected), "工具请求被拒绝"),
            (corrections, "用户纠正"),
            (len(self.failed), "工具执行失败"),
        )
        return "、".join(f"{label} {count} 次" for count, label in observed if count)


def _record(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, list):
        return " ".join(
            text[:4096] for part in value[:8] if isinstance(part, dict) and isinstance(text := part.get("text"), str)
        )[:4096]
    return ""


def _error(facts: Friction, call: str, result: str, *, failed: bool) -> None:
    result = result.lower()
    if "auto mode cannot determine the safety of" in result:
        return
    if any(phrase in result for phrase in _REJECTION) or result.startswith("rejected by user"):
        facts.rejected.add(call)
        facts.failed.discard(call)
    elif failed and call not in facts.rejected:
        facts.failed.add(call)


def _claude(facts: Friction, row: dict[str, JsonValue], *, unattended: bool) -> bool:
    if not unattended and row.get("entrypoint") in ("sdk", "sdk-cli", "sdk-ts", "sdk-py", "headless"):
        return True
    message = _record(row.get("message"))
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for value in content:
        part = _record(value)
        kind = part.get("type")
        if row.get("type") == "assistant" and kind == "tool_use":
            key, name = part.get("id"), part.get("name")
            if isinstance(key, str) and isinstance(name, str):
                facts.tools[key] = name
        if row.get("type") == "user":
            if kind == "text" and _text(part.get("text")).startswith(_INTERRUPTED):
                facts.interrupts += 1
            if kind == "tool_result" and part.get("is_error") is True:
                call = part.get("tool_use_id")
                if isinstance(call, str):
                    _error(facts, call, _text(part.get("content")), failed=True)
    return False


def _codex(facts: Friction, row: dict[str, JsonValue]) -> None:
    item = _record(row.get("payload"))
    kind = item.get("type")
    call = item.get("call_id")
    if row.get("type") == "event_msg":
        if kind == "turn_aborted" and item.get("reason") in ("interrupted", "user_interrupted"):
            facts.interrupts += 1
        if kind == "exec_command_end" and isinstance(call, str) and type(item.get("exit_code")) is int:
            _error(facts, call, "", failed=item["exit_code"] != 0)
        return
    if row.get("type") != "response_item" or not isinstance(call, str):
        return
    if kind in ("function_call", "custom_tool_call", "mcp_tool_call") and isinstance(name := item.get("name"), str):
        facts.tools[call] = name
    if kind in ("function_call_output", "custom_tool_call_output", "mcp_tool_call_output"):
        text = _text(item.get("output"))
        exit_code = _EXIT_CODE.search(text)
        _error(facts, call, text, failed=bool(exit_code and int(exit_code[1]) != 0) or item.get("is_error") is True)


def scan(
    path: Path, runtime: Literal["codex", "claude-code"], session: str, *,
    unattended: bool = False, collect_usage: bool = False,
) -> Friction | None:
    """扫描有界 JSONL，只在明确的无人值守退出入口接收该类宿主会话。"""
    facts = Friction(usage=NativeUsage() if collect_usage else None)
    remaining = _MAX_BYTES
    identified = False
    try:
        with path.open("rb") as stream:
            stamp = os.fstat(stream.fileno())
            if not stat.S_ISREG(stamp.st_mode) or not 0 < stamp.st_size <= remaining:
                return None
            skipping_line = False
            while remaining > 0 and (line := stream.readline(min(_MAX_LINE, remaining))):
                remaining -= len(line)
                if skipping_line or len(line) == _MAX_LINE:
                    skipping_line = not line.endswith(b"\n")
                    continue
                try:
                    row = _record(json.loads(line))
                except (ValueError, RecursionError):
                    continue
                if runtime == "claude-code":
                    if row.get("sessionId") == session and row.get("isSidechain") is not True:
                        identified = True
                        if _claude(facts, row, unattended=unattended):
                            return None
                        if facts.usage is not None:
                            facts.usage.claude(row)
                    continue
                if row.get("type") == "session_meta":
                    meta = _record(row.get("payload"))
                    source = meta.get("source")
                    allowed = _INTERACTIVE_CODEX | {"exec", "sdk"} if unattended else _INTERACTIVE_CODEX
                    if meta.get("id") != session or not isinstance(source, str) or source not in allowed:
                        return None
                    identified = True
                elif identified:
                    _codex(facts, row)
                    if facts.usage is not None:
                        facts.usage.codex(row, session, path.name.casefold())
    except OSError:
        return None
    if not identified:
        return None
    if runtime == "claude-code" and facts.usage is not None:
        facts.usage.claude_children(path)
    return facts
