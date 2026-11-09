"""官方 CLI 的受限只读适配；不向模型暴露命令行，也不读取宿主机个人账号。"""

import asyncio
import contextlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import cast

import aiohttp

from weora.modules.context.domain import ContextError, Json, SecretCipher
from weora.modules.context.feishu import FeishuHttpReader

CLI_VERSION = "1.0.80"
MESSAGE_PATH = "/im/v1/messages"


class FeishuCliProcess:
    """每次调用使用独立临时目录和环境；只接受管理员指定的原生二进制。"""

    def __init__(self, executable: str, *, timeout: float = 2.5) -> None:
        path = Path(executable)
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.suffix.lower() in {".ps1", ".cmd", ".bat", ".sh"}
            or (os.name == "nt" and path.suffix.lower() != ".exe")
        ):
            raise ValueError("飞书 CLI 必须配置已安装原生二进制的绝对路径，不能使用 shell 包装器")
        if not 0 < timeout <= 2.5:
            raise ValueError("飞书 CLI 超时必须大于 0 且不超过 2.5 秒")
        self.executable, self.timeout = str(path.resolve()), timeout
        self._verified = False
        self._version_lock = asyncio.Lock()

    @staticmethod
    def _environment(directory: str) -> dict[str, str]:
        # 白名单而非复制 os.environ：不继承个人 token、代理、调试开关或 Weora 数据库密钥。
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        }
        environment.update(
            {
                "HOME": directory,
                "USERPROFILE": directory,
                "APPDATA": directory,
                "LOCALAPPDATA": directory,
                "XDG_CONFIG_HOME": directory,
                "XDG_CACHE_HOME": directory,
                "TMPDIR": directory,
                "TEMP": directory,
                "TMP": directory,
                "LARKSUITE_CLI_CONFIG_DIR": directory,
                "LARKSUITE_CLI_BRAND": "feishu",
                "LARKSUITE_CLI_DEFAULT_AS": "bot",
                "LARKSUITE_CLI_STRICT_MODE": "bot",
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
                "LARKSUITE_CLI_PROXY_ENABLE": "false",
            }
        )
        return environment

    @staticmethod
    async def _read(stream: asyncio.StreamReader, limit: int) -> bytes:
        content = bytearray()
        while chunk := await stream.read(65536):
            content.extend(chunk)
            if len(content) > limit:
                raise ContextError("PROVIDER_UNAVAILABLE", "飞书 CLI 响应超出大小限制")
        return bytes(content)

    @staticmethod
    async def _discard(stream: asyncio.StreamReader) -> None:
        # 进程被杀后仍需排空已写入管道的数据；只丢弃，不再次缓存超限正文。
        while await stream.read(65536):
            pass

    async def _run(
        self,
        arguments: tuple[str, ...],
        *,
        app_id: str | None = None,
        token: str | None = None,
        parameters: dict[str, str] | None = None,
    ) -> tuple[int, bytes, bytes]:
        with tempfile.TemporaryDirectory(prefix="weora-feishu-") as directory:
            environment = self._environment(directory)
            if token is not None and app_id is not None:
                # 只下放短期应用令牌。App Secret 不进入 CLI、argv 或临时文件。
                environment["LARKSUITE_CLI_APP_ID"] = app_id
                environment["LARKSUITE_CLI_TENANT_ACCESS_TOKEN"] = token
            process: asyncio.subprocess.Process | None = None
            tasks: list[asyncio.Task[object]] = []
            try:
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        self.executable,
                        *arguments,
                        cwd=directory,
                        env=environment,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                )
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    # 取消可能恰好发生在操作系统已启动、Python 尚未返回句柄的窗口。
                    process = await spawn
                    raise
                assert process.stdin is not None and process.stdout is not None and process.stderr is not None

                async def write_parameters() -> None:
                    assert process is not None and process.stdin is not None
                    if parameters is not None:
                        process.stdin.write(json.dumps(parameters).encode())
                        await process.stdin.drain()
                    process.stdin.close()

                stdout_task = asyncio.create_task(self._read(process.stdout, 2_000_000))
                stderr_task = asyncio.create_task(self._read(process.stderr, 65_536))
                tasks = [
                    cast(asyncio.Task[object], task)
                    for task in (
                        stdout_task,
                        stderr_task,
                        asyncio.create_task(write_parameters()),
                        asyncio.create_task(process.wait()),
                    )
                ]
                # 不用 communicate 无界缓存；任一管道超限立即终止原生 CLI。
                async with asyncio.timeout(self.timeout):
                    await asyncio.gather(*tasks)
                return process.returncode or 0, stdout_task.result(), stderr_task.result()
            except asyncio.CancelledError:
                raise
            except ContextError:
                raise
            except (OSError, TimeoutError):
                raise ContextError("PROVIDER_UNAVAILABLE", "飞书 CLI 不可用或读取超时") from None
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if process is not None and process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                if process is not None:
                    assert process.stdout is not None and process.stderr is not None
                    # Windows 的 wait 可能依赖管道 EOF，必须和排空并行，不能先 wait 再读。
                    await asyncio.gather(
                        process.wait(),
                        self._discard(process.stdout),
                        self._discard(process.stderr),
                    )

    async def verify(self) -> None:
        """固定版本契约；只做一次并发合并检查，不自动升级或下载。"""
        async with self._version_lock:
            if not self._verified:
                code, stdout, _ = await self._run(("--version",))
                if code != 0 or stdout.strip() != f"lark-cli version {CLI_VERSION}".encode():
                    raise ContextError("PROVIDER_UNAVAILABLE", f"飞书 CLI 版本必须为 {CLI_VERSION}")
                self._verified = True

    @staticmethod
    def _error(stderr: bytes) -> ContextError:
        # 不回显 CLI 的 message/hint：里面可能包含正文、命令参数、应用信息。
        try:
            envelope = json.loads(stderr)
            error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
            kind, subtype, code = error.get("type"), error.get("subtype"), error.get("code")
            if code in {99991663, 99991664, 99991668} or (
                kind == "authentication" and subtype in {"token_invalid", "token_expired"}
            ):
                return ContextError("TOKEN_EXPIRED", "应用访问凭证已失效")
            if kind == "authorization" or code in {
                99991672,
                99991679,
                230002,
                230006,
                230013,
                230027,
                230050,
                230073,
                231203,
            }:
                return ContextError("PERMISSION_DENIED", "应用缺少权限、未入群或消息不可见")
            if (kind == "api" and subtype == "rate_limit") or code == 429:
                return ContextError("RATE_LIMITED", "飞书接口限流，请稍后重试")
        except (ValueError, AttributeError, TypeError):
            pass
        return ContextError("PROVIDER_UNAVAILABLE", "飞书 CLI 读取失败，请检查连接与权限")

    async def get(self, app_id: str, token: str, path: str, params: dict[str, str] | None) -> Json:
        """只允许历史/话题页及单消息 GET；不能执行发送、下载、搜索或任意 URL。"""
        if not re.fullmatch(r"/im/v1/messages(?:/om_[A-Za-z0-9_-]{1,125})?", path):
            raise ContextError("INVALID_PARAMETERS", "飞书 CLI 不允许访问此资源")
        await self.verify()
        code, stdout, stderr = await self._run(
            ("api", "GET", "/open-apis" + path, "--as", "bot", "--format", "json", "--params", "-"),
            app_id=app_id,
            token=token,
            parameters=params or {},
        )
        if code != 0:
            raise self._error(stderr)
        try:
            envelope = json.loads(stdout)
            if not isinstance(envelope, dict) or envelope.get("ok") is not True or envelope.get("identity") != "bot":
                raise ValueError
            # CLI 成功外层使用 ok，而非 OpenAPI 的 code。data 已由 CLI 解包。
            data = envelope.get("data")
            if not isinstance(data, dict):
                raise ValueError
            return {"data": data}
        except (ValueError, TypeError):
            raise ContextError("PROVIDER_UNAVAILABLE", "飞书 CLI 返回格式或身份不符合约定") from None


class FeishuCliReader(FeishuHttpReader):
    """复用既有正文解析/分页校验；只替换读消息传输，探测和可靠回复维持 HTTP。"""

    def __init__(self, session: aiohttp.ClientSession, cipher: SecretCipher, cli: FeishuCliProcess) -> None:
        super().__init__(session, cipher)
        self.cli = cli

    async def _get(self, connection: Json, path: str, params: dict[str, str] | None = None) -> Json:
        if path != MESSAGE_PATH and not path.startswith(MESSAGE_PATH + "/"):
            return await super()._get(connection, path, params)
        token = await self._token(connection)
        try:
            return await self.cli.get(str(connection["app_id"]), token, path, params)
        except ContextError as error:
            if error.code != "TOKEN_EXPIRED":
                raise
            # 仅失效凭证刷新一次，不对权限失败或超时偷偷切回 HTTP、扩大预算。
            token = await self._token(connection, force=True, rejected=token)
            return await self.cli.get(str(connection["app_id"]), token, path, params)
