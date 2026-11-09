"""代码托管只读事件适配器；GitHub 行为参考固定上游，Codeup 使用官方 OAPI 契约。"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from urllib.parse import quote, urlsplit
from uuid import UUID

import aiohttp

from weora.modules.coordination.domain import CoordinationError, Json
from weora.modules.coordination.hosting import CodeEvent, ReviewSnapshot
from weora.modules.delivery.application.ports import ReviewReference
from weora.modules.runs.application.ports import RunActor


@dataclass(frozen=True)
class HostedReviewIdentity:
    provider: Literal["github", "codeup"]
    api_origin: str
    repository: str
    web_url: str
    token: str = field(repr=False)
    organization: str = ""


@dataclass(frozen=True)
class HostedDiscussion:
    """已授权仓库中讨论的定位和事实去重键；不是原生交付记录，不提供运行授权。"""

    record_id: UUID
    remote_id: str
    url: str


def _object(value: object) -> Json:
    if not isinstance(value, dict):
        raise CoordinationError("CODE_SOURCE_PROTOCOL")
    return cast(Json, value)


def _rows(value: object) -> list[Json]:
    if not isinstance(value, list) or len(value) > 1000:
        raise CoordinationError("CODE_SOURCE_READ_INCOMPLETE")

    return [_object(item) for item in value]


def _text(value: object, limit: int = 24000) -> str:
    if value is not None and not isinstance(value, str):
        raise CoordinationError("CODE_SOURCE_PROTOCOL")
    return (value or "")[:limit]


def _instant(value: object) -> str | None:
    """关闭时间只接受带时区的供应商事实，不能拿轮询时刻替代。"""
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(_text(value, 128))
        if instant.tzinfo is None:
            raise ValueError
        return instant.astimezone(UTC).isoformat()
    except ValueError:
        raise CoordinationError("CODE_SOURCE_PROTOCOL") from None


class HostedHTTP:
    """受信连接的有界 HTTP 运输；分页只增加本地页号，永不跟随外部任意链接。"""

    async def _request(
        self,
        identity: HostedReviewIdentity,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, str] | None = None,
        body: Json | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[object, dict[str, str]]:
        origin = urlsplit(identity.api_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise CoordinationError("CODE_SOURCE_ORIGIN_DENIED")
        if not identity.token or any(char.isspace() for char in identity.token):
            raise CoordinationError("CODE_SOURCE_CREDENTIAL_UNAVAILABLE")
        headers = (
            {"Authorization": "Bearer " + identity.token, "Accept": "application/vnd.github+json"}
            if identity.provider == "github"
            else {"x-yunxiao-token": identity.token}
        )
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session,
                session.request(
                    method, identity.api_origin + path, headers=headers, params=params, json=body, allow_redirects=False
                ) as response,
            ):
                if response.status not in expected:
                    raise CoordinationError("CODE_SOURCE_UNAVAILABLE")
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise CoordinationError("CODE_SOURCE_READ_INCOMPLETE")
                return json.loads(raw), {key.lower(): value for key, value in response.headers.items()}
        except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
            raise CoordinationError("CODE_SOURCE_UNAVAILABLE") from None

    @staticmethod
    def _event(reference: ReviewReference | HostedDiscussion, kind: str, data: Json, at: str) -> CodeEvent:
        """事件 ID 包括内容版本，重复补读不唤醒，编辑、评审表态和 CI 变化会产生新事实。"""
        body = json.dumps(data, ensure_ascii=False, sort_keys=True)
        identity = hashlib.sha256(f"{reference.record_id}\0{kind}\0{body}".encode()).hexdigest()
        return CodeEvent(identity, kind, body, at)

    async def _github_pages(
        self,
        identity: HostedReviewIdentity,
        path: str,
        *,
        since: str | None = None,
        page: int = 1,
        collection: str | None = None,
        parameters: dict[str, str] | None = None,
    ) -> tuple[list[Json], int]:
        rows: list[Json] = []
        for _ in range(5):
            params = {**(parameters or {}), "per_page": "100", "page": str(page)}
            if since:
                params["since"] = since
            value, headers = await self._request(identity, path, params=params)
            rows.extend(_rows(_object(value).get(collection)) if collection else _rows(value))
            if 'rel="next"' not in headers.get("link", ""):
                return rows, page
            # 只增加登记 API 的页号，不跟随服务端返回的任意 URL。
            page += 1
        raise CoordinationError("CODE_SOURCE_READ_INCOMPLETE")

    async def _codeup_comments(self, identity: HostedReviewIdentity, path: str) -> list[Json]:
        """OAPI 的子评论可能只给 ID；有界补读正文，缺失或分页不明时不伪称完整。"""
        pending = _rows((await self._request(identity, path, method="POST", body={"state": "OPENED"}))[0])
        comments: dict[str, Json] = {}
        requested: set[str] = set()
        requests = 0
        while pending:
            comment = pending.pop(0)
            identity_key = _text(comment.get("comment_biz_id"), 256)
            if not identity_key:
                raise CoordinationError("CODE_SOURCE_PROTOCOL")
            if identity_key in comments:
                continue
            comments[identity_key] = comment
            children = comment.get("child_comments_list", [])
            if not isinstance(children, list) or len(children) > 1000 or len(comments) > 1000:
                raise CoordinationError("CODE_SOURCE_READ_INCOMPLETE")
            if any(not isinstance(child, (dict, str)) for child in children):
                raise CoordinationError("CODE_SOURCE_PROTOCOL")
            pending.extend(_object(child) for child in children if isinstance(child, dict))
            missing = list(
                dict.fromkeys(
                    child
                    for child in children
                    if isinstance(child, str) and child not in comments and child not in requested
                )
            )
            if len(missing) + len(comments) > 1000:
                raise CoordinationError("CODE_SOURCE_READ_INCOMPLETE")
            for offset in range(0, len(missing), 100):
                requests += 1
                if requests > 20:
                    raise CoordinationError("CODE_SOURCE_READ_INCOMPLETE")
                wanted = missing[offset : offset + 100]
                loaded = _rows(
                    (
                        await self._request(
                            identity, path, method="POST", body={"state": "OPENED", "comment_biz_id_list": wanted}
                        )
                    )[0]
                )
                if set(wanted) - {str(item.get("comment_biz_id")) for item in loaded}:
                    raise CoordinationError("CODE_SOURCE_READ_INCOMPLETE")
                requested.update(wanted)
                pending.extend(loaded)
        return list(comments.values())


class HostedReviewBodies(HostedHTTP):
    """共享供应商正文读取，调用方分别校验原生交付或仓库来源授权；不伪造原生 Run。"""

    async def _github_review(
        self,
        identity: HostedReviewIdentity,
        reference: ReviewReference | HostedDiscussion,
        cursor: Json,
    ) -> ReviewSnapshot:
        """保留无正文评审、行内位置和 CI 版本；评论用更新时间游标，评审从上轮尾页继续。"""
        started = datetime.now(UTC)
        at = started.isoformat()
        prefix = "/repos/" + "/".join(quote(part, safe="") for part in identity.repository.split("/"))
        number = reference.remote_id
        pull = _object((await self._request(identity, f"{prefix}/pulls/{number}"))[0])
        if str(pull.get("number")) != number or pull.get("html_url") != reference.url:
            raise CoordinationError("CODE_SOURCE_REVIEW_MISMATCH")
        state = pull.get("state")
        if state not in {"open", "closed"}:
            raise CoordinationError("CODE_SOURCE_PROTOCOL")
        head = _object(pull.get("head"))
        sha = _text(head.get("sha"), 64)
        if len(sha) not in {40, 64} or any(char not in "0123456789abcdef" for char in sha):
            raise CoordinationError("CODE_SOURCE_PROTOCOL")
        events = [
            self._event(
                reference,
                "pull",
                {
                    "state": state,
                    "merged": pull.get("merged") is True,
                    "head": sha,
                    "title": _text(pull.get("title"), 300),
                },
                at,
            )
        ]
        since = cast(str | None, cursor.get("since"))
        for kind, path in (("comment", f"issues/{number}/comments"), ("inline", f"pulls/{number}/comments")):
            rows, _ = await self._github_pages(identity, f"{prefix}/{path}", since=since)
            for row in rows:
                user = _object(row.get("user") or {"login": "ghost"})
                events.append(
                    self._event(
                        reference,
                        kind,
                        {
                            "id": str(row.get("id")),
                            "author": _text(user.get("login"), 256),
                            "body": _text(row.get("body")),
                            "path": row.get("path"),
                            "line": row.get("line"),
                            "diff_hunk": _text(row.get("diff_hunk"), 1200),
                            "parent": row.get("in_reply_to_id"),
                            "updated_at": row.get("updated_at"),
                        },
                        at,
                    )
                )
        reviews, last = await self._github_pages(
            identity,
            f"{prefix}/pulls/{number}/reviews",
            page=int(str(cursor.get("review_page", 1))),
        )
        for review in reviews:
            events.append(
                self._event(
                    reference,
                    "review",
                    {
                        "id": str(review.get("id")),
                        "state": review.get("state"),
                        "commit_id": review.get("commit_id"),
                        "body": _text(review.get("body")),
                        "author": _object(review.get("user") or {"login": "ghost"}).get("login"),
                        "submitted_at": review.get("submitted_at"),
                    },
                    at,
                )
            )
        checks, _ = await self._github_pages(identity, f"{prefix}/commits/{sha}/check-runs", collection="check_runs")
        statuses, _ = await self._github_pages(identity, f"{prefix}/commits/{sha}/statuses")
        for check in checks + statuses:
            output = _object(check.get("output", {}))
            events.append(
                self._event(
                    reference,
                    "ci",
                    {
                        "head": sha,
                        "id": str(check.get("id")),
                        "name": check.get("name", check.get("context")),
                        "state": check.get("conclusion", check.get("state", check.get("status"))),
                        "description": _text(check.get("description", output.get("summary")), 2000),
                    },
                    at,
                )
            )
        outcome: Literal["open", "closed", "merged"] = (
            "open" if state == "open" else "merged" if pull.get("merged") is True else "closed"
        )
        return ReviewSnapshot(
            outcome,
            at,
            tuple(events),
            {
                "since": (started - timedelta(seconds=5)).isoformat(),
                "review_page": last,
            },
            closed_at=_instant(pull.get("closed_at") or pull.get("merged_at")),
        )

    async def _codeup_review(
        self,
        identity: HostedReviewIdentity,
        reference: ReviewReference | HostedDiscussion,
        cursor: Json,
    ) -> ReviewSnapshot:
        """OAPI 评论没有 since 参数：摘要变化时读取，十分钟补偿一次编辑，未完成读取不推进游标。"""
        now = datetime.now(UTC)
        at = now.isoformat()
        prefix = "/oapi/v1/codeup"
        if identity.organization:
            prefix += "/organizations/" + quote(identity.organization, safe="")
        prefix += "/repositories/" + quote(identity.repository, safe="") + "/changeRequests/" + reference.remote_id
        row = _object((await self._request(identity, prefix))[0])
        if str(row.get("localId")) != reference.remote_id:
            raise CoordinationError("CODE_SOURCE_REVIEW_MISMATCH")
        states: dict[str, Literal["open", "closed", "merged"]] = {
            "UNDER_DEV": "open",
            "UNDER_REVIEW": "open",
            "TO_BE_MERGED": "open",
            "CLOSED": "closed",
            "MERGED": "merged",
        }
        outcome = states.get(str(row.get("state")))
        if not outcome:
            raise CoordinationError("CODE_SOURCE_PROTOCOL")
        events = [
            self._event(
                reference,
                "pull",
                {
                    "state": outcome,
                    "revision": row.get("mergedRevision"),
                    "title": _text(row.get("title"), 300),
                },
                at,
            )
        ]
        for reviewer in _rows(row.get("reviewers", [])):
            if reviewer.get("hasReviewed") is True:
                events.append(
                    self._event(
                        reference,
                        "review",
                        {
                            "author": reviewer.get("userId"),
                            "state": reviewer.get("reviewOpinionStatus"),
                            "review_time": reviewer.get("reviewTime"),
                            "basis": "provider_current_review",
                        },
                        at,
                    )
                )
        signature = json.dumps([row.get("totalCommentCount"), row.get("unResolvedCommentCount"), row.get("updatedAt")])
        checked = datetime.fromisoformat(str(cursor.get("comments_checked_at", "2000-01-01T00:00:00+00:00")))
        next_cursor = dict(cursor)
        if cursor.get("comments_signature") != signature or now - checked >= timedelta(minutes=10):
            for comment in await self._codeup_comments(identity, prefix + "/comments/list"):
                events.append(
                    self._event(
                        reference,
                        "comment",
                        {
                            "id": comment.get("comment_biz_id"),
                            "body": _text(comment.get("content")),
                            "path": comment.get("filePath"),
                            "line": comment.get("line_number"),
                            "deleted": comment.get("is_deleted"),
                            "resolved": comment.get("resolved"),
                            "edited_at": comment.get("last_edit_time"),
                            "parent": comment.get("parent_comment_biz_id"),
                        },
                        at,
                    )
                )
            next_cursor.update(comments_signature=signature, comments_checked_at=at)
        patches = _rows((await self._request(identity, prefix + "/diffs/patches"))[0])
        sources = [patch for patch in patches if patch.get("relatedMergeItemType") == "MERGE_SOURCE"]
        if sources:
            latest = max(sources, key=lambda patch: int(str(patch.get("versionNo", 0))))
            patch_id = _text(latest.get("patchSetBizId"), 256)
            if not patch_id:
                raise CoordinationError("CODE_SOURCE_PROTOCOL")
            ci = _object(
                (await self._request(identity, prefix + "/ciCheckList", params={"patchSetBizId": patch_id}))[0]
            )
            events.append(
                self._event(
                    reference,
                    "ci",
                    {
                        "head": latest.get("commitId"),
                        "patch_id": patch_id,
                        "state": ci.get("status"),
                        "counts": ci.get("checkStatInfo"),
                        "checks": [
                            {"name": item.get("taskName"), "state": item.get("state")}
                            for item in _rows(ci.get("checkList", []))
                        ],
                        "statuses": [
                            {"name": item.get("context"), "state": item.get("state")}
                            for item in _rows(ci.get("commitStatuses", []))
                        ],
                    },
                    at,
                )
            )
        else:
            events.append(self._event(reference, "ci", {"state": "unknown", "reason": "no_source_patch"}, at))
        return ReviewSnapshot(outcome, at, tuple(events), next_cursor)


class HostedReviewEvents(HostedReviewBodies):
    """每轮重新取得受信连接，增量游标与消费事务一起保存；模型无权指定 API 或凭据。"""

    def __init__(self, identity: Callable[[RunActor, ReviewReference], Awaitable[HostedReviewIdentity]]) -> None:
        self.identity = identity

    async def _authorized_identity(self, actor: RunActor, reference: ReviewReference) -> HostedReviewIdentity:
        identity = await self.identity(actor, reference)
        number = reference.remote_id
        if not number.isdecimal() or int(number) <= 0:
            raise CoordinationError("CODE_SOURCE_REVIEW_INVALID")
        expected = identity.web_url.rstrip("/") + ("/pull/" if identity.provider == "github" else "/change/") + number
        if reference.url != expected:
            raise CoordinationError("CODE_SOURCE_REVIEW_MISMATCH")
        return identity

    async def authorize(self, actor: RunActor, reference: ReviewReference) -> None:
        """缓存不是持久权限：每次读取都复核同一连接和冻结的仓库绑定。"""
        await self._authorized_identity(actor, reference)

    async def read(self, actor: RunActor, reference: ReviewReference, cursor: Json) -> ReviewSnapshot:
        if not isinstance(cursor, dict):
            raise CoordinationError("CODE_SOURCE_PROTOCOL")
        identity = await self._authorized_identity(actor, reference)
        try:
            if identity.provider == "github":
                result = await self._github_review(identity, reference, cursor)
            else:
                result = await self._codeup_review(identity, reference, cursor)
        except (ValueError, TypeError, OverflowError):
            raise CoordinationError("CODE_SOURCE_PROTOCOL") from None
        await self.authorize(actor, reference)
        return result
