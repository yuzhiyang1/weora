"""供应商契约反例；固定上游 GitHub 行为和官方 Codeup OAPI 字段，完全不访问网络。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.delivery.test_service import scope
from weora.modules.coordination.domain import CoordinationError, Json, RepositorySource
from weora.modules.coordination.hosting_http import HostedReviewEvents, HostedReviewIdentity
from weora.modules.coordination.repository_http import RepositoryEvents
from weora.modules.delivery.application.ports import ReviewReference
from weora.modules.runs.application.ports import RunActor


class ScriptedReview(HostedReviewEvents):
    def __init__(self, identity: HostedReviewIdentity) -> None:
        async def resolve(actor: RunActor, reference: ReviewReference) -> HostedReviewIdentity:
            return identity

        super().__init__(resolve)
        self.calls: list[tuple[str, str, object]] = []
        self.responses: dict[str, object] = {}
        self.links: dict[str, str] = {}

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
        self.calls.append((path, method, body or params))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(body), {}
        return response, {"link": self.links.get(path, "")}


@pytest.mark.anyio
async def test_github_bodyless_review_inline_place_ci_versions_and_incremental_cursor() -> None:
    identity = HostedReviewIdentity(
        "github", "https://api.github.com", "team/repo", "https://github.com/team/repo", "secret"
    )
    reader = ScriptedReview(identity)
    actor, reference = (
        RunActor(uuid4(), uuid4()),
        ReviewReference(uuid4(), uuid4(), scope(), "12", identity.web_url + "/pull/12"),
    )
    prefix, sha = "/repos/team/repo", "a" * 40
    reader.responses = {
        prefix + "/pulls/12": {
            "number": 12,
            "html_url": reference.url,
            "state": "open",
            "head": {"sha": sha},
            "title": "fix",
        },
        prefix + "/issues/12/comments": [{"id": 1, "user": None, "body": "追问", "updated_at": "2026-11-04T00:00:00Z"}],
        prefix + "/pulls/12/comments": [
            {
                "id": 2,
                "user": {"login": "reviewer"},
                "body": "这里有问题",
                "path": "src/x.py",
                "line": 7,
                "diff_hunk": "@@\n+ x",
                "in_reply_to_id": 3,
            }
        ],
        prefix + "/pulls/12/reviews": [
            {"id": 3, "state": "APPROVED", "commit_id": sha, "body": "", "user": {"login": "reviewer"}}
        ],
        prefix + f"/commits/{sha}/check-runs": {
            "check_runs": [{"id": 4, "name": "tests", "conclusion": "failure", "output": {"summary": "测试失败"}}]
        },
        prefix + f"/commits/{sha}/statuses": [],
    }
    first = await reader.read(actor, reference, {})
    assert first.outcome == "open" and first.cursor["since"]
    facts = {item.kind: json.loads(item.text) for item in first.events}
    assert facts["review"]["state"] == "APPROVED" and facts["review"]["body"] == ""
    assert facts["inline"]["path"] == "src/x.py" and facts["inline"]["parent"] == 3
    assert facts["ci"]["state"] == "failure" and facts["comment"]["author"] == "ghost"
    second = await reader.read(actor, reference, first.cursor)
    assert [item.id for item in first.events] == [item.id for item in second.events]
    assert any(isinstance(args, dict) and args.get("since") == first.cursor["since"] for _, _, args in reader.calls)
    reader.responses[prefix + "/pulls/12"] = {
        "number": 12,
        "html_url": reference.url,
        "state": "closed",
        "merged": True,
        "head": {"sha": sha},
    }
    assert (await reader.read(actor, reference, second.cursor)).outcome == "merged"


@pytest.mark.anyio
async def test_codeup_uses_official_state_and_updated_at_and_loads_child_comment_ids() -> None:
    identity = HostedReviewIdentity(
        "codeup", "https://openapi.example.com", "123", "https://codeup.example.com/team/repo", "secret", "org"
    )
    reader = ScriptedReview(identity)
    actor, reference = (
        RunActor(uuid4(), uuid4()),
        ReviewReference(uuid4(), uuid4(), scope(), "9", identity.web_url + "/change/9"),
    )
    prefix = "/oapi/v1/codeup/organizations/org/repositories/123/changeRequests/9"
    root = {"comment_biz_id": "root", "content": "根评论", "child_comments_list": ["child"]}
    child = {"comment_biz_id": "child", "content": "子评论", "parent_comment_biz_id": "root"}
    reader.responses = {
        prefix: {
            "localId": 9,
            "state": "CLOSED",
            "updatedAt": "2026-11-04T00:00:00Z",
            "totalCommentCount": 2,
            "reviewers": [{"hasReviewed": True, "userId": "reviewer", "reviewOpinionStatus": "NOT_PASS"}],
        },
        prefix + "/comments/list": lambda body: [child] if body and body.get("comment_biz_id_list") else [root],
        prefix + "/diffs/patches": [
            {"relatedMergeItemType": "MERGE_SOURCE", "patchSetBizId": "patch", "versionNo": 2, "commitId": "a" * 40}
        ],
        prefix + "/ciCheckList": {"status": "FAIL", "checkList": [{"taskName": "build", "state": "FAIL"}]},
    }
    observed = await reader.read(actor, reference, {})
    assert observed.outcome == "closed"
    comments = [json.loads(event.text) for event in observed.events if event.kind == "comment"]
    assert {item["id"] for item in comments} == {"root", "child"}
    calls = len([path for path, _, _ in reader.calls if path.endswith("comments/list")])
    await reader.read(actor, reference, observed.cursor)
    assert len([path for path, _, _ in reader.calls if path.endswith("comments/list")]) == calls


@pytest.mark.anyio
async def test_pagination_does_not_follow_arbitrary_link_and_malformed_cursor_cannot_look_complete() -> None:
    identity = HostedReviewIdentity(
        "github", "https://api.github.com", "team/repo", "https://github.com/team/repo", "secret"
    )
    reader = ScriptedReview(identity)
    reader.responses["/comments"] = []
    reader.links["/comments"] = '<https://steal.example/secrets>; rel="next"'
    with pytest.raises(CoordinationError, match="CODE_SOURCE_READ_INCOMPLETE"):
        await reader._github_pages(identity, "/comments")
    assert {path for path, _, _ in reader.calls} == {"/comments"}


class ScriptedRepository(RepositoryEvents):
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

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
        return self.responses[path], {}


@pytest.mark.anyio
async def test_repository_observation_keeps_unmentioned_and_bodyless_reviews_but_rejects_foreign_reply_target() -> None:
    now = datetime.now(UTC)
    at = now.isoformat()
    identity = HostedReviewIdentity(
        "github", "https://api.github.com", "team/repo", "https://github.com/team/repo", "secret"
    )
    source = RepositorySource("repo", "remote", "weora", {"author": "user"}, observe_pulls=True, observe_comments=True)
    prefix = "/repos/team/repo"
    reader = ScriptedRepository(
        {
            prefix + "/issues": [
                {
                    "id": 1,
                    "number": 3,
                    "pull_request": {},
                    "created_at": at,
                    "title": "请修复",
                    "body": "沿用交互",
                    "user": {"login": "author", "type": "User"},
                }
            ],
            prefix + "/pulls/3/reviews": [
                {
                    "id": 2,
                    "submitted_at": at,
                    "state": "CHANGES_REQUESTED",
                    "body": "",
                    "user": {"login": "author", "type": "User"},
                }
            ],
            prefix + "/pulls/3": {"number": 3, "state": "open"},
            prefix + "/issues/comments": [],
            prefix + "/pulls/comments": [],
        }
    )
    snapshot = await reader.poll(identity, source, {"since": (now - timedelta(seconds=10)).isoformat()})
    assert {item.kind for item in snapshot.messages} == {"pull", "review"}
    assert all(not reader.mentions(item.text, source.agent_login) for item in snapshot.messages)
    reader.responses[prefix + "/issues/comments"] = [
        {"id": 7, "issue_url": "https://api.github.com/repos/other/repo/issues/3"}
    ]
    with pytest.raises(CoordinationError, match="CODE_SOURCE_REVIEW_MISMATCH"):
        await reader.poll(identity, source, {"since": (now - timedelta(seconds=10)).isoformat()})


def test_mentions_reject_email_and_longer_login() -> None:
    assert RepositoryEvents.mentions("请 @weora 修复", "weora")
    assert not RepositoryEvents.mentions("mail@weora.example @weora-long", "weora")
