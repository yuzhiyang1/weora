"""CLI 进程边界和适配器契约回归；测试不使用本机账号或真实飞书网络。"""

import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiohttp
import pytest
from cryptography.fernet import Fernet

from tests.context.test_infrastructure import TokenReader
from tests.support.settings import IsolatedAppSettings
from weora.modules.context.domain import ContextError, Json
from weora.modules.context.feishu import FernetSecrets
from weora.modules.context.feishu_cli import FeishuCliProcess, FeishuCliReader

FIXTURE = Path(__file__).parents[1] / "fixtures" / "feishu_cli.py"


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FeishuCliProcess:
    """只在测试中把原生入口换成 Python 夹具；生产没有自由 argv 扩展。"""
    original = asyncio.create_subprocess_exec

    async def launch(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        # kwargs 的调用签名由标准库定义；测试在原生进程边界插入固定夹具。
        return await original(args[0], str(FIXTURE), *args[1:], **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    return FeishuCliProcess(sys.executable)


def test_process_uses_fixed_bot_argv_and_isolated_environment(
    cli: FeishuCliProcess, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "LARKSUITE_CLI_USER_ACCESS_TOKEN", "LARKSUITE_CLI_AUTH_PROXY", "LARKSUITE_CLI_PROFILE",
        "WEORA_DATABASE_URL", "HTTP_PROXY", "OPENCLAW_HOME", "LARKSUITE_CLI_APP_SECRET",
    ):
        monkeypatch.setenv(key, "private-token-do-not-leak")

    async def scenario() -> None:
        result = await cli.get("cli_test", "short-token", "/im/v1/messages", {"page_token": "x & --as user"})
        data = cast(Json, result["data"])
        invocation = cast(Json, data["invocation"])
        assert invocation["argv"] == [
            "api", "GET", "/open-apis/im/v1/messages", "--as", "bot", "--format", "json", "--params", "-",
        ]
        assert invocation["parameters"] == {"page_token": "x & --as user"}
        assert invocation["brand"] == "feishu" and invocation["strict_mode"] == "bot"
        assert invocation["has_token"] is True
        assert invocation["cwd"] == invocation["config_dir"]
        assert not await asyncio.to_thread(Path(str(invocation["cwd"])).exists)
        environment = cast(list[str], invocation["environment"])
        assert "LARKSUITE_CLI_USER_ACCESS_TOKEN" not in environment
        assert "LARKSUITE_CLI_APP_SECRET" not in environment
        assert "WEORA_DATABASE_URL" not in environment and "HTTP_PROXY" not in environment
        assert "short-token" not in repr(result) and "private-token-do-not-leak" not in repr(result)
    asyncio.run(scenario())


@pytest.mark.parametrize("mode, expected", [
    ("cli_permission", "PERMISSION_DENIED"), ("cli_expired", "TOKEN_EXPIRED"),
    ("cli_rate", "RATE_LIMITED"), ("cli_broken_error", "PROVIDER_UNAVAILABLE"),
    ("cli_malformed", "PROVIDER_UNAVAILABLE"), ("cli_wrong_identity", "PROVIDER_UNAVAILABLE"),
    ("cli_false_ok", "PROVIDER_UNAVAILABLE"), ("cli_oversize", "PROVIDER_UNAVAILABLE"),
    ("cli_stderr_oversize", "PROVIDER_UNAVAILABLE"),
])
def test_process_rejects_errors_and_never_echoes_provider_details(
    cli: FeishuCliProcess, mode: str, expected: str,
) -> None:
    async def scenario() -> None:
        with pytest.raises(ContextError) as error:
            await cli.get(mode, "short-token", "/im/v1/messages", {})
        assert error.value.code == expected
        assert "private-token" not in str(error.value) and "short-token" not in str(error.value)
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_reap_process_and_delete_temp_directory(
    cli: FeishuCliProcess, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    async def scenario() -> None:
        processes: list[asyncio.subprocess.Process] = []
        directories: list[str] = []
        started = asyncio.Event()
        original = asyncio.create_subprocess_exec

        async def capture(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
            process = await original(*args, **kwargs)  # type: ignore[arg-type]
            processes.append(process)
            directories.append(str(kwargs["cwd"]))
            started.set()
            return process

        await cli.verify()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        cli.timeout = 0.2
        task = asyncio.create_task(cli.get("cli_slow", "short-token", "/im/v1/messages", {}))
        if cancel:
            await started.wait()
            await asyncio.sleep(0.02)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ContextError):
            await task
        assert processes and all(process.returncode is not None for process in processes)
        for directory in directories:
            assert not await asyncio.to_thread(Path(directory).exists)
    asyncio.run(scenario())


def test_version_mismatch_and_arbitrary_paths_fail_closed(
    cli: FeishuCliProcess, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        for path in (
            "https://attacker.invalid", "/im/v1/messages/om_one/resources/file", "/im/v1/chats",
            "/im/v1/messages/mget",
        ):
            with pytest.raises(ContextError) as error:
                await cli.get("cli_test", "short-token", path, {})
            assert error.value.code == "INVALID_PARAMETERS"
        monkeypatch.setattr("weora.modules.context.feishu_cli.CLI_VERSION", "999.0")
        with pytest.raises(ContextError, match="999.0"):
            await cli.verify()
    asyncio.run(scenario())


def test_cli_reader_keeps_parser_and_paging_contract(cli: FeishuCliProcess) -> None:
    async def scenario() -> None:
        cipher = FernetSecrets(Fernet.generate_key().decode())
        connection: Json = {"id": str(uuid4()), "app_id": "cli_test", "secret": cipher.encrypt("secret"), "version": 1}
        async with aiohttp.ClientSession() as session:
            reader = FeishuCliReader(session, cipher, cli)
            # 复用既有单飞 token 夹具，不允许本测试访问飞书网络。
            tokens = TokenReader(session, cipher)
            reader._token = tokens._token  # type: ignore[method-assign]
            now = datetime.now(UTC)
            page = await reader.messages(connection, chat_id="oc_project", start=now, end=now,
                                         thread_id="omt_one", page_token="next", descending=True)
            assert page.next_token == "opaque-next"
            assert page.items[0].parse_status == "partial"
            assert page.items[0].resources[0]["file_name"] == "plan.txt"
            message = await reader.message(connection, "om_one")
            assert message == page.items[0]
            assert message.author_id == "ou_author" and message.chat_id == "oc_project"
            with pytest.raises(ContextError):
                await reader.messages(dict(connection, app_id="cli_bad_page"), chat_id="oc_project",
                                      start=now, end=now, page_token=None)
    asyncio.run(scenario())


def test_reader_preserves_time_window_order_and_single_page_budget(
    cli: FeishuCliProcess, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        calls: list[tuple[str, dict[str, str] | None]] = []

        async def read(app_id: str, token: str, path: str, params: dict[str, str] | None) -> Json:
            calls.append((path, params))
            return {"data": {"items": [], "has_more": False}}

        monkeypatch.setattr(cli, "get", read)
        cipher = FernetSecrets(Fernet.generate_key().decode())
        async with aiohttp.ClientSession() as session:
            reader = FeishuCliReader(session, cipher, cli)
            tokens = TokenReader(session, cipher)
            reader._token = tokens._token  # type: ignore[method-assign]
            connection: Json = {"id": str(uuid4()), "app_id": "cli_test", "version": 1,
                                "secret": cipher.encrypt("secret")}
            now = datetime.now(UTC)
            await reader.messages(connection, chat_id="oc_project", start=now, end=now,
                                  page_token="next", descending=True)
            assert calls[0] == ("/im/v1/messages", {
                "container_id_type": "chat", "container_id": "oc_project", "page_size": "50",
                "sort_type": "ByCreateTimeDesc", "start_time": str(int(now.timestamp())),
                "end_time": str(int(now.timestamp()) + 1), "page_token": "next",
            })
            await reader.messages(connection, chat_id="oc_project", start=now, end=now,
                                  page_token=None, thread_id="omt_one")
            assert calls[1] == ("/im/v1/messages", {
                "container_id_type": "thread", "container_id": "omt_one",
                "page_size": "50", "sort_type": "ByCreateTimeAsc",
            })
            assert len(calls) == 2
    asyncio.run(scenario())


def test_cancellation_during_spawn_still_reaps_child(
    cli: FeishuCliProcess, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        await cli.verify()
        original = asyncio.create_subprocess_exec
        started, release = asyncio.Event(), asyncio.Event()
        processes: list[asyncio.subprocess.Process] = []

        async def pause_launch(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
            process = await original(*args, **kwargs)  # type: ignore[arg-type]
            processes.append(process)
            started.set()
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", pause_launch)
        task = asyncio.create_task(cli.get("cli_slow", "short-token", "/im/v1/messages", {}))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
    asyncio.run(scenario())


@pytest.mark.parametrize("failure, retries", [("TOKEN_EXPIRED", 1), ("PERMISSION_DENIED", 0)])
def test_token_refresh_is_bounded_and_other_failures_never_fall_back(
    cli: FeishuCliProcess, monkeypatch: pytest.MonkeyPatch, failure: str, retries: int,
) -> None:
    async def scenario() -> None:
        cipher = FernetSecrets(Fernet.generate_key().decode())
        received: list[str] = []

        async def read(app_id: str, token: str, path: str, params: dict[str, str] | None) -> Json:
            received.append(token)
            raise ContextError(failure)

        monkeypatch.setattr(cli, "get", read)
        async with aiohttp.ClientSession() as session:
            tokens = TokenReader(session, cipher)
            reader = FeishuCliReader(session, cipher, cli)
            reader._token = tokens._token  # type: ignore[method-assign]
            connection: Json = {"id": str(uuid4()), "app_id": "cli_test", "version": 1,
                                "secret": cipher.encrypt("secret")}
            with pytest.raises(ContextError) as error:
                await reader.message(connection, "om_one")
            assert error.value.code == failure
            assert received == [f"t-{number}" for number in range(1, retries + 2)]
            assert tokens.auth_calls == retries + 1
            assert all(request["path"] == "/auth/v3/tenant_access_token/internal" for request in tokens.requests)
    asyncio.run(scenario())


def test_cli_reader_does_not_replace_http_probes_or_replies(cli: FeishuCliProcess) -> None:
    async def scenario() -> None:
        cipher = FernetSecrets(Fernet.generate_key().decode())
        async with aiohttp.ClientSession() as session:
            reader = FeishuCliReader(session, cipher, cli)
            tokens = TokenReader(session, cipher)
            reader._token = tokens._token  # type: ignore[method-assign]
            reader._json = tokens._json  # type: ignore[method-assign]
            connection: Json = {"id": str(uuid4()), "app_id": "cli_test", "version": 1,
                                "secret": cipher.encrypt("secret")}
            tokens.response = {"bot": {"open_id": "ou_bot"}, "data": {"message_id": "om_reply"}}
            assert (await reader.probe(connection))["bot_open_id"] == "ou_bot"
            assert await reader.reply(connection, "om_origin", "result", "business-key") == "om_reply"
            assert tokens.requests[-1]["method"] == "POST"
            assert tokens.requests[-1]["path"] == "/im/v1/messages/om_origin/reply"
    asyncio.run(scenario())


def test_managed_reader_selects_cli_and_keeps_http_rollback() -> None:
    # 装配入口会加载其他领域的 ORM；独立进程避免污染单独运行的 Context 存储测试。
    subprocess.run(
        [sys.executable, str(FIXTURE.with_name("feishu_cli_bootstrap.py"))],
        check=True, capture_output=True, timeout=10,
    )


def test_cli_requires_explicit_configuration_and_rejects_shell_wrappers(tmp_path: Path) -> None:
    assert IsolatedAppSettings().feishu_reader_backend == "http"
    with pytest.raises(ValueError, match="绝对路径"):
        IsolatedAppSettings(feishu_reader_backend="cli")
    for executable in ("lark-cli", str(tmp_path / "missing.exe"), str(FIXTURE.with_suffix(".ps1"))):
        with pytest.raises(ValueError):
            FeishuCliProcess(executable)
