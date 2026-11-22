"""读写同一 PR/MR 上有归属的 CI 建议，独立处理经验和索引的否决。"""

import os
import re
from collections.abc import Callable

from weora_cli.collab_client import CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_mr import (
    ReviewTarget,
    _authorization,
    _object,
    _pages,
    _request,
    _review_detail,
    _source_commit,
    ci_reviewable,
    require_ci_reviewable,
)


def marker_label(value: str) -> str:
    """兼容 HTML 注释形式的参数，并将标签限制在本工具拥有的命名空间内。"""
    value = value.removeprefix("<!--").removesuffix("-->").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value) or "--" in value:
        raise ValueError("评论标记必须是 1 到 80 个字母、数字或分隔符。")
    return value


def comment_marker(scope: str, label: str, kind: str) -> str:
    return f"<!-- weora:knowledge:ci:{scope}:{label}:{kind} -->"


def comment_text(marker: str, title: str, content: str, signal: str = "") -> str:
    # 建议正文的斜杠命令按普通文字显示。
    text = re.sub(r"(?m)^([ \t]*)/", r"\1&#47;", content.replace("\r\n", "\n").replace("\r", "\n"))
    hint = f"\n\n> 如不希望此建议在合并后发布，请对此评论添加 {signal}。" if signal else ""
    heading = " ".join(title.split())
    return f"{marker}\n\n## {heading}\n\n{text}{hint}\n"


def _login(row: dict[str, JsonValue]) -> str:
    name = row.get("login") or row.get("username") or row.get("id")
    return str(name) if isinstance(name, (str, int)) and not isinstance(name, bool) else ""


class ReviewComments:
    def __init__(
        self, target: ReviewTarget, head: str, author: str | None = None, *, require_open: bool = False,
    ) -> None:
        self.target = target
        self.head = head
        self.auth = _authorization(target.provider)
        self.configured_author = author
        self.require_open = require_open
        self.repo, _, _, review = _review_detail(target, self.auth)
        self._check_review(review)
        self.notes = f"{self.repo}/issues/{target.number}/comments"
        self.author = (author or self._authenticated_author()).casefold()

    def _check_review(self, review: dict[str, JsonValue]) -> None:
        if _source_commit(review) != self.head:
            raise CollabError("KNOWLEDGE_CI_HEAD_CHANGED", "代码评审的源提交已变化，请重新运行。")
        if self.require_open:
            require_ci_reviewable(ci_reviewable(review))

    def _authenticated_author(self, *, allow_configured: bool = False) -> str:
        try:
            author = _login(_object(_request(self.target, self.auth, "/user")[0]))
        except CollabError as error:
            if error.code != "KNOWLEDGE_MR_ACCESS_DENIED":
                raise
            if allow_configured and self.configured_author:
                # GitHub App 安装令牌没有 /user，按显式配置确认评论归属。
                return self.configured_author
            if os.environ.get("GITHUB_ACTIONS") != "true":
                raise
            author = "github-actions[bot]"
        if not author:
            raise CollabError("KNOWLEDGE_CI_COMMENT_AUTHOR", "代码托管平台未返回评论账号身份。")
        return author

    def find(self, markers: set[str]) -> dict[str, dict[str, JsonValue]]:
        found: dict[str, dict[str, JsonValue]] = {}
        for row in _pages(self.target, self.auth, self.notes, max_pages=20, strict=True):
            body = row.get("body")
            if not isinstance(body, str):
                continue
            first = body.partition("\n")[0].strip()
            if first not in markers:
                continue
            account = row.get("user")
            login = _login(account).casefold() if isinstance(account, dict) else ""
            if login != self.author:
                continue
            if first in found:
                raise CollabError("KNOWLEDGE_CI_COMMENT_AMBIGUOUS", "同一发布账号存在重复建议评论，请核对后重跑。")
            found[first] = row
        return found

    @staticmethod
    def _id(row: dict[str, JsonValue]) -> str:
        value = str(row.get("id"))
        if not re.fullmatch(r"[1-9][0-9]{0,18}", value):
            raise CollabError("KNOWLEDGE_CI_COMMENT_INVALID", "代码托管平台未返回有效的评论编号。")
        return value

    def _note_path(self, note: dict[str, JsonValue]) -> str:
        return f"{self.repo}/issues/comments/{self._id(note)}"

    def publish(self, messages: dict[str, str], renew: Callable[[], None]) -> list[JsonValue]:
        """同一标记正文有变化才更新，失败重跑时按归属和标记找回评论。"""
        if self.configured_author:
            actual = self._authenticated_author(allow_configured=True).casefold()
            if self.author != actual:
                raise CollabError("KNOWLEDGE_CI_COMMENT_AUTHOR", "当前令牌的账号与配置的评论发布账号不一致。")
        current = self.find(set(messages))
        results: list[JsonValue] = []
        for marker, content in messages.items():
            note = current.get(marker)
            if note is not None and note.get("body") == content:
                results.append({"marker": marker, "status": "unchanged"})
                continue
            # 每次远程写入前核对提交、评审状态与租约。
            self._check_review(_review_detail(self.target, self.auth)[3])
            renew()
            if note is None:
                _request(self.target, self.auth, self.notes, method="POST", payload={"body": content})
            else:
                _request(self.target, self.auth, self._note_path(note), method="PATCH", payload={"body": content})
            results.append({"marker": marker, "status": "created" if note is None else "updated"})
        return results

    def rejected(self, note: dict[str, JsonValue] | None) -> bool:
        if note is None:
            return False
        return any(row.get("content") == "-1" for row in _pages(
            self.target, self.auth, self._note_path(note) + "/reactions", max_pages=20, strict=True,
        ))
