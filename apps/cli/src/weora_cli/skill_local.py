"""Skill 本机目录归属、恢复日志与 SQLite 上报队列。"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from weora_cli.collab_client import CollabError, as_object, write_json
from weora_cli.commands import JsonValue


def state_root() -> Path:
    override = os.environ.get("WEORA_SKILL_STATE")
    return Path(override).resolve() if override else Path.home() / ".weora/skills"


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def conflict(target: Path) -> CollabError:
    return CollabError("SKILL_LOCAL_CONFLICT", f"目标存在个人副本或本地改动：{target}")


def unsafe(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def inventory(root: Path) -> dict[str, str]:
    """遇到链接或非普通文件即停止，归属记录只覆盖实际写入的文件。"""
    if unsafe(root) or not root.is_dir():
        raise conflict(root)
    result = {}
    for directory, dirs, files in os.walk(root):
        parent = Path(directory)
        if any(unsafe(parent / name) for name in dirs):
            raise conflict(root)
        for name in files:
            file = parent / name
            if unsafe(file) or not file.is_file():
                raise conflict(root)
            with file.open("rb") as stream:
                result[file.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}


def _safe_part(part: str) -> bool:
    return (
        part not in {"", ".", ".."}
        and part.rstrip(" .") == part
        and not any(char in part for char in '<>:"\\|?*')
        and not any(ord(char) < 32 for char in part)
        and part.split(".", 1)[0].upper() not in _RESERVED
    )


def unpack(package: Path, staged: Path, slug: str) -> dict[str, str]:
    """严格按单一 Skill 根解包，防止跨平台路径冲突与包膨胀。"""
    try:
        with ZipFile(package) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= 5000 or sum(e.file_size for e in entries) > 200 * 1024**2:
                raise ValueError
            seen: set[str] = set()
            names: dict[str, str] = {}
            for entry in entries:
                parts = entry.filename.rstrip("/").split("/")
                if parts[0] != slug or not all(_safe_part(part) for part in parts):
                    raise ValueError
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or (len(parts) == 1 and not entry.is_dir()):
                    raise ValueError
                for end in range(1, len(parts) + 1):
                    prefix = "/".join(parts[:end])
                    folded = prefix.casefold()
                    if names.setdefault(folded, prefix) != prefix:
                        raise ValueError
                key = "/".join(parts).casefold()
                if key in seen:
                    raise ValueError
                seen.add(key)
                target = staged.joinpath(*parts[1:])
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=64 * 1024)
            files = inventory(staged)
            if "SKILL.md" not in files:
                raise ValueError
            return files
    except (BadZipFile, RuntimeError, ValueError, OSError) as exc:
        raise CollabError("SKILL_ARCHIVE_INVALID", "Skill 包格式或归档路径不安全。") from exc


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "skills.sqlite3", timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS binding (
                id TEXT PRIMARY KEY, root TEXT NOT NULL, runtime TEXT NOT NULL, scope TEXT NOT NULL,
                profile TEXT NOT NULL, base TEXT NOT NULL, workspace TEXT NOT NULL, device TEXT NOT NULL, checked TEXT,
                usage INTEGER NOT NULL DEFAULT 0,
                UNIQUE(root, runtime, scope, profile)
            );
            CREATE TABLE IF NOT EXISTS installation (
                binding TEXT NOT NULL, slug TEXT NOT NULL, target TEXT NOT NULL, slot TEXT NOT NULL,
                generation TEXT NOT NULL, version_id TEXT NOT NULL, version TEXT NOT NULL,
                digest TEXT NOT NULL, files TEXT NOT NULL, pinned INTEGER NOT NULL,
                PRIMARY KEY (binding, slug)
            );
            CREATE TABLE IF NOT EXISTS excluded (
                binding TEXT NOT NULL, slug TEXT NOT NULL, PRIMARY KEY(binding, slug)
            );
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT, binding TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_commit (operation TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS sync_lease (
                binding TEXT PRIMARY KEY, owner TEXT NOT NULL, acquired_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_result (binding TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS turn (session TEXT PRIMARY KEY, id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS secret (id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL);
            INSERT OR IGNORE INTO secret(id, value) VALUES (1, lower(hex(randomblob(32))));
        """)
        yield db
    finally:
        db.close()


def queue(db: sqlite3.Connection, binding: str, kind: str, payload: dict[str, JsonValue]) -> None:
    db.execute("INSERT INTO pending(binding, kind, payload) VALUES (?, ?, ?)", (binding, kind, json.dumps(payload)))


def _remove_directory(path: Path, parent: Path) -> None:
    if path.parent != parent or unsafe(path) or path.resolve().parent != parent.resolve():
        raise conflict(path)
    if path.exists():
        shutil.rmtree(path)


def recover(db: sqlite3.Connection) -> None:
    """在 SQLite 写锁内回滚未提交的文件替换，或清理已提交的备份。"""
    journal = state_root() / "install-journal.json"
    if not journal.exists():
        return
    data = as_object(json.loads(journal.read_text(encoding="utf-8")))
    target = Path(str(data["target"]))
    parent = target.parent.parent
    operation = str(data["operation"])
    if not re.fullmatch(r"[a-f0-9]{32}", operation) or any(unsafe(p) for p in (parent, target.parent, target)):
        raise conflict(target)
    staged = parent / f".weora-stage-{operation}"
    backup = parent / f".weora-backup-{operation}"
    if unsafe(staged) or unsafe(backup):
        raise conflict(target)
    committed = db.execute("SELECT 1 FROM file_commit WHERE operation=?", (operation,)).fetchone()
    if not committed and backup.exists():
        if target.exists():
            if inventory(target) != data.get("files"):
                raise conflict(target)
            _remove_directory(target, target.parent)
        backup.replace(target)
    elif not committed and not data.get("had_target") and target.exists():
        if inventory(target) != data.get("files"):
            raise conflict(target)
        _remove_directory(target, target.parent)
    elif not committed and data.get("had_target") and not target.exists():
        raise conflict(target)
    _remove_directory(staged, parent)
    _remove_directory(backup, parent)
    journal.unlink()
    db.execute("DELETE FROM file_commit WHERE operation=?", (operation,))


def change_files(
    db: sqlite3.Connection,
    target: Path,
    package: Path | None,
    slug: str,
    action_id: str,
    expected: dict[str, str] | None,
) -> dict[str, str]:
    """持久化改名意图，后续 SQLite 提交和回执一起确定安装结果。"""
    recover(db)
    if any(unsafe(path) for path in (target, *target.parents)):
        raise conflict(target)
    if target.exists() and (expected is None or inventory(target) != expected):
        raise conflict(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    parent = target.parent.parent
    staged = parent / f".weora-stage-{action_id}"
    backup = parent / f".weora-backup-{action_id}"
    files: dict[str, str] = {}
    if package is not None:
        staged.mkdir()
        try:
            files = unpack(package, staged, slug)
        except Exception:
            _remove_directory(staged, parent)
            raise
    write_json(
        state_root() / "install-journal.json",
        {
            "target": str(target),
            "operation": action_id,
            "had_target": target.exists(),
            "files": {name: value for name, value in files.items()},
        },
    )
    if target.exists():
        target.replace(backup)
    if package is not None:
        staged.replace(target)
    # 文件操作提交标记独立于上报队列，网络重试和清理备份可以并行。
    db.execute("INSERT INTO file_commit VALUES (?)", (action_id,))
    return files


@contextmanager
def temporary_package() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="weora-skill-") as directory:
        yield Path(directory) / "skill.zip"
