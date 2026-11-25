"""项目资料是模型参考数据；登记仓库不等于授予网络、代码执行或推送权限。"""

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID


class ProjectError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class ProjectActor:
    user_id: UUID
    workspace_id: UUID


class ProjectMembers(Protocol):
    async def role(self, actor: ProjectActor) -> str | None: ...


class ProjectSecrets(Protocol):
    def encrypt(self, value: str) -> str: ...
    def decrypt(self, value: str) -> str: ...


@dataclass(frozen=True)
class RepositorySource:
    """后台短时读取连接；秘密不进入 repr、任务快照或模型工具参数。"""

    workspace_id: UUID
    repository_id: UUID
    git_url: str
    branch: str
    username: str = field(default="", repr=False)
    token: str = field(default="", repr=False)


class RepositoryReader(Protocol):
    async def check(self, source: RepositorySource) -> str: ...
    async def clone(self, source: RepositorySource, revision: str) -> UUID: ...
    async def read(self, cache_id: UUID, revision: str, path: str) -> dict[str, object]: ...
    async def discard(self, cache_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class ReportRepository:
    """已通过检查的仓库连接。只有同步 Worker 需要解密的 Git 读取身份。"""

    name: str
    connection_version: int
    source: RepositorySource | None = None


@dataclass(frozen=True, slots=True)
class RepositoryBlob:
    """固定提交中的普通文本候选，目录、符号链接和子模块不作为源码。"""

    path: str
    oid: str
    size: int


@dataclass(frozen=True, slots=True)
class FixedRepository:
    repository_id: UUID
    connection_version: int
    name: str
    revision: str
    cache_id: UUID
    git_url: str = ""
    provider: str = ""


@runtime_checkable
class RepositoryIndexReader(Protocol):
    async def index_blobs(
        self, cache_id: UUID, revision: str, paths: tuple[str, ...] = (),
    ) -> list[RepositoryBlob]: ...

    async def index_text(self, cache_id: UUID, blob: RepositoryBlob) -> str | None: ...


@runtime_checkable
class RepositoryPagedReader(Protocol):
    """同一固定提交内的行窗口读取；游标不能改变仓库或提交权限。"""

    async def read_page(
        self, cache_id: UUID, revision: str, path: str,
        *, start_line: int = 1, max_lines: int = 200, start_column: int = 0,
    ) -> dict[str, object]: ...


@runtime_checkable
class RepositoryDirectoryReader(Protocol):
    async def directory(self, cache_id: UUID, revision: str, path: str) -> dict[str, object]: ...


@runtime_checkable
class RepositorySearchReader(Protocol):
    async def find(self, cache_id: UUID, revision: str, path: str, pattern: str) -> dict[str, object]: ...
    async def search(self, cache_id: UUID, revision: str, path: str, query: str) -> dict[str, object]: ...


def validate_repository_directory_path(path: str) -> None:
    """空字符串表示仓库根，其余路径与读文件使用同一拒绝规则。"""
    if path:
        validate_repository_file_path(path)
        if any(part in {"", "."} for part in path.split("/")):
            raise ProjectError("REPOSITORY_PATH_DENIED", "目录路径不可读取")


@dataclass(frozen=True)
class RepositoryArchive:
    """只在后台传递的固定提交归档，不进入模型或普通 API。"""

    path: Path
    revision: str
    sha256: str
    restricted_paths_removed: bool = False


# 旧交付沙箱的默认归档预算；原生运行通过可信装配层显式传入自己的预算。
DEFAULT_REPOSITORY_ARCHIVE_BYTES = 64 * 1024 * 1024


class RepositoryArchiveExporter(Protocol):
    """可选后台能力：只读来源不必实现沙箱归档。"""

    async def archive(
        self, cache_id: UUID, revision: str, *, max_bytes: int = DEFAULT_REPOSITORY_ARCHIVE_BYTES,
    ) -> RepositoryArchive: ...


def git_address(value: str) -> str:
    """接受 HTTPS/显式 SSH 及常见 SCP 写法；拒绝地址内嵌秘密和本机协议。"""
    value = value.strip()
    if value.startswith("git@") and ":" in value and "://" not in value:
        host, path = value.split(":", 1)
        value = "ssh://" + host + "/" + path
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ProjectError("INVALID_GIT_URL", "Git 地址格式错误") from None
    if (
        url.scheme not in {"https", "ssh"}
        or not url.hostname
        or not url.path.strip("/")
        or url.password
        or url.query
        or url.fragment
        or "%" in url.netloc
        or "\\" in value
        or (url.scheme == "https" and url.username is not None)
        or (url.scheme == "ssh" and url.username != "git")
        or any(char.isspace() or ord(char) < 32 for char in value)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ProjectError("INVALID_GIT_URL", "请填写无密码的 HTTPS 或 git 用户 SSH 仓库地址")
    # 云效网页地址会跳转登录站点；Git 协议需要 .git。只规范已知官方主机，不猜测其他平台规则。
    # 规范化保持同一主机、同一仓库，不依赖跨站重定向，也不重复追加后缀。
    if url.scheme == "https" and url.hostname == "codeup.aliyun.com":
        path = url.path.rstrip("/")
        value = url._replace(path=path if path.endswith(".git") else path + ".git").geturl()
    return value


def validate_repository_file_path(path: str) -> None:
    """在读取或准备仓库前拒绝越界路径和常见凭据文件。"""
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
