"""由原生插件启动的 stdio 桥。只有宿主确认接收后才回报投递成功。"""

import json
import os
import sys
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

from weora_cli.collab_client import Client, CollabError
from weora_cli.collab_native import WakeState
from weora_cli.collaboration import open_local_session


def run(client: Client, runtime: str, native: str, name: str) -> None:
    if runtime not in {"opencode", "pi"}:
        raise CollabError("UNSUPPORTED_BRIDGE", "该客户端没有安装 Weora 原生插件。")
    stopped = Event()
    responses: Queue[tuple[str, WakeState]] = Queue(maxsize=8)

    def read_host() -> None:
        try:
            pending = b""
            while chunk := os.read(sys.stdin.fileno(), 4096):
                pending += chunk
                if len(pending) > 8192:
                    break
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        return
                    receipt, status = data.get("id"), data.get("state")
                    if not isinstance(receipt, str) or status not in ("accepted", "failed", "unknown"):
                        return
                    responses.put((receipt, status), timeout=1)
        except (OSError, ValueError, Full):
            pass
        finally:
            stopped.set()

    def start_reader(_session: str) -> None:
        # Windows 上先写登记帧，再读取子进程管道，避免宿主等待双方启动。
        Thread(target=read_host, daemon=True).start()

    def deliver(hint: str) -> WakeState:
        if stopped.is_set():
            return "failed"
        receipt = str(uuid4())
        try:
            print(json.dumps({"event": "wake", "id": receipt, "hint": hint}, ensure_ascii=False), flush=True)
        except OSError:
            return "unknown"
        deadline = monotonic() + 10
        while not stopped.is_set() and monotonic() < deadline:
            try:
                received, status = responses.get(timeout=0.25)
            except Empty:
                continue
            if receipt == received:
                return status
        return "unknown"

    open_local_session(client, runtime, native, name, deliver, stop=stopped, on_registered=start_reader)
