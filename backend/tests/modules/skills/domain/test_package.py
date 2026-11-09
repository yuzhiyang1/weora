"""SkillPackageInspector 的安全边界与确定性行为。"""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from weora.modules.skills.domain.errors import (
    SkillArchiveUnsafePathError,
    SkillManifestInvalidError,
    SkillManifestMissingError,
    SkillRootAmbiguousError,
)
from weora.modules.skills.domain.package import SkillPackageInspector


def build_archive(files: dict[str, bytes], *, timestamp: tuple[int, int, int, int, int, int] | None = None) -> bytes:
    """按指定归档元数据构造不可信测试输入。"""
    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED) as archive:
        for path, content in files.items():
            if timestamp is None:
                archive.writestr(path, content)
                continue
            info = ZipInfo(path, date_time=timestamp)
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    return output.getvalue()


def valid_files() -> dict[str, bytes]:
    """返回一个包含引用资料的完整 Skill 目录。"""
    return {
        "release-note/SKILL.md": (
            b"---\nname: release-note\ndescription: Generate release notes\n---\n\n# Instructions\n"
        ),
        "release-note/references/template.md": b"# Template\n",
        "release-note/scripts/render.py": b"print('render')\n",
    }


def test_inspector_preserves_directory_and_rebuilds_deterministic_archive() -> None:
    """ZIP 时间戳不同不能影响内容摘要或最终标准包。"""
    inspector = SkillPackageInspector()

    first = inspector.inspect(build_archive(valid_files(), timestamp=(2024, 1, 1, 0, 0, 0)))
    second = inspector.inspect(build_archive(valid_files(), timestamp=(2026, 8, 22, 9, 30, 0)))

    assert first.content_sha256 == second.content_sha256
    assert first.package_sha256 == second.package_sha256
    assert first.archive == second.archive
    assert [item.path for item in first.files] == [
        "SKILL.md",
        "references/template.md",
        "scripts/render.py",
    ]
    with ZipFile(BytesIO(first.archive)) as normalized:
        assert normalized.namelist() == [
            "release-note/SKILL.md",
            "release-note/references/template.md",
            "release-note/scripts/render.py",
        ]


@pytest.mark.parametrize(
    ("files", "expected_error"),
    [
        ({"a/SKILL.md": b"---\nname: a\ndescription: A\n---", "b/x.txt": b"x"}, SkillRootAmbiguousError),
        ({"a/readme.md": b"hello"}, SkillManifestMissingError),
        ({"a/SKILL.md": b"name: a"}, SkillManifestInvalidError),
        ({"a/../secret.txt": b"secret", "a/SKILL.md": b"x"}, SkillArchiveUnsafePathError),
        ({"C:/a/SKILL.md": b"x"}, SkillArchiveUnsafePathError),
    ],
)
def test_inspector_rejects_unsafe_or_invalid_archives(
    files: dict[str, bytes],
    expected_error: type[Exception],
) -> None:
    """结构或路径不安全的包必须在对象存储写入前失败。"""
    with pytest.raises(expected_error):
        SkillPackageInspector().inspect(build_archive(files))


def test_inspector_rejects_backslash_path_from_cross_platform_archive() -> None:
    """即使 Windows ZIP 写入器会规范化分隔符，也要覆盖外部包中的反斜杠。"""
    archive = build_archive({"a/SKILL.md": b"x"})
    archive_with_backslash = archive.replace(b"a/SKILL.md", b"a\\SKILL.md")

    with pytest.raises(SkillArchiveUnsafePathError):
        SkillPackageInspector().inspect(archive_with_backslash)


def test_inspector_marks_private_key_as_publish_blocker() -> None:
    """上传可展示风险，但私钥内容必须阻断后续发布。"""
    files = valid_files()
    files["release-note/assets/service.pem"] = b"-----BEGIN PRIVATE KEY-----\nsecret\n"

    inspected = SkillPackageInspector().inspect(build_archive(files))

    assert any(item.code == "PRIVATE_KEY_DETECTED" and item.blocks_publish for item in inspected.warnings)
    assert any(item.status == "compatible_with_warnings" for item in inspected.compatibility)


def test_inspector_accepts_explicit_directory_entries_from_common_zip_tools() -> None:
    """Windows 和 macOS 压缩工具生成的目录条目不能被误判为第二个根。"""
    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("release-note/", b"")
        archive.writestr("release-note/references/", b"")
        archive.writestr(
            "release-note/SKILL.md",
            b"---\nname: release-note\ndescription: Generate release notes\n---\n",
        )
        archive.writestr("release-note/references/template.md", b"# Template\n")

    inspected = SkillPackageInspector().inspect(output.getvalue())

    assert [item.path for item in inspected.files] == [
        "SKILL.md",
        "references/template.md",
    ]
