"""有界文本窗口：按原始行号续读，超长单行也保留字符游标。"""

import codecs
import hashlib


class TextWindow:
    """流式校验整个 UTF-8 文本，只保留请求窗口，不把大文件完整放进内存。"""

    def __init__(self, start_line: int = 1, max_lines: int | None = 200, start_column: int = 0) -> None:
        if start_line < 1 or start_column < 0 or (max_lines is not None and not 1 <= max_lines <= 1000):
            raise ValueError("文本读取范围无效")
        self.start_line, self.start_column, self.max_lines = start_line, start_column, max_lines
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.digest = hashlib.sha256()
        self.line, self.column, self.chars = 1, 0, 0
        self.parts: list[str] = []
        self.end_line, self.end_column = start_line, start_column
        # 小文件兼容完整读取；分页窗口限制字符数，避免 JSON 编码后超过工具输出预算。
        self.max_chars = 65536 if max_lines is None else 24000

    def feed(self, chunk: bytes) -> None:
        self.digest.update(chunk)
        self._text(self.decoder.decode(chunk))

    def _text(self, text: str) -> None:
        if "\0" in text:
            raise ValueError("文件不是普通文本")
        fragments = text.split("\n")
        for index, fragment in enumerate(fragments):
            newline = index < len(fragments) - 1
            value = fragment + ("\n" if newline else "")
            if newline and self.line == self.start_line and self.column + len(fragment) < self.start_column:
                raise ValueError("起始字符偏移超过该行长度")
            if self.line >= self.start_line and (
                self.max_lines is None or self.line < self.start_line + self.max_lines
            ):
                offset = max(0, self.start_column - self.column) if self.line == self.start_line else 0
                selected = value[offset:offset + self.max_chars - self.chars]
                if selected:
                    self.parts.append(selected)
                    self.chars += len(selected)
                    self.end_line = self.line + int(selected.endswith("\n"))
                    self.end_column = 0 if selected.endswith("\n") else self.column + offset + len(selected)
            if newline:
                self.line, self.column = self.line + 1, 0
            else:
                self.column += len(fragment)

    def result(self) -> dict[str, object]:
        self._text(self.decoder.decode(b"", final=True))
        if self.line == self.start_line and self.column < self.start_column:
            raise ValueError("起始字符偏移超过该行长度")
        total_lines = self.line if self.column else self.line - 1
        remaining = (self.end_line, self.end_column) < (self.line, self.column)
        if self.start_line > total_lines:
            remaining = False
        return {
            "content": "".join(self.parts),
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line if self.end_column else self.end_line - 1,
            "total_lines": total_lines,
            "truncated": remaining,
            "next_line": self.end_line if remaining else None,
            "next_column": self.end_column if remaining else None,
            "sha256": self.digest.hexdigest(),
        }
