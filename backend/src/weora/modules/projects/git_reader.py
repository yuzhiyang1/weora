"""固定 Git 对象的只读接入：不检出、不执行 hook/filter、不继承个人认证。"""

import asyncio
import base64
import hashlib
import ipaddress
import os
import posixpath
import re
import shlex
import shutil
import socket
import stat
import tarfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.git import (
    ConfiguredCredentials,
    GitRunner,
    IsolatedGitRunner,
    StreamingGitRunner,
)
from weora.modules.projects.domain import (
    DEFAULT_REPOSITORY_ARCHIVE_BYTES,
    ProjectError,
    RepositoryArchive,
    RepositorySource,
    git_address,
    validate_repository_directory_path,
    validate_repository_file_path,
)
from weora.shared.text_window import TextWindow


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
        sync_timeout_seconds: int = 600,
        sync_max_bytes: int = 2 * 1024**3,
    ) -> None:
        self.root = root.resolve()
        self.runner = runner or IsolatedGitRunner(timeout=45)
        self.private_origins = private_origins
        self.ca_files = dict(ca_files or {})
        self.ssh_credentials = ssh_credentials
        self.max_bytes = max_bytes
        self.sync_runner = runner or IsolatedGitRunner(timeout=sync_timeout_seconds)
        self.sync_max_bytes = sync_max_bytes

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

    async def _run(self, path: Path, env: Mapping[str, str], *args: str, runner: GitRunner | None = None) -> bytes:
        try:
            return await (runner or self.runner).run(path, env, *args)
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

    async def _run_bounded(
        self,
        path: Path,
        env: Mapping[str, str],
        *args: str,
        measure: Callable[[], int],
        max_bytes: int,
        error: ProjectError,
        runner: GitRunner | None = None,
    ) -> None:
        process = asyncio.create_task(self._run(path, env, *args, runner=runner))
        try:
            while not process.done():
                await asyncio.wait({process}, timeout=0.2)
                if await asyncio.to_thread(measure) > max_bytes:
                    raise error
            await process
        finally:
            if not process.done():
                process.cancel()
            with suppress(asyncio.CancelledError, ProjectError):
                await process

    async def _fetch(
        self, path: Path, env: Mapping[str, str], *args: str, max_bytes: int, runner: GitRunner | None = None
    ) -> None:
        await self._run_bounded(
            path,
            env,
            "fetch",
            *args,
            measure=lambda: self._size(path),
            max_bytes=max_bytes,
            error=ProjectError("REPOSITORY_SIZE_LIMIT", "仓库超出本地存储预算"),
            runner=runner,
        )

    async def sync(self, source: RepositorySource, cache_id: UUID) -> str:
        """后台更新服务端裸库。旧提交有独立引用，运行只读取已发布的 SHA。"""
        source = replace(source, git_url=git_address(source.git_url))
        revision = await self.check(source)
        path = self._path(cache_id)
        created = not path.exists()
        if created:
            await asyncio.to_thread(path.mkdir, parents=True, exist_ok=False)
        try:
            return await self._sync_objects(path, source, revision)
        except BaseException:
            # 仅清理本次新建、尚未发布的目录，增量同步失败后旧提交仍需供已有任务读取。
            if created:
                await self.discard(cache_id)
            raise

    async def _sync_objects(self, path: Path, source: RepositorySource, revision: str) -> str:
        env = await self._environment(path, source)
        object_format = "sha256" if len(revision) == 64 else "sha1"
        if not (path / "HEAD").is_file():
            await self._run(path, env, "init", "--bare", f"--object-format={object_format}", "--template=", ".")
        retained = f"refs/weora/commits/{revision}"
        if await asyncio.to_thread(self._size, path) > self.sync_max_bytes:
            raise ProjectError("REPOSITORY_SIZE_LIMIT", "仓库超出本地存储预算")
        try:
            if revision_id(await self._run(path, env, "rev-parse", "--verify", retained + "^{commit}")) == revision:
                return revision
        except ProjectError as error:
            if error.code != "REPOSITORY_GIT_FAILED":
                raise
        # 同步引用提供下一次协商的已有对象。首次只取一个提交，后续复用已下载的内容。
        depth = () if (path / "refs" / "weora" / "synced").is_file() else ("--depth=1",)
        await self._fetch(
            path,
            env,
            "--no-tags",
            "--no-auto-gc",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            *depth,
            "--",
            source.git_url,
            f"+{revision}:refs/weora/synced",
            max_bytes=self.sync_max_bytes,
            runner=self.sync_runner,
        )
        actual = revision_id(await self._run(path, env, "rev-parse", "--verify", "refs/weora/synced^{commit}"))
        if actual != revision:
            raise ProjectError("REPOSITORY_REVISION_MISMATCH", "同步结果与目标提交不一致")
        await self._run(path, env, "update-ref", retained, revision)
        return revision

    async def has_synced_revision(self, cache_id: UUID, revision: str) -> bool:
        revision_id(revision.encode())
        path = self._path(cache_id)
        if not (path / "HEAD").is_file():
            return False
        try:
            actual = await self._run(
                path,
                await self._environment(path),
                "rev-parse",
                "--verify",
                f"refs/weora/commits/{revision}^{{commit}}",
            )
        except ProjectError as error:
            if error.code == "REPOSITORY_GIT_FAILED":
                return False
            raise
        return revision_id(actual) == revision

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
            await self._fetch(
                path,
                env,
                "--no-tags",
                "--depth=1",
                "--no-recurse-submodules",
                "--",
                source.git_url,
                revision,
                max_bytes=self.max_bytes,
            )
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
        validate_repository_directory_path(path)
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
            visible = self._visible_path(name)
            if visible is not None:
                entries.append({"name": visible, "kind": kind.decode()})
        return {"path": path, "entries": entries, "truncated": len(records) > 200}

    @staticmethod
    def _visible_path(raw: bytes) -> str | None:
        """目录和搜索结果也遵守读取限制，避免暴露被拒绝的路径或匹配内容。"""
        try:
            path = raw.decode("utf-8")
            validate_repository_file_path(path)
        except (UnicodeError, ProjectError):
            return None
        return path

    async def find(self, cache_id: UUID, revision: str, path: str, pattern: str) -> dict[str, object]:
        revision_id(revision.encode())
        validate_repository_directory_path(path)
        root = self._path(cache_id)
        env = await self._environment(root)
        args = ("--", path) if path else ()
        raw = await self._run(root, env, "--literal-pathspecs", "ls-tree", "-rz", "--full-tree", revision, *args)
        paths: list[str] = []
        for record in filter(None, raw.split(b"\0")):
            meta, name = record.split(b"\t", 1)
            if not meta.startswith((b"100644 blob ", b"100755 blob ")):
                continue
            visible = self._visible_path(name)
            if visible and fnmatchcase(visible if "/" in pattern else visible.rsplit("/", 1)[-1], pattern):
                paths.append(visible)
                if len(paths) == 101:
                    break
        return {"paths": paths[:100], "truncated": len(paths) > 100}

    async def search(self, cache_id: UUID, revision: str, path: str, query: str) -> dict[str, object]:
        revision_id(revision.encode())
        validate_repository_directory_path(path)
        if not query.strip() or any(ord(char) < 32 for char in query):
            raise ProjectError("REPOSITORY_SEARCH_INVALID", "请输入单行搜索关键词")
        root = self._path(cache_id)
        env = await self._environment(root)
        args = ("--", path) if path else ()
        # 按字面搜索，不运行文本转换器。每个文件保留少量命中，结果交给调用者继续精读。
        try:
            raw = await self.runner.run(
                root, env, "--literal-pathspecs", "grep", "--no-textconv", "--no-color", "-I", "-F", "-i",
                "-n", "-z", "-m", "51", "-e", query, revision, *args, success_codes=(0, 1),
            )
        except TimeoutError:
            raise ProjectError("REPOSITORY_TIMEOUT", "仓库搜索超时，请缩小目录范围") from None
        except DeliveryError as error:
            if error.code == "GIT_OUTPUT_LIMIT":
                raise ProjectError("REPOSITORY_SEARCH_LIMIT", "命中太多，请缩小目录范围") from None
            raise ProjectError("REPOSITORY_GIT_FAILED", "固定提交搜索失败") from None
        hits: list[dict[str, object]] = []
        prefix = revision.encode() + b":"
        offset = 0
        while offset < len(raw):
            # Git 用 NUL 分隔路径和行号。先处理路径，防止文件名里的换行伪造新记录。
            path_end = raw.find(b"\0", offset)
            number_end = raw.find(b"\0", path_end + 1)
            line_end = raw.find(b"\n", number_end + 1)
            if min(path_end, number_end, line_end) < 0:
                raise ProjectError("REPOSITORY_PROTOCOL_ERROR", "Git 返回了不完整的搜索记录")
            name = raw[offset:path_end]
            number = raw[path_end + 1:number_end]
            content = raw[number_end + 1:line_end]
            offset = line_end + 1
            visible = self._visible_path(name[len(prefix):]) if name.startswith(prefix) else None
            if visible and number.isdigit():
                hit: dict[str, object] = {
                    "path": visible, "line": int(number), "text": content[:400].decode("utf-8", "replace"),
                }
                if len(content) > 400:
                    hit["text_truncated"] = True
                hits.append(hit)
                if len(hits) == 51:
                    break
        return {"matches": hits[:50], "truncated": len(hits) > 50}

    async def read(self, cache_id: UUID, revision: str, path: str) -> dict[str, object]:
        """小文件兼容完整返回，大文件默认返回首个可续读窗口。"""
        return await self._read(cache_id, revision, path, start_line=1, max_lines=None, start_column=0)

    async def read_page(
        self, cache_id: UUID, revision: str, path: str,
        *, start_line: int = 1, max_lines: int = 200, start_column: int = 0,
    ) -> dict[str, object]:
        return await self._read(
            cache_id, revision, path, start_line=start_line, max_lines=max_lines, start_column=start_column,
        )

    async def _read(
        self, cache_id: UUID, revision: str, path: str,
        *, start_line: int, max_lines: int | None, start_column: int,
    ) -> dict[str, object]:
        revision_id(revision.encode())
        validate_repository_file_path(path)
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
        if size > self.max_bytes:
            raise ProjectError("REPOSITORY_FILE_TOO_LARGE", "文件超过登记仓库的读取预算")
        try:
            # 页预算控制返回正文，整体扫描只用于 UTF-8 校验、行总数和完整内容哈希。
            window = TextWindow(start_line, max_lines if max_lines is not None or size <= 65536 else 200, start_column)
            if isinstance(self.runner, StreamingGitRunner):
                await self.runner.stream(
                    root, env, "cat-file", "blob", object_id, consumer=window.feed, max_bytes=self.max_bytes,
                )
            else:
                window.feed(await self._run(root, env, "cat-file", "blob", object_id))
            page = window.result()
        except UnicodeError:
            raise ProjectError("REPOSITORY_FILE_UNAVAILABLE", "文件不是 UTF-8 文本") from None
        except ValueError as error:
            raise ProjectError("REPOSITORY_FILE_UNAVAILABLE", str(error)) from None
        except TimeoutError:
            raise ProjectError("REPOSITORY_TIMEOUT", "仓库文件读取超时") from None
        except DeliveryError:
            raise ProjectError("REPOSITORY_GIT_FAILED", "固定提交文件读取失败") from None
        return {
            "ok": True, "path": path, "revision": revision, **page,
            "lfs_pointer": start_line == 1 and start_column == 0
            and str(page["content"]).startswith("version https://git-lfs.github.com/spec/v1"),
        }

    async def archive(
        self, cache_id: UUID, revision: str, *, max_bytes: int = DEFAULT_REPOSITORY_ARCHIVE_BYTES,
    ) -> RepositoryArchive:
        """按调用方预算导出固定提交，敏感路径与只读文件接口执行同一规则。"""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ProjectError("REPOSITORY_ARCHIVE_LIMIT", "归档预算必须是正整数字节数")
        revision_id(revision.encode())
        root = self._path(cache_id)
        target = root / ("weora-export-" + uuid4().hex + ".tar")
        limit = max_bytes

        def archive_size() -> int:
            try:
                return target.stat().st_size
            except FileNotFoundError:
                return 0

        try:
            # 不同执行内核使用显式预算；超过上限仍立即停止进程，不裁掉仓库内容。
            await self._run_bounded(
                root,
                await self._environment(root),
                "archive",
                "--format=tar",
                "--output=" + str(target),
                revision,
                measure=archive_size,
                max_bytes=limit,
                error=ProjectError("REPOSITORY_ARCHIVE_LIMIT", "固定提交归档超过沙箱预算"),
            )
            if target.is_symlink() or target.resolve().parent != root or target.stat().st_size > limit:
                raise ProjectError("REPOSITORY_ARCHIVE_LIMIT", "固定提交归档超过沙箱预算或路径无效")
            def prepare_archive() -> tuple[str, bool]:
                filtered = target.with_suffix(".filtered.tar")
                removed = False
                try:
                    with tarfile.open(target, "r:") as archive:
                        for member in archive:
                            if not self._archive_member_allowed(member):
                                removed = True
                                break
                    if removed:
                        # 避免 Git pathspec 参数超限，也不让受限内容进入后续容器。
                        with tarfile.open(target, "r:") as archive, tarfile.open(filtered, "x:") as safe:
                            for member in archive:
                                if not self._archive_member_allowed(member):
                                    continue
                                if member.isfile():
                                    source = archive.extractfile(member)
                                    if source is None:
                                        raise tarfile.ReadError("missing archive file")
                                    with source:
                                        safe.addfile(member, source)
                                else:
                                    safe.addfile(member)
                        filtered.replace(target)
                except (tarfile.TarError, OSError):
                    raise ProjectError("REPOSITORY_ARCHIVE_INVALID", "无法验证固定提交归档") from None
                finally:
                    filtered.unlink(missing_ok=True)
                # 大仓归档只按块进入内存，同时防止生成后文件增长绕过预算。
                digest = hashlib.sha256()
                size = 0
                with target.open("rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ProjectError("REPOSITORY_ARCHIVE_LIMIT", "固定提交归档不是普通文件")
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise ProjectError("REPOSITORY_ARCHIVE_LIMIT", "固定提交归档超过沙箱预算")
                        digest.update(chunk)
                return digest.hexdigest(), removed

            hashing = asyncio.create_task(asyncio.to_thread(prepare_archive))
            try:
                digest, removed = await asyncio.shield(hashing)
            except asyncio.CancelledError:
                # Windows 打开的文件不能立即删除；先等后台只读 hash 释放句柄再清理归档。
                await asyncio.gather(hashing, return_exceptions=True)
                raise
            return RepositoryArchive(target, revision, digest, restricted_paths_removed=removed)
        except BaseException:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise

    @staticmethod
    def _archive_member_allowed(member: tarfile.TarInfo) -> bool:
        try:
            validate_repository_file_path(member.name)
            if member.issym():
                # 相对链接可保留，但不能指向归档之外或跳回受限路径。
                if member.linkname.startswith("/"):
                    return False
                directory = PurePosixPath(member.name).parent.as_posix()
                destination = posixpath.normpath(posixpath.join(directory, member.linkname))
                validate_repository_file_path(destination)
        except ProjectError:
            return False
        return member.isfile() or member.isdir() or member.issym()

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
