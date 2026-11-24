"""覆盖飞书 @ 入站、Agent 工具装配和知识审核之间的实际边界。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from tests.api.test_projects import project, repository
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import member
from tests.context.test_context import inbound
from tests.context.test_run_integration import ContextRuns
from tests.context.test_run_integration import contextual as contextual
from weora.bootstrap.worker import _pump_context
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import Json, Message
from weora.modules.runs.application.ports import RunActor
from weora.modules.runs.infrastructure.execution_access import ModulesExecutionAccess
from weora.modules.runs.infrastructure.unit_of_work import SqlAlchemyRunsUnitOfWork
from weora.modules.tools.application.gateway import ToolScope

pytestmark = pytest.mark.anyio

FIND = "knowledge.find_revision_target"
SUBMIT = "knowledge.submit_revision"
OLD = "XX 规则适用于练习中心和模考。"
NEW = "XX 规则继续适用于练习中心，模考停用该规则。"


async def prepare_group(h: ContextRuns) -> dict[str, str]:
    project_id = str((await project(h.original))["id"])
    repo = await repository(h.original, project_id, "knowledge-revision")
    route = {
        "source_id": str(h.source_id), "agent_id": h.original.agent_id, "agent_version": "0.1.0",
        "project_id": project_id, "repository_ids": [repo["id"]], "share_project_with_group": True,
    }
    saved = await h.client.post(h.base + "/context/routes", json=route, headers=h.headers)
    assert saved.status_code == 200, saved.text
    await h.context.probe(h.context_actor, h.connection_id)
    await h.context.bind_identity(h.context_actor, h.connection_id, "ou_user", h.actor.user_id)
    return {"project_id": project_id}


async def send(h: ContextRuns, instruction: str, *, quote: str | None = None, open_id: str = "ou_user") -> UUID:
    key = uuid4().hex
    event = inbound(key, f"om_{key}", open_id)
    message = cast(Json, cast(Json, event["event"])["message"])
    message["content"] = json.dumps({"text": "@_user_1 " + instruction})
    if quote is not None:
        message["parent_id"] = quote
    received = await FeishuChannels(h.context).receive(h.actor.workspace_id, h.connection_id, event)
    await _pump_context(h.app)
    inbox = next(item for item in await h.context.store.list(h.actor.workspace_id, "inbox")
                 if item["id"] == received["inbox_id"])
    assert inbox["status"] == "SUBMITTED"
    return UUID(str(inbox["run_id"]))


def execution_access(h: ContextRuns) -> ModulesExecutionAccess:
    return ModulesExecutionAccess(
        identity=h.app.state.identity_service, agents=h.app.state.agent_registry_service,
        uow_factory=lambda: SqlAlchemyRunsUnitOfWork(h.original.sessions), context=h.context,
        executions=h.app.state.execution_store, gateway=h.app.state.tool_gateway,
        projects=h.app.state.context_access.projects,
    )


async def publish_rule(h: ContextRuns, scope: dict[str, str]) -> str:
    root = h.base + "/knowledge"
    draft = await h.client.post(root, params=scope, headers=h.headers, json={
        "operation_id": str(uuid4()), "kind": "rule", "title": "mockcenter XX 规则", "tags": ["考试"],
        "body": f"# 适用范围\n\n{OLD}\n\n保留其他内容。",
        "source": {"type": "wiki", "ref": "https://wiki.example.test/original",
                   "resource_path": "rules/exam.md"},
        "rule_paths": ["exam/**"], "related": {"REFERENCES": ["docs/exam.md"]},
    })
    assert draft.status_code == 201, draft.text
    entry = str(draft.json()["id"])
    reviewed = await h.client.post(f"{root}/{entry}/review", params=scope, headers=h.headers, json={
        "revision": 1, "decision": "accept", "reason": "初始规则",
    })
    assert reviewed.status_code == 200, reviewed.text
    return entry


async def test_mention_creates_pending_revision_with_verified_evidence_and_reply(contextual: ContextRuns) -> None:
    h = contextual
    scope = await prepare_group(h)
    configured = await h.client.put(h.base + "/coordination/config", headers=h.headers, json={
        "source_id": str(h.source_id), "route_key": f"{h.connection_id}:oc_project:-",
        "main_agent_id": h.original.agent_id, "main_agent_version": "0.1.0",
        "worker_agent_id": h.original.agent_id, "worker_agent_version": "0.1.0",
        "allowed_actors": ["ou_user"], "rules": [], "expected_revision": 0, "mode": "off",
    })
    assert configured.status_code == 200 and configured.json()["config"]["enabled"] is True, configured.text
    entry = await publish_rule(h, scope)
    shared = await publish_rule(h, {})
    root = h.base + "/knowledge"
    prd = "https://wiki.example.test/exam?from=group#mock"
    h.reader.single = Message(
        "om_confirm", "oc_project", None, "ou_product", datetime.now(UTC) - timedelta(minutes=1),
        f"好的，练习中心保留，模考关闭这条规则。{prd}",
    )
    run_id = await send(h, "请帮我把产品确认的 XX 规则更新到项目知识库", quote="om_confirm")
    detail = await h.app.state.run_service.access_metadata(h.actor, run_id)
    runtime_input = await execution_access(h).load_input(detail.run)
    assert {FIND, SUBMIT} <= {tool.name for tool in runtime_input.tools}
    assert "模考关闭这条规则" in runtime_input.instruction
    execution = h.app.state.execution_store
    lease = await execution.claim(workspace_id=h.actor.workspace_id, run_id=run_id, owner="knowledge", lease_seconds=60)
    assert lease is not None
    tool = ToolScope(run_id, h.actor, lease.owner, lease.fencing_token)
    gateway = h.app.state.tool_gateway
    found = await gateway.invoke(tool, "find", FIND, {"query": "mockcenter"})
    assert [candidate["id"] for candidate in found["items"]] == [entry]
    denied = await gateway.invoke(tool, "shared", FIND, {"id": shared})
    assert denied["error"]["code"] == "KNOWLEDGE_NOT_FOUND"
    baseline = await gateway.invoke(tool, "read", FIND, {"id": entry})
    assert baseline["revision"] == 1 and OLD in baseline["body"]
    args = {"id": entry, "base_revision": 1, "expected_text": OLD, "replacement": NEW, "prd_url": prd}
    shared_write = await gateway.invoke(tool, "shared-write", SUBMIT, args | {"id": shared})
    assert shared_write["error"]["code"] == "KNOWLEDGE_NOT_FOUND"
    fabricated = await gateway.invoke(tool, "fabricated", SUBMIT, args | {"prd_url": f"{prd}-fabricated"})
    assert fabricated["error"]["code"] == "KNOWLEDGE_FEISHU_PRD_UNVERIFIED"
    unmatched = await gateway.invoke(tool, "unmatched", SUBMIT, args | {"expected_text": "不存在的原文"})
    assert unmatched["error"]["code"] == "KNOWLEDGE_FRAGMENT_NOT_UNIQUE"
    receipt = await gateway.invoke(tool, "revise", SUBMIT, args)
    assert receipt["id"] == entry and receipt["revision"] == 2 and receipt["revision_status"] == "pending", receipt
    assert "body" not in receipt
    assert await gateway.invoke(tool, "another-call", SUBMIT, args) == receipt

    active = (await h.client.get(f"{root}/{entry}", params=scope)).json()
    assert active["revision"] == 1 and OLD in active["body"]
    pending = await h.client.get(f"{root}/{entry}/review", params=scope | {"revision": "2"})
    assert pending.status_code == 200, pending.text
    saved = pending.json()
    assert saved["body"] == active["body"].replace(OLD, NEW)
    assert saved["rule_paths"] == ["exam/**"] and saved["tags"] == ["考试"]
    assert saved["source"] == active["source"] | {
        "feishu_quote_id": "om_confirm", "feishu_source_id": str(h.source_id),
        "feishu_message_id": detail.task.origin_metadata["message_id"],
        "feishu_prd_ref": "https://wiki.example.test/exam#mock",
    }
    assert await execution.finish(lease, status="SUCCEEDED", output="已提交项目知识修订，版本 2 待审核。")
    await _pump_context(h.app)
    deliveries = await h.context.store.list(h.actor.workspace_id, "delivery")
    sent = next(row for row in deliveries if row.get("business_key") == f"run-result:{run_id}")
    assert sent["status"] == "SENT"
    assert h.context.cipher.decrypt(str(sent["text"])) == "已提交项目知识修订，版本 2 待审核。"
    approved = await h.client.post(f"{root}/{entry}/review", params=scope, headers=h.headers, json={
        "revision": 2, "decision": "accept", "reason": "核对了产品确认",
    })
    assert approved.status_code == 200, approved.text
    current = (await h.client.get(f"{root}/{entry}", params=scope)).json()
    assert current["revision"] == 2 and NEW in current["body"]


async def test_unrelated_mention_and_read_only_recovery_cannot_submit(contextual: ContextRuns) -> None:
    h = contextual
    await prepare_group(h)
    run_id = await send(h, "怎么更新知识库？")
    detail = await h.app.state.run_service.access_metadata(h.actor, run_id)
    payload = await execution_access(h).load_input(detail.run)
    assert not {FIND, SUBMIT} & {tool.name for tool in payload.tools}
    lease = await h.app.state.execution_store.claim(
        workspace_id=h.actor.workspace_id, run_id=run_id, owner="unrelated", lease_seconds=60,
    )
    assert lease is not None
    tool = ToolScope(run_id, h.actor, lease.owner, lease.fencing_token)
    args = {"id": str(uuid4()), "base_revision": 1, "expected_text": OLD, "replacement": NEW}
    gateway = h.app.state.tool_gateway
    denied = await gateway.invoke(tool, "not-a-command", SUBMIT, args)
    assert denied["error"]["code"] == "KNOWLEDGE_AUDIENCE_NOT_AUTHORIZED"
    denied = await gateway.invoke(replace(tool, role="recovery"), "recovery", SUBMIT, args)
    assert denied["error"]["code"] == "RECOVERY_READ_ONLY"


async def test_group_member_cannot_revise_another_authors_knowledge(contextual: ContextRuns) -> None:
    h = contextual
    scope = await prepare_group(h)
    entry = await publish_rule(h, scope)
    async with member(h.original, "项目普通成员") as (_, user_id):
        actor = RunActor(UUID(user_id), h.actor.workspace_id)
        await h.context.set_grant(h.context_actor, h.source_id, actor.user_id, True)
        await h.context.bind_identity(h.context_actor, h.connection_id, "ou_member", actor.user_id)
        run_id = await send(h, "更新知识库，XX 规则在模考中停用。", open_id="ou_member")
        lease = await h.app.state.execution_store.claim(
            workspace_id=actor.workspace_id, run_id=run_id, owner="member-knowledge", lease_seconds=60,
        )
        assert lease is not None
        gateway = h.app.state.tool_gateway
        tool = ToolScope(run_id, actor, lease.owner, lease.fencing_token)
        assert await gateway.invoke(tool, "find-author-only", FIND, {"query": "mockcenter"}) == {"items": []}
        denied = await gateway.invoke(tool, "author-only", SUBMIT, {
            "id": entry, "base_revision": 1, "expected_text": OLD, "replacement": NEW,
        })
        assert denied["error"]["code"] == "KNOWLEDGE_FORBIDDEN"
        await h.context.set_grant(h.context_actor, h.source_id, actor.user_id, False)
        denied = await gateway.invoke(tool, "after-revocation", FIND, {"id": entry})
        assert denied["error"]["code"] == "ACCESS_REVOKED"
