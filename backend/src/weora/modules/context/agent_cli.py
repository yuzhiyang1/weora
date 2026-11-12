"""执行官方飞书 CLI；真实 Bot 令牌只由本地请求代理持有。"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import re
import secrets
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import aiohttp
from aiohttp import web
from yarl import URL

from weora.modules.context.domain import ContextError, Json
from weora.modules.context.feishu_cli import FeishuCliProcess
from weora.modules.tools.application.gateway import ToolSpec, failure

_FILENAME = r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$"
_TRANSFER_LIMIT = 96_000
_FEISHU_ORIGIN = "https://open.feishu.cn"

FEISHU_AGENT_CLI = ToolSpec(
    "lark.cli.exec",
    "用当前飞书群绑定的 Bot 执行官方 lark-cli 原生命令。argv 不含 lark-cli 本身。"
    "可用 lark.cli.skill 查官方 Skill，或用 --help、领域 --help 和 schema 查命令。只允许读取飞书 OpenAPI。"
    "例如读电子表格可先查 argv=['sheets','+cells-get','--help']，再读取任务给出的链接。"
    "stdin 可传原始文本，files 可传当前命令所需的 Base64 文件，output_files 可取回指定文件。",
    {
        "type": "object",
        "required": ["argv"],
        "additionalProperties": False,
        "properties": {
            "argv": {"type": "array", "minItems": 1, "maxItems": 128, "items": {"type": "string", "maxLength": 32000}},
            "stdin": {"type": "string", "maxLength": 32000},
            "files": {
                "type": "object",
                "maxProperties": 8,
                "propertyNames": {"pattern": _FILENAME},
                "additionalProperties": {"type": "string", "maxLength": 48000},
            },
            "output_files": {
                "type": "array",
                "maxItems": 8,
                "uniqueItems": True,
                "items": {"type": "string", "pattern": _FILENAME},
            },
        },
    },
    # 保留既有缓存和恢复边界。旧调用的回执可能来自修复前的写操作。
    effect="write",
    timeout_seconds=40,
)


class FeishuAgentCli:
    """使用官方 authsidecar 协议和独立文件系统执行单次命令。"""

    def __init__(self, executable: str) -> None:
        path = Path(executable)
        if not path.is_absolute() or not path.is_file() or not Path("/usr/bin/bwrap").is_file():
            raise ValueError("Agent 飞书 CLI 需要原生 CLI 绝对路径和 Linux bubblewrap")
        self.executable = str(path.resolve())

    async def execute(self, arguments: Json, app_id: str, token: Callable[[], Awaitable[str]]) -> Json:
        argv = cast(list[str], arguments["argv"])
        if any("\0" in value for value in argv):
            return failure("TOOL_ARGUMENTS_INVALID")
        if argv[0] in {"auth", "config", "update"}:
            # 运行身份来自 Weora 连接。CLI 不管理宿主机账户、凭据或安装版本。
            return failure("FEISHU_CLI_MANAGED_BY_WEORA")

        proxy = _BotProxy(secrets.token_hex(32), token)
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=30)) as session:
                server = web.Server(
                    lambda request: proxy.forward(request, session), access_log=None, handler_cancellation=True
                )
                runner = web.ServerRunner(server, shutdown_timeout=1)
                await runner.setup()
                try:
                    await web.SockSite(runner, listener).start()
                    with tempfile.TemporaryDirectory(prefix="weora-lark-agent-") as directory:
                        return await self._run(Path(directory), arguments, app_id, proxy, listener.getsockname()[1])
                finally:
                    await runner.cleanup()
        finally:
            listener.close()

    async def _run(self, directory: Path, arguments: Json, app_id: str, proxy: "_BotProxy", port: int) -> Json:
        try:
            for name, encoded in cast(dict[str, str], arguments.get("files", {})).items():
                (directory / name).write_bytes(base64.b64decode(encoded, validate=True))
        except ValueError:
            return failure("TOOL_ARGUMENTS_INVALID")
        environment = FeishuCliProcess._environment("/work")
        environment.pop("SSL_CERT_FILE", None)
        environment.pop("SSL_CERT_DIR", None)
        environment.update(
            {
                "LARKSUITE_CLI_APP_ID": app_id,
                "LARKSUITE_CLI_AUTH_PROXY": f"http://127.0.0.1:{port}",
                "LARKSUITE_CLI_PROXY_KEY": proxy.key,
            }
        )
        # 不挂载宿主机 HOME、代码仓库或 /proc。文件参数只能访问本次工具的临时目录。
        command = [
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-all",
            "--share-net",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            self.executable,
            "/lark-cli",
            "--ro-bind-try",
            "/etc/resolv.conf",
            "/etc/resolv.conf",
            "--ro-bind-try",
            "/etc/ssl/certs",
            "/etc/ssl/certs",
            "--dev",
            "/dev",
            "--bind",
            str(directory),
            "/work",
            "--chdir",
            "/work",
            "--",
            "/lark-cli",
            *cast(list[str], arguments["argv"]),
        ]
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            await self._stop(process)
            raise
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(FeishuCliProcess._read(process.stdout, 96_000))
        stderr_task = asyncio.create_task(FeishuCliProcess._read(process.stderr, 12_000))
        tasks = (
            stdout_task,
            stderr_task,
            asyncio.create_task(self._stdin(process, str(arguments.get("stdin", "")))),
            asyncio.create_task(process.wait()),
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._stop(process)
        if proxy.requires_write_approval:
            return failure("FEISHU_CLI_WRITE_REQUIRES_APPROVAL")
        result: Json = {"ok": process.returncode == 0, "exit_code": process.returncode}
        for name, value in (("stdout", stdout_task.result()), ("stderr", stderr_task.result())):
            if value:
                result[name] = value.decode("utf-8", errors="replace").replace(proxy.key, "[redacted]")
        if proxy.log_id:
            result["feishu_log_id"] = proxy.log_id
        if process.returncode:
            result["error"] = {"code": "FEISHU_CLI_EXIT"}
        artifacts = {}
        total = 0
        for name in cast(list[str], arguments.get("output_files", [])):
            path = directory / name
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as stream:
                    data = stream.read(_TRANSFER_LIMIT - total + 1)
                total += len(data)
                if total > _TRANSFER_LIMIT:
                    return failure("TOOL_OUTPUT_LIMIT")
                artifacts[name] = base64.b64encode(data).decode()
        if artifacts:
            result["files"] = artifacts
        return result

    @staticmethod
    async def _stdin(process: asyncio.subprocess.Process, text: str) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(text.encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        assert process.stdout is not None and process.stderr is not None
        await asyncio.gather(
            process.wait(), FeishuCliProcess._discard(process.stdout), FeishuCliProcess._discard(process.stderr)
        )


class _BotProxy:
    """按官方 v1 协议核验 CLI 请求，仅在飞书出站连接中注入 Bot 令牌。"""

    def __init__(self, key: str, token: Callable[[], Awaitable[str]]) -> None:
        self.key, self.token = key, token
        self.log_id: str | None = None
        self.requires_write_approval = False

    async def _read_signed_body(self, request: web.BaseRequest) -> bytes:
        """核验飞书目标、签名时效与正文完整性，供出站转发使用。"""
        headers = request.headers
        origin = headers.get("X-Lark-Proxy-Target", "")
        auth_header = "Authorization"
        identity = headers.get("X-Lark-Proxy-Identity")
        version = headers.get("X-Lark-Proxy-Version")
        timestamp = headers.get("X-Lark-Proxy-Timestamp", "")
        if (
            origin != _FEISHU_ORIGIN
            or headers.get("X-Lark-Proxy-Auth-Header") != auth_header
            or identity != "bot"
            or version != "v1"
            or not re.fullmatch(r"[0-9]{10}", timestamp)
            or abs(time.time() - int(timestamp)) > 60
            or not request.raw_path.startswith("/")
        ):
            raise web.HTTPForbidden()
        # 只转发已签名请求，且限制入站正文；不能把代理当通用带凭据 HTTP 客户端。
        body = await request.read()
        digest = hashlib.sha256(body).hexdigest()
        canonical = "\n".join(
            (
                version,
                request.method,
                origin.removeprefix("https://"),
                request.raw_path,
                digest,
                timestamp,
                identity,
                auth_header,
            )
        )
        signature = hmac.new(self.key.encode(), canonical.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(signature, headers.get("X-Lark-Proxy-Signature", "")):
            raise web.HTTPForbidden()
        return body

    async def forward(self, request: web.BaseRequest, session: aiohttp.ClientSession) -> web.StreamResponse:
        """校验 CLI 签名和只读权限，再用群绑定的 Bot 令牌转发并流式返回响应。"""
        body = await self._read_signed_body(request)

        # 快捷命令和原始 API 共用代理。只有已签名的实际请求才能触发写审批提示。
        if request.method not in {"GET", "HEAD"}:
            self.requires_write_approval = True
            raise web.HTTPForbidden(text="FEISHU_CLI_WRITE_REQUIRES_APPROVAL")
        if not request.path.startswith("/open-apis/"):
            raise web.HTTPForbidden()

        # 通过全部策略后才取令牌。禁止重定向，令牌仅发送给已核验的飞书目标。
        try:
            token = await self.token()
            out_headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() in {"content-type", "accept", "user-agent", "range", "x-request-id", "x-lark-request-id"}
                or key.lower().startswith("x-cli-")
            }
            out_headers["Authorization"] = f"Bearer {token}"
            async with session.request(
                request.method,
                URL(_FEISHU_ORIGIN + request.raw_path, encoded=True),
                data=body or None,
                headers=out_headers,
                allow_redirects=False,
            ) as upstream:
                self.log_id = upstream.headers.get("X-Tt-Logid")
                response = web.StreamResponse(
                    status=upstream.status,
                    headers={
                        key: value
                        for key, value in upstream.headers.items()
                        if key.lower() in {"content-type", "content-disposition", "x-tt-logid", "retry-after"}
                    },
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ContextError, aiohttp.ClientError, TimeoutError):
            raise web.HTTPBadGateway() from None
