"""校验评分目录的文本与逻辑路径，保持版本内容可跨平台复现。"""

import unicodedata
from pathlib import PureWindowsPath

from weora.modules.knowledge.models import ScorerFile

MAX_SCORER_FOLDER_BYTES = 1024 * 1024
SCORER_DIRECTORY_TOKEN = "${TEAMAI_SCORER_DIR}"


def normalize_scorer_files(prompt: str, files: list[ScorerFile] | None) -> list[ScorerFile] | None:
    if files is None:
        if SCORER_DIRECTORY_TOKEN in prompt:
            raise ValueError("目录变量只能用于含 SCORER.md 的评分目录。")
        return None
    content_bytes = len(prompt.encode("utf-8"))
    keys: set[str] = set()
    normalized: list[ScorerFile] = []
    for file in files:
        path = file["path"]
        parts = path.split("/")
        if (
            len(path) > 512 or parts[0].casefold() == "scorer.md"
            or any(
                not part or part.startswith(".") or part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()
                or any(char in '<>:"\\|?*' or not char.isprintable() for char in part)
                for part in parts
            )
        ):
            raise ValueError("评分附件必须使用目录内可移植的相对路径。")
        key = unicodedata.normalize("NFC", path).casefold()
        if key in keys:
            raise ValueError("评分附件不能使用跨平台冲突的路径。")
        keys.add(key)
        content = file["content"].replace("\r\n", "\n")
        if "\x00" in content:
            raise ValueError("评分附件必须是 UTF-8 文本。")
        content_bytes += len(content.encode("utf-8"))
        if content_bytes > MAX_SCORER_FOLDER_BYTES:
            raise ValueError("评分正文与附件合计不能超过 1 MiB。")
        normalized.append(ScorerFile(path=path, content=content))
    for key in keys:
        parent = key.rpartition("/")[0]
        while parent:
            if parent in keys:
                raise ValueError("评分附件的文件与目录路径不能重叠。")
            parent = parent.rpartition("/")[0]
    return sorted(normalized, key=lambda row: row["path"])
