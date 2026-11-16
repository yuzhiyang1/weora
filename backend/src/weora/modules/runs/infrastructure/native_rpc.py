"""在受控容器中运行完整 CLI，按各引擎的线协议处理会话。"""

import asyncio
import hashlib
import io
import json
import logging
import re
import stat
import tarfile
from base64 import b64decode, b64encode
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from uuid import UUID, uuid4, uuid5
from zipfile import BadZipFile, ZipFile

import aiohttp

from weora.modules.agents.application.ports import RuntimeSkillPackage
from weora.modules.projects.domain import RepositoryArchive
from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput, RuntimeSession
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, ToolCaller
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.native_model import Json, NativeModelProxy, WriteFrame

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_FRAME_LIMIT = 48 * 1024 * 1024
NATIVE_ARCHIVE_LIMIT = 512 * 1024 * 1024
_SKILL_LIMIT = 32 * 1024 * 1024
_OMP_FRAME_LIMIT = 64 * 1024 * 1024
_LOG = logging.getLogger(__name__)
Progress = Callable[[str], Awaitable[None]]
DiffSink = Callable[[UUID, str, str, bool], Awaitable[None]]


def skill_tar(packages: tuple[RuntimeSkillPackage, ...]) -> bytes:
    """按发布包原目录打包，拒绝跨包路径、链接和重复名称。"""
    result = io.BytesIO()
    slugs: set[str] = set()
    size = 0
    try:
        with tarfile.open(fileobj=result, mode="w") as target:
            for package in packages:
                if not _SLUG.fullmatch(package.slug) or package.slug in slugs:
                    raise ValueError
                slugs.add(package.slug)
                names: set[str] = set()
                with ZipFile(io.BytesIO(package.archive)) as archive:
                    for entry in archive.infolist():
                        name = entry.filename.rstrip("/")
                        path = PurePosixPath(name)
                        mode = entry.external_attr >> 16
                        if (
                            str(path) != name
                            or path.is_absolute()
                            or ".." in path.parts
                            or "\\" in name
                            or not path.parts
                            or path.parts[0] != package.slug
                            or name in names
                            or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                        ):
                            raise ValueError
                        names.add(name)
                        if entry.is_dir():
                            continue
                        size += entry.file_size
                        if size > _SKILL_LIMIT:
                            raise ValueError
                        item = tarfile.TarInfo(name)
                        item.size = entry.file_size
                        item.mode = 0o555 if "scripts" in path.parts[1:-1] else 0o444
                        with archive.open(entry) as source:
                            target.addfile(item, source)
                if f"{package.slug}/SKILL.md" not in names:
                    raise ValueError
    except (ValueError, BadZipFile, OSError, RuntimeError):
        raise RuntimeFailure("RUNTIME_MATERIAL_INVALID") from None
    return result.getvalue()


@dataclass(frozen=True, slots=True)
class NativeSandbox:
    command: tuple[str, ...]
    image: str
    workspace_id: UUID
    session_id: UUID
    memory_mb: int = 1024
    cpus: float = 1
    pids: int = 128
    temporary_mb: int = 512
    extensions: tuple[str, ...] = ()
    read_only: bool = False

    @property
    def volume(self) -> str:
        return f"weora-rpc-{self.workspace_id.hex}-{self.session_id.hex}"

    def invocation(self, name: str, owner: str, wall_seconds: int) -> tuple[str, ...]:
        # 任务卷是唯一可变的持久挂载。模型和 MCP 仅经附着的 RPC 管道出容器。
        return (
            *self.command,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--pull=never",
            # 附着 RPC 包含任务正文和工具结果，只进入 Worker 管道，禁用容器日志复制。
            "--log-driver=none",
            "--network",
            "none",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--memory",
            f"{self.memory_mb}m",
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids),
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self.temporary_mb}m,mode=1777",
            "--label",
            "weora.native-rpc=true",
            "--label",
            f"weora.rpc-owner={owner}",
            "--mount",
            f"type=volume,source={self.volume},target=/weora-session" + (",readonly" if self.read_only else ""),
            *(
                ("--mount", f"type=volume,source={self.volume}-readonly,target=/weora-readonly-session")
                if self.read_only else ()
            ),
            "--user",
            "0:0",
            "--entrypoint",
            "/usr/bin/timeout",
            self.image,
            "--kill-after=5s",
            str(wall_seconds + 15),
            "node",
            "/opt/weora-coding-tools/native-rpc.mjs",
        )


@dataclass(slots=True)
class OmpFrame:
    """按 OMP v2 元数据校验连续分片，正文使用 UTF-8 严格解码。"""

    identity: str
    count: int
    total: int
    parts: bytearray
    index: int = 0

    def append(self, frame: Json) -> Json | None:
        data = frame.get("data")
        if (
            frame.get("chunkId") != self.identity
            or frame.get("index") != self.index
            or frame.get("count") != self.count
            or frame.get("byteLength") != self.total
            or not isinstance(data, str)
            or len(data) > 400_000
        ):
            raise ValueError
        self.parts.extend(b64decode(data, validate=True))
        self.index += 1
        if len(self.parts) > self.total:
            raise ValueError
        if self.index < self.count:
            return None
        if len(self.parts) != self.total:
            raise ValueError
        result = json.loads(self.parts.decode("utf-8"))
        if not isinstance(result, dict) or result.get("type") == "rpc_chunk":
            raise ValueError
        return result


class NativeRpcConnection:
    """相关 ID 和事件消费共用一个读循环，工具执行与模型流并行返回。"""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        omp: bool,
        model: NativeModelProxy,
        http: aiohttp.ClientSession,
        call: ToolCaller,
        progress: Progress,
        diff: DiffSink,
        tools: dict[str, str],
        repositories: dict[UUID, RepositoryArchive],
        inputs_read: Callable[[list[str]], Awaitable[None]] | None = None,
        read_only: bool = False,
        authorize: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.process, self.omp, self.model, self.http = process, omp, model, http
        self.call_namespace = uuid4().hex
        self.call, self.progress, self.diff, self.tools, self.repositories = call, progress, diff, tools, repositories
        self.ready, self.started, self.closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.settled, self.prompt_result = asyncio.Event(), asyncio.Event()
        self.failure: RuntimeFailure | None = None
        self.requests: dict[str, asyncio.Task[None]] = {}
        self.pending: dict[str, asyncio.Future[Json]] = {}
        self.commands = asyncio.Lock()
        self.stop_reason = ""
        self.text = ""
        self.report_summary = ""
        self.platform_budget_exhausted = False
        self.prompt_id: str | None = None
        self.turn_started = False
        self.admitted: dict[str, list[str]] = {}
        self.initial_input: tuple[str, tuple[str, ...]] | None = None
        self.inputs_read = inputs_read
        self.read_only, self.authorize = read_only, authorize
        self.acknowledged: asyncio.Queue[str] = asyncio.Queue()
        self.diff_repos: set[UUID] = set()
        self.omp_protocols: list[object] = []
        self.omp_v2 = False
        self.omp_frame: OmpFrame | None = None

    async def send(self, frame: Json) -> None:
        assert self.process.stdin is not None
        try:
            line = json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
            async with self.commands:
                self.process.stdin.write(line)
                await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise self.failure or RuntimeFailure("NATIVE_RPC_PROCESS_FAILED") from None

    async def command(self, command: str, *, request_id: str | None = None, **arguments: object) -> Json:
        request_id = request_id or uuid4().hex
        future: asyncio.Future[Json] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"channel": "rpc", "data": {"id": request_id, "type": command, **arguments}})
            response = await asyncio.wait_for(future, 20)
            if response.get("command") != command or response.get("success") is not True:
                raise RuntimeFailure("NATIVE_RPC_COMMAND_FAILED")
            data = response.get("data")
            return data if isinstance(data, dict) else {}
        finally:
            self.pending.pop(request_id, None)

    async def read(self) -> None:
        assert self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError
                channel = frame.get("channel")
                if channel == "started":
                    self.started.set()
                    await self.progress("native.resumed" if frame.get("resumed") is True else "native.started")
                elif channel == "rpc" and isinstance(frame.get("data"), dict):
                    await self._rpc(frame["data"])
                elif channel in {"model", "tool"}:
                    self._background(str(frame["id"]), self._serve(frame))
                elif channel == "cancel_request":
                    self._cancel_request(str(frame["id"]))
                elif channel == "diff":
                    if self.read_only:
                        raise RuntimeFailure("NATIVE_READ_ONLY_VIOLATION")
                    repository = UUID(str(frame["repository"]))
                    archive = self.repositories.get(repository)
                    body = frame.get("diff")
                    if not archive or frame.get("revision") != archive.revision or not isinstance(body, str):
                        raise ValueError
                    await self.diff(repository, archive.revision, body, frame.get("truncated") is True)
                    self.diff_repos.add(repository)
                elif channel == "closed":
                    if not self.read_only and self.diff_repos != set(self.repositories):
                        raise RuntimeFailure("NATIVE_DIFF_FAILED")
                    self.closed.set()
                    return
                elif channel == "error":
                    code = str(frame.get("code"))
                    raise RuntimeFailure(code if re.fullmatch(r"[A-Z_]{3,64}", code) else "NATIVE_RPC_PROCESS_FAILED")
                else:
                    raise ValueError
            raise RuntimeFailure("NATIVE_RPC_PROCESS_FAILED")
        except RuntimeFailure as error:
            self.failure = error
        except (ValueError, TypeError, KeyError, OSError):
            self.failure = RuntimeFailure("NATIVE_RPC_PROTOCOL_ERROR")
        finally:
            if self.failure:
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(self.failure)

    def _background(self, key: str, coroutine: Awaitable[None]) -> None:
        async def run() -> None:
            try:
                await coroutine
            except RuntimeFailure as error:
                self.failure = error
            except (OSError, ValueError, KeyError, TypeError):
                self.failure = RuntimeFailure("NATIVE_RPC_PROTOCOL_ERROR")
            finally:
                self.requests.pop(key, None)

        self.requests[key] = asyncio.create_task(run())

    def _cancel_request(self, key: str) -> None:
        task = self.requests.get(key)
        if task:
            task.cancel()

    async def cancel_requests(self) -> None:
        running = tuple(self.requests.values())
        for request in running:
            request.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    async def _rpc(self, event: Json) -> None:
        if self.omp and (self.omp_frame is not None or event.get("type") == "rpc_chunk"):
            if not self.omp_v2 or event.get("type") != "rpc_chunk":
                raise ValueError
            if self.omp_frame is None:
                count, total, identity = event.get("count"), event.get("byteLength"), event.get("chunkId")
                if (
                    not isinstance(count, int)
                    or not 1 <= count <= 256
                    or not isinstance(total, int)
                    or not 1 <= total <= _OMP_FRAME_LIMIT
                    or not isinstance(identity, str)
                    or not identity
                    or len(identity) > 128
                ):
                    raise ValueError
                self.omp_frame = OmpFrame(identity, count, total, bytearray())
            complete = self.omp_frame.append(event)
            if complete is None:
                return
            self.omp_frame = None
            event = complete
        kind = event.get("type")
        event_id = str(event.get("id", ""))
        if kind == "ready":
            if not self.omp or event.get("protocolVersion") != 1:
                raise RuntimeFailure("NATIVE_RPC_PROTOCOL_ERROR")
            protocols = event.get("supportedProtocolVersions")
            self.omp_protocols = protocols if isinstance(protocols, list) else [1]
            self.ready.set()
        elif kind == "response":
            pending = self.pending.get(event_id)
            if pending and not pending.done():
                pending.set_result(event)
        elif kind == "agent_start":
            self.settled.clear()
            self.turn_started = True
        elif kind == "message_end":
            if self._message_end(event.get("message")):
                await self.progress("native.assistant.finished")
            if self.inputs_read and not self.acknowledged.empty():
                # 先持久化真实入会话的输入，再允许后续 report 使用这些目标作为完成依据。
                read = []
                while not self.acknowledged.empty():
                    read.append(self.acknowledged.get_nowait())
                await self.inputs_read(read)
        elif kind == "agent_settled" and not self.omp:
            if event.get("aborted") is True:
                self.stop_reason = "aborted"
            self.settled.set()
        elif kind == "prompt_result" and self.omp and event_id == self.prompt_id:
            if event.get("status") != "completed":
                self.stop_reason = str(event.get("status", "error"))
            self.prompt_result.set()
            if event.get("sessionSettled") is True:
                self.settled.set()
        elif kind == "session_settled" and self.omp:
            self.settled.set()
        elif kind == "host_tool_call" and self.omp:
            self._background("omp:" + event_id, self._host_tool(event))
        elif kind == "host_tool_cancel" and self.omp:
            self._cancel_request("omp:" + str(event.get("targetId", "")))
        elif kind == "extension_ui_request" and event.get("method") in {"select", "confirm", "input", "editor"}:
            # 交互批准结果必须由可信入口提供，Headless 会话收到弹窗时按取消处理。
            await self.send(
                {
                    "channel": "rpc",
                    "data": {"type": "extension_ui_response", "id": event_id, "cancelled": True, "confirmed": False},
                }
            )
        elif kind == "tool_execution_end":
            name = str(event.get("toolName"))
            if name in {"bash", "read", "edit", "write", "grep", "find", "ls", "glob"}:
                status = "error" if event.get("isError") is True else "finished"
                await self.progress(f"native.tool.{name}.{status}")
        elif kind == "rpc_frame_error" and self.omp:
            raise RuntimeFailure("NATIVE_RPC_FRAME_TOO_LARGE")

    def _message_end(self, message: object) -> bool:
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        text = (
            content
            if isinstance(content, str)
            else "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if isinstance(content, list)
            else ""
        )
        if message.get("role") == "assistant":
            self.stop_reason = str(message.get("stopReason", ""))
            # 终态必须对应最后一条 assistant，不能把早先的工具前旁白误作最终回答。
            self.text = text
            return bool(text.strip())
        elif (self.admitted or self.initial_input) and message.get("role") == "user":
            # 引擎将 steering 落入原生历史后，平台才把该输入标记为已读。
            parts = [text]
            if isinstance(content, list):
                parts.extend(
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                )
            for part in dict.fromkeys(parts):
                if self.initial_input and part == self.initial_input[0]:
                    for identity in self.initial_input[1]:
                        self.acknowledged.put_nowait(identity)
                    self.initial_input = None
                matching = self.admitted.get(part)
                if matching:
                    self.acknowledged.put_nowait(matching.pop(0))
                    if not matching:
                        self.admitted.pop(part, None)
        return False

    async def _tool(self, name: object, identity: str, arguments: object) -> Json:
        tool = self.tools.get(str(name))
        if not tool or not isinstance(arguments, dict):
            return {"ok": False, "error": {"code": "TOOL_NOT_ALLOWED"}}
        if self.platform_budget_exhausted and tool == "orchestrator_report" and arguments.get("state") == "ready":
            return self._budget_result()
        result = await self.call(identity, tool, arguments)
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else result.get("code")
        if code == "TOOL_BUDGET_EXCEEDED":
            self.platform_budget_exhausted = True
            return result | self._budget_result()
        if (
            tool == "orchestrator_report" and result.get("ok") is True
            and arguments.get("state") in {"ready", "partial"}
            and isinstance(arguments.get("summary"), str)
        ):
            self.report_summary = str(arguments["summary"]).strip()
        return result

    @staticmethod
    def _budget_result() -> Json:
        # 平台工具配额与引擎内建工具分开；耗尽必须保留事实并续接，不能冒充目标完成。
        return {
            "ok": False, "error": {"code": "TOOL_BUDGET_EXCEEDED"},
            "instruction": (
                "本轮平台工具预算已耗尽。请调用 orchestrator_report，state=partial，"
                "summary 保留已核实事实，remaining_work 列明原任务尚未完成的工作。"
                "不要报告 ready，也不要缩小用户目标；平台会保留会话继续处理。"
            ),
        }

    async def _serve(self, frame: Json) -> None:
        if self.authorize and not await self.authorize():
            raise RuntimeFailure("ACCESS_REVOKED")
        if frame["channel"] == "model":
            await self.model.stream(self.http, frame, self.send)
            return
        identity = str(frame["id"])
        result = await self._tool(
            frame.get("name"), "pi-mcp:" + self.call_namespace + ":" + identity, frame.get("arguments")
        )
        await self.send(
            {
                "channel": "tool_result",
                "id": identity,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "isError": result.get("ok") is False,
                },
            }
        )

    async def _host_tool(self, frame: Json) -> None:
        identity = str(frame["id"])
        result = await self._tool(
            frame.get("toolName"), "omp:" + self.call_namespace + ":" + identity, frame.get("arguments")
        )
        await self.send(
            {
                "channel": "rpc",
                "data": {
                    "type": "host_tool_result",
                    "id": identity,
                    "isError": result.get("ok") is False,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                        "details": {},
                    },
                },
            }
        )


class NativeCodingRuntime:
    def __init__(
        self,
        sandbox: NativeSandbox,
        *,
        key: str,
        session: RuntimeSession | None,
        call: ToolCaller,
        load_repositories: Callable[[], Awaitable[dict[UUID, RepositoryArchive]]],
        check_access: Callable[[], Awaitable[bool]],
        progress: Progress,
        diff: DiffSink,
        test_model_origin: str | None = None,
    ) -> None:
        self.sandbox, self.key, self.session, self.call = sandbox, key, session, call
        self.load_repositories, self.check_access = load_repositories, check_access
        self.progress, self.diff = progress, diff
        self.test_model_origin = test_model_origin

    async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
        # 在读取材料和创建容器前验证授权；长会话还会在每次轮询中持续复核。
        if not await self.check_access():
            raise RuntimeFailure("ACCESS_REVOKED")
        repositories = await self.load_repositories()
        try:
            return await self._run(payload, profile, repositories)
        finally:
            for archive in repositories.values():
                archive.path.unlink(missing_ok=True)

    async def _run(
        self, payload: RuntimeInput, profile: ExecutionProfile, repositories: dict[UUID, RepositoryArchive]
    ) -> str:
        omp = profile.runtime_id == OMP_RPC_RUNTIME
        model = NativeModelProxy(profile, self.key, test_origin=self.test_model_origin)
        skills = skill_tar(payload.skill_packages)
        if sum(item.path.stat().st_size for item in repositories.values()) > NATIVE_ARCHIVE_LIMIT:
            raise RuntimeFailure("NATIVE_REPOSITORY_TOO_LARGE")
        sandbox = self.sandbox
        if any(archive.restricted_paths_removed for archive in repositories.values()):
            # 旧卷的文件和原生历史可能已带入敏感内容。只为受影响的任务换卷，保留 Task 和冻结版本。
            sandbox = replace(sandbox, session_id=uuid5(sandbox.session_id, "restricted-repository-paths:v1"))
        # 同一任务一条容器名，Docker 原子拒绝并发挂载可写工作目录。
        name = sandbox.volume
        owner = uuid4().hex
        try:
            process = await asyncio.create_subprocess_exec(
                *sandbox.invocation(name, owner, profile.wall_timeout_seconds),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_FRAME_LIMIT,
            )
        except OSError:
            raise RuntimeFailure("NATIVE_SANDBOX_UNAVAILABLE") from None
        prefix = "weora_" if omp else ""
        # Gateway 标识经映射保留原名，用同一 schema 执行运行时授权与参数校验。
        specs = {
            prefix + spec.name.replace(".", "__"): spec
            for spec in payload.tools
            if spec.runtime_id is None
            and not spec.name.startswith(("sandbox.", "delivery.", "repo."))
            and spec.name not in {"plan.propose", "browser.verify"}
        }
        tools: list[Json] = [
            {"name": name, "description": spec.description, "inputSchema": spec.input_schema}
            for name, spec in specs.items()
        ]
        async with aiohttp.ClientSession(trust_env=False) as http:
            conn = NativeRpcConnection(
                process,
                omp=omp,
                model=model,
                http=http,
                call=self.call,
                progress=self.progress,
                diff=self.diff,
                tools={name: spec.name for name, spec in specs.items()},
                repositories=repositories,
                inputs_read=self._inputs_read,
                read_only=sandbox.read_only,
                authorize=self._authorize,
            )
            reader = asyncio.create_task(conn.read())
            stderr = asyncio.create_task(self._drain_stderr(process))
            try:
                await conn.send(
                    {
                        "channel": "setup",
                        "engine": "omp" if omp else "pi",
                        "provider": profile.provider,
                        "model": profile.model_id,
                        "model_base": model.route.base,
                        "model_api": model.route.api,
                        "session_id": str(sandbox.session_id),
                        "read_only": sandbox.read_only,
                        "system_prompt": payload.system_prompt,
                        "repositories": [
                            {"id": str(repo), "revision": archive.revision} for repo, archive in repositories.items()
                        ],
                        "skills": [package.slug for package in payload.skill_packages],
                        "extensions": list(sandbox.extensions),
                        "tools": tools,
                    }
                )
                await self._archive(conn.send, io.BytesIO(skills))
                for archive in repositories.values():
                    with archive.path.open("rb") as source:
                        await self._archive(conn.send, source, expected_digest=archive.sha256)
                await self._wait(conn, conn.ready if omp else conn.started)
                if omp:
                    if 2 in conn.omp_protocols:
                        negotiated = await conn.command("negotiate_protocol", protocolVersion=2)
                        if negotiated.get("protocolVersion") != 2:
                            raise RuntimeFailure("NATIVE_RPC_PROTOCOL_ERROR")
                        conn.omp_v2 = True
                    await conn.command(
                        "set_host_tools",
                        tools=[
                            {
                                "name": item["name"],
                                "description": item["description"],
                                "parameters": item["inputSchema"],
                                "loadMode": "essential",
                            }
                            for item in tools
                        ],
                    )
                if not await self.check_access():
                    raise RuntimeFailure("ACCESS_REVOKED")
                conn.prompt_id = uuid4().hex
                if self.session and self.session.initial_input_ids:
                    conn.initial_input = (payload.instruction, tuple(self.session.initial_input_ids))
                accepted = await conn.command("prompt", request_id=conn.prompt_id, message=payload.instruction)
                if accepted.get("disposition") == "handled" or accepted.get("agentInvoked") is False:
                    raise RuntimeFailure("NATIVE_PROMPT_NOT_STARTED")
                await self._turn(conn)
                await self._close(conn)
                return self._output(conn)
            except asyncio.CancelledError:
                # 尽量让引擎自己落盘。权限已撤销时产物回调拒绝写，任务卷仍保留会话。
                if conn.turn_started:
                    with suppress(RuntimeFailure, TimeoutError):
                        async with asyncio.timeout(4):
                            if not omp:
                                await conn.command("clear_queue")
                            await conn.command("abort_and_restore_queue" if omp else "abort")
                            await self._close(conn)
                raise
            except TimeoutError:
                raise RuntimeFailure("NATIVE_RPC_TIMEOUT") from None
            finally:
                await conn.cancel_requests()
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await self._stop(process, owner)
                await stderr

    @staticmethod
    def _output(conn: NativeRpcConnection) -> str:
        """原生结束事件只代表循环停止；截断和空白结果不能升级为运行成功。"""
        if conn.stop_reason in {"error", "aborted"}:
            # 模型 HTTP 原因已由代理脱敏，不能把付费/鉴权失败再次压成泛化的 MODEL_FAILED。
            raise RuntimeFailure(
                "MODEL_ABORTED" if conn.stop_reason == "aborted" else conn.model.failure_code or "MODEL_FAILED"
            )
        if conn.stop_reason == "length":
            raise RuntimeFailure("MODEL_TRUNCATED")
        text = conn.text.strip() or conn.report_summary
        if not text:
            raise RuntimeFailure("EMPTY_RESPONSE")
        return text

    async def _turn(self, conn: NativeRpcConnection) -> None:
        sent: set[str] = set()
        while not conn.settled.is_set() or (conn.omp and not conn.prompt_result.is_set()):
            if conn.failure:
                raise conn.failure
            if not await self.check_access():
                raise RuntimeFailure("ACCESS_REVOKED")
            await self._ack(conn)
            if self.session and self.session.pull:
                for identity, message in await self.session.pull():
                    if identity in sent or conn.settled.is_set():
                        continue
                    conn.admitted.setdefault(message, []).append(identity)
                    await conn.command("steer", message=message)
                    sent.add(identity)
            await asyncio.sleep(0.25)
        await self._ack(conn)

    async def _ack(self, conn: NativeRpcConnection) -> None:
        acknowledged = []
        while not conn.acknowledged.empty():
            acknowledged.append(conn.acknowledged.get_nowait())
        if acknowledged:
            await self._inputs_read(acknowledged)

    async def _authorize(self) -> bool:
        """每次把历史送往模型前重验来源代次，撤权不能只清平台文本而继续使用原生日志。"""
        try:
            if not await self.check_access():
                return False
            if self.session and self.session.pull:
                # pull 不确认已读；这里只触发会话租约/历史代次检查，输入仍由正常轮询送达。
                await self.session.pull()
        except RuntimeFailure:
            # 保留策略/历史换代原因，让编排器创建安全的新会话并续接未完成目标。
            raise
        except Exception:
            # 无法证明当前授权时停止外发，不把应用层异常遗留在后台任务中。
            return False
        return True

    async def _inputs_read(self, identities: list[str]) -> None:
        if self.session and self.session.checkpoint:
            # 原生历史由引擎落盘；平台检查点只更新输入回执，保留已有审计消息。
            await self.session.checkpoint(self.session.messages, identities)

    @staticmethod
    async def _archive(send: WriteFrame, source: io.BufferedIOBase, *, expected_digest: str | None = None) -> None:
        digest = hashlib.sha256()
        while data := source.read(64 * 1024):
            digest.update(data)
            await send({"channel": "archive_chunk", "data": b64encode(data).decode()})
        if expected_digest and digest.hexdigest() != expected_digest:
            raise RuntimeFailure("REPOSITORY_SNAPSHOT_INTEGRITY_FAILED")
        await send({"channel": "archive_end"})

    @staticmethod
    async def _wait(conn: NativeRpcConnection, event: asyncio.Event) -> None:
        async with asyncio.timeout(90):
            while not event.is_set():
                if conn.failure:
                    raise conn.failure
                await asyncio.sleep(0.05)

    async def _close(self, conn: NativeRpcConnection) -> None:
        await conn.send({"channel": "close"})
        await self._wait(conn, conn.closed)
        if await conn.process.wait() != 0:
            raise RuntimeFailure("NATIVE_RPC_PROCESS_FAILED")

    async def _stop(self, process: asyncio.subprocess.Process, owner: str) -> None:
        if process.stdin:
            process.stdin.close()
        if process.returncode is None:
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 3)
        if process.returncode is None:
            # Docker 的附着客户端退出不代表容器已停止。只清理本次租约启动的容器 ID。
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if process.returncode != 0:
            await self._remove_owned(owner)

    async def _remove_owned(self, owner: str) -> None:
        async def docker(*args: str) -> bytes:
            child = await asyncio.create_subprocess_exec(
                *self.sandbox.command,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(child.communicate(), 5)
                return stdout if child.returncode == 0 else b""
            finally:
                if child.returncode is None:
                    with suppress(ProcessLookupError):
                        child.kill()
                    await child.wait()

        with suppress(OSError, TimeoutError):
            found = await docker("ps", "-aq", "--no-trunc", "--filter", f"label=weora.rpc-owner={owner}")
            for identity in found.decode("ascii", errors="ignore").splitlines():
                if re.fullmatch(r"[0-9a-f]{64}", identity):
                    await docker("rm", "-f", identity)

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
        """原生进程的 stderr 可能含仓库或模型数据，仅记录是否有诊断输出。"""
        assert process.stderr is not None
        seen = False
        while await process.stderr.read(8192):
            seen = True
        if seen and process.returncode:
            _LOG.warning("原生 RPC 进程退出并产生诊断输出，退出码 %s", process.returncode)
