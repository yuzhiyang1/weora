"""受信任交付代理中的通用 Git 发布；不在沙箱内注入凭据，也不检出/执行仓库代码。"""

import asyncio
import base64
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr

from weora.modules.delivery.application.code_hosting import PublishedBranch, RepositoryConnection, delivery_branch
from weora.modules.delivery.domain.models import DeliveryError, PlanScope, safe_path


@dataclass(frozen=True)
class CredentialMaterial:
    """后端凭据资料；repr 不含秘密，SSH 身份与 known_hosts 必须显式登记。"""

    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)
    ssh_key_path: str = field(default="", repr=False)
    known_hosts_path: str = field(default="", repr=False)


class Credentials(Protocol):
    def get(self, reference: str) -> CredentialMaterial: ...


class ConfiguredCredentials:
    """只从显式配置的引用取值，不查个人 Git/SSH 登录态；不把静态 PAT 冒充短时令牌。"""

    def __init__(self, entries: Mapping[str, SecretStr], api_token_files: Mapping[str, str] | None = None) -> None:
        self.entries = dict(entries)
        self.api_token_files = dict(api_token_files or {})
        if self.entries.keys() & self.api_token_files.keys():
            raise ValueError("API 文件凭据引用不能覆盖既有 Git 凭据引用")

    def get(self, reference: str) -> CredentialMaterial:
        if reference in self.api_token_files:
            try:
                path = Path(self.api_token_files[reference])
                if not path.is_absolute() or not path.is_file() or path.stat().st_size > 4096:
                    raise ValueError
                token = path.read_text(encoding="utf-8-sig").strip()
                if not token or any(char.isspace() for char in token):
                    raise ValueError
                return CredentialMaterial(api_token=token)
            except (OSError, ValueError):
                raise DeliveryError("CREDENTIAL_UNAVAILABLE", "登记的 API 令牌文件不可用") from None
        secret = self.entries.get(reference)
        if secret is None:
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "该连接的凭据引用未配置")
        try:
            values = json.loads(secret.get_secret_value())
            if not isinstance(values, dict) or not all(isinstance(value, str) for value in values.values()):
                raise ValueError
            return CredentialMaterial(**values)
        except (ValueError, TypeError):
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "凭据配置结构错误") from None


class ChangeExporter(Protocol):
    async def export_changes(self, run_id: UUID, scope: PlanScope) -> list[dict[str, str | None]]: ...


class GitRunner(Protocol):
    async def run(
        self, cwd: Path, env: Mapping[str, str], *args: str,
        stdin: bytes | None = None, success_codes: tuple[int, ...] = (0,),
    ) -> bytes: ...


class IsolatedGitRunner:
    """仅接受应用层固定 argv；限制时间/输出，失败不回显可能包含凭据的 stderr。"""

    def __init__(self, executable: str = "git", timeout: float = 60) -> None:
        self.executable, self.timeout = executable, timeout

    async def run(
        self, cwd: Path, env: Mapping[str, str], *args: str,
        stdin: bytes | None = None, success_codes: tuple[int, ...] = (0,),
    ) -> bytes:
        options: dict[str, object] = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        )
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *args,
                cwd=cwd,
                env=dict(env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **options,  # type: ignore[arg-type]
            )
        except OSError:
            raise DeliveryError("GIT_NOT_CONFIGURED", "受控 Git 可执行文件不可用") from None

        async def collect() -> bytes:
            async def feed() -> None:
                assert process.stdin is not None
                try:
                    if stdin:
                        process.stdin.write(stdin)
                        await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # 退出码负责判断失败，不能让输入管道异常暴露命令细节。
                finally:
                    process.stdin.close()
                # 取消 drain 时必须先回到外层杀进程，不能在 finally 等待不读 stdin 的子进程。
                with suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()

            async def read() -> bytes:
                assert process.stdout is not None
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > 2 * 1024 * 1024:
                        raise DeliveryError("GIT_OUTPUT_LIMIT", "Git 输出超过限制")
                return bytes(output)

            tasks = (asyncio.create_task(feed()), asyncio.create_task(read()))
            try:
                _, output = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await process.wait()
            if process.returncode not in success_codes:
                raise DeliveryError("GIT_OPERATION_FAILED", "受控 Git 操作失败，请核对登记仓库和授权")
            return output

        try:
            return await asyncio.wait_for(collect(), self.timeout)
        except BaseException:
            if process.returncode is None:
                if sys.platform == "win32":
                    # 仅清理本次创建的 Git 进程树，不停止用户的 Git/SSH 或其他进程。
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        env={
                            key: value for key, value in env.items() if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR"}
                        },
                    )
                    await killer.wait()
                else:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
            # 超时/输出超限时 read 协程已取消；仅 wait() 不会排空 Windows 管道。
            # communicate 收尾本次进程的管道，防止事件循环退出后留下 transport。
            await process.communicate()
            raise


def git_environment(root: Path, connection: RepositoryConnection, credential: CredentialMaterial) -> dict[str, str]:
    """丢弃宿主 Git 配置、代理、凭据助手和 SSH agent；认证只对登记目标生效。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "COMSPEC",
            "PATHEXT",
        }
    }
    env.update(
        {
            "HOME": str(root),
            "USERPROFILE": str(root),
            "XDG_CONFIG_HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "https:ssh",
            "GIT_AUTHOR_NAME": "Weora",
            "GIT_AUTHOR_EMAIL": "weora@localhost",
            "GIT_COMMITTER_NAME": "Weora",
            "GIT_COMMITTER_EMAIL": "weora@localhost",
        }
    )
    url = urlsplit(connection.clone_url)
    if url.scheme not in {"https", "ssh"} or not url.hostname or url.password or url.query or url.fragment:
        raise DeliveryError("REMOTE_DENIED", "Git 地址必须是已登记的无秘密 HTTPS/SSH URL")
    configs = [
        ("credential.helper", ""),
        ("core.hooksPath", str(root / "no-hooks")),
        ("init.templateDir", str(root / "no-template")),
        ("http.followRedirects", "false"),
        ("http.sslVerify", "true"),
        ("protocol.file.allow", "never"),
        ("protocol.ext.allow", "never"),
    ]
    if url.scheme == "https":
        if url.username or not credential.username or not credential.password:
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "HTTPS Git 需要显式用户名与凭据，不接受 URL 内嵌认证")
        basic = base64.b64encode(f"{credential.username}:{credential.password}".encode()).decode()
        configs.append((f"http.{connection.clone_url}.extraHeader", "Authorization: Basic " + basic))
    else:
        if url.username != "git" or not credential.ssh_key_path or not credential.known_hosts_path:
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "SSH 需要 git 用户、专用密钥及固定 known_hosts")
        if not all(
            Path(path).is_absolute() and Path(path).is_file()
            for path in (
                credential.ssh_key_path,
                credential.known_hosts_path,
            )
        ):
            raise DeliveryError("CREDENTIAL_UNAVAILABLE", "SSH 密钥与 known_hosts 必须是管理员登记的现存绝对路径")
        command = [
            "ssh",
            "-F",
            os.devnull,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "GlobalKnownHostsFile=" + os.devnull,
            "-o",
            "UserKnownHostsFile=" + credential.known_hosts_path,
            "-i",
            credential.ssh_key_path,
        ]
        env["GIT_SSH_COMMAND"] = shlex.join(command)
        env["GIT_SSH_VARIANT"] = "ssh"
    env["GIT_CONFIG_COUNT"] = str(len(configs))
    for index, (key, value) in enumerate(configs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


class CliGitPublisher:
    """固定基准上构造批准文件树；新建或按精确旧 SHA 续写专用分支，不运行用户 hooks。"""

    def __init__(self, exporter: ChangeExporter, credentials: Credentials, runner: GitRunner | None = None) -> None:
        self.exporter, self.credentials = exporter, credentials
        self.runner = runner or IsolatedGitRunner()

    @staticmethod
    def _revision(raw: bytes) -> str:
        value = raw.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            raise DeliveryError("GIT_PROTOCOL", "Git 未返回合法完整提交或对象标识")
        return value

    async def publish(
        self,
        run_id: UUID,
        scope: PlanScope,
        connection: RepositoryConnection,
        business_key: str,
        title: str,
        *,
        previous: PublishedBranch | None = None,
    ) -> PublishedBranch:
        if previous:
            if (
                not scope.source_delivery_id
                or scope.source_branch != previous.name
                or scope.source_revision != previous.revision
                or not re.fullmatch(r"weora/[0-9a-f]{24}", previous.name)
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", previous.revision)
            ):
                raise DeliveryError("REMOTE_DENIED", "续写分支必须与新审批冻结的分支及 SHA 完全一致")
        elif scope.source_delivery_id:
            raise DeliveryError("BRANCH_REQUIRED", "续写不能退化为另外创建分支")
        branch = previous.name if previous else delivery_branch(business_key)
        if branch == scope.target_branch:
            raise DeliveryError("REMOTE_DENIED", "任务分支不能是批准的目标分支")
        if scope.remote_binding != connection.fingerprint or scope.repository_id != connection.repository_id:
            raise DeliveryError("REMOTE_BINDING_CHANGED", "原仓库连接不匹配")
        if scope.target_branch not in connection.target_branches:
            raise DeliveryError("REMOTE_DENIED", "目标分支不在允许范围")
        if not {"git.commit", "git.push"} <= set(scope.actions):
            raise DeliveryError("ACTION_DENIED", "提交与推送必须分别批准")
        changes = await self.exporter.export_changes(run_id, scope)
        if not changes and not previous:
            raise DeliveryError("EMPTY_DIFF", "没有改动，不发布空分支")
        # 交付代理只接受批准路径和有界 Base64，不信任执行沙箱返回的任意文件。
        decoded: list[tuple[str, bytes | None]] = []
        total = 0
        for change in changes:
            path = safe_path(cast(str, change["path"]))
            if not scope.allows_path(path):
                raise DeliveryError("PATH_DENIED", "Diff 越过批准路径")
            try:
                content = base64.b64decode(change["content"], validate=True) if change["content"] is not None else None
            except (ValueError, TypeError):
                raise DeliveryError("GIT_PROTOCOL", "Diff 内容不是合法 Base64") from None
            total += len(content or b"")
            if total > 8 * 1024 * 1024 or len(changes) > 100:
                raise DeliveryError("GIT_OUTPUT_LIMIT", "Diff 超过发布限制")
            decoded.append((path, content))
        with tempfile.TemporaryDirectory(prefix="weora-git-delivery-") as directory:
            root = Path(directory)
            env = git_environment(root, connection, self.credentials.get(connection.credential_ref))
            # 所有 Git 仓库位于本次临时目录，完全不修改用户工作目录。
            await self.runner.run(
                root,
                env,
                "init",
                "--bare",
                "--quiet",
                "--object-format=" + ("sha256" if len(scope.base_revision) == 64 else "sha1"),
            )
            await self.runner.run(
                root, env, "fetch", "--quiet", "--no-tags", "--depth=1", "--", connection.clone_url, scope.base_revision
            )
            fetched = self._revision(await self.runner.run(root, env, "rev-parse", "FETCH_HEAD"))
            if fetched != scope.base_revision:
                raise DeliveryError("BASE_REVISION_CHANGED", "取回的仓库基准与批准记录不同")
            if previous:
                ref = "refs/heads/" + branch
                actual = await self.runner.run(root, env, "ls-remote", "--refs", "--", connection.clone_url, ref)
                if actual.decode().strip() != previous.revision + "\t" + ref:
                    raise DeliveryError("BRANCH_CHANGED", "原 PR 分支已前进或被删除，需要重新核对和审批")
                await self.runner.run(
                    root,
                    env,
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--depth=1",
                    "--",
                    connection.clone_url,
                    previous.revision,
                )
                if self._revision(await self.runner.run(root, env, "rev-parse", "FETCH_HEAD")) != previous.revision:
                    raise DeliveryError("BRANCH_CHANGED", "取回的旧提交与审批记录不同")
            await self.runner.run(root, env, "read-tree", scope.base_revision)
            for path, content in decoded:
                if content is None:
                    # 裸库没有工作树；index-info 的 mode=0 删除项，不触发磁盘路径检查。
                    removal = "0 " + "0" * len(scope.base_revision) + "\t" + path + "\x00"
                    await self.runner.run(root, env, "update-index", "-z", "--index-info", stdin=removal.encode())
                else:
                    blob = self._revision(
                        await self.runner.run(root, env, "hash-object", "-w", "--stdin", stdin=content)
                    )
                    # 不改变已有可执行文件位；新增文件默认普通文件，拒绝 symlink/submodule。
                    entry = await self.runner.run(root, env, "ls-files", "--stage", "--", path)
                    mode = entry.decode().split(" ", 1)[0] if entry else "100644"
                    if mode not in {"100644", "100755"}:
                        raise DeliveryError("PATH_DENIED", "不发布符号链接或子模块改动")
                    await self.runner.run(root, env, "update-index", "--add", "--cacheinfo", mode, blob, path)
            tree = self._revision(await self.runner.run(root, env, "write-tree"))
            parent = previous.revision if previous else scope.base_revision
            base_tree = self._revision(await self.runner.run(root, env, "rev-parse", parent + "^{tree}"))
            if tree == base_tree:
                raise DeliveryError("EMPTY_DIFF", "发布内容与固定基准相同，不创建空提交")
            commit = self._revision(
                await self.runner.run(
                    root,
                    env,
                    "commit-tree",
                    tree,
                    "-p",
                    parent,
                    stdin=(title + "\n\n<!-- weora-delivery:" + business_key + " -->\n").encode(),
                )
            )
            ref = "refs/heads/" + branch
            # 临时裸库也建立同名本地分支；只向该专用分支发布 HEAD，不碰用户工作树。
            await self.runner.run(root, env, "update-ref", ref, commit)
            await self.runner.run(root, env, "symbolic-ref", "HEAD", ref)
            # 初次要求分支不存在；续写只接受批准的旧 SHA，检查后竞态也由远端 CAS 拒绝。
            await self.runner.run(
                root,
                env,
                "push",
                "--porcelain",
                "--force-with-lease=" + ref + ":" + (previous.revision if previous else ""),
                "--",
                connection.clone_url,
                "HEAD:" + ref,
            )
            actual = await self.runner.run(root, env, "ls-remote", "--refs", "--", connection.clone_url, ref)
            if actual.decode().strip() != commit + "\t" + ref:
                raise ConnectionError("REMOTE_UNKNOWN")
            return PublishedBranch(branch, commit)
