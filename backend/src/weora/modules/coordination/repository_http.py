"""上游仓库观察的供应商实现：GitHub Issue/PR/评论/评审/默认分支 CI，Codeup MR/评论。

仅使用管理员已登记连接；Codeup 不伪装提供 GitHub Issue 和默认分支检查能力。
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote

from weora.modules.coordination.domain import CoordinationError, Json, RepositorySource
from weora.modules.coordination.hosting import ReviewSnapshot
from weora.modules.coordination.hosting_http import (
    HostedDiscussion,
    HostedReviewBodies,
    HostedReviewIdentity,
    _instant,
    _object,
    _rows,
    _text,
)
from weora.modules.coordination.repository_source import RepositoryMessage, RepositoryPoll, RepositoryReview


class RepositoryEvents(HostedReviewBodies):
    async def discussion(
        self, identity: HostedReviewIdentity, reference: HostedDiscussion, cursor: Json
    ) -> ReviewSnapshot:
        """仓库入口只读已订阅 PR；来源授权由 Intake 逐任务复核，不借用虚构的交付或 Run。"""
        number = reference.remote_id
        expected = identity.web_url.rstrip("/") + ("/pull/" if identity.provider == "github" else "/change/") + number
        if not number.isdecimal() or int(number) <= 0 or reference.url != expected:
            raise CoordinationError("CODE_SOURCE_REVIEW_MISMATCH")
        try:
            return await (
                self._github_review(identity, reference, cursor)
                if identity.provider == "github"
                else self._codeup_review(identity, reference, cursor)
            )
        except (ValueError, TypeError, OverflowError):
            raise CoordinationError("CODE_SOURCE_PROTOCOL") from None

    @staticmethod
    def mentions(text: str, login: str) -> bool:
        """与上游 mentions 一样拒绝邮箱和更长账号的部分命中。"""
        return bool(re.search(r"(?:^|[^a-zA-Z0-9_@])@" + re.escape(login) + r"(?![a-zA-Z0-9_-])", text, re.I))

    @staticmethod
    def _message(
        identity: HostedReviewIdentity,
        number: str,
        row_id: str,
        actor: str,
        at: str,
        text: str,
        kind: str,
        is_pull: bool,
        human: bool = True,
        root: str = "",
    ) -> RepositoryMessage:
        if not number.isdecimal() or int(number) < 1 or not actor or not row_id:
            raise CoordinationError("CODE_SOURCE_PROTOCOL")
        if datetime.fromisoformat(at).tzinfo is None:
            raise CoordinationError("CODE_SOURCE_PROTOCOL")
        version = at if kind in {"comment", "inline", "review"} else ""
        digest = hashlib.sha256(
            json.dumps([kind, number, row_id, version, text], ensure_ascii=False).encode()
        ).hexdigest()
        url = (
            identity.web_url.rstrip("/")
            + (
                "/pull/"
                if identity.provider == "github" and is_pull
                else "/issues/"
                if identity.provider == "github"
                else "/change/"
            )
            + number
        )
        return RepositoryMessage(digest, number, actor, human, at, text[:24000], kind, is_pull, url, root[:24000])

    async def poll(self, identity: HostedReviewIdentity, source: RepositorySource, cursor: Json) -> RepositoryPoll:
        """完整有界补读才推进截止；首次边界由服务器保存，不把历史存量自动变成新任务。"""
        now = datetime.now(UTC)
        try:
            since = datetime.fromisoformat(str(cursor["since"]))
            if since.tzinfo is None:
                raise ValueError
            followed = cursor.get("followed_numbers", [])
            if not isinstance(followed, list) or any(
                not isinstance(item, str) or not item.isdecimal() for item in followed
            ):
                raise CoordinationError("CODE_SOURCE_PROTOCOL")
            followed_numbers = set(cast(list[str], followed))
            messages, reviews = await (
                self._github(identity, source, since, followed_numbers)
                if identity.provider == "github"
                else self._codeup(identity, source, since, followed_numbers)
            )
        except (ValueError, TypeError, KeyError):
            raise CoordinationError("CODE_SOURCE_PROTOCOL") from None
        return RepositoryPoll(tuple(messages), {"since": (now - timedelta(seconds=5)).isoformat()}, tuple(reviews))

    async def _github(
        self, identity: HostedReviewIdentity, source: RepositorySource, since: datetime, followed: set[str]
    ) -> tuple[list[RepositoryMessage], list[RepositoryReview]]:
        prefix = "/repos/" + "/".join(quote(part, safe="") for part in identity.repository.split("/"))
        rows, _ = await self._github_pages(
            identity,
            prefix + "/issues",
            since=since.isoformat(),
            parameters={"state": "all", "sort": "updated", "direction": "asc"},
        )
        messages: list[RepositoryMessage] = []
        observations: list[RepositoryReview] = []
        roots: dict[str, tuple[bool, str]] = {}
        for row in rows:
            number, is_pull = str(row.get("number")), row.get("pull_request") is not None
            root = _text(row.get("title"), 300) + "\n\n" + _text(row.get("body"))
            roots[number] = is_pull, root
            at = str(row.get("created_at"))
            user = _object(row.get("user") or {"login": "ghost", "type": "User"})
            actor = _text(user.get("login"), 256)
            if datetime.fromisoformat(at) >= since and (
                (source.observe_pulls if is_pull else source.observe_issues) or self.mentions(root, source.agent_login)
            ):
                messages.append(
                    self._message(
                        identity,
                        number,
                        str(row.get("id")),
                        actor,
                        at,
                        root,
                        "pull" if is_pull else "issue",
                        is_pull,
                        user.get("type") == "User" and actor != source.agent_login,
                        root,
                    )
                )
            if is_pull:
                pull = _object((await self._request(identity, f"{prefix}/pulls/{number}"))[0])
                if pull.get("state") not in {"open", "closed"} or str(pull.get("number")) != number:
                    raise CoordinationError("CODE_SOURCE_PROTOCOL")
                observations.append(
                    RepositoryReview(
                        number,
                        "open" if pull["state"] == "open" else "merged" if pull.get("merged") is True else "closed",
                        datetime.now(UTC).isoformat(),
                        closed_at=_instant(pull.get("closed_at") or pull.get("merged_at")),
                    )
                )
                reviews, _ = await self._github_pages(identity, f"{prefix}/pulls/{number}/reviews")
                for review in reviews:
                    at = _text(review.get("submitted_at"), 128)
                    if not at or datetime.fromisoformat(str(at)) < since:
                        continue
                    user = _object(review.get("user") or {"login": "ghost", "type": "User"})
                    actor = _text(user.get("login"), 256)
                    text = f"{review.get('state')} review of commit {review.get('commit_id')}\n\n" + _text(
                        review.get("body")
                    )
                    if source.observe_comments or number in followed or self.mentions(text, source.agent_login):
                        messages.append(
                            self._message(
                                identity,
                                number,
                                str(review.get("id")),
                                actor,
                                str(at),
                                text,
                                "review",
                                True,
                                user.get("type") == "User" and actor != source.agent_login,
                                root,
                            )
                        )
        for kind, path, pointer in (
            ("comment", "/issues/comments", "issue_url"),
            ("inline", "/pulls/comments", "pull_request_url"),
        ):
            comments, _ = await self._github_pages(identity, prefix + path, since=since.isoformat())
            for comment in comments:
                parent = _text(comment.get(pointer), 2048)
                expected = identity.api_origin + prefix + ("/pulls/" if kind == "inline" else "/issues/")
                if not parent.startswith(expected) or not parent[len(expected) :].isdecimal():
                    raise CoordinationError("CODE_SOURCE_REVIEW_MISMATCH")
                number = parent[len(expected) :]
                if number not in roots:
                    row = _object((await self._request(identity, f"{prefix}/issues/{number}"))[0])
                    roots[number] = (
                        row.get("pull_request") is not None,
                        _text(row.get("title"), 300) + "\n\n" + _text(row.get("body")),
                    )
                is_pull, root = roots[number]
                text = _text(comment.get("body"))
                if kind == "inline":
                    text = (
                        f"{comment.get('path')}:{comment.get('line', comment.get('original_line'))}\n"
                        + _text(comment.get("diff_hunk"), 1200)
                        + "\n"
                        + text
                    )
                if (
                    not source.observe_comments
                    and number not in followed
                    and not self.mentions(text, source.agent_login)
                ):
                    continue
                user = _object(comment.get("user") or {"login": "ghost", "type": "User"})
                actor = _text(user.get("login"), 256)
                messages.append(
                    self._message(
                        identity,
                        number,
                        str(comment.get("id")),
                        actor,
                        str(comment.get("updated_at", comment.get("created_at"))),
                        text,
                        kind,
                        is_pull,
                        user.get("type") == "User" and actor != source.agent_login,
                        root,
                    )
                )
        if source.observe_ci:
            repository = _object((await self._request(identity, prefix))[0])
            branch = _text(repository.get("default_branch"), 256)
            commit = _object((await self._request(identity, prefix + "/commits/" + quote(branch, safe="")))[0])
            sha = _text(commit.get("sha"), 64)
            # 仅允许完整提交摘要进入 URL 路径，供应商异常响应不能改变请求目标。
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
                raise CoordinationError("CODE_SOURCE_PROTOCOL")
            checks, _ = await self._github_pages(
                identity, f"{prefix}/commits/{sha}/check-runs", collection="check_runs"
            )
            statuses, _ = await self._github_pages(identity, f"{prefix}/commits/{sha}/statuses")
            facts = [
                {
                    "name": item.get("name", item.get("context")),
                    "state": item.get("conclusion", item.get("state", item.get("status"))),
                }
                for item in checks + statuses
            ]
            # 默认分支 CI 没有任意人类作者，只能使用管理员明确绑定的 ci 自动身份。
            text = json.dumps({"branch": branch, "head": sha, "checks": facts}, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(text.encode()).hexdigest()
            messages.append(
                RepositoryMessage(
                    digest,
                    "ci:" + branch,
                    "ci",
                    False,
                    datetime.now(UTC).isoformat(),
                    text,
                    "ci",
                    False,
                    identity.web_url,
                    "",
                )
            )
        return messages, observations

    async def _codeup(
        self, identity: HostedReviewIdentity, source: RepositorySource, since: datetime, followed: set[str]
    ) -> tuple[list[RepositoryMessage], list[RepositoryReview]]:
        prefix = "/oapi/v1/codeup" + (
            "/organizations/" + quote(identity.organization, safe="") if identity.organization else ""
        )
        messages: list[RepositoryMessage] = []
        observations: list[RepositoryReview] = []
        for page in range(1, 6):
            rows = _rows(
                (
                    await self._request(
                        identity,
                        prefix + "/changeRequests",
                        params={
                            "projectIds": identity.repository,
                            "page": str(page),
                            "perPage": "100",
                            "orderBy": "updated_at",
                            "sort": "desc",
                        },
                    )
                )[0]
            )
            before = False
            for row in rows:
                if datetime.fromisoformat(str(row.get("updatedAt"))) < since:
                    before = True
                    continue
                number = str(row.get("localId"))
                detail_path = (
                    prefix + "/repositories/" + quote(identity.repository, safe="") + "/changeRequests/" + number
                )
                detail = _object((await self._request(identity, detail_path))[0])
                if str(detail.get("localId")) != number:
                    raise CoordinationError("CODE_SOURCE_REVIEW_MISMATCH")
                outcome = {
                    "UNDER_DEV": "open",
                    "UNDER_REVIEW": "open",
                    "TO_BE_MERGED": "open",
                    "CLOSED": "closed",
                    "MERGED": "merged",
                }.get(str(detail.get("state")))
                if outcome is None:
                    raise CoordinationError("CODE_SOURCE_PROTOCOL")
                from typing import Literal, cast

                observations.append(
                    RepositoryReview(
                        number, cast(Literal["open", "closed", "merged"], outcome), datetime.now(UTC).isoformat()
                    )
                )
                author = _object(detail.get("author"))
                root = _text(detail.get("title"), 300) + "\n\n" + _text(detail.get("description"))
                at = str(detail.get("createdAt"))
                if datetime.fromisoformat(at) >= since and (
                    source.observe_pulls or self.mentions(root, source.agent_login)
                ):
                    messages.append(
                        self._message(
                            identity, number, number, str(author.get("userId")), at, root, "pull", True, root=root
                        )
                    )
                for reviewer in _rows(detail.get("reviewers", [])):
                    at = _text(reviewer.get("reviewTime"), 128)
                    if reviewer.get("hasReviewed") is True and at and datetime.fromisoformat(at) >= since:
                        text = str(reviewer.get("reviewOpinionStatus")) + " review of current merge request"
                        if source.observe_comments or number in followed:
                            messages.append(
                                self._message(
                                    identity,
                                    number,
                                    "review:" + str(reviewer.get("userId")),
                                    str(reviewer.get("userId")),
                                    at,
                                    text,
                                    "review",
                                    True,
                                    root=root,
                                )
                            )
                for comment in await self._codeup_comments(identity, detail_path + "/comments/list"):
                    comment_id = _text(comment.get("comment_biz_id"), 256)
                    at = str(comment.get("last_edit_time") or comment.get("comment_time"))
                    text = _text(comment.get("content"))
                    if comment.get("is_deleted") is True or comment.get("state") == "DRAFT":
                        continue
                    if comment.get("comment_type") == "INLINE_COMMENT":
                        text = f"{comment.get('filePath')}:{comment.get('line_number')}\n" + text
                    if datetime.fromisoformat(at) >= since and (
                        source.observe_comments or number in followed or self.mentions(text, source.agent_login)
                    ):
                        actor = str(_object(comment.get("author")).get("userId", ""))
                        messages.append(
                            self._message(identity, number, comment_id, actor, at, text, "comment", True, root=root)
                        )
            if len(rows) < 100 or before:
                return messages, observations
        raise CoordinationError("CODE_SOURCE_READ_INCOMPLETE")

    async def reply(self, identity: HostedReviewIdentity, number: str, text: str, business_key: str) -> str:
        """回复写入限定原讨论；业务标记便于人工查回，未知发送永不自动重试。"""
        if not number.isdecimal() or int(number) <= 0:
            raise CoordinationError("CODE_SOURCE_REPLY_UNAVAILABLE")
        content = text + "\n\n<!-- weora-reply:" + business_key + " -->"
        if identity.provider == "github":
            prefix = "/repos/" + "/".join(quote(part, safe="") for part in identity.repository.split("/"))
            row = _object(
                (
                    await self._request(
                        identity,
                        f"{prefix}/issues/{number}/comments",
                        method="POST",
                        body={"body": content},
                        expected=(201,),
                    )
                )[0]
            )
            value = row.get("id")
        else:
            prefix = "/oapi/v1/codeup" + (
                "/organizations/" + quote(identity.organization, safe="") if identity.organization else ""
            )
            path = (
                prefix
                + "/repositories/"
                + quote(identity.repository, safe="")
                + "/changeRequests/"
                + number
                + "/comments"
            )
            row = _object(
                (
                    await self._request(
                        identity,
                        path,
                        method="POST",
                        body={"content": content, "comment_type": "GLOBAL_COMMENT", "draft": False},
                        expected=(200, 201),
                    )
                )[0]
            )
            value = row.get("comment_biz_id")
        if not isinstance(value, (str, int)) or not str(value):
            raise CoordinationError("CHANNEL_RECONCILIATION_REQUIRED")
        return str(value)
