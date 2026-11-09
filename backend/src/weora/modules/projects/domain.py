"""项目资料是模型参考数据；登记仓库不等于授予网络、代码执行或推送权限。"""

from dataclasses import dataclass, field
from pathlib import Path
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


@runtime_checkable
class RepositoryDirectoryReader(Protocol):
    async def directory(self, cache_id: UUID, revision: str, path: str) -> dict[str, object]: ...


@dataclass(frozen=True)
class RepositoryArchive:
    """只在后台传递的固定提交归档，不进入模型或普通 API。"""

    path: Path
    revision: str
    sha256: str


class RepositoryArchiveExporter(Protocol):
    """可选后台能力：只读来源不必实现沙箱归档。"""

    async def archive(self, cache_id: UUID, revision: str) -> RepositoryArchive: ...


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
