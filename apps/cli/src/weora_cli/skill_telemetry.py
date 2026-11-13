"""Adapted from TeamAI src/skill-use.ts at 1104f305. See TEAMAI-LICENSE.txt."""

import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection, Error, Row
from uuid import uuid4

from weora_cli.collab_client import CollabError, as_object, read_config
from weora_cli.commands import JsonValue
from weora_cli.skill_adapters import skill_roots, tool_home
from weora_cli.skill_local import database, digest, inventory, now, queue
from weora_cli.skills import client_for, flush, is_current_binding, pull

# TeamAI 的各工具 Skill 调用声明，外加显式路径读取的共用分支。
_SKILL_TOOLS = {
    "claude": {"Skill"},
    "copilot": {"skill"},
    "qoder": {"Skill"},
    "qoder-cn": {"Skill"},
    "zcode": {"Skill"},
    "codebuddy": {"Skill", "skill", "use_skill"},
    "dsh": {"skill"},
    "workbuddy": {"Skill", "skill"},
    "opencode": {"skill"},
    "muse": {"read_skill"},
    "hermes": {"skill_view"},
    "trae": {"Skill"},
    "trae-cn": {"Skill"},
}
_PROMPT_FORMS = {
    "codex": r"(?<![\w$])\$([a-z0-9][a-z0-9-]*)\b",
    "pi": r'^(?:/skill:|<skill name=")([a-z0-9][a-z0-9-]*)',
    "codebuddy": r"^(?:/|@command://)([a-z0-9][a-z0-9-]*)(?=\s|$)",
    "muse": r"(?<![\w$])\$([a-z0-9][a-z0-9-]*)\b",
}
_DEFAULT_PROMPT = r"^/([a-z0-9][a-z0-9-]*)(?=\s|$)"
_READ_TOOLS = {"read", "read_file", "readfile", "view_file", "fs_read"}
_SHELL_TOOLS = {
    "bash",
    "shell",
    "exec_command",
    "powershell",
    "terminal",
    "run_terminal_cmd",
    "runcommand",
    "execute_bash",
    "execute_pwsh",
}
_FAILED = {"failure", "failed", "error"}


def _field(payload: dict[str, JsonValue], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bindings(db: Connection, runtime: str, cwd: Path) -> list[Row]:
    config = read_config()
    return [
        row
        for row in db.execute(
            "SELECT * FROM binding WHERE runtime=? AND profile=? ORDER BY length(root) DESC",
            (runtime, str(tool_home(runtime))),
        )
        if is_current_binding(row, config) and (row["scope"] == "user" or cwd.is_relative_to(Path(row["root"])))
    ]


def _tool_candidate(runtime: str, payload: dict[str, JsonValue], cwd: Path) -> tuple[str, Path | None]:
    raw_tool = _field(payload, "tool_name", "toolName", "tool")
    if _field(payload, "tool_status", "status").lower() in _FAILED or payload.get("isError"):
        return "", None
    value = (
        payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("toolArgs")
        or payload.get("params")
        or payload.get("arguments")
    )
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {"skill": value}
    if not isinstance(value, dict):
        return "", None
    if raw_tool in _SKILL_TOOLS.get(runtime, set()):
        return _field(value, "skill", "name", "id", "skill_name", "skillName", "command"), None
    path = _field(value, "file_path", "filePath", "path", "file")
    if path.startswith("skill://") and runtime == "omp" and raw_tool.lower() == "read":
        return path.removeprefix("skill://").rstrip("/"), None
    if raw_tool.lower() in _SHELL_TOOLS:
        command = _field(value, "command", "cmd")
        try:
            parts = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            return "", None
        if len(parts) < 2 or parts[0].lower() not in {"cat", "type", "get-content"}:
            return "", None
        # 只接受可确定为单文件读取的命令，复杂 shell 语句留作采集缺口。
        args = [p.strip("\"'") for p in parts[1:] if p.lower() not in {"-raw", "-literalpath", "--"}]
        if len(args) != 1 or any(ch in args[0] for ch in ";|&<>"):
            return "", None
        path = args[0]
    elif raw_tool.lower() not in _READ_TOOLS:
        return "", None
    if not path:
        return "", None
    file = (cwd / Path(os.path.expandvars(path)).expanduser()).resolve()
    return (file.parent.name, file) if file.name == "SKILL.md" else ("", None)


def _texts(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    if isinstance(value, dict):
        return [text for key in ("stdout", "content", "text", "output", "result") for text in _texts(value.get(key))]
    return []


def _verified_output(runtime: str, payload: dict[str, JsonValue], file: Path) -> bool:
    response = (
        payload.get("tool_response")
        or payload.get("toolResponse")
        or payload.get("toolResult")
        or payload.get("tool_result")
        or payload.get("tool_output")
        or payload.get("result")
    )
    if isinstance(response, dict):
        if response.get("isError") or response.get("is_error") or response.get("error"):
            return False
        if response.get("success") is False or _field(response, "status").lower() in _FAILED:
            return False
        name = _field(payload, "tool_name", "toolName", "tool")
        # 原生 Skill 调用可返回明确的成功状态而不重复返回正文。普通 shell 仍须验证输出。
        if name in _SKILL_TOOLS.get(runtime, set()) and (
            response.get("success") is True or _field(response, "status").lower() in {"success", "ok"}
        ):
            return True
    expected = file.read_text(encoding="utf-8").strip()
    return bool(expected) and any(expected in text.replace("\r\n", "\n") for text in _texts(response))


def observe(db: Connection, runtime: str, event: str, payload: dict[str, JsonValue], cwd: Path) -> None:
    bindings = _bindings(db, runtime, cwd)
    if not bindings:
        return
    session = _field(payload, "session_id", "sessionId", "session_key", "conversation_id", "conversationId")
    if not session:
        return
    profile = digest(str(tool_home(runtime)).encode())
    secret = bytes.fromhex(db.execute("SELECT value FROM secret WHERE id=1").fetchone()[0])

    def keyed(value: str) -> str:
        return hmac.new(secret, f"{runtime}:{profile}:{value}".encode(), hashlib.sha256).hexdigest()

    session_key = keyed(session)
    turn = _field(payload, "turn_id", "turnId", "prompt_id", "generation_id", "generationId")
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not turn and event == "prompt":
            turn = uuid4().hex
            db.execute("INSERT OR REPLACE INTO turn VALUES (?, ?)", (session_key, turn))
        elif not turn:
            row = db.execute("SELECT id FROM turn WHERE session=?", (session_key,)).fetchone()
            turn = row[0] if row else ""
        if not turn:
            return
        candidates: set[tuple[str, Path | None]]
        if event == "prompt":
            prompt = _field(payload, "prompt", "message", "text")
            if not prompt and runtime == "kiro":
                # Kiro 的 Prompt Submit 可从环境变量提供正文，正文只在本机匹配。
                prompt = os.environ.get("USER_PROMPT", "")
            candidates = {(name, None) for name in re.findall(_PROMPT_FORMS.get(runtime, _DEFAULT_PROMPT), prompt)}
        else:
            candidates = {_tool_candidate(runtime, payload, cwd)}
        for name, file in candidates:
            if not name:
                continue
            _attribute(db, bindings, runtime, payload, cwd, event, name, file, profile, session_key, keyed(turn))


def _attribute(
    db: Connection,
    bindings: list[Row],
    runtime: str,
    payload: dict[str, JsonValue],
    cwd: Path,
    event: str,
    name: str,
    file: Path | None,
    profile: str,
    session: str,
    turn: str,
) -> None:
    candidates = []
    # 用户范围的 Skill 也可能被当前检出的项目副本遮蔽。
    checkout = next((level for level in (cwd, *cwd.parents) if (level / ".git").exists()), None)
    for binding in bindings:
        installation = db.execute(
            "SELECT * FROM installation WHERE binding=? AND slug=?",
            (binding["id"], name),
        ).fetchone()
        if installation is None or not binding["usage"]:
            continue
        target = Path(installation["target"])
        manifest = target / "SKILL.md"
        if file is not None and file != manifest.resolve():
            continue
        if file is None:
            root = Path(binding["root"]) if binding["root"] else checkout
            visible = {p / name / "SKILL.md" for p in skill_roots(runtime, cwd, root)}
            if any(p.resolve() != manifest.resolve() and p.is_file() for p in visible):
                continue
        if not manifest.is_file() or inventory(target) != json.loads(installation["files"]):
            continue
        if event == "tool" and not _verified_output(runtime, payload, manifest):
            continue
        candidates.append((binding, installation))
    # 同一物理副本可由项目和用户绑定共同管理，取最具体的归属。
    if not candidates or len({(r["target"], r["version_id"]) for _, r in candidates}) != 1:
        return
    binding, installation = candidates[0]
    queue(
        db,
        binding["id"],
        "usage",
        {
            "installation_id": installation["generation"],
            "runtime": runtime,
            "profile_id": profile,
            "session_key": session,
            "turn_key": turn,
            "kind": "mentioned" if event == "prompt" else "loaded",
            "occurred_at": now(),
        },
    )


def _detached(runtime: str, cwd: Path, event: str) -> None:
    command = [sys.executable, "-m", "weora_cli", "skill", "hook", "--runtime", runtime, "--event", event]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **options,
    )


def hook(runtime: str, event: str) -> None:
    """宿主 Hook 失败时保持会话可用，本机队列可由手动命令补传。"""
    try:
        if event in {"sync", "drain"}:
            with database() as db:
                for binding in _bindings(db, runtime, Path.cwd().resolve()):
                    try:
                        client = client_for(binding)
                        previous = binding["checked"]
                        if event == "drain" or (
                            previous and datetime.now(UTC) - datetime.fromisoformat(previous) < timedelta(seconds=60)
                        ):
                            flush(db, binding, client)
                        else:
                            pull(db, binding, client)
                    except (CollabError, Error, OSError):
                        continue
            return
        payload = as_object(json.loads(sys.stdin.read(4 * 1024**2 + 1) or "{}"))
        cwd = Path(_field(payload, "cwd", "workspace_root", "workspaceRoot") or Path.cwd()).resolve()
        if event in {"start", "stop"}:
            _detached(runtime, cwd, "sync" if event == "start" else "drain")
            return
        with database() as db:
            observe(db, runtime, event, payload, cwd)
    except (CollabError, Error, OSError, ValueError):
        return
