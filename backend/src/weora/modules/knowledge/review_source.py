"""把 PR/MR 来源匹配到已登记的 Git 地址。这里只判断身份，来源链接不能发起网络请求。"""

import re
from urllib.parse import unquote, urlsplit

_PATH_PART = re.compile(r"[a-zA-Z0-9_.-]+")
_GITHUB = "github.com"
RepoKey = tuple[str, int | None, str]


def _key(host: str, port: int | None, path: str) -> RepoKey | None:
    parts = path.strip("/").split("/")
    if len(parts) < 2 or any(not _PATH_PART.fullmatch(part) or part in {".", ".."} for part in parts):
        return None
    repo = "/".join(parts)
    return host, port, repo.lower() if host == _GITHUB else repo


def registered_repo(address: str) -> RepoKey | None:
    """已登记 HTTPS 与标准 SSH 地址可指向同一仓库，非标准端口保留隔离。"""
    try:
        if address.startswith("git@") and ":" in address and "://" not in address:
            server, path = address.split(":", 1)
            address = f"ssh://{server}/{path}"
        url = urlsplit(address)
        host, port = url.hostname, url.port
        if host is None or url.scheme not in {"https", "ssh"} or url.query or url.fragment:
            return None
        user = "git" if url.scheme == "ssh" else None
        if url.username != user or url.password is not None:
            return None
        ssh_default = 2222 if host == "gitcode.com" else 22
        if port == (ssh_default if url.scheme == "ssh" else 443):
            port = None
        return _key(host, port, unquote(url.path).rstrip("/").removesuffix(".git"))
    except (ValueError, UnicodeError):
        return None


def review_repo(address: str) -> RepoKey | None:
    identity = review_identity(address)
    return identity[0] if identity is not None else None


def review_identity(address: str) -> tuple[RepoKey, str] | None:
    """评审编号与仓库一起标识来源，地址编码和 GitHub 大小写别名不会产生重复记录。"""
    try:
        url = urlsplit(address)
        host, port = url.hostname, url.port
        if (
            host != _GITHUB
            or url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            return None
        path, _, number = unquote(url.path).rpartition("/pull/")
        if not re.fullmatch("[1-9][0-9]{0,17}", number):
            return None
        key = _key(host, port if port != 443 else None, path)
        return (key, number) if key and key[2].count("/") == 1 else None
    except (ValueError, UnicodeError):
        return None
