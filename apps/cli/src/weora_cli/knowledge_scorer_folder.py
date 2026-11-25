"""读取评分目录的 UTF-8 文件，为发布和本地试跑生成同一份快照。"""

import os
import stat
import unicodedata
from pathlib import Path, PureWindowsPath

from weora_cli.commands import JsonValue

MAX_FOLDER_BYTES = 1024 * 1024
DIRECTORY_TOKEN = "${TEAMAI_SCORER_DIR}"
_MARKER = "SCORER.md"


def _ordinary(path: Path) -> os.stat_result:
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if reparse or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError(f"评分目录不支持链接或特殊文件：{path.name}。")
    return info


def _read(path: Path, remaining: int) -> bytes:
    info = _ordinary(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"评分文件不是普通文件：{path.name}。")
    if info.st_size > remaining:
        raise ValueError("评分目录总大小不能超过 1 MiB。")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(info, opened):
            raise ValueError(f"读取期间评分文件发生变化：{path.name}。")
        data = stream.read(remaining + 1)
        after = os.fstat(stream.fileno())
    if len(data) != info.st_size or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"读取期间评分文件发生变化：{path.name}。")
    return data


def _path_key(relative: Path) -> str:
    if len(relative.as_posix()) > 512 or any(
        part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()
        or any(char in '<>:"\\|?*' or not char.isprintable() for char in part)
        for part in relative.parts
    ):
        raise ValueError(f"评分附件路径无法跨平台使用：{relative.as_posix()}。")
    return unicodedata.normalize("NFC", relative.as_posix()).casefold()


def _walk_failure(error: OSError) -> None:
    raise error


def read_scorer_source(path: Path) -> tuple[str, str, list[JsonValue] | None]:
    """SCORER.md 与同目录可见文本一起读取，隐藏项在遍历前排除。"""
    path = Path(os.path.abspath(path.expanduser()))
    selected = _ordinary(path)
    if stat.S_ISDIR(selected.st_mode):
        root, marker = path, path / _MARKER
    elif path.name == _MARKER and path.parent.name != "scorers":
        root, marker = path.parent, path
    else:
        if path.suffix != ".md":
            raise ValueError("请使用 <名称>.md 或 <名称>/SCORER.md。")
        return path.stem, _read(path, MAX_FOLDER_BYTES).decode("utf-8-sig"), None

    _ordinary(root)
    root = root.resolve(strict=True)
    marker = root / marker.name
    standard = _read(marker, MAX_FOLDER_BYTES)
    remaining = MAX_FOLDER_BYTES - len(standard)
    seen = {"scorer.md"}
    files: list[dict[str, str]] = []
    for directory, names, filenames in os.walk(root, followlinks=False, onerror=_walk_failure):
        names[:] = sorted(name for name in names if not name.startswith("."))
        for candidate in (Path(directory) / name for name in names):
            _ordinary(candidate)
            key = _path_key(candidate.relative_to(root))
            if key in seen:
                raise ValueError("评分目录包含跨平台冲突的路径。")
            seen.add(key)
        for filename in sorted(name for name in filenames if not name.startswith(".")):
            file = Path(directory) / filename
            if file == marker:
                continue
            relative = file.relative_to(root)
            key = _path_key(relative)
            if key in seen or not file.resolve(strict=True).is_relative_to(root):
                raise ValueError("评分附件路径冲突或超出评分目录。")
            if len(files) >= 1000:
                raise ValueError("评分目录不能超过 1000 份附件。")
            seen.add(key)
            data = _read(file, remaining)
            remaining -= len(data)
            content = data.decode("utf-8").replace("\r\n", "\n")
            if "\0" in content:
                raise ValueError(f"评分附件必须是文本：{relative.as_posix()}。")
            files.append({"path": relative.as_posix(), "content": content})
    return root.name, standard.decode("utf-8-sig"), [
        {"path": file["path"], "content": file["content"]} for file in sorted(files, key=lambda file: file["path"])
    ]
