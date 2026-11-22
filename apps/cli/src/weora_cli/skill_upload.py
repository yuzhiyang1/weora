"""将本地 Skill 目录或已有归档提交到登录设备所属的工作空间。"""

import os
import stat
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from weora_cli.collab_client import Client, CollabError, paired_client, read_config, request_json
from weora_cli.commands import JsonValue
from weora_cli.skill_local import database, is_current_binding, state_root

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


def _directory_zip(source: Path, root_name: str = "skill") -> bytes:
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
                member = f"{root_name}/{path.relative_to(source).as_posix()}"
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


def _installed_source(source: Path, client: Client, project: UUID | None) -> tuple[UUID | None, UUID | None]:
    """只从当前配对身份的安装记录判断原资产。旧绑定和冲突记录需要先处理。"""
    if not (state_root() / "skills.sqlite3").exists():
        return project, None
    config = read_config()
    with database() as db:
        records = db.execute(
            "SELECT i.target, i.slug, i.version_id, b.base, b.workspace, b.device, b.project_id "
            "FROM installation i JOIN binding b ON b.id=i.binding",
        ).fetchall()
    origins: set[tuple[UUID | None, UUID]] = set()
    for record in records:
        if Path(record["target"]) != source:
            continue
        if (
            not is_current_binding(record, config) or record["base"] != client.base
            or record["workspace"] != client.workspace or record["slug"] != source.name
        ):
            raise CollabError("SKILL_CONTRIBUTION_ORIGIN", "本地安装来源与当前设备不一致，请核对已有的 Skill 绑定。")
        origin = UUID(record["project_id"]) if record["project_id"] else None
        origins.add((origin, UUID(record["version_id"])))
    if not origins:
        return project, None
    if len(origins) != 1:
        raise CollabError("SKILL_CONTRIBUTION_ORIGIN", "该目录的多个安装记录指向不同来源，请先核对已有绑定。")
    origin, version = origins.pop()
    if project is not None and project != origin:
        raise CollabError("SKILL_CONTRIBUTION_ORIGIN", "指定项目和本地安装范围不同，请使用原安装所在的项目。")
    return origin, version


def push(path: str, *, project: UUID | None = None, namespace: str = "") -> dict[str, JsonValue]:
    """把本机包提交到共享或指定项目的团队目录。"""
    client = paired_client(timeout=60)
    source = Path(os.path.abspath(Path(path).expanduser()))
    version: UUID | None = None
    try:
        info = _check_path(source)
        if stat.S_ISDIR(info.st_mode):
            project, version = _installed_source(source, client, project)
            archive = _directory_zip(source, source.name if version is not None else "skill")
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
    fields = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in (("namespace", namespace), ("source_version_id", str(version) if version else ""))
        if value
    )
    url = f"{client.base}/api/v1/workspaces/{client.workspace}/skill-distribution/contributions"
    if project is not None:
        url += "?" + urlencode({"project_id": str(project)})
    return request_json(
        "POST", url,
        token=client.token, timeout=client.timeout, body=fields + prefix + archive + f"\r\n--{boundary}--\r\n".encode(),
        content_type=f"multipart/form-data; boundary={boundary}",
    )
