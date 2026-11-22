"""将代码证据版本映射到可移植的 TeamAI wiki 路径。"""

import re
from hashlib import sha256
from urllib.parse import unquote, urlsplit

from weora.modules.knowledge.resource_paths import normalize_resource_path
from weora.modules.knowledge.review_source import registered_repo

_HOST_PROVIDERS = {
    "github.com": "github", "git.woa.com": "tgit", "cnb.cool": "cnb",
    "gitlab.com": "gitlab", "gitcode.com": "gitcode",
}


def wiki_namespaces(
    address: str | None, provider: str | None, directory: str | None,
) -> tuple[str, ...]:
    """登记仓库按托管方和路径命名，项目内独立快照使用目录名称。"""
    if address and (identity := registered_repo(address)) is not None:
        host, _, canonical = identity
        platform = _HOST_PROVIDERS.get(host, "gitlab" if provider == "gitlab" else "git")
        if address.startswith("git@") and "://" not in address:
            original = address.split(":", 1)[1]
        else:
            original = urlsplit(address).path
        hosted = unquote(original).strip("/").removesuffix(".git")
        owner, _, repo = hosted.rpartition("/")
        full = f"{platform}__{owner.replace('/', '-')}__{repo}"
        canonical_owner, _, canonical_repo = canonical.rpartition("/")
        normalized = f"{platform}__{canonical_owner.replace('/', '-')}__{canonical_repo}"
        return tuple(dict.fromkeys((full, normalized)))
    if directory:
        name = directory.strip()
        if name and name not in {".", ".."} and normalize_resource_path(name) == name and "/" not in name:
            return (name,)
    return ()


def wiki_locations(
    namespaces: tuple[str, ...], source: dict[str, str],
) -> tuple[str, ...]:
    """使用版本自身的来源选择可读路径，排除内部 JSON 配置资产。"""
    if generated := source.get("wiki_page"):
        assets = (generated,)
    elif asset := source.get("design_asset"):
        if not asset.lower().endswith(".md"):
            return ()
        assets = (f"docs/{asset}",)
    elif path := source.get("path"):
        # 延用离线索引的文件证据名，源码路径可以包含扩展名、空格及多级目录。
        assets = (f"files/{sha256(path.encode()).hexdigest()}.md",)
    else:
        assets = ("overview.md",)
    return tuple(dict.fromkeys(
        location for namespace in namespaces for asset in assets
        if (location := normalize_resource_path(f"teamwiki/evidence/code/{namespace}/{asset}")) is not None
    ))


def safe_page_segment(value: str) -> str:
    """保留团队树里的名称，拒绝会跳出路径或在链接中表示锚点的字符。"""
    if normalize_resource_path(value) == value and re.fullmatch(r"[\w.-]+", value, re.UNICODE):
        return value
    return "name-" + sha256(value.encode()).hexdigest()[:24]
