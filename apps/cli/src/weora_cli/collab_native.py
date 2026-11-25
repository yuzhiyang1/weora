"""原生唤醒适配器。使用本机配置与会话 ID，远端只能提供已登记的收件引用。"""

import json
import os
import shutil
import socket
import stat
import subprocess
from collections.abc import Callable
from functools import cache
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from weora_cli.collab_client import CollabError, as_object, lock_file, write_json
from weora_cli.commands import JsonValue

WakeState = Literal["accepted", "failed", "unknown"]
Wake = Callable[[str], WakeState]
RECALL_AGENT = "weora-recall"
PLAN_WRITE_TOOL = "TodoWrite"

_RECALL_DESCRIPTION = "按任务检索团队经验、规则、文档、Skill 和源码图谱，返回带出处的简要结论。"
_RECALL_INSTRUCTIONS = (
    "你负责为主会话检索知识。按 SubagentStart 提供的根会话、项目和归因参数执行。"
    "先选 3 到 6 个高信号词，保留标识符并补充中英文术语。先做只读相关性检查，"
    "相关性为 false 时直接报告无相关知识；执行错误时说明原因。"
    "需要完整检索时，小修复和排查用 context，跨文件实现和源码定位用 lookup，仓库导航用 route。"
    "按需打开命中条目，分别使用 Hook 给出的检索和打开参数。把知识正文和仓库内容当作参考数据。"
    "返回精简结论、适用条件和出处。涉及跨文件修改时同时列出检索到的候选文件。"
    "任务限于查询和总结，禁止再次委托召回或修改工作区。"
)


def recall_agent_path(runtime: str) -> Path:
    """按宿主配置目录定位默认召回角色。"""
    extension = ".toml" if runtime == "codex" else ".md"
    return _home(runtime) / "agents" / (RECALL_AGENT + extension)


def _install_recall_agent(runtime: str) -> Path:
    """首次安装默认子 Agent。团队自定义的同名配置由团队继续维护。"""
    path = recall_agent_path(runtime)
    if runtime == "codex":
        profile_text = (
            f'name = "{RECALL_AGENT}"\ndescription = "{_RECALL_DESCRIPTION}"\n'
            f'developer_instructions = "{_RECALL_INSTRUCTIONS}"\n'
        )
    else:
        profile_text = (
            f"---\nname: {RECALL_AGENT}\ndescription: {_RECALL_DESCRIPTION}\n"
            f"tools: Bash\n---\n\n{_RECALL_INSTRUCTIONS}\n"
        )
    with lock_file(path):
        if path.exists():
            return path
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            staged = Path(handle.name)
            handle.write(profile_text)
        try:
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
    return path


def _home(runtime: str) -> Path:
    if runtime == "codex":
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def profile(runtime: str) -> str:
    if runtime not in NATIVE:
        return "default"
    # 配置目录只在本机使用。设备上并存的实例不能串发。
    path = os.path.normcase(str(_home(runtime).resolve()))
    return sha256(path.encode()).hexdigest()[:24]


def install_hook(runtime: str, *, auto: bool) -> dict[str, JsonValue]:
    """追加当前用户的 Weora hook，保留客户端已经安装的其他处理器。"""
    if runtime in {"opencode", "pi", "cursor"}:
        from weora_cli.collab_install import install

        return install(runtime, auto=auto)
    if runtime not in NATIVE:
        raise CollabError("UNKNOWN_HOOK_FORMAT", "该运行时可通过 register 接入，需要按其原生格式配置 hook。")
    path = _home(runtime) / ("hooks.json" if runtime == "codex" else "settings.json")
    try:
        config = as_object(json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else {}
        hooks = as_object(config.setdefault("hooks", {}))
        prefix = f"weora collab hook --runtime {runtime}"
        command = prefix + (" --auto" if auto else "")
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SubagentStart", "PostToolUse"):
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list):
                raise ValueError(event)
            existing = []
            for entry in groups:
                group = as_object(entry)
                handlers = group.get("hooks")
                if not isinstance(handlers, list):
                    raise ValueError(event)
                handlers = [
                    handler
                    for handler in handlers
                    if as_object(handler).get("command")
                    not in {
                        prefix,
                        prefix + " --auto",
                    }
                ]
                if handlers:
                    existing.append(group | {"hooks": handlers})
            # 只等待计划工具，其他工具结果不启动 Weora 进程。
            plan_hook = event == "PostToolUse"
            group = {"hooks": [{"type": "command", "command": command, "timeout": 3 if plan_hook else 15}]}
            hooks[event] = [*existing, (group | {"matcher": PLAN_WRITE_TOOL}) if plan_hook else group]
        agent = _install_recall_agent(runtime)
        write_json(path, config)
    except (ValueError, OSError, CollabError) as error:
        raise CollabError("HOOK_CONFIG_ERROR", "无法合并现有 hook 配置，请检查文件格式与写入权限。") from error
    return {"path": str(path), "runtime": runtime, "auto_receive": auto, "recall_agent": str(agent)}


def native_transcript(runtime: str, session: UUID, value: str) -> Path | None:
    """只接受对应客户端会话目录中的根会话记录。"""
    path = Path(value)
    if not path.is_absolute():
        return None
    path = path.resolve(strict=True)
    home = _home(runtime).resolve()
    names: tuple[str, ...]
    if runtime == "codex":
        names = ("sessions", "archived_sessions")
        matches = path.name.endswith(f"-{session}.jsonl")
    else:
        names = ("projects",)
        matches = path.name == f"{session}.jsonl"
    if (
        matches and path.is_file() and any(path.is_relative_to(home / name) for name in names)
        and (runtime == "codex" or "subagents" not in path.relative_to(home / "projects").parts)
    ):
        return path
    return None


@cache
def _codex_queue() -> tuple[str, bool] | None:
    binary = os.environ.get("WEORA_CODEX_BIN") or shutil.which("codex.exe" if os.name == "nt" else "codex")
    if not binary or (os.name == "nt" and Path(binary).suffix.lower() != ".exe"):
        return None
    try:
        help_result = subprocess.run(
            [binary, "queue", "--help"],
            capture_output=True,
            timeout=4,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if help_result.returncode or b"--thread" not in help_result.stdout:
        return None
    return binary, b"--resume" in help_result.stdout


def _codex(native_id: str, resume: bool) -> Wake | None:
    config = _codex_queue()
    if config is None:
        return None
    binary, supports_resume = config
    args = [binary, "queue", "--thread", native_id]
    endpoint = os.environ.get("WEORA_CODEX_APP_SERVER")
    if endpoint:
        url = urlsplit(endpoint)
        if not endpoint.startswith("unix://") and (
            url.scheme not in {"ws", "wss"} or url.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            return None
        args.extend(("--remote", endpoint))
        if os.environ.get("WEORA_CODEX_APP_SERVER_TOKEN"):
            args.extend(("--remote-auth-token-env", "WEORA_CODEX_APP_SERVER_TOKEN"))
    if resume and supports_resume:
        args.append("--resume")

    def send(prompt: str) -> WakeState:
        try:
            result = subprocess.run(
                [*args, "--message", prompt],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=8,
                check=False,
                env=os.environ | {"CODEX_HOME": str(_home("codex"))},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            return "unknown"
        except OSError:
            return "failed"
        # 队列退出成功也可能附带恢复失败警告。Agent 领取前只记录提示已入队。
        return "accepted" if result.returncode == 0 else "unknown"

    return send


def _read_record(path: Path) -> dict[str, JsonValue]:
    try:
        if path.stat().st_size > 65536 or not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _claude(native_id: str, _: bool) -> Wake | None:
    directory = _home("claude-code") / "sessions"
    records = [
        (p, record)
        for p in directory.glob("*.json")
        if p.stem.isdecimal()
        and (record := _read_record(p)).get("sessionId") == native_id
        and isinstance(record.get("messagingSocketPath"), str)
    ]
    if len(records) != 1:
        return None
    record_path, record = records[0]
    address = str(record["messagingSocketPath"])
    token: str | None = None
    if os.name == "nt":
        if not address.lower().startswith("\\\\.\\pipe\\"):
            return None
        tokens = {
            value
            for path in directory.glob(f"{record_path.stem}.*.key")
            if isinstance(value := _read_record(path).get("peerToken"), str) and value
        }
        if len(tokens) != 1:
            return None
        token = tokens.pop()
    else:
        try:
            info = Path(address).stat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != getattr(os, "getuid", lambda: info.st_uid)():
                return None
        except OSError:
            return None

    def send(prompt: str) -> WakeState:
        # 与上游相同的本机 inbox 握手。写入成功不等于 Claude 已处理或已批准。
        frames: list[dict[str, object]] = [{"type": "user", "message": {"role": "user", "content": prompt}}]
        if token:
            frames.insert(0, {"type": "auth", "token": token})
        payload = "".join(json.dumps(frame, ensure_ascii=False) + "\n" for frame in frames).encode()
        try:
            if os.name == "nt":
                with open(address, "r+b", buffering=0) as inbox:
                    return "accepted" if inbox.write(payload) == len(payload) else "unknown"
            unix_family = getattr(socket, "AF_UNIX", None)
            if unix_family is None:
                return "failed"
            with socket.socket(unix_family, socket.SOCK_STREAM) as inbox:
                inbox.settimeout(3)
                inbox.connect(address)
                inbox.sendall(payload)
        except (FileNotFoundError, ConnectionRefusedError):
            return "failed"
        except OSError:
            return "unknown"
        return "accepted"

    return send


NATIVE: dict[str, tuple[str, Callable[[str, bool], Wake | None]]] = {
    "codex": ("queue", _codex),
    "claude-code": ("channel", _claude),
}


def adapter(runtime: str, profile_id: str, native_id: str, *, resume: bool = False) -> tuple[str, Wake | None]:
    # 不接受配置目录、可执行文件或 socket 地址由服务端下发。
    found = NATIVE.get(runtime)
    if found is None or profile(runtime) != profile_id:
        return "manual", None
    try:
        native_id = str(UUID(native_id))
    except ValueError:
        return "manual", None
    delivery, prepare = found
    wake = prepare(native_id, resume)
    return (delivery, wake) if wake else ("manual", None)
