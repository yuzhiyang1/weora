"""PR/MR 草稿与近期经验的关键词相似度。"""

import re
from datetime import timedelta

LOOKBACK = timedelta(days=14)
MIN_OVERLAP = 0.60
_TOKEN = re.compile(r"[a-zA-Z]{2,}|[\u4e00-\u9fff]")
_STOP_WORDS = frozenset({
    "the", "an", "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may", "might", "shall", "can",
    "need", "dare", "ought", "used", "in", "on", "at", "to", "for", "of", "and", "or",
    "but", "not", "with", "from", "by", "as", "this", "that", "it", "he", "she", "we", "they",
}) | frozenset("的了是在有和与或不也都就被由从到对于")


def keywords(body: str) -> set[str]:
    """使用 TeamAI 的英文词和中日韩表意文字单字规则，保留技术关键词。"""
    return {token.group().lower() for token in _TOKEN.finditer(body)} - _STOP_WORDS


def overlap(left: set[str], right: set[str]) -> float:
    """重复词只计一次。两份空内容不会形成匹配。"""
    return len(left & right) / len(left | right) if left and right else 0.0
