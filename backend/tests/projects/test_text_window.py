"""读取页游标必须无遗漏，尤其覆盖跨字节 UTF-8 与超长单行。"""

from pathlib import Path

import pytest

from weora.modules.tools.infrastructure.repository import ReadOnlyRepository
from weora.shared.text_window import TextWindow


def test_text_pages_reassemble_unicode_and_long_lines() -> None:
    original = "前言\r\n" + "长行🙂" * 20000 + "\n尾行"
    encoded = original.encode()
    line, column = 1, 0
    chunks: list[str] = []
    while True:
        window = TextWindow(line, 2, column)
        for start in range(0, len(encoded), 997):
            window.feed(encoded[start:start + 997])
        page = window.result()
        chunks.append(str(page["content"]))
        assert page["total_lines"] == 3
        if not page["truncated"]:
            break
        following = (int(str(page["next_line"])), int(str(page["next_column"])))
        assert following > (line, column)
        line, column = following
    assert "".join(chunks) == original


@pytest.mark.parametrize("content,total", [(b"", 0), (b"a", 1), (b"a\n", 1), (b"\n\n", 2)])
def test_empty_and_final_newline_counts(content: bytes, total: int) -> None:
    window = TextWindow()
    window.feed(content)
    page = window.result()
    assert page["content"] == content.decode()
    assert page["total_lines"] == total and page["next_line"] is None


def test_readonly_directory_adapter_uses_same_page_contract(tmp_path: Path) -> None:
    content = "".join(f"第 {index} 行\n" for index in range(15000))
    (tmp_path / "large.txt").write_text(content, encoding="utf-8", newline="")
    first = ReadOnlyRepository._read(tmp_path, "large.txt", {})
    assert first["ok"] is True and first["next_line"] == 201
    second = ReadOnlyRepository._read(tmp_path, "large.txt", {"start_line": 201, "max_lines": 10})
    assert second["content"] == "".join(content.splitlines(keepends=True)[200:210])

@pytest.mark.parametrize("content", [b"short", b"short\nnext"])
def test_invalid_column_never_returns_a_nonadvancing_cursor(content: bytes) -> None:
    window = TextWindow(1, 1, 100)
    with pytest.raises(ValueError, match="起始字符偏移"):
        window.feed(content)
        window.result()
