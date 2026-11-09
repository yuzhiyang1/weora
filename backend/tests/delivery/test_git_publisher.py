"""通用发布器的真实 Git 临时裸库验证；网络仅在测试替身中映射为本地夹具。"""

import asyncio
import base64
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from tests.delivery.test_code_hosting import connection
from tests.delivery.test_service import scope
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.git import (
    CliGitPublisher,
    ConfiguredCredentials,
    CredentialMaterial,
    IsolatedGitRunner,
    git_environment,
)


def credentials() -> ConfiguredCredentials:
    return ConfiguredCredentials(
        {
            "test-credential-v1": SecretStr(
                json.dumps(
                    {
                        "username": "test-user",
                        "password": "test-secret",
                        "api_token": "test-api-token",
                    }
                )
            )
        }
    )


class Changes:
    def __init__(self) -> None:
        self.changes: list[dict[str, str | None]] = [
            {"path": "src/a.py", "content": base64.b64encode(b"fixed\n").decode()},
            {"path": "tests/old.py", "content": None},
            {"path": "src/new.txt", "content": base64.b64encode(b"new\n").decode()},
        ]

    async def export_changes(self, run_id: UUID, scope: PlanScope) -> list[dict[str, str | None]]:
        return self.changes


class LocalGitTransport:
    """仅测试开放 file 协议；生产发布器从未允许 file，也不接受模型指定的 URL。"""

    def __init__(self, remote: Path, expected_url: str) -> None:
        self.remote, self.expected_url = remote, expected_url
        self.actual = IsolatedGitRunner()
        self.calls: list[tuple[str, ...]] = []
        self.directories: list[Path] = []

    async def run(self, cwd: Path, env: Mapping[str, str], *args: str, stdin: bytes | None = None) -> bytes:
        self.calls.append(args)
        self.directories.append(cwd)
        assert "test-secret" not in str(args)
        if args[0] in {"fetch", "push", "ls-remote"}:
            assert self.expected_url in args
            args = tuple(str(self.remote) if arg == self.expected_url else arg for arg in args)
            env = dict(env) | {"GIT_ALLOW_PROTOCOL": "file"}
        return await self.actual.run(cwd, env, *args, stdin=stdin)


async def bare_fixture(path: Path) -> str:
    await asyncio.to_thread(path.mkdir)
    env = git_environment(path, connection(), credentials().get("test-credential-v1"))
    git = IsolatedGitRunner()
    await git.run(path, env, "init", "--bare", "--quiet")
    for name in ("src/a.py", "tests/old.py"):
        blob = (await git.run(path, env, "hash-object", "-w", "--stdin", stdin=b"original\n")).decode().strip()
        await git.run(path, env, "update-index", "--add", "--cacheinfo", "100755", blob, name)
    tree = (await git.run(path, env, "write-tree")).decode().strip()
    revision = (await git.run(path, env, "commit-tree", tree, stdin=b"fixture base\n")).decode().strip()
    await git.run(path, env, "update-ref", "refs/heads/main", revision)
    return revision


@pytest.mark.anyio
async def test_real_git_publish_preserves_base_mode_and_target_and_refuses_overwrite(tmp_path: Path) -> None:
    target = connection()
    remote = tmp_path / "remote.git"
    revision = await bare_fixture(remote)
    approved = replace(scope(remote=True), base_revision=revision, remote_binding=target.fingerprint)
    transport = LocalGitTransport(remote, target.clone_url)
    publisher = CliGitPublisher(Changes(), credentials(), transport)
    published = await publisher.publish(uuid4(), approved, target, "d" * 64, "修复夹具")
    assert published.name == "weora/" + "d" * 24
    git, env = IsolatedGitRunner(), git_environment(remote, target, credentials().get(target.credential_ref))
    assert (await git.run(remote, env, "rev-parse", "main")).decode().strip() == revision
    assert (await git.run(remote, env, "rev-parse", published.revision + "^1")).decode().strip() == revision
    assert await git.run(remote, env, "show", published.revision + ":src/a.py") == b"fixed\n"
    assert (await git.run(remote, env, "ls-tree", "-r", published.revision)).decode().find("tests/old.py") == -1
    mode = (await git.run(remote, env, "ls-tree", published.revision, "src/a.py")).decode()
    assert mode.startswith("100755")
    message = await git.run(remote, env, "show", "-s", "--format=%B", published.revision)
    assert ("weora-delivery:" + "d" * 64).encode() in message
    # 再发布不同提交到同一业务分支应拒绝，不能强推覆盖已存在的结果。
    with pytest.raises(DeliveryError) as error:
        await publisher.publish(uuid4(), approved, target, "d" * 64, "different fixture title")
    assert error.value.code == "GIT_OPERATION_FAILED"
    assert all(not directory.exists() for directory in transport.directories)
    assert not any(call[0] in {"checkout", "merge", "reset"} for call in transport.calls)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["path", "base64", "empty", "binding", "actions"])
async def test_invalid_diff_or_authorization_never_starts_git(tmp_path: Path, kind: str) -> None:
    target, changes = connection(), Changes()
    approved = replace(scope(remote=True), remote_binding=target.fingerprint)
    if kind == "path":
        changes.changes = [{"path": "outside.py", "content": None}]
    elif kind == "base64":
        changes.changes[0]["content"] = "not base64!"
    elif kind == "empty":
        changes.changes = []
    elif kind == "binding":
        approved = replace(approved, remote_binding="b" * 64)
    else:
        approved = replace(approved, actions=("pr.create",))
    runner = LocalGitTransport(tmp_path / "not-created.git", target.clone_url)
    with pytest.raises(DeliveryError):
        await CliGitPublisher(changes, credentials(), runner).publish(uuid4(), approved, target, "d" * 64, "修复")
    assert not runner.calls


def test_git_environment_does_not_inherit_helpers_proxies_or_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_VALUE_0", "SSH_AUTH_SOCK", "HTTP_PROXY", "WEORA_TEST_SECRET"):
        monkeypatch.setenv(key, "must-not-leak")
    env = git_environment(tmp_path, connection(), credentials().get("test-credential-v1"))
    assert "must-not-leak" not in str(env)
    assert "HTTP_PROXY" not in env and "SSH_AUTH_SOCK" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert str(tmp_path) == env["HOME"]
    assert "test-secret" not in repr(credentials().get("test-credential-v1"))
    values = [env[f"GIT_CONFIG_VALUE_{index}"] for index in range(int(env["GIT_CONFIG_COUNT"]))]
    assert "false" in values and "" in values


def test_ssh_needs_explicit_identity_and_pinned_known_hosts(tmp_path: Path) -> None:
    target = replace(connection(), clone_url="ssh://git@git.example.test/team/project.git")
    with pytest.raises(DeliveryError):
        git_environment(tmp_path, target, CredentialMaterial())
    key, hosts = tmp_path / "fixture-key", tmp_path / "fixture-hosts"
    key.touch()
    hosts.touch()
    env = git_environment(tmp_path, target, CredentialMaterial(ssh_key_path=str(key), known_hosts_path=str(hosts)))
    assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]
    assert "IdentitiesOnly=yes" in env["GIT_SSH_COMMAND"]


def test_missing_or_bad_credentials_are_redacted() -> None:
    for configured in ({}, {"missing": SecretStr("secret-not-json")}):
        with pytest.raises(DeliveryError) as error:
            ConfiguredCredentials(configured).get("missing")
        assert "secret-not-json" not in str(error.value)


@pytest.mark.anyio
async def test_runner_executes_real_binary_with_minimal_environment(tmp_path: Path) -> None:
    env = git_environment(tmp_path, connection(), credentials().get("test-credential-v1"))
    output = await IsolatedGitRunner().run(tmp_path, env, "--version")
    assert output.startswith(b"git version ")
    assert "WEORA_DATABASE_URL" not in env and env.get("PATH") == os.environ.get("PATH")


@pytest.mark.anyio
async def test_runner_bounds_output_timeout_and_error_redaction(tmp_path: Path) -> None:
    env = git_environment(tmp_path, connection(), credentials().get("test-credential-v1"))
    with pytest.raises(DeliveryError) as oversized:
        await IsolatedGitRunner(sys.executable).run(tmp_path, env, "-c", "print('x'*3000000)")
    assert oversized.value.code == "GIT_OUTPUT_LIMIT"
    with pytest.raises(TimeoutError):
        await IsolatedGitRunner(sys.executable, timeout=0.1).run(tmp_path, env, "-c", "import time;time.sleep(10)")
    with pytest.raises(TimeoutError):
        # 子进程不读 stdin 时也必须按时退出，不能卡在输入管道的 finally。
        await asyncio.wait_for(
            IsolatedGitRunner(sys.executable, timeout=0.1).run(
                tmp_path, env, "-c", "import time;time.sleep(10)", stdin=b"x" * 1048576
            ),
            timeout=3,
        )
    with pytest.raises(DeliveryError) as failed:
        await IsolatedGitRunner(sys.executable).run(
            tmp_path, env, "-c", "import sys;print('secret-test-only',file=sys.stderr);sys.exit(1)"
        )
    assert "secret-test-only" not in str(failed.value)
    with pytest.raises(DeliveryError) as missing:
        await IsolatedGitRunner(str(tmp_path / "nonexistent-git")).run(tmp_path, env, "--version")
    assert missing.value.code == "GIT_NOT_CONFIGURED"
