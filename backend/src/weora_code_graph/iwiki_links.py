"""从 iWiki 术语定位可核对的代码证据。"""

import re
from collections import defaultdict
from dataclasses import dataclass
from itertools import chain
from pathlib import PurePosixPath

from weora_code_graph.reconcile import Page

_API = re.compile(r"/api/[a-z0-9/_-]+", re.I)
_INLINE = re.compile(r"\x60([A-Za-z_][A-Za-z0-9_]{2,119})\x60")
_PASCAL = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b")
_DECLARATION = re.compile(r"(?m)^- [a-z][a-z_-]* \x60([A-Za-z_]\w{1,119})\x60（第 \d+ 行）")
_LABEL_WORD = re.compile(r"([a-z0-9])([A-Z])")
_BODY_API = re.compile(r"(?<![\w/-])/api/[a-z0-9/_-]+(?![\w/-])", re.I)
_BODY_WORD = re.compile(r"\b[a-z_][a-z0-9_]{3,119}\b", re.I)
_TERMS_LIMIT = 80
LINKS_LIMIT = 30


def key_terms(content: str) -> list[str]:
    terms: dict[str, str] = {}
    for pattern in (_API, _INLINE, _PASCAL, _SNAKE):
        for match in pattern.finditer(content):
            term = match[1] if match.lastindex else match[0]
            if len(term) <= 120 and (pattern != _SNAKE or len(term) >= 5):
                terms.setdefault(term.casefold(), term)
                if len(terms) == _TERMS_LIMIT:
                    return list(terms.values())
    return list(terms.values())


@dataclass(frozen=True, slots=True)
class CodeLink:
    page: Page
    term: str
    confidence: str


def _label(value: str) -> str:
    return " ".join(re.split(r"[\s_-]+", _LABEL_WORD.sub(r"\1 \2", value).casefold())).strip()


class CodeEvidence:
    def __init__(self, pages: list[Page]) -> None:
        self.pages = {page.id: page for page in pages}
        self.labels: dict[str, set[str]] = defaultdict(set)
        self.contents: dict[str, str | None] = {}
        for page in pages:
            title = page.title.strip("\x60")
            stem = PurePosixPath(title).stem
            for label in (title, stem, *_DECLARATION.findall(page.body)):
                for alias in {_label(label), label.casefold()}:
                    self.labels[alias].add(page.id)
            # 一次扫描整批代码证据。只记出现文件唯一的术语，避免逐页逐术语重复读取。
            for match in chain(_BODY_API.finditer(page.body), _BODY_WORD.finditer(page.body)):
                term = match[0].casefold()
                if len(term) <= 120 and self.contents.setdefault(term, page.id) != page.id:
                    self.contents[term] = None

    def _locate(self, term: str) -> tuple[str, str] | None:
        targets = self.labels.get(_label(term), set()) | self.labels.get(term.casefold(), set())
        if targets:
            return (next(iter(targets)), "0.8") if len(targets) == 1 else None
        match = self.contents.get(term.casefold())
        return (match, "0.6") if match is not None else None

    def links(self, terms: list[str] | tuple[str, ...]) -> list[CodeLink]:
        selected: dict[str, CodeLink] = {}
        for term in terms:
            if match := self._locate(term):
                identity, confidence = match
                old = selected.get(identity)
                if old is None or confidence > old.confidence:
                    selected[identity] = CodeLink(self.pages[identity], term, confidence)
        return sorted(selected.values(), key=lambda link: (-float(link.confidence), link.page.id))[:LINKS_LIMIT]
