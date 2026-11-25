"""只读列出组织可见仓库，生成能交给项目仓库计划校验的地址。"""

import re
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from urllib.parse import quote, unquote, urlsplit

from weora_cli.collab_client import CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_mr import ReviewRepository, _authorization, _request

_PART = re.compile(r"[a-zA-Z0-9_.-]+")
_PAGE_TIMEOUT = 30.0
_PAGE_SIZE = 100


@dataclass(frozen=True)
class Organization(ReviewRepository):
    web: str


def _valid_path(path: str) -> bool:
    return bool(path and len(path) <= 1000) and all(
        _PART.fullmatch(part) and part not in {".", ".."} for part in path.split("/")
    )


def parse_org(value: str) -> Organization:
    try:
        text = value.strip().rstrip("/")
        if "://" not in text:
            first = text.split("/", 1)[0]
            host = "" if "." in first or ":" in first else "github.com/"
            text = "https://" + host + text
        url = urlsplit(text)
        host = url.netloc.lower()
        _ = url.port
        if url.scheme != "https" or not host or url.username is not None or url.password is not None:
            raise ValueError
        if url.query or url.fragment:
            raise ValueError
        path = unquote(url.path).strip("/")
        if not _valid_path(path):
            raise ValueError
        if host == "github.com" and "/" not in path:
            return Organization("github", "https://api.github.com", path, "https://github.com")
        if host == "gitcode.com" and "/" not in path:
            return Organization("gitcode", "https://api.gitcode.com/api/v5", path, "https://gitcode.com")
    except (ValueError, UnicodeError, CollabError):
        pass
    raise CollabError("KNOWLEDGE_ORG_INVALID", "请输入 GitHub 或 GitCode 组织地址。")


def _page(
    org: Organization,
    auth: dict[str, str],
    endpoint: str,
    params: dict[str, str],
) -> JsonValue:
    answers: Queue[JsonValue | Exception] = Queue(maxsize=1)

    def read() -> None:
        try:
            value, _ = _request(org, auth, endpoint, params=params, timeout=_PAGE_TIMEOUT)
        except Exception as exc:
            answers.put(exc)
        else:
            answers.put(value)

    # socket 空闲超时不能约束慢速响应体。主线程也对整页设期限，超时立即放弃组织计划。
    Thread(target=read, daemon=True).start()
    try:
        answer = answers.get(timeout=_PAGE_TIMEOUT)
    except Empty:
        raise CollabError("KNOWLEDGE_ORG_TIMEOUT", "托管平台组织列表一页读取超时。") from None
    if isinstance(answer, Exception):
        raise answer
    return answer


def _rows(value: JsonValue) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list):
        raise CollabError("KNOWLEDGE_ORG_RESPONSE", "托管平台返回的组织列表格式无效。")
    rows: list[dict[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, dict):
            raise CollabError("KNOWLEDGE_ORG_RESPONSE", "托管平台返回的仓库记录格式无效。")
        rows.append(item)
    return rows


def _list(
    org: Organization,
    auth: dict[str, str],
    endpoint: str,
    max_repos: int,
    *,
    options: dict[str, str] | None = None,
    allow_missing: bool = False,
) -> list[dict[str, JsonValue]] | None:
    page = 1
    collected: list[dict[str, JsonValue]] = []
    while len(collected) < max_repos:
        params = (options or {}) | {"per_page": str(_PAGE_SIZE), "page": str(page)}
        try:
            rows = _rows(_page(org, auth, endpoint, params))
        except CollabError as error:
            if allow_missing and page == 1 and error.data == {"status": 404}:
                return None
            raise
        collected.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        page += 1
    return collected[:max_repos]


def list_org(
    value: str,
    max_repos: int,
    *,
    include: re.Pattern[str] | None,
    exclude: re.Pattern[str] | None,
    include_archived: bool,
) -> list[dict[str, JsonValue]]:
    org = parse_org(value)
    auth = _authorization(org.provider)
    namespace = quote(org.repository, safe="")
    github = org.provider == "github"
    rows = _list(org, auth, f"/orgs/{namespace}/repos", max_repos, allow_missing=github)
    if github and not rows:
        rows = _list(org, auth, f"/users/{namespace}/repos", max_repos)
    repos: dict[str, dict[str, JsonValue]] = {}
    for row in rows or []:
        name = row.get("full_name")
        if not isinstance(name, str) or "/" not in name or not _valid_path(name):
            raise CollabError("KNOWLEDGE_ORG_RESPONSE", "托管平台返回了无效的仓库路径。")
        if (not include_archived and row.get("archived") is True) or (include and not include.search(name)):
            continue
        if exclude and exclude.search(name):
            continue
        address = f"{org.web}/{name}.git"
        repos[address] = {"url": address, "auth": "token", "priority": "normal"}
    return list(repos.values())
