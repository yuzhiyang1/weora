"""接收进程的脱敏状态采样。独立于 Inbox 提交与 ACK，不承担事件可靠性。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from uuid import uuid4

from weora.modules.context.domain import Json


class ReceiverTelemetry:
    def __init__(self, persist: Callable[[Json], Awaitable[None]], *, interval: float = 15) -> None:
        self.persist, self.interval = persist, interval
        self._stop, self._lock = Event(), Lock()
        self._state: Json = {
            "instance_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "transport_state": "starting",
            "reconnects": 0,
            "data_frames": 0,
        }
        self._thread = Thread(target=self._run, name="weora-receiver-status", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def observe(self, code: str) -> None:
        states = {
            "FEISHU_CHANNEL_CONNECTED": "connected",
            "FEISHU_CHANNEL_DISCONNECTED": "disconnected",
            "FEISHU_CHANNEL_RECONNECTING": "reconnecting",
            "FEISHU_CONNECT_ERROR": "error",
            "FEISHU_RECEIVE_LOOP_ERROR": "error",
            "FEISHU_PING_ERROR": "error",
        }
        with self._lock:
            now = datetime.now(UTC).isoformat()
            if code in states:
                self._state.update(transport_state=states[code], transport_changed_at=now)
            if code == "FEISHU_CHANNEL_RECONNECTING":
                self._state["reconnects"] = int(str(self._state["reconnects"])) + 1
            if code == "FEISHU_EVENT_RECEIVED":
                self._state["last_callback_at"] = now
            if code == "FEISHU_INBOX_COMMITTED":
                self._state["last_inbox_commit_at"] = now
            if code == "FEISHU_CONTROL_PONG":
                self._state["last_pong_at"] = now
            if code == "FEISHU_DATA_FRAME":
                self._state["data_frames"] = int(str(self._state["data_frames"])) + 1
                self._state["last_data_frame_at"] = now
            if code in {
                "FEISHU_CONNECT_ERROR",
                "FEISHU_RECEIVE_LOOP_ERROR",
                "FEISHU_PING_ERROR",
                "FEISHU_EVENT_HANDLER_ERROR",
                "FEISHU_INBOX_FAILED",
            }:
                self._state.update(last_error_code=code, last_error_at=now)

    def snapshot(self) -> Json:
        with self._lock:
            return dict(self._state, heartbeat_at=datetime.now(UTC).isoformat())

    def _flush(self) -> None:
        async def bounded() -> None:
            await asyncio.wait_for(self.persist(self.snapshot()), timeout=4)

        # 观测失败只会让心跳过期，不能阻塞或伪造 Inbox 的成功 ACK。
        with suppress(Exception):
            asyncio.run(bounded())

    def _run(self) -> None:
        self._flush()
        while not self._stop.wait(self.interval):
            self._flush()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        with self._lock:
            self._state["transport_state"] = "stopped"
        self._flush()
