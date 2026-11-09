"""固定 Git 对象的只读接入：不检出、不执行 hook/filter、不继承个人认证。"""

import asyncio
import base64
import hashlib
import ipaddress
import os
import re
import shlex
import shutil
import socket
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.git import ConfiguredCredentials, GitRunner, IsolatedGitRunner
from weora.modules.projects.domain import ProjectError, RepositoryArchive, RepositorySource, git_address


def revision_id(raw: bytes) -> str:
    value = raw.decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ProjectError("REPOSITORY_PROTOCOL_ERROR", "Git 提交标识无效")
    return value


class GitRepositoryReader:
    """仅管理员可配置存储、私网例外、CA 与 SSH 身份；模型不能覆盖它们。"""

    def __init__(
        self,
        root: Path,
        *,
        runner: GitRunner | None = None,
        private_origins: tuple[str, ...] = (),
        ca_files: Mapping[str, str] | None = None,
        ssh_credentials: ConfiguredCredentials | None = None,
        max_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.root = root.resolve()
        self.runner = runner or IsolatedGitRunner(timeout=45)
        self.private_origins = private_origins
        self.ca_files = dict(ca_files or {})
        self.ssh_credentials = ssh_credentials
        self.max_bytes = max_bytes

    def _path(self, cache_id: UUID) -> Path:
        target = self.root / str(cache_id)
        if target.resolve().parent != self.root or target.is_symlink():
            raise ProjectError("REPOSITORY_CACHE_DENIED", "代码快照存储边界无效")
        return target

    @staticmethod
    def _base_env(root: Path) -> dict[str, str]:
        # 空白环境不会继承代理、credential helper、SSH agent、Git Trace 或个人配置。
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper()
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
                "GIT_ASKPASS": "",
                "GIT_LFS_SKIP_SMUDGE": "1",
            }
        )
        return env

    async def _environment(self, root: Path, source: RepositorySource | None = None) -> dict[str, str]:
        env = self._base_env(root)
        configs = [
            ("credential.helper", ""),
            ("core.hooksPath", str(root / "no-hooks")),
            ("init.templateDir", ""),
            ("http.followRedirects", "false"),
            ("http.sslVerify", "true"),
            ("protocol.file.allow", "never"),
            ("protocol.ext.allow", "never"),
            ("fetch.fsckObjects", "true"),
            ("transfer.fsckObjects", "true"),
            ("http.lowSpeedLimit", "1"),
            ("http.lowSpeedTime", "15"),
        ]
        if source:
            url = urlsplit(git_address(source.git_url))
            host = url.hostname or ""
            port = url.port or (443 if url.scheme == "https" else 22)
            origin = f"{url.scheme}://{url.netloc.split('@')[-1]}"
            if port != (443 if url.scheme == "https" else 22) and origin not in self.private_origins:
                raise ProjectError("REPOSITORY_NETWORK_DENIED", "非标准端口需要管理员明确登记网络目标")
            try:
                addresses = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5
                )
            except (OSError, TimeoutError):
                raise ProjectError("REPOSITORY_DNS_FAILED", "仓库域名解析失败") from None
            ips = list(dict.fromkeys(item[4][0] for item in addresses))
            if not ips or (
                origin not in self.private_origins and any(not ipaddress.ip_address(ip).is_global for ip in ips)
            ):
                raise ProjectError("REPOSITORY_NETWORK_DENIED", "仓库目标解析到私网或保留地址，需要管理员明确登记")
            # 锁定已检查的解析结果，禁止解析与连接之间的 DNS 重绑定。
            ip = ips[0]
            if url.scheme == "https":
                pinned = f"[{ip}]" if ":" in ip else ip
                configs += [("http.curloptResolve", f"{host}:{port}:{pinned}")]
                if source.token:
                    if not source.username or any(c in source.username for c in ":\r\n"):
                        raise ProjectError(
                            "REPOSITORY_USERNAME_REQUIRED", "使用 Git 密码或访问令牌时请填写 Git 认证用户名"
                        )
                    header = base64.b64encode(f"{source.username}:{source.token}".encode()).decode()
                    configs += [(f"http.{source.git_url}.extraHeader", "Authorization: Basic " + header)]
                if origin in self.ca_files:
                    configs += [("http.sslCAInfo", self.ca_files[origin]), ("http.schannelUseSSLCAInfo", "true")]
            else:
                if self.ssh_credentials is None:
                    raise ProjectError("REPOSITORY_SSH_UNCONFIGURED", "SSH 需要管理员登记专用密钥和 known_hosts")
                try:
                    material = self.ssh_credentials.get(f"project:{source.workspace_id}:{source.repository_id}")
                except DeliveryError:
                    raise ProjectError("REPOSITORY_SSH_UNCONFIGURED", "SSH 专用密钥或 known_hosts 尚未配置") from None
                if not material.ssh_key_path or not material.known_hosts_path:
                    raise ProjectError("REPOSITORY_SSH_UNCONFIGURED", "SSH 专用密钥或 known_hosts 尚未配置")
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
                    f"HostName={ip}",
                    "-o",
                    f"HostKeyAlias={host}",
                    "-o",
                    "GlobalKnownHostsFile=" + os.devnull,
                    "-o",
                    "UserKnownHostsFile=" + material.known_hosts_path,
                    "-i",
                    material.ssh_key_path,
                ]
                env["GIT_SSH_COMMAND"], env["GIT_SSH_VARIANT"] = shlex.join(command), "ssh"
        env["GIT_CONFIG_COUNT"] = str(len(configs))
        for index, (key, value) in enumerate(configs):
            env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
        return env

    async def _run(self, path: Path, env: Mapping[str, str], *args: str) -> bytes:
        try:
            return await self.runner.run(path, env, *args)
        except TimeoutError:
            raise ProjectError("REPOSITORY_TIMEOUT", "仓库连接或读取超时") from None
        except DeliveryError:
            raise ProjectError("REPOSITORY_GIT_FAILED", "Git 操作失败，请检查地址、分支、凭据和网络") from None

    async def check(self, source: RepositorySource) -> str:
        # 兼容旧库保存的云效网页地址；请求 URL 与认证 Header 的作用域必须一致。
        source = replace(source, git_url=git_address(source.git_url))
        cache_id = uuid4()
        path = self._path(cache_id)
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=False)
        try:
            env = await self._environment(path, source)
            ref = "refs/heads/" + source.branch
            raw = await self._run(path, env, "ls-remote", "--exit-code", "--refs", "--", source.git_url, ref)
            parts = raw.strip().split(b"\t")
            if len(parts) != 2 or parts[1].decode("utf-8") != ref:
                raise ProjectError("REPOSITORY_BRANCH_UNAVAILABLE", "默认分支不存在或返回不完整")
            return revision_id(parts[0])
        finally:
            await self.discard(cache_id)

    def _size(self, path: Path) -> int:
        size = 0
        for entry in path.rglob("*"):
            with suppress(FileNotFoundError):
                if entry.is_file():
                    size += entry.stat().st_size
        return size

    async def clone(self, source: RepositorySource, revision: str) -> UUID:
        source = replace(source, git_url=git_address(source.git_url))
        revision_id(revision.encode())
        cache_id = uuid4()
        path = self._path(cache_id)
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=False)
        try:
            env = await self._environment(path, source)
            object_format = "sha256" if len(revision) == 64 else "sha1"
            await self._run(path, env, "init", "--bare", f"--object-format={object_format}", "--template=", ".")
            fetch = asyncio.create_task(
                self._run(
                    path,
                    env,
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "--no-recurse-submodules",
                    "--",
                    source.git_url,
                    revision,
                )
            )
            try:
                while not fetch.done():
                    await asyncio.wait({fetch}, timeout=0.2)
                    if await asyncio.to_thread(self._size, path) > self.max_bytes:
                        raise ProjectError("REPOSITORY_SIZE_LIMIT", "仓库超出当前只读快照大小限制")
                await fetch
            finally:
                if not fetch.done():
                    fetch.cancel()
                with suppress(asyncio.CancelledError, ProjectError):
                    await fetch
            actual = revision_id(await self._run(path, env, "rev-parse", "--verify", revision + "^{commit}"))
            if actual != revision:
                raise ProjectError("REPOSITORY_REVISION_MISMATCH", "仓库提交与固定快照不一致")
            return cache_id
        except BaseException:
            await self.discard(cache_id)
            raise

    async def directory(self, cache_id: UUID, revision: str, path: str) -> dict[str, object]:
        """只列固定 Git 对象中的直接子项；不访问工作树、符号链接或凭据文件正文。"""
        revision_id(revision.encode())
        if path and (
            path.startswith(("/", "\\"))
            or any(part in {"", ".", "..", ".git"} for part in path.split("/"))
            or "\\" in path
            or ":" in path
            or any(ord(char) < 32 for char in path)
        ):
            raise ProjectError("REPOSITORY_PATH_DENIED", "目录路径不可读取")
        root = self._path(cache_id)
        env = await self._environment(root)
        raw = await self._run(root, env, "ls-tree", "-z", revision if not path else f"{revision}:{path}")
        entries = []
        records = [item for item in raw.split(b"\0") if item]
        for record in records[:200]:
            meta, name = record.split(b"\t", 1)
            mode, kind, _ = meta.split(b" ")
            if mode not in {b"040000", b"100644", b"100755"}:
                continue
            entries.append({"name": name.decode("utf-8", errors="replace"), "kind": kind.decode()})
        return {"path": path, "entries": entries, "truncated": len(records) > 200}

    async def read(self, cache_id: UUID, revision: str, path: str) -> dict[str, object]:
        revision_id(revision.encode())
        parts = PurePosixPath(path).parts
        if (
            not parts
            or path.startswith("/")
            or any(ord(c) < 32 for c in path)
            or "\\" in path
            or ":" in path
            or any(
                p in {"..", "."}
                or p != p.rstrip(" .")
                or p.casefold() in {".git", ".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
                or p.casefold().startswith(".env.")
                for p in parts
            )
        ):
            raise ProjectError("REPOSITORY_PATH_DENIED", "文件路径不可读取")
        root = self._path(cache_id)
        env = await self._environment(root)
        raw = await self._run(root, env, "ls-tree", "-z", "--full-tree", revision, "--", path)
        records = [item for item in raw.split(b"\0") if item]
        if len(records) != 1:
            raise ProjectError("REPOSITORY_FILE_UNAVAILABLE", "固定提交中没有该文件")
        meta, name = records[0].split(b"\t", 1)
        mode, kind, oid = meta.split(b" ")
        if mode not in {b"100644", b"100755"} or kind != b"blob" or name.decode("utf-8") != path:
            raise ProjectError("REPOSITORY_PATH_DENIED", "不读取符号链接、目录或子模块")
        object_id = revision_id(oid)
        size = int((await self._run(root, env, "cat-file", "-s", object_id)).strip())
        if size > 65536:
            raise ProjectError("REPOSITORY_FILE_TOO_LARGE", "只读文件超过 64 KiB")
        content = await self._run(root, env, "cat-file", "blob", object_id)
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            raise ProjectError("REPOSITORY_FILE_UNAVAILABLE", "文件不是 UTF-8 文本") from None
        if "\0" in text:
            raise ProjectError("REPOSITORY_FILE_UNAVAILABLE", "文件不是普通文本")
        return {
            "ok": True,
            "path": path,
            "revision": revision,
            "content": text,
            "sha256": hashlib.sha256(content).hexdigest(),
            "lfs_pointer": text.startswith("version https://git-lfs.github.com/spec/v1"),
        }

    async def archive(self, cache_id: UUID, revision: str) -> RepositoryArchive:
        """从已准备对象导出 tar，不 checkout、不执行过滤器，路径完全由后台生成。"""
        revision_id(revision.encode())
        root = self._path(cache_id)
        target = root / ("weora-export-" + uuid4().hex + ".tar")
        try:
            await self._run(
                root, await self._environment(root), "archive", "--format=tar", "--output=" + str(target), revision
            )
            if target.is_symlink() or target.resolve().parent != root or target.stat().st_size > 64 * 1024 * 1024:
                raise ProjectError("REPOSITORY_ARCHIVE_LIMIT", "固定提交归档超过沙箱预算或路径无效")
            digest = await asyncio.to_thread(lambda: hashlib.sha256(target.read_bytes()).hexdigest())
            return RepositoryArchive(target, revision, digest)
        except BaseException:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise

    async def discard(self, cache_id: UUID) -> None:
        """只删除平台生成的 UUID 目录，先验证绝对目标始终在专用缓存根内。"""
        path = self._path(cache_id)
        if path.exists():

            def readonly_file(action: Callable[..., object], name: str, error: BaseException) -> None:
                # Windows Git 的 pack/index 默认只读，只调整本次 UUID 缓存内的生成文件。
                target = Path(name)
                if (
                    not isinstance(error, PermissionError)
                    or target.is_symlink()
                    or not target.resolve().is_relative_to(path)
                ):
                    raise error
                os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
                action(name)

            await asyncio.to_thread(shutil.rmtree, path, onexc=readonly_file)
