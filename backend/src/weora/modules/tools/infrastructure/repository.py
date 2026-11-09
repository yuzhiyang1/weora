"""只读仓库工具；目录仅由管理员配置，模型没有宿主机路径参数。"""

import asyncio
import os
import stat
import sys
from pathlib import Path, PurePosixPath

from weora.modules.tools.application.gateway import ToolScope, ToolSpec, failure

READ_FILE = ToolSpec(
    "repo.file.read",
    "读取固定提交的 UTF-8 文件。项目任务必须显式提供本次选中仓库 repository_id；不能传地址或凭据。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 400},
            "repository_id": {"type": "string", "pattern": "^[0-9a-fA-F-]{36}$"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)


class ReadOnlyRepository:
    def __init__(self, root: Path | None) -> None:
        self._root = root.resolve() if root is not None else None

    async def read(self, scope: ToolScope, arguments: dict[str, object]) -> dict[str, object]:
        del scope  # Gateway 已核验运行主体；仓库范围不能来自模型或工具结果。
        root = self._root
        if root is None:
            return failure("REPOSITORY_NOT_CONFIGURED")
        raw = arguments.get("path")
        if not isinstance(raw, str):
            return failure("TOOL_ARGUMENTS_INVALID")
        return await asyncio.to_thread(self._read, root, raw)

    @staticmethod
    def _read(root: Path, raw: str) -> dict[str, object]:
        path = PurePosixPath(raw)
        if path.is_absolute() or "\\" in raw or ":" in raw or any(part in {"..", ".git"} for part in path.parts):
            return failure("REPOSITORY_PATH_DENIED")
        # Windows 会忽略大小写和尾随空格/句点；必须在打开文件前拒绝别名，
        # 否则 .ENV 或 .env<空格> 能读到同一份敏感文件。各执行平台沿用同一策略。
        if any(part != part.rstrip(" .") for part in path.parts):
            return failure("REPOSITORY_PATH_DENIED")
        if any(
            part in {".git", ".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"} or part.startswith(".env.")
            for part in (component.casefold() for component in path.parts)
        ):
            return failure("REPOSITORY_PATH_DENIED")
        target = root.joinpath(*path.parts)
        try:
            # 拒绝所有链接/重解析点，而不只拒绝当前解析后越界的链接。
            for candidate in (target, *target.parents):
                if candidate == root:
                    break
                info = candidate.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
                ):
                    return failure("REPOSITORY_PATH_DENIED")
            target.resolve(strict=True).relative_to(root)
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                _verify_open_handle(stream.fileno(), root)
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return failure("REPOSITORY_PATH_DENIED")
                content = stream.read(65537)
            if len(content) > 65536:
                return failure("REPOSITORY_FILE_TOO_LARGE")
            return {"ok": True, "path": raw, "content": content.decode("utf-8")}
        except (OSError, ValueError, UnicodeError):
            return failure("REPOSITORY_FILE_UNAVAILABLE")


def _verify_open_handle(descriptor: int, root: Path) -> None:
    """校验已打开的实际文件，避免检查后替换父目录链接的竞态。"""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        final_path = kernel.GetFinalPathNameByHandleW
        final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        size = final_path(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise OSError("文件句柄不可验证")
        value = buffer.value
        value = "\\\\" + value[8:] if value.startswith("\\\\?\\UNC\\") else value.removeprefix("\\\\?\\")
        Path(value).resolve().relative_to(root)
    else:
        # 生产执行节点限定 Linux；不支持的系统失败关闭，不降级到先检查再打开。
        actual = Path(f"/proc/self/fd/{descriptor}")
        if not actual.exists():
            raise OSError("当前平台不能验证文件句柄")
        actual.resolve(strict=True).relative_to(root)
