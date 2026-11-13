"""用 Kimi 自己的 ACP 进程持有一个指定会话，不向其他终端注入输入。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from queue import Empty, Full, Queue
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic

from weora_cli.collab_client import Client, CollabError, as_object, config_path, write_json
from weora_cli.collab_mcp import binding_prefix
from weora_cli.collab_native import WakeState
from weora_cli.collaboration import open_local_session
from weora_cli.commands import JsonValue


class KimiAcp:
    def __init__(self, process: subprocess.Popen[str], inbox_tools: set[str]) -> None:
        self.process = process
        self.inbox_tools = inbox_tools
        self.closed = Event()
        self.events: Queue[dict[str, JsonValue]] = Queue(maxsize=512)
        self.request_id = 0
        Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.events.put(as_object(json.loads(line)), timeout=5)
        except (OSError, ValueError, CollabError, Full):
            pass
        finally:
            self.closed.set()

    def _write(self, body: dict[str, JsonValue]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **body}) + "\n")
        self.process.stdin.flush()

    def call(self, method: str, params: dict[str, JsonValue], *, timeout: float = 15) -> dict[str, JsonValue]:
        self.request_id += 1
        request_id = self.request_id
        self._write({"id": request_id, "method": method, "params": params})
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            try:
                frame = self.events.get(timeout=min(0.5, max(0, deadline - monotonic())))
            except Empty:
                if self.closed.is_set():
                    break
                continue
            if frame.get("id") == request_id and ("result" in frame or "error" in frame):
                if "error" in frame:
                    error = as_object(frame["error"])
                    raise CollabError("KIMI_ACP_REJECTED", f"Kimi 拒绝 {method}（RPC {error.get('code')}）。")
                return as_object(frame["result"])
            if "id" not in frame or "method" not in frame:
                continue
            if frame["method"] == "session/request_permission":
                self._write({"id": frame["id"], "result": {"outcome": self._permission(frame.get("params"))}})
            else:
                self._write({"id": frame["id"], "error": {"code": -32601, "message": "Client method unavailable"}})
        raise TimeoutError(method)

    def _permission(self, params: JsonValue) -> dict[str, JsonValue]:
        # 仅允许本次启动的收件工具。远程正文不能批准本机 shell、文件或其他 MCP 工具。
        payload = params if isinstance(params, dict) else {}
        tool = payload.get("toolCall")
        options = payload.get("options")
        title = tool.get("title") if isinstance(tool, dict) else None
        # 旧版 Kimi 会在工具名后附加参数摘要。审批只比较分隔符前的完整工具名。
        if isinstance(title, str) and title.partition(":")[0] in self.inbox_tools and isinstance(options, list):
            for option in options:
                if isinstance(option, dict) and option.get("kind") == "allow_once":
                    option_id = option.get("optionId")
                    if isinstance(option_id, str):
                        return {"outcome": "selected", "optionId": option_id}
        return {"outcome": "cancelled"}


def run_kimi(client: Client, name: str, native: str | None) -> None:
    executable = shutil.which("kimi.exe" if os.name == "nt" else "kimi")
    if not executable:
        raise CollabError("KIMI_NOT_INSTALLED", "请安装 Kimi Code CLI 并完成本机登录。")
    cwd = str(Path.cwd())
    cli = str(Path(sys.argv[0]).resolve())
    with TemporaryDirectory(prefix="weora-kimi-") as temporary:
        binding = Path(temporary) / "session.json"
        prefix = binding_prefix(binding)
        mcp: list[JsonValue] = [
            {
                "name": "weora",
                "command": cli,
                "args": ["collab", "mcp", "--runtime", "kimi-code", "--name", name, "--binding-file", str(binding)],
                "env": [{"name": "WEORA_COLLAB_CONFIG", "value": str(config_path().resolve())}],
            }
        ]
        with subprocess.Popen(
            [executable, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            encoding="utf-8",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ) as process:
            try:
                acp = KimiAcp(process, {prefix + tool for tool in ("claim", "reply", "ack")})
                acp.call(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {
                            "name": "weora",
                            "version": "0.1.0",
                        },
                    },
                )
                params: dict[str, JsonValue] = {"cwd": cwd, "mcpServers": mcp}
                if native:
                    params["sessionId"] = native
                    acp.call("session/load", params)
                else:
                    result = acp.call("session/new", params)
                    native = str(result["sessionId"])
                print(json.dumps({"event": "kimi_ready", "native_id": native}), flush=True)

                def deliver(hint: str) -> WakeState:
                    if acp.closed.is_set():
                        return "failed"
                    try:
                        acp.call(
                            "session/prompt",
                            {
                                "sessionId": native,
                                "prompt": [
                                    {
                                        "type": "text",
                                        "text": (
                                            f"Use MCP tools {prefix}claim, {prefix}reply and {prefix}ack "
                                            "instead of shell commands. " + hint
                                        ),
                                    }
                                ],
                            },
                            timeout=90,
                        )
                        return "accepted"
                    except CollabError as error:
                        print(str(error), file=sys.stderr, flush=True)
                        return "failed"
                    except (OSError, TimeoutError):
                        # 无法证明提示是否进入会话。停止监听，避免误处理延迟响应。
                        acp.closed.set()
                        return "unknown"

                open_local_session(
                    client,
                    "kimi-code",
                    native,
                    name,
                    deliver,
                    stop=acp.closed,
                    on_registered=lambda session: write_json(binding, {"session_id": session}),
                )
            except (OSError, TimeoutError) as error:
                raise CollabError("KIMI_ACP_UNAVAILABLE", "Kimi ACP 连接失败，请检查本机登录和版本。") from error
            finally:
                if process.poll() is None:
                    process.terminate()
