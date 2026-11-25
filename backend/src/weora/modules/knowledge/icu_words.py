"""通过系统 ICU 获取和 Intl.Segmenter 同类的词边界。"""

import re
import sys
from collections.abc import Iterator
from ctypes import CDLL, POINTER, byref, c_char_p, c_int32, c_uint16, c_void_p
from ctypes.util import find_library
from functools import lru_cache

_UTF16 = "utf-16-le" if sys.byteorder == "little" else "utf-16-be"
_MAX_UNITS = 50_000
_WORD_STATUS = 100  # ICU 词状态从数字 100 起，前面的状态是空格和标点等。


class WordIterator:
    """ICU 的迭代器持有输入缓冲区，关闭前必须让 UTF-16 数组保持存活。"""

    def __init__(self, library: CDLL, suffix: str) -> None:
        self.open = getattr(library, "ubrk_open" + suffix)
        self.open.argtypes = [c_int32, c_char_p, POINTER(c_uint16), c_int32, POINTER(c_int32)]
        self.open.restype = c_void_p
        self.next = getattr(library, "ubrk_next" + suffix)
        self.next.argtypes = [c_void_p]
        self.next.restype = c_int32
        self.status = getattr(library, "ubrk_getRuleStatus" + suffix)
        self.status.argtypes = [c_void_p]
        self.status.restype = c_int32
        self.close = getattr(library, "ubrk_close" + suffix)
        self.close.argtypes = [c_void_p]
        self.close.restype = None


@lru_cache(maxsize=1)
def _iterator() -> WordIterator:
    # Windows 的系统库使用无版本符号，Debian 等发行版在符号名后附加 ICU 主版本。
    if sys.platform == "win32":
        return WordIterator(CDLL("icu.dll", winmode=0x800), "")
    library_name = find_library("icuuc") or find_library("icucore")
    if library_name is None:
        raise RuntimeError("知识库词级检索需要系统 ICU。请为 API 和迁移环境安装同一版本的 ICU。")
    library = CDLL(library_name)
    if hasattr(library, "ubrk_open"):
        return WordIterator(library, "")
    version = re.search(r"\.(\d+)(?:\.|$)", library_name)
    if version is None:
        raise RuntimeError(f"无法识别 ICU 词边界接口 {library_name}")
    return WordIterator(library, "_" + version[1])


def require_icu() -> None:
    """空库迁移也要检查依赖，避免发布成功后才发现词典缺失。"""
    _iterator()


def boundaries(text: str) -> Iterator[tuple[str, bool]]:
    """逐段保留原始拼写和词状态，标点或空格会切断连续汉字。"""
    if not text:
        return
    icu = _iterator()
    encoded = text.encode(_UTF16, errors="surrogatepass")[:_MAX_UNITS * 2]
    unit_count = len(encoded) // 2
    units = (c_uint16 * unit_count).from_buffer_copy(encoded)
    status = c_int32()
    iterator = icu.open(1, b"zh_CN", units, unit_count, byref(status))  # 1 是 ICU 的词边界模式。
    try:
        if not iterator or status.value > 0:
            raise RuntimeError(f"ICU 无法创建中文词边界迭代器，错误码 {status.value}")
        start = 0
        while (end := icu.next(iterator)) >= 0:
            segment = encoded[start * 2:end * 2].decode(_UTF16, errors="surrogatepass")
            yield segment, icu.status(iterator) >= _WORD_STATUS
            start = end
    finally:
        if iterator:
            icu.close(iterator)
