"""Skill 包安全校验、标准化和静态兼容性评估。"""

import hashlib
import mimetypes
import re
import stat
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

import yaml

from weora.modules.skills.domain.errors import (
    SkillArchiveInvalidError,
    SkillArchiveUnsafePathError,
    SkillManifestInvalidError,
    SkillManifestMissingError,
    SkillPackageTooLargeError,
    SkillRootAmbiguousError,
)

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[a-zA-Z]:")
_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",  # 私钥头部检测常量，不含实际密钥。 gitleaks:allow
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
)
_SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}


@dataclass(frozen=True, slots=True)
class SkillFile:
    """标准化 Skill 包中的单个普通文件。"""

    path: str
    size: int
    media_type: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SkillWarning:
    """不会阻止上传，但需要发布者关注的风险。"""

    code: str
    message: str
    path: str | None = None
    blocks_publish: bool = False


@dataclass(frozen=True, slots=True)
class SkillCompatibility:
    """一个 Runtime 目标的静态兼容性结论。"""

    target: str
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class InspectedSkillPackage:
    """校验成功后可安全持久化的标准化 Skill 包。"""

    slug: str
    name: str
    description: str
    archive: bytes
    package_sha256: str
    content_sha256: str
    files: tuple[SkillFile, ...]
    compatibility: tuple[SkillCompatibility, ...]
    warnings: tuple[SkillWarning, ...]


@dataclass(frozen=True, slots=True)
class SkillPackageLimits:
    """防止压缩炸弹和超大文本占用资源的统一限制。"""

    max_archive_bytes: int = 50 * 1024 * 1024
    max_expanded_bytes: int = 200 * 1024 * 1024
    max_entries: int = 5_000
    max_manifest_bytes: int = 1024 * 1024


class SkillPackageInspector:
    """把不可信 ZIP 输入转换为确定性、可校验的 Skill 包。"""

    def __init__(self, limits: SkillPackageLimits | None = None) -> None:
        self._limits = limits or SkillPackageLimits()

    def inspect(self, archive_bytes: bytes) -> InspectedSkillPackage:
        """完成路径、结构、清单、摘要、风险和兼容性校验。"""
        if len(archive_bytes) > self._limits.max_archive_bytes:
            raise SkillPackageTooLargeError

        try:
            with ZipFile(BytesIO(archive_bytes), mode="r") as archive:
                entries = archive.infolist()
                files_by_path, _ = self._read_files(archive, entries)
        except SkillPackageTooLargeError:
            raise
        except (BadZipFile, EOFError, OSError, RuntimeError) as exc:
            raise SkillArchiveInvalidError from exc

        manifest_content = files_by_path.get("SKILL.md")
        if manifest_content is None:
            raise SkillManifestMissingError
        if len(manifest_content) > self._limits.max_manifest_bytes:
            raise SkillPackageTooLargeError

        name, description = self._parse_manifest(manifest_content)
        slug = name.strip().lower()
        if len(slug) > 64 or not _SLUG_PATTERN.fullmatch(slug):
            raise SkillManifestInvalidError

        files = tuple(self._describe_file(path, content) for path, content in sorted(files_by_path.items()))
        content_sha256 = self._content_digest(files)
        normalized_archive = self._build_archive(slug, files_by_path)
        warnings = self._find_warnings(files_by_path)
        return InspectedSkillPackage(
            slug=slug,
            name=name.strip(),
            description=description.strip(),
            archive=normalized_archive,
            package_sha256=hashlib.sha256(normalized_archive).hexdigest(),
            content_sha256=content_sha256,
            files=files,
            compatibility=self._evaluate_compatibility(warnings),
            warnings=warnings,
        )

    def _read_files(
        self,
        archive: ZipFile,
        entries: list[ZipInfo],
    ) -> tuple[dict[str, bytes], str]:
        """先校验全部元数据，再读取普通文件，避免任何落盘解压。"""
        if not entries or len(entries) > self._limits.max_entries:
            raise SkillPackageTooLargeError

        roots: set[str] = set()
        normalized_paths: set[str] = set()
        regular_entries: list[tuple[ZipInfo, str]] = []
        expanded_size = 0

        for entry in entries:
            # Windows 上 zipfile 会把 filename 的反斜杠规范化，orig_filename 才保留不可信原值。
            original_path = entry.orig_filename
            self._validate_path(original_path)
            path = PurePosixPath(original_path.rstrip("/"))
            if len(path.parts) < 2:
                if entry.is_dir() and len(path.parts) == 1:
                    roots.add(path.parts[0])
                    continue
                raise SkillRootAmbiguousError
            roots.add(path.parts[0])
            relative_path = unicodedata.normalize(
                "NFC",
                PurePosixPath(*path.parts[1:]).as_posix(),
            )
            if not relative_path:
                continue
            collision_key = relative_path.casefold()
            if collision_key in normalized_paths:
                raise SkillArchiveUnsafePathError
            normalized_paths.add(collision_key)

            unix_mode = entry.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise SkillArchiveUnsafePathError
            file_type = stat.S_IFMT(unix_mode)
            if file_type and not entry.is_dir() and not stat.S_ISREG(unix_mode):
                raise SkillArchiveUnsafePathError
            if entry.flag_bits & 0x1:
                raise SkillArchiveInvalidError
            if entry.is_dir():
                continue

            expanded_size += entry.file_size
            if expanded_size > self._limits.max_expanded_bytes:
                raise SkillPackageTooLargeError
            regular_entries.append((entry, relative_path))

        if len(roots) != 1:
            raise SkillRootAmbiguousError

        files: dict[str, bytes] = {}
        for entry, relative_path in regular_entries:
            try:
                content = archive.read(entry)
            except (BadZipFile, RuntimeError, OSError) as exc:
                raise SkillArchiveInvalidError from exc
            if len(content) != entry.file_size:
                raise SkillArchiveInvalidError
            files[relative_path] = content
        return files, next(iter(roots))

    @staticmethod
    def _validate_path(path: str) -> None:
        """拒绝跨平台上可能变成绝对路径或目录穿越的名字。"""
        if not path or "\x00" in path or "\\" in path:
            raise SkillArchiveUnsafePathError
        if path.startswith("/") or _WINDOWS_DRIVE_PATTERN.match(path):
            raise SkillArchiveUnsafePathError
        parts = PurePosixPath(path.rstrip("/")).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise SkillArchiveUnsafePathError

    @staticmethod
    def _parse_manifest(content: bytes) -> tuple[str, str]:
        """只解析 Frontmatter 数据，不渲染或执行 Markdown 正文。"""
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillManifestInvalidError from exc
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillManifestInvalidError
        try:
            end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
            metadata = yaml.safe_load("\n".join(lines[1:end]))
        except (StopIteration, yaml.YAMLError) as exc:
            raise SkillManifestInvalidError from exc
        if not isinstance(metadata, dict):
            raise SkillManifestInvalidError
        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not name.strip():
            raise SkillManifestInvalidError
        if not isinstance(description, str) or not description.strip():
            raise SkillManifestInvalidError
        if len(name.strip()) > 64 or len(description.strip()) > 2_000:
            raise SkillManifestInvalidError
        return name, description

    @staticmethod
    def _describe_file(path: str, content: bytes) -> SkillFile:
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return SkillFile(
            path=path,
            size=len(content),
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    @staticmethod
    def _content_digest(files: tuple[SkillFile, ...]) -> str:
        """路径、大小和逐文件摘要共同形成与 ZIP 元数据无关的内容摘要。"""
        digest = hashlib.sha256()
        for item in files:
            digest.update(item.path.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(str(item.size).encode("ascii"))
            digest.update(b"\x00")
            digest.update(item.sha256.encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _build_archive(root_name: str, files: dict[str, bytes]) -> bytes:
        """使用固定时间、顺序和权限重建包，使同内容得到同一包摘要。"""
        output = BytesIO()
        with ZipFile(output, mode="w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for path, content in sorted(files.items()):
                info = ZipInfo(filename=f"{root_name}/{path}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        return output.getvalue()

    @staticmethod
    def _find_warnings(files: dict[str, bytes]) -> tuple[SkillWarning, ...]:
        warnings: list[SkillWarning] = []
        for path, content in sorted(files.items()):
            filename = PurePosixPath(path).name.lower()
            if (
                filename in _SENSITIVE_FILE_NAMES
                or filename.startswith(".env.")
                or filename.endswith((".pem", ".key", ".p12", ".pfx"))
            ):
                warnings.append(
                    SkillWarning(
                        code="SENSITIVE_FILE_NAME",
                        message="文件名可能包含凭据，请确认可以在团队内分发",
                        path=path,
                    )
                )
            if any(marker in content for marker in _PRIVATE_KEY_MARKERS):
                warnings.append(
                    SkillWarning(
                        code="PRIVATE_KEY_DETECTED",
                        message="检测到私钥内容，移除后才能发布",
                        path=path,
                        blocks_publish=True,
                    )
                )
        return tuple(warnings)

    @staticmethod
    def _evaluate_compatibility(
        warnings: tuple[SkillWarning, ...],
    ) -> tuple[SkillCompatibility, ...]:
        status = "compatible_with_warnings" if warnings else "compatible"
        return (
            SkillCompatibility("generic_agent_skill", status, "符合通用 Agent Skill 目录约定"),
            SkillCompatibility("codex", status, "可由后续 Codex Runtime Adapter 安装"),
            SkillCompatibility("claude_code", status, "可由后续 Claude Code Adapter 安装"),
            SkillCompatibility(
                "dify_agent",
                "not_evaluated",
                "Dify 暂无原生 Skill 包安装协议，后续通过适配器转换",
            ),
        )
