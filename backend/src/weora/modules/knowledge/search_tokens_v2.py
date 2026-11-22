"""第二版词项索引与用户可读的查询词覆盖。"""

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import groupby, pairwise

from weora.modules.knowledge.icu_words import boundaries
from weora.modules.knowledge.resource_paths import normalize_resource_path
from weora.modules.knowledge.search_tokens_v1 import TAG, TEXT, TITLE, infer_domain

_MAX_TERM_LENGTH = 160  # 与 PostgreSQL 词项列一致，超长无分隔符文本不参与检索。
_MAX_QUERY_TERMS = 32
_HAN = re.compile(r"[一-鿿]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_STOP = {
    "什么", "什么样", "怎么样", "怎么", "怎样", "如何", "哪个", "哪里", "多少", "多久", "是不是", "是否",
    "为什么", "为啥", "干什么", "凭什么", "怎么办", "可以", "需要", "应该", "现在", "已经", "还是",
    "或者", "但是", "因为", "所以", "如果", "以及", "这个", "那个", "这种", "那种", "一下", "一个",
    "一直", "有没有", "会不会", "能不能", "要不要", "可不可以",
}


def _single_han(segment: str, is_word: bool) -> bool:
    return is_word and len(segment) == 1 and _HAN.fullmatch(segment) is not None


def _indexed(text: str) -> tuple[list[str], int]:
    """统计出现次数，同时拆分驼峰词并恢复相邻的单字词。"""
    tokens: list[str] = []
    atoms: list[str] = []
    words = 0
    for segment, is_word in boundaries(text):
        if is_word:
            words += 1
            word = segment.lower()
            if len(word) <= _MAX_TERM_LENGTH:
                tokens.append(word)
                tokens.extend(dict.fromkeys(
                    piece.lower() for piece in _CAMEL.split(segment)
                    if 2 <= len(piece) <= _MAX_TERM_LENGTH and piece.lower() != word
                ))
        if _single_han(segment, is_word):
            atoms.append(segment)
        else:
            tokens.extend(left + right for left, right in pairwise(atoms))
            atoms.clear()
    tokens.extend(left + right for left, right in pairwise(atoms))
    return tokens, words


def _display_words(text: str) -> Iterator[str]:
    """保留查询原有拼写，并把分离的中文后缀与前面的词合并。"""
    output: list[str] = []
    previous_was_han = False
    for single_han, group in groupby(boundaries(text), lambda item: _single_han(*item)):
        if single_han:
            characters = [segment for segment, _ in group]
            if previous_was_han:
                output[-1] += characters.pop(0)
            if characters:
                output.append("".join(characters))
            previous_was_han = False
            continue
        for segment, is_word in group:
            if is_word:
                output.append(segment)
            previous_was_han = is_word and _HAN.fullmatch(segment) is not None
    yield from output


@dataclass(frozen=True)
class QueryWords:
    indexed: tuple[str, ...]
    words: tuple[tuple[str, frozenset[str]], ...]

    def coverage(self, title_and_tags: set[str]) -> tuple[list[str], list[str]]:
        """以用户输入的词为单位，判断标题或标签有没有提供证据。"""
        matched: list[str] = []
        missing: list[str] = []
        for word, terms in self.words:
            (matched if terms & title_and_tags else missing).append(word)
        return matched, missing


def compile_query(text: str, *, with_coverage: bool = True) -> QueryWords:
    indexed, _ = _indexed(text)
    terms = tuple(dict.fromkeys(token for token in indexed if token not in _STOP))[:_MAX_QUERY_TERMS]
    words = tuple((word, frozenset(_indexed(word)[0])) for word in _display_words(text)) if with_coverage else ()
    return QueryWords(terms, words)


def title_and_tag_terms(title: str, tags: list[str]) -> set[str]:
    return set(_indexed(title)[0]) | set(_indexed(" ".join(tags))[0])


def _legacy_domain(terms: set[str], source: dict[str, str]) -> str:
    path = source.get("path") or source.get("resource_path")
    if not path and source.get("type") in {"file", "wiki"}:
        path = normalize_resource_path(source.get("ref", ""))
    return infer_domain(terms, path or "")


def legacy_document_domain(tags: list[str], source: dict[str, str]) -> str:
    """以原词项和路径重建 0096 及以前的领域投影。"""
    return _legacy_domain(set(_indexed(" ".join(tags))[0]), source)


def document_index(
    title: str, body: str, tags: list[str], source: dict[str, str],
) -> tuple[str, int, int, dict[str, tuple[int, int]]]:
    title_tokens, title_words = _indexed(title)
    body_tokens, body_words = _indexed(body)
    tag_tokens, tag_words = _indexed(" ".join(tags))
    title_terms, tag_terms = set(title_tokens), set(tag_tokens)
    frequency = Counter(body_tokens)
    frequency.update(title_tokens)
    frequency.update(tag_tokens)
    postings = {
        token: (TEXT | (TITLE if token in title_terms else 0) | (TAG if token in tag_terms else 0), count)
        for token, count in frequency.items()
    }
    return _legacy_domain(tag_terms, source), len(title_terms), title_words + body_words + tag_words, postings
