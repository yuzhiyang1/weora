"""Harness 子进程共用的受控 JSONL 传输。"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput, RuntimeSession
from weora.modules.runs.application.harness import PI_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.application.session_delivery import session_history_window
from weora.shared.runtime_limits import (
    MAX_CHECKPOINT_FRAME_BYTES,
    MAX_CHECKPOINT_FRAMES,
    MAX_CHECKPOINT_TOTAL_BYTES,
    MAX_SESSION_HISTORY_BYTES,
    MAX_TOOL_CALLS,
)

_HOME_VARIABLES = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")

_SAFE_CODES = frozenset(
    {
        "MODEL_FAILED",
        "MODEL_ABORTED",
        "MODEL_OUTPUT_LIMIT",
        "MODEL_UNKNOWN",
        "PROTOCOL_ERROR",
        "TOOL_BUDGET_EXCEEDED",
        "MODEL_TRUNCATED",
        "EMPTY_RESPONSE",
        "UNEXPECTED_TOOL_CALL",
        "PROTOCOL_INPUT_LIMIT",
        "MODEL_TIMEOUT",
        "RUN_TIMEOUT",
        "RUNTIME_PROTOCOL_OUTPUT_LIMIT",
    }
)


class ProcessRuntime:
    """入口由管理员配置，不接受模型或 HTTP 提供的命令、目录与环境变量。"""

    def __init__(
        self,
        *,
        executable: str,
        runtime_id: str,
        providers: frozenset[str],
        entry: Path,
        api_key: str,
        call_tool: Callable[[str, str, dict[str, object]], Awaitable[dict[str, object]]] | None = None,
        session: RuntimeSession | None = None,
        private_home: bool = False,
        watch_parent: bool = False,
        report_ready: bool = False,
    ) -> None:
        self._executable = executable
        self._runtime_id, self._providers = runtime_id, providers
        self._entry = entry.resolve()
        self._api_key = api_key
        self._call_tool = call_tool
        self._session = session
        self._private_home = private_home
        self._watch_parent = watch_parent
        self._report_ready = report_ready
        self._protocol = "weora.runtime.v2" if session else "weora.runtime.v1"

    async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
        self._delivery_started = False
        self._history_prefix: list[dict[str, object]] = []
        model_messages: list[dict[str, object]] = []
        history_offsets = [0]
        if self._session:
            # 会话超过预算时保留已保存的历史并明确失败，不静默裁掉旧消息。
            history_offsets = self._history_offsets(self._session.messages)
            if self._runtime_id == PI_RUNTIME:
                # Pi 按模型窗口压缩；恢复前按字节裁剪会丢掉可用于摘要的证据。
                model_messages = self._session.messages
            else:
                self._history_prefix, model_messages = session_history_window(self._session.messages)
                history_offsets = history_offsets[len(self._history_prefix) :]
        if profile.runtime_id != self._runtime_id or profile.provider not in self._providers:
            raise RuntimeFailure("MODEL_CONFIGURATION_MISMATCH")
        if not self._entry.is_file() or not self._api_key:
            raise RuntimeFailure("RUNTIME_NOT_CONFIGURED")
        request = json.dumps(
            {
                "protocol": self._protocol,
                "type": "run",
                "system_prompt": payload.system_prompt,
                "instruction": payload.instruction,
                "api_key": self._api_key,
                "provider": profile.provider,
                "model_id": profile.model_id,
                "timeout_seconds": profile.timeout_seconds,
                **({"max_tokens": profile.max_tokens} if profile.max_tokens is not None else {}),
                **(
                    {
                        "session": {
                            "id": self._session.id,
                            "messages": model_messages,
                            "initial_input_ids": self._session.initial_input_ids,
                            "checkpoint_mode": "delta",
                        }
                    }
                    if self._session
                    else {}
                ),
                **(
                    {"execution_timeout_seconds": profile.execution_timeout_seconds}
                    if profile.execution_timeout_seconds is not None
                    else {}
                ),
                **(
                    {
                        "tools": [
                            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
                            for tool in payload.tools
                        ]
                    }
                    if payload.tools
                    else {}
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if self._session and len(request) > MAX_SESSION_HISTORY_BYTES + MAX_CHECKPOINT_FRAME_BYTES:
            raise RuntimeFailure("PROTOCOL_INPUT_LIMIT")
        # NODE_OPTIONS、个人模型凭据、代理和云凭据均不继承；SDK 只使用显式提供方密钥。
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "LANG",
                "LC_ALL",
            }
        }
        if self._watch_parent:
            # 由受信适配器声明管道的生命周期语义，不接受宿主环境或请求覆盖。
            environment["WEORA_RUNTIME_WATCH_PARENT"] = "1"
        # 旧运行只有一份总预算。仅在独立整体时限存在时协商启动就绪帧。
        expect_ready = self._report_ready and profile.execution_timeout_seconds is not None
        if expect_ready:
            environment["WEORA_RUNTIME_REPORT_READY"] = "1"
        process: asyncio.subprocess.Process | None = None
        directory: TemporaryDirectory[str] | None = None
        try:
            directory = TemporaryDirectory(prefix="weora-harness-")
            # SDK 可能读 HOME 或系统配置目录。为每次执行提供空的私有目录。
            if self._private_home:
                environment.update(dict.fromkeys(_HOME_VARIABLES, directory.name))
            async with asyncio.timeout(profile.wall_timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    self._executable,
                    str(self._entry),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                    cwd=directory.name,
                    creationflags=_creation_flags(),
                    start_new_session=sys.platform != "win32",
                    limit=MAX_CHECKPOINT_FRAME_BYTES if self._session else 300001,
                )
                assert process.stdin is not None and process.stdout is not None and process.stderr is not None

                exchange = asyncio.create_task(
                    self._exchange(
                        process,
                        request,
                        payload,
                        profile.timeout_seconds,
                        model_messages,
                        history_offsets,
                        expect_ready,
                    )
                )
                errors = asyncio.create_task(self._read_bounded(process.stderr, 16384))
                try:
                    result, _ = await asyncio.gather(exchange, errors)
                finally:
                    # 先结束协议读取，再由退出清理接管管道，避免两个协程同时读。
                    exchange.cancel()
                    errors.cancel()
                    await asyncio.gather(exchange, errors, return_exceptions=True)
                if await self._read_bounded(process.stdout, 1):
                    raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
                await process.wait()
                if process.returncode != 0:
                    raise RuntimeFailure("RUNTIME_PROCESS_FAILED")
                return result
        except TimeoutError as exc:
            raise RuntimeFailure("RUN_TIMEOUT" if profile.execution_timeout_seconds else "MODEL_TIMEOUT") from exc
        except OSError as exc:
            raise RuntimeFailure(
                "SESSION_DELIVERY_FAILED" if self._session and not self._delivery_started else "RUNTIME_PROCESS_FAILED"
            ) from exc
        except (ValueError, UnicodeError) as exc:
            raise RuntimeFailure("RUNTIME_PROCESS_FAILED") from exc
        finally:
            if process is not None:
                await self._terminate(process)
            if directory is not None:
                # Windows 下必须先退出以该目录为 cwd 的子进程，再清理临时目录。
                directory.cleanup()

    async def _exchange(
        self,
        process: asyncio.subprocess.Process,
        request: bytes,
        payload: RuntimeInput,
        model_seconds: float,
        model_messages: list[dict[str, object]],
        history_offsets: list[int],
        expect_ready: bool,
    ) -> str:
        """双向 JSONL 只转发工具请求；Actor、凭据和 fencing 不取自子进程。"""
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(request + b"\n")
        await process.stdin.drain()
        # 受管 Pi 用 stdin EOF 判断父进程已退出，普通运行也保留写端直到收到终态。
        if not self._watch_parent and not payload.tools and not self._session:
            process.stdin.close()
        # 双向协议的任一方向失效都应终止本轮，尤其不能忽略输入侧的撤权异常。
        reader = asyncio.create_task(
            self._read_frames(process, payload, model_seconds, model_messages, history_offsets, expect_ready)
        )
        feeder: asyncio.Task[None] | None = None
        if self._session and self._session.pull:
            feeder = asyncio.create_task(self._feed(process))
        try:
            if feeder:
                await asyncio.wait({reader, feeder}, return_when=asyncio.FIRST_COMPLETED)
                if feeder.done():
                    await feeder
            return await reader
        finally:
            # 取消协议另一方向的等待，再由 run 终止自己创建的 Harness 进程。
            pending = [reader, *([feeder] if feeder else [])]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _feed(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdin is not None and self._session is not None and self._session.pull is not None
        sent: set[str] = set()
        while not process.stdin.is_closing():
            for identity, text in await self._session.pull():
                if identity in sent:
                    continue
                if len(text) > 24000:
                    raise RuntimeFailure("PROTOCOL_INPUT_LIMIT")
                process.stdin.write(
                    json.dumps(
                        {"protocol": self._protocol, "type": "steer", "id": identity, "text": text}, ensure_ascii=False
                    ).encode()
                    + b"\n"
                )
                await process.stdin.drain()
                sent.add(identity)
            await asyncio.sleep(0.2)

    async def _read_frames(
        self,
        process: asyncio.subprocess.Process,
        payload: RuntimeInput,
        model_seconds: float,
        model_messages: list[dict[str, object]],
        history_offsets: list[int],
        expect_ready: bool,
    ) -> str:
        """校验输出预算，分派会话回执与工具请求，直到收到模型终态。"""
        assert process.stdin is not None and process.stdout is not None
        permitted = {tool.name for tool in payload.tools}
        output_bytes = tool_calls = checkpoint_frames = checkpoint_bytes = 0
        history = model_messages.copy()
        awaiting_checkpoint_tail = False
        remaining = model_seconds
        while True:
            started = asyncio.get_running_loop().time()
            try:
                # 启动阶段由外层整体时限约束。模型计时仍跳过 Gateway 工具等待。
                async with asyncio.timeout(None if expect_ready else max(0, remaining)):
                    line = await process.stdout.readline()
                elapsed = asyncio.get_running_loop().time() - started
            except TimeoutError:
                raise RuntimeFailure("MODEL_TIMEOUT") from None
            except ValueError as exc:
                raise RuntimeFailure("RUNTIME_PROTOCOL_OUTPUT_LIMIT") from exc
            if not line:
                await process.wait()
                if process.returncode != 0:
                    raise RuntimeFailure(
                        "SESSION_DELIVERY_FAILED"
                        if self._session and not self._delivery_started
                        else "RUNTIME_PROCESS_FAILED"
                    )
                raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
            try:
                frame = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID") from exc
            if not isinstance(frame, dict) or frame.get("protocol") != self._protocol:
                raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
            frame_type = frame.get("type")
            if expect_ready:
                expect_ready = False
                if frame_type == "ready":
                    if len(line) > 300000:
                        raise RuntimeFailure("RUNTIME_PROTOCOL_OUTPUT_LIMIT")
                    if set(frame) != {"protocol", "type"}:
                        raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
                    continue
                # 旧入口直接发业务帧，首帧等待仍计入模型预算。
                if elapsed >= remaining:
                    raise RuntimeFailure("MODEL_TIMEOUT")
            remaining -= elapsed
            is_checkpoint = self._session is not None and frame_type in ("checkpoint", "checkpoint_delta")
            if awaiting_checkpoint_tail and frame_type not in ("checkpoint_delta", "accepted", "model_retry"):
                raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")

            # 历史随会话增长，单独计量检查点，避免挤占普通输出与工具帧预算。
            if len(line) > (MAX_CHECKPOINT_FRAME_BYTES if is_checkpoint else 300000):
                raise RuntimeFailure("RUNTIME_PROTOCOL_OUTPUT_LIMIT")
            if is_checkpoint:
                checkpoint_frames += 1
                checkpoint_bytes += len(line)
                if checkpoint_frames > MAX_CHECKPOINT_FRAMES or checkpoint_bytes > MAX_CHECKPOINT_TOTAL_BYTES:
                    raise RuntimeFailure("RUNTIME_PROTOCOL_OUTPUT_LIMIT")
            else:
                output_bytes += len(line)
                if output_bytes > 2000000:
                    raise RuntimeFailure("RUNTIME_PROTOCOL_OUTPUT_LIMIT")

            if self._session and frame_type == "accepted":
                if set(frame) != {"protocol", "type", "id"} or not isinstance(frame["id"], str):
                    raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
                if self._session.accepted:
                    await self._session.accepted(frame["id"])
                continue
            if self._session and frame_type == "model_retry":
                status = frame.get("status")
                if set(frame) != {"protocol", "type", "status"} or (
                    status is not None
                    and (
                        not isinstance(status, dict)
                        or set(status) != {"attempt", "delay_ms"}
                        or type(status["attempt"]) is not int
                        or not 1 <= status["attempt"] <= 3
                        or type(status["delay_ms"]) is not int
                        or not 0 <= status["delay_ms"] <= 60000
                    )
                ):
                    raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
                if self._session.status:
                    await self._session.status(status)
                continue
            if is_checkpoint:
                awaiting_checkpoint_tail = await self._apply_checkpoint_frame(
                    frame, line, history, history_offsets, awaiting_checkpoint_tail
                )
                continue
            if frame_type != "tool_call":
                # 最终响应之后关闭输入，防止退出等待和后续越权工具请求。
                if not process.stdin.is_closing():
                    process.stdin.close()
                return self._result(line)
            tool_calls += 1
            if tool_calls > MAX_TOOL_CALLS:
                raise RuntimeFailure("TOOL_BUDGET_EXCEEDED")
            # 从这个边界起，工具可能已经产生副作用；后续断连必须走原生恢复，不能重投整轮。
            self._delivery_started = True
            call_id, name, arguments = frame.get("id"), frame.get("name"), frame.get("arguments")
            if (
                not isinstance(call_id, str)
                or not isinstance(name, str)
                or name not in permitted
                or not isinstance(arguments, dict)
                or self._call_tool is None
                or set(frame) != {"protocol", "type", "id", "name", "arguments"}
            ):
                raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
            result = await self._call_tool(call_id, name, arguments)
            encoded = json.dumps(
                {"protocol": self._protocol, "type": "tool_result", "id": call_id, "result": result},
                ensure_ascii=False,
            ).encode()
            if len(encoded) > 300000 or self._api_key.encode() in encoded:
                raise RuntimeFailure("RUNTIME_PROTOCOL_OUTPUT_LIMIT")
            process.stdin.write(encoded + b"\n")
            await process.stdin.drain()

    async def _apply_checkpoint_frame(
        self,
        frame: dict[str, object],
        line: bytes,
        history: list[dict[str, object]],
        history_offsets: list[int],
        awaiting_tail: bool,
    ) -> bool:
        """合并一帧会话历史，收到末帧后才一起保存历史与已读回执。

        返回 True 表示当前批次尚未收齐，内存中的变更还未提交。
        """
        assert self._session is not None
        self._delivery_started = True

        # 旧版检查点会替换整份可见历史，增量帧显式指定保留的前缀。
        fields = {"protocol", "type", "messages", "read"}
        keep: object = 0
        has_more = False
        if frame["type"] == "checkpoint_delta":
            fields.add("keep")
            keep = frame.get("keep")
            if "more" in frame:
                fields.add("more")
                if type(frame["more"]) is not bool:
                    raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
                has_more = frame["more"]

        # 续帧只能追加，且已读回执必须随末帧到达，避免确认一段未持久化的历史。
        messages, read_ids = frame.get("messages"), frame.get("read")
        if (
            set(frame) != fields
            or type(keep) is not int
            or not 0 <= keep <= len(history)
            or (awaiting_tail and keep != len(history))
            or (has_more and (not messages or read_ids))
            or not isinstance(messages, list)
            or any(
                not isinstance(item, dict) or item.get("role") not in {"user", "assistant", "toolResult"}
                for item in messages
            )
            or not isinstance(read_ids, list)
            or any(not isinstance(item, str) for item in read_ids)
            or self._api_key.encode() in line
        ):
            raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")

        # 复用保留前缀的字节计数，先验证剩余预算，再替换内存中的尾部。
        suffix_offsets = self._history_offsets(messages, base=history_offsets[keep])
        history[keep:] = messages
        history_offsets[keep + 1 :] = suffix_offsets[1:]
        if not has_more and self._session.checkpoint:
            await self._session.checkpoint(self._history_prefix + history, read_ids)
        return has_more

    def _result(self, content: bytes) -> str:
        try:
            message = json.loads(content)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID") from exc
        if not isinstance(message, dict) or message.get("protocol") != self._protocol:
            raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
        if message.get("type") == "failed":
            if set(message) != {"protocol", "type", "code"}:
                raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
            code = message.get("code")
            raise RuntimeFailure(code if isinstance(code, str) and code in _SAFE_CODES else "MODEL_FAILED")
        text = message.get("text")
        if set(message) != {"protocol", "type", "text"}:
            raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
        if message.get("type") != "completed" or not isinstance(text, str) or not text.strip() or len(text) > 65536:
            raise RuntimeFailure("RUNTIME_PROTOCOL_INVALID")
        if self._api_key in text:
            raise RuntimeFailure("MODEL_RESPONSE_REDACTED")
        return text

    @staticmethod
    def _history_offsets(messages: list[dict[str, object]], *, base: int = 0) -> list[int]:
        """记录每个前缀的大小，追加时无需反复序列化旧日志。"""
        offsets = [base]
        for message in messages:
            size = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode())
            offset = offsets[-1] + size + 1
            if offset > MAX_SESSION_HISTORY_BYTES:
                raise RuntimeFailure("RUNTIME_PROTOCOL_OUTPUT_LIMIT")
            offsets.append(offset)
        return offsets

    @staticmethod
    async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while chunk := await reader.read(4096):
            size += len(chunk)
            if size > limit:
                raise RuntimeFailure("RUNTIME_PROTOCOL_OUTPUT_LIMIT")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """仅终止本适配器创建的进程树，等待退出后才报告本地取消。"""
        if process.returncode is not None:
            return
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        with suppress(ProcessLookupError):
            process.kill()
        assert process.stdout is not None and process.stderr is not None
        # 超限后未消费的管道会阻止 Windows 等待退出。丢弃剩余数据，不能重新累积到内存。
        await asyncio.gather(
            process.wait(), ProcessRuntime._discard(process.stdout), ProcessRuntime._discard(process.stderr)
        )

    @staticmethod
    async def _discard(reader: asyncio.StreamReader) -> None:
        """排空已终止子进程的管道，释放等待退出时的背压。"""
        while await reader.read(8192):
            pass


def _creation_flags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return 0
