"""外部创建的 PR 也补读 CI；仓库消息游标、候选开关和自动事实不能丢掉订阅语义。"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.api.test_runs import runs as runs
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from tests.coordination.test_engine import result
from tests.coordination.test_hosting_events import ScriptedRepository
from tests.coordination.test_repository_source import accept, configure, message
from weora.modules.coordination.domain import CoordinationError, Json, Report, RepositorySource, State
from weora.modules.coordination.engine import Engine
from weora.modules.coordination.hosting import CodeEvent, ReviewSnapshot
from weora.modules.coordination.hosting_http import HostedDiscussion, HostedReviewIdentity
from weora.modules.coordination.polling import compensate


@pytest.mark.anyio
@pytest.mark.parametrize("blocked", [False, True])
async def test_owned_external_pull_keeps_ci_cursor_full_note_and_waits_for_human(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch, blocked: bool
) -> None:
    h = contextual
    service, scope_id, reader, _ = await configure(h)
    root = replace(message("root"), is_pull=True, url="https://github.com/team/configurable-repo/pull/42")
    reader.messages = (root,)
    await compensate(service, h.actor.workspace_id, scope_id)
    ticket_id = await accept(h, service, scope_id)
    if blocked:

        def wait(engine: Engine, state: State) -> None:
            ticket = engine.ticket(ticket_id)
            engine.report(ticket_id, ticket.assignments[-1].id, Report("blocked", "等人类确认", engine.now()))

        await service.store.transact(h.actor.workspace_id, scope_id, wait)
    before = await service.store.snapshot(h.actor.workspace_id, scope_id)
    generation = next(item.generation for item in before.effects if item.kind == "worker")
    body = "CI 失败的完整诊断\n" * 1000
    cursors: list[Json] = []

    async def discussion(identity: HostedReviewIdentity, locator: HostedDiscussion, cursor: Json) -> ReviewSnapshot:
        assert locator.remote_id == "42" and locator.url == root.url
        cursors.append(cursor)
        return ReviewSnapshot(
            "open", datetime.now(UTC).isoformat(), (CodeEvent("ci-1", "ci", body, root.at),), {"version": 1}
        )

    monkeypatch.setattr(reader, "discussion", discussion)
    reader.messages = ()  # 没有任何新仓库消息，仍必须读取已有 PR 的 CI。
    await compensate(service, h.actor.workspace_id, scope_id)
    first = await service.store.snapshot(h.actor.workspace_id, scope_id)
    notes = [item for item in first.tickets[0].inputs if item.kind == "note"]
    assert "".join((item.text or "").split("\n", 1)[1] for item in notes) == body
    assert all(not item.human for item in notes)
    if blocked:
        assert all(item.id not in first.tickets[0].undelivered for item in notes)
        assert next(item.generation for item in first.effects if item.kind == "worker") == generation
    await compensate(service, h.actor.workspace_id, scope_id)
    second = await service.store.snapshot(h.actor.workspace_id, scope_id)
    assert len(second.tickets[0].inputs) == len(first.tickets[0].inputs)
    assert cursors[1] == {"version": 1}
    if blocked:
        reader.messages = (replace(root, id="human-followup", text="继续修复", at=datetime.now(UTC).isoformat()),)
        await compensate(service, h.actor.workspace_id, scope_id)
        resumed = await service.store.snapshot(h.actor.workspace_id, scope_id)
        assert len(resumed.tickets) == 1
        assert next(item.generation for item in resumed.effects if item.kind == "worker") > generation


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["github", "codeup"])
async def test_repository_discussion_reads_real_provider_shape_without_native_delivery(provider: str) -> None:
    if provider == "github":
        identity = HostedReviewIdentity(
            "github", "https://api.github.com", "team/repo", "https://github.com/team/repo", "test"
        )
        locator = HostedDiscussion(uuid4(), "9", identity.web_url + "/pull/9")
        prefix, sha = "/repos/team/repo", "a" * 40
        responses: dict[str, object] = {
            prefix + "/pulls/9": {"number": 9, "html_url": locator.url, "state": "open", "head": {"sha": sha}},
            prefix + "/issues/9/comments": [],
            prefix + "/pulls/9/comments": [],
            prefix + "/pulls/9/reviews": [],
            prefix + f"/commits/{sha}/check-runs": {
                "check_runs": [{"id": 1, "name": "tests", "conclusion": "failure"}]
            },
            prefix + f"/commits/{sha}/statuses": [],
        }
    else:
        identity = HostedReviewIdentity(
            "codeup", "https://api.example.test", "123", "https://codeup.example.test/team/repo", "test", "org"
        )
        locator = HostedDiscussion(uuid4(), "9", identity.web_url + "/change/9")
        prefix = "/oapi/v1/codeup/organizations/org/repositories/123/changeRequests/9"
        responses = {
            prefix: {
                "localId": 9,
                "state": "UNDER_REVIEW",
                "updatedAt": datetime.now(UTC).isoformat(),
                "reviewers": [],
            },
            prefix + "/comments/list": [],
            prefix + "/diffs/patches": [
                {"relatedMergeItemType": "MERGE_SOURCE", "versionNo": 1, "patchSetBizId": "patch", "commitId": "a" * 40}
            ],
            prefix + "/ciCheckList": {"status": "FAIL", "checkList": [{"taskName": "tests", "state": "FAIL"}]},
        }
    observed = await ScriptedRepository(responses).discussion(identity, locator, {})
    assert observed.outcome == "open" and any(item.kind == "ci" for item in observed.events)


@pytest.mark.anyio
@pytest.mark.parametrize("is_pull", [False, True])
async def test_followed_discussion_receives_unmentioned_human_comment_even_when_global_comments_disabled(
    is_pull: bool,
) -> None:
    identity = HostedReviewIdentity(
        "github", "https://api.github.com", "team/repo", "https://github.com/team/repo", "test"
    )
    now = datetime.now(UTC).isoformat()
    prefix = "/repos/team/repo"
    reader = ScriptedRepository(
        {
            prefix + "/issues": [],
            prefix + "/pulls/comments": [],
            prefix + "/issues/comments": [
                {
                    "id": 1,
                    "issue_url": identity.api_origin + prefix + "/issues/9",
                    "updated_at": now,
                    "body": "继续修复",
                    "user": {"login": "author", "type": "User"},
                }
            ],
            prefix + "/issues/9": {
                "number": 9,
                "pull_request": {} if is_pull else None,
                "title": "原讨论",
                "body": "原范围",
            },
        }
    )
    source = RepositorySource("repo", "remote", "weora", {"author": "user"}, observe_comments=False)
    ordinary = await reader.poll(identity, source, {"since": now})
    followed = await reader.poll(identity, source, {"since": now, "followed_numbers": ["9"]})
    assert not ordinary.messages and len(followed.messages) == 1 and followed.messages[0].human


@pytest.mark.anyio
async def test_one_external_pull_failure_does_not_advance_its_cursor_or_block_healthy_pull(
    contextual: ContextRuns, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = contextual
    service, scope_id, reader, _ = await configure(h)
    base = "https://github.com/team/configurable-repo/pull/"
    reader.messages = tuple(
        replace(message("root-" + number), number=number, is_pull=True, url=base + number) for number in ("42", "43")
    )
    await compensate(service, h.actor.workspace_id, scope_id)
    evaluations = await service.store.transact(h.actor.workspace_id, scope_id, lambda engine, _: engine.pass_due())
    for evaluation in evaluations:
        await service.tool(
            h.actor.workspace_id,
            scope_id,
            "read-" + evaluation.id,
            "orchestrator_eval_read",
            {"eval_id": evaluation.id},
        )
        decided = await service.tool(
            h.actor.workspace_id,
            scope_id,
            "decide-" + evaluation.id,
            "orchestrator_eval_decide",
            dict(result(), eval_id=evaluation.id),
        )
        assert decided["ok"] is True

    async def discussion(identity: HostedReviewIdentity, locator: HostedDiscussion, cursor: Json) -> ReviewSnapshot:
        if locator.remote_id == "42":
            raise CoordinationError("CODE_SOURCE_UNAVAILABLE")
        return ReviewSnapshot(
            "open",
            datetime.now(UTC).isoformat(),
            (CodeEvent("healthy-ci", "ci", "健康线程的新 CI 事实", datetime.now(UTC).isoformat()),),
            {"healthy": True},
        )

    monkeypatch.setattr(reader, "discussion", discussion)
    reader.messages = ()
    await compensate(service, h.actor.workspace_id, scope_id)
    state = await service.store.snapshot(h.actor.workspace_id, scope_id)
    assert len(state.tickets) == 2
    for ticket in state.tickets:
        origin = next(item for item in state.triggers if item.id == ticket.origin_id)
        thread = origin.threads[0]
        if origin.origin["number"] == "42":
            assert f"repository:discussion:{thread}" not in state.checkpoints
            assert state.checkpoints[f"code_poll:error:{thread}"]
        else:
            assert state.checkpoints[f"repository:discussion:{thread}"] == {"healthy": True}
            assert any("健康线程的新 CI 事实" in (item.text or "") for item in ticket.inputs)
