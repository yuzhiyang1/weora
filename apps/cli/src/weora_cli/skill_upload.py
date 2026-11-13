"""将本地 Skill 目录或已有归档提交到登录设备所属的工作空间。"""

import os
import stat
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from weora_cli.collab_client import CollabError, paired_client, request_json
from weora_cli.commands import JsonValue

_MAX_ARCHIVE = 50 * 1024**2
_MAX_EXPANDED = 200 * 1024**2
_MAX_FILES = 5000


def _too_large() -> CollabError:
    """保持本机限制与服务端包大小和文件数量契约一致。"""
    return CollabError("SKILL_PACKAGE_TOO_LARGE", "Skill 包超过允许的大小或文件数量限制。")


def _check_path(path: Path) -> os.stat_result:
    """拒绝符号链接、Windows junction 和设备文件，避免打包目录外内容。"""
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if reparse or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise CollabError("SKILL_UNSAFE_LOCAL_PATH", f"Skill 包不支持链接或特殊文件：{path}")
    return info


def _directory_zip(source: Path) -> bytes:
    """在内存构建单根目录 ZIP，并在读取及压缩时检查上限。"""
    if not (source / "SKILL.md").is_file():
        raise CollabError("SKILL_MANIFEST_MISSING", "请选择根目录包含 SKILL.md 的 Skill 目录。")
    output = BytesIO()
    expanded = count = 0
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for directory, dirs, files in os.walk(source, onerror=_raise_walk_error):
            dirs.sort()
            for name in dirs:
                _check_path(Path(directory) / name)
            for name in sorted(files):
                path = Path(directory) / name
                info = _check_path(path)
                expanded += info.st_size
                count += 1
                if expanded > _MAX_EXPANDED or count > _MAX_FILES:
                    raise _too_large()
                member = f"skill/{path.relative_to(source).as_posix()}"
                with path.open("rb") as stream, archive.open(member, "w") as target:
                    remaining = info.st_size
                    while chunk := stream.read(128 * 1024):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise CollabError("SKILL_FILE_CHANGED", f"打包期间文件大小改变：{path}")
                        target.write(chunk)
                        if output.tell() > _MAX_ARCHIVE:
                            raise _too_large()
                    if remaining:
                        raise CollabError("SKILL_FILE_CHANGED", f"打包期间文件大小改变：{path}")
    return output.getvalue()


def _raise_walk_error(error: OSError) -> None:
    """目录无法读取时终止上传，避免提交不完整的包。"""
    raise error


def push(path: str) -> dict[str, JsonValue]:
    """验证本机文件并上传，返回 Skill 标识及待审状态。"""
    client = paired_client(timeout=60)
    source = Path(os.path.abspath(Path(path).expanduser()))
    try:
        info = _check_path(source)
        if stat.S_ISDIR(info.st_mode):
            archive = _directory_zip(source)
        elif source.suffix.lower() in {".zip", ".skill"}:
            if info.st_size > _MAX_ARCHIVE:
                raise _too_large()
            with source.open("rb") as stream:
                archive = stream.read(_MAX_ARCHIVE + 1)
        else:
            raise CollabError("SKILL_ARCHIVE_INVALID", "请选择 Skill 目录、.zip 或 .skill 文件。")
    except OSError as exc:
        raise CollabError("SKILL_LOCAL_READ_FAILED", f"无法读取 Skill 路径：{source}") from exc
    if len(archive) > _MAX_ARCHIVE:
        raise _too_large()
    boundary = uuid4().hex
    # 文件名使用固定传输值，避免路径信息或引号进入 multipart Header。
    prefix = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="package"; filename="skill.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode()
    return request_json(
        "POST", f"{client.base}/api/v1/workspaces/{client.workspace}/skill-distribution/contributions",
        token=client.token, timeout=client.timeout, body=prefix + archive + f"\r\n--{boundary}--\r\n".encode(),
        content_type=f"multipart/form-data; boundary={boundary}",
    )
