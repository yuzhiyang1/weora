"""识别导入知识的逻辑路径，与标题、来源链接和版本号独立。"""

from hashlib import sha256
from pathlib import PurePosixPath


def normalize_resource_path(value: str) -> str | None:
    """只接受团队索引使用的相对路径，统一 Windows 和 POSIX 分隔符。"""
    path = PurePosixPath(value.strip().replace("\\", "/"))
    if not path.parts or path.is_absolute() or ".." in path.parts or ":" in value:
        return None
    if any(not char.isprintable() for char in value):
        return None
    return path.as_posix()


def resource_key(source: dict[str, str]) -> str | None:
    """显式路径优先，历史文件和 Wiki 数据可沿用已有相对来源路径。"""
    path = source.get("resource_path")
    if path is None and source.get("type") in {"file", "wiki"}:
        path = source.get("ref")
    normalized = normalize_resource_path(path) if path else None
    return sha256(normalized.encode()).hexdigest() if normalized else None
