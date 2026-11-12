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
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from weora_cli.collab_client import CollabError, as_object, write_json
from weora_cli.commands import JsonValue

WakeState = Literal["accepted", "failed", "unknown"]
Wake = Callable[[str], WakeState]


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
    if runtime not in NATIVE:
        raise CollabError("UNKNOWN_HOOK_FORMAT", "该运行时可通过 register 接入，需要按其原生格式配置 hook。")
    path = _home(runtime) / ("hooks.json" if runtime == "codex" else "settings.json")
    try:
        config = as_object(json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else {}
        hooks = as_object(config.setdefault("hooks", {}))
        prefix = f"weora collab hook --runtime {runtime}"
        command = prefix + (" --auto" if auto else "")
        for event in ("SessionStart", "UserPromptSubmit"):
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
            hooks[event] = [*existing, {"hooks": [{"type": "command", "command": command, "timeout": 15}]}]
        write_json(path, config)
    except (ValueError, OSError, CollabError) as error:
        raise CollabError("HOOK_CONFIG_ERROR", "无法合并现有 hook 配置，请检查文件格式与写入权限。") from error
    return {"path": str(path), "runtime": runtime, "auto_receive": auto}


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
