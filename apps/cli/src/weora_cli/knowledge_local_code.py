"""采集设备目录源码，向平台发送相对路径与快照内容。"""

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from weora_code_graph.extractor import language_for, priority

from weora_cli.collab_client import CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_session_origin import _git, checkout_remote, resolve_commit

_IGNORED = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "coverage",
    "__pycache__",
    "generated",
    "test",
    "tests",
    "__tests__",
    "fixtures",
    "teamwiki",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PATH_CONTROL = re.compile(r"[\x00-\x1f\x7f:\\]")
_MAX_FILE_BYTES = 256_000
_MAX_TOTAL_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Candidate:
    path: str
    stamp: os.stat_result


def source_path(path: str) -> bool:
    parts = path.split("/")
    language = language_for(path)
    return (
        language is not None and not _PATH_CONTROL.search(path)
        and all(
            part and not part.startswith(".")
            and (part.casefold() not in _IGNORED or (part == "Tests" and language == "Swift"))
            for part in parts
        )
        and not any(dedicated in parts[-1].casefold() for dedicated in (".test.", ".spec."))
    )


def source_priority(path: str) -> tuple[int, int, str]:
    return priority(path)[0], path.count("/"), path


def decode_source(data: bytes) -> str:
    text = data.decode("utf-8")
    if len(data) >= _MAX_FILE_BYTES or _CONTROL.search(text):
        raise ValueError("源码必须为有效的 UTF-8 文本。")
    return text


def _collect(root: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory.resolve(strict=True) != directory:
            raise OSError("source directory redirected")
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name.casefold()
                # SwiftPM 将 Tests 下的源码组织为独立模块，协议只在各自的目标内解析。
                if name.startswith(".") or (name in _IGNORED and entry.name != "Tests") or _PATH_CONTROL.search(name):
                    continue
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if len(relative) > 480:
                    continue
                stamp = entry.stat(follow_symlinks=False)
                # Windows junction、符号链接和设备文件都不属于普通源码目录。
                if getattr(stamp, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                if stat.S_ISDIR(stamp.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(stamp.st_mode) and stamp.st_size < _MAX_FILE_BYTES and source_path(relative):
                    candidates.append(Candidate(relative, stamp))
    return sorted(candidates, key=lambda entry: source_priority(entry.path))


def _signature(stamp: os.stat_result) -> tuple[int, int, int, int]:
    return stamp.st_dev, stamp.st_ino, stamp.st_size, stamp.st_mtime_ns


def _read(root: Path, candidate: Candidate) -> str:
    path = root / candidate.path
    try:
        if path.resolve(strict=True) != path:
            raise OSError("source redirected")
        before = path.lstat()
        # Windows 的 DirEntry.stat 不提供稳定的设备与文件 ID，打开前用路径重新取身份。
        if not stat.S_ISREG(before.st_mode) or (before.st_size, before.st_mtime_ns) != (
            candidate.stamp.st_size,
            candidate.stamp.st_mtime_ns,
        ):
            raise OSError("source changed")
        with path.open("rb") as stream:
            if _signature(os.fstat(stream.fileno())) != _signature(before):
                raise OSError("source redirected")
            data = stream.read(_MAX_FILE_BYTES)
            current = os.fstat(stream.fileno())
        if _signature(current) != _signature(before):
            raise OSError("source changed")
        return decode_source(data)
    except (OSError, ValueError) as exc:
        raise CollabError(
            "KNOWLEDGE_LOCAL_UNREADABLE", f"源码已变化或不是可读取的 UTF-8 文本（{candidate.path}）。",
        ) from exc


def _checkout(root: Path) -> dict[str, JsonValue] | None:
    origin = checkout_remote(root)
    top = _git(root, "rev-parse", "--show-toplevel") if origin else None
    commit = resolve_commit(root, "HEAD") if top else None
    if not (origin and top and commit):
        return None
    try:
        subdirectory = root.relative_to(Path(top).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    subdir = subdirectory.as_posix() if subdirectory.parts else ""
    if len(subdir) > 480 or _PATH_CONTROL.search(subdir):
        return None
    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", ".")
    # Git 状态无法读取时仍可以提交快照，但不能声明设备工作区干净。
    return {"origin": origin, "commit": commit, "subdir": subdir, "dirty": dirty != ""}


def local_snapshot(
    path: Path,
    *,
    limit: int,
    preview: bool,
    incremental: bool,
) -> dict[str, JsonValue]:
    try:
        root = path.resolve(strict=True)
        name = root.name
        if not name or _PATH_CONTROL.search(name) or not name.strip():
            raise CollabError("KNOWLEDGE_LOCAL_DIRECTORY", "请选择有名称的源码目录。")
        checkout = _checkout(root)
        candidates = _collect(root)
    except (OSError, RuntimeError) as exc:
        raise CollabError("KNOWLEDGE_LOCAL_DIRECTORY", "本地目录不存在或无法完整扫描。") from exc
    chosen = candidates[:limit]
    result: dict[str, JsonValue] = {"name": name[:160], "candidates": len(candidates), "max_files": limit}
    if checkout is not None:
        result["checkout"] = checkout
    if preview:
        return result | {"preview": True, "files": [candidate.path for candidate in chosen]}
    if sum(candidate.stamp.st_size for candidate in chosen) > _MAX_TOTAL_BYTES:
        raise CollabError("KNOWLEDGE_LOCAL_TOO_LARGE", "当前选择超过 8 MiB，请调低 --max-files 或指定更小的目录。")
    if len({candidate.path.casefold() for candidate in chosen}) != len(chosen):
        raise CollabError("KNOWLEDGE_LOCAL_DUPLICATE", "目录包含仅大小写不同的源码路径。")
    # 平台以项目、设备用户和目录指纹区分来源，设备绝对路径留在本机。
    key = hashlib.sha256(os.fsencode(os.path.normcase(root))).hexdigest()
    files: list[JsonValue] = [{"path": candidate.path, "text": _read(root, candidate)} for candidate in chosen]
    if checkout is not None and _checkout(root) != checkout:
        raise CollabError("KNOWLEDGE_LOCAL_CHECKOUT_CHANGED", "采集期间 Git 状态或来源发生变化，请重新导入。")
    return result | {"source_key": key, "incremental": incremental, "files": files}
