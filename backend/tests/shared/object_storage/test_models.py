"""对象存储公共模型的行为测试。"""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from weora.shared.object_storage import BytesObjectSource, FileObjectSource, ObjectKey


def test_object_key_rejects_parent_directory_segments() -> None:
    """逻辑对象键不能通过父目录片段逃逸业务命名空间。"""
    with pytest.raises(ValueError, match="父目录"):
        ObjectKey("skills/workspace-1/../secret")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/skills/package.skill",
        "skills/package.skill/",
        "skills//package.skill",
        "skills/./package.skill",
        r"skills\package.skill",
        "skills/package\x00.skill",
        f"skills/{'a' * 1018}",
    ],
)
def test_object_key_rejects_ambiguous_or_unsafe_shapes(value: str) -> None:
    """调用方不能创建在不同 Provider 中含义不一致的对象键。"""
    with pytest.raises(ValueError):
        ObjectKey(value)


def test_bytes_source_can_be_opened_repeatedly_with_stable_integrity_metadata() -> None:
    """重试上传时每次都必须从同一内容的起点重新读取。"""
    source = BytesObjectSource(
        b"weora-skill",
        content_type="application/vnd.weora.skill+zip",
        filename="demo.skill",
    )

    with source.open() as first_stream:
        first_read = first_stream.read()
    with source.open() as second_stream:
        second_read = second_stream.read()

    assert first_read == second_read == b"weora-skill"
    assert source.content_length == len(b"weora-skill")
    assert source.sha256 == hashlib.sha256(b"weora-skill").hexdigest()


def test_file_source_can_be_reopened_for_sdk_retries() -> None:
    """上传临时文件时，每次重试都必须获得从头开始的新文件句柄。"""
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        package_path = Path(directory) / "demo.skill"
        package_path.write_bytes(b"file-backed-skill")
        source = FileObjectSource(
            path=package_path,
            content_type="application/vnd.weora.skill+zip",
            filename="demo.skill",
        )

        with source.open() as first_stream:
            first_read = first_stream.read()
        with source.open() as second_stream:
            second_read = second_stream.read()

        assert first_read == second_read == b"file-backed-skill"
        assert source.content_length == package_path.stat().st_size
        assert source.sha256 == hashlib.sha256(b"file-backed-skill").hexdigest()


def test_file_source_rejects_content_changed_after_digest_was_fixed() -> None:
    """摘要计算后文件发生变化时，不能用旧摘要上传新内容。"""
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        package_path = Path(directory) / "demo.skill"
        package_path.write_bytes(b"original")
        source = FileObjectSource(path=package_path, content_type="application/zip")
        package_path.write_bytes(b"changed-content")

        with pytest.raises(ValueError, match="发生变化"), source.open():
            pass
