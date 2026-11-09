"""真实 Run/租约/Gateway/HTTP/SQLite 闭环，执行器与远端使用明确的 Fake。"""

import json
from dataclasses import asdict, replace
from typing import cast
from uuid import UUID

import pytest

from tests.api.test_runs import RunsHarness, runs
from tests.delivery.fakes import Browser, Remote, Sandbox
from tests.delivery.test_service import scope
from weora.bootstrap.code_hosting import HostingConnectionSettings
from weora.bootstrap.execution_services import configure_execution_services
from weora.bootstrap.settings import AppSettings
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.domain.models import PlanScope
from weora.modules.runs.infrastructure.execution_store import SqlAlchemyExecutionStore
from weora.modules.runs.infrastructure.module_access import RunContentGuard
from weora.modules.tools.application.gateway import ToolGateway, ToolScope

delivery_runs = runs


def proposal_scope(selected: PlanScope) -> dict[str, object]:
    """客户端只提交可写字段；冻结的连接、分支与 SHA 必须由平台从交付记录注入。"""
    return cast(
        dict[str, object],
        json.loads(
            json.dumps(
                {
                    key: value
                    for key, value in asdict(selected).items()
                    if key not in {"remote_binding", "source_branch", "source_revision"}
                }
            )
        ),
    )


@pytest.mark.anyio
async def test_supplement_queues_planning_without_granting_execution(delivery_runs: RunsHarness) -> None:
    """真实 HTTP/事务验证补充意见唤醒规划，但不能批准占位记录或执行命令。"""
    runs = delivery_runs
    run_id, _, _, _ = await configure(runs)
    root = f"{runs.path}/{run_id}"
    proposal = {"scope": proposal_scope(scope()), "summary": "初始计划", "risk": "隔离测试"}
    injected = {**proposal, "scope": {**proposal_scope(scope()), "source_branch": "weora/" + "d" * 24}}
    assert (await runs.client.post(root + "/plan", headers=runs.headers(), json=injected)).status_code == 422
    first = await runs.client.post(root + "/plan", headers=runs.headers(), json=proposal)
    assert first.status_code == 200
    supplemented = await runs.client.post(
        root + f"/plan/{first.json()['plan']['id']}/decision",
        headers=runs.headers(),
        json={"decision": "supplement", "reason": "补入日志读取命令"},
    )
    assert supplemented.status_code == 200
    revised = supplemented.json()["plan"]
    assert revised["status"] == "REVISION_REQUIRED" and revised["expires_at"] is None
    assert (await runs.client.get(root)).json()["status"] == "QUEUED"
    denied = await runs.client.post(
        root + f"/plan/{revised['id']}/decision",
        headers=runs.headers(),
        json={"decision": "approve"},
    )
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "INVALID_DECISION"
    proposal["summary"] = "补齐后的 Agent 提案"
    pending = await runs.client.post(root + "/plan", headers=runs.headers(), json=proposal)
    assert pending.status_code == 200 and pending.json()["plan"]["status"] == "PENDING"
    assert (await runs.client.get(root)).json()["status"] == "WAITING_APPROVAL"


@pytest.mark.anyio
async def test_plan_schema_explains_permission_names_not_tool_names(delivery_runs: RunsHarness) -> None:
    """模型实际收到的 schema 必须列出审批权限，避免只能猜测工具名。"""
    _, gateway, _, _ = await configure(delivery_runs)
    proposal = next(spec for spec in gateway.specifications() if spec.name == "plan.propose")
    properties = proposal.input_schema["properties"]["scope"]["properties"]
    assert set(properties["actions"]["items"]["enum"]) == {
        "file.write",
        "command.run",
        "git.diff",
        "browser.verify",
        "git.commit",
        "git.push",
        "pr.create",
        "review.create",
    }
    assert "sandbox.file.write" not in properties["actions"]["items"]["enum"]
    assert "工具名" in properties["actions"]["description"]


@pytest.mark.anyio
async def test_real_bootstrap_registers_codeup_and_freezes_http_plan(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    target = HostingConnectionSettings(
        workspace_id=UUID(runs.workspace_id),
        remote_id="test-remote",
        version="v1",
        provider="codeup",
        repository_id="test-repo",
        platform_repository="123",
        clone_url="https://git.example.test/team/repo.git",
        api_origin="https://api.example.test",
        web_url="https://git.example.test/team/repo",
        credential_ref="explicit-missing-test-credential",
        target_branches=("main",),
    )
    settings = cast(AppSettings, runs.app.state.settings).model_copy(
        update={
            "sandbox_workspace_id": UUID(runs.workspace_id),
            "hosting_connections": (target,),
        }
    )
    configure_execution_services(runs.app, settings, RunContentGuard())
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert created.status_code == 201
    requested = proposal_scope(scope(remote=True))
    proposed = await runs.client.post(
        f"{runs.path}/{created.json()['id']}/plan",
        headers=runs.headers(),
        json={
            "scope": requested,
            "summary": "使用登记云效连接",
            "risk": "仅验证提案，不执行远端写入",
        },
    )
    assert proposed.status_code == 200, proposed.text
    frozen = proposed.json()["plan"]["scope"]["remote_binding"]
    assert frozen == target.connection().fingerprint
    assert "explicit-missing-test-credential" not in proposed.text


async def configure(runs: RunsHarness) -> tuple[UUID, ToolGateway, DeliveryService, SqlAlchemyExecutionStore]:
    configured_settings = cast(AppSettings, runs.app.state.settings).model_copy(
        update={"sandbox_workspace_id": UUID(runs.workspace_id)}
    )
    configure_execution_services(runs.app, configured_settings, RunContentGuard())
    service = cast(DeliveryService, runs.app.state.delivery_service)
    service.sandbox, service.browser, service.remote = Sandbox(), Browser(), Remote()
    response = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    assert response.status_code == 201, response.text
    return (
        UUID(response.json()["id"]),
        cast(ToolGateway, runs.app.state.tool_gateway),
        service,
        cast(SqlAlchemyExecutionStore, runs.app.state.execution_store),
    )


@pytest.mark.anyio
async def test_approved_run_gateway_to_artifacts_and_draft_pr(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    run_id, gateway, service, executions = await configure(runs)
    lease = await executions.claim(
        workspace_id=UUID(runs.workspace_id), run_id=run_id, owner="console-worker", lease_seconds=30
    )
    assert lease
    from weora.modules.runs.application.ports import RunActor

    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    console = ToolScope(run_id, actor, lease.owner, lease.fencing_token, "console")
    proposed = await gateway.invoke(
        console,
        "plan-1",
        "plan.propose",
        {
            # 连接指纹由平台注入，不属于模型可提交的提案参数。
            "scope": proposal_scope(scope(remote=True)),
            "summary": "修复并验证登录",
            "risk": "需要回归认证",
        },
    )
    assert proposed["ok"] is True, proposed
    assert (await runs.client.get(f"{runs.path}/{run_id}")).json()["status"] == "WAITING_APPROVAL"
    assert not await executions.authorize_token(actor, run_id, lease.fencing_token, lease.owner)
    assert not await executions.finish(lease, status="SUCCEEDED", output="旧 Worker 的迟到结果")
    plan_id = proposed["plan_id"]
    approved = await runs.client.post(
        f"{runs.path}/{run_id}/plan/{plan_id}/decision", json={"decision": "approve"}, headers=runs.headers()
    )
    assert approved.status_code == 200, approved.text
    assert (await runs.client.get(f"{runs.path}/{run_id}")).json()["status"] == "QUEUED"
    execution_lease = await executions.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="sandbox-worker", lease_seconds=30
    )
    assert execution_lease and execution_lease.fencing_token > lease.fencing_token
    sandbox = ToolScope(run_id, actor, execution_lease.owner, execution_lease.fencing_token, "sandbox")
    write = await gateway.invoke(sandbox, "write-1", "sandbox.file.write", {"path": "src/login.py", "content": "fixed"})
    assert write["ok"] is True, write
    command = await gateway.invoke(sandbox, "test-1", "sandbox.command", {"argv": ["python", "-m", "pytest"]})
    assert command["status"] == "PASSED" and command["exit_code"] == 0
    diff = await gateway.invoke(sandbox, "diff-1", "sandbox.diff", {})
    assert diff["kind"] == "diff" and diff["body"] == "+ fixed"
    pr = await gateway.invoke(sandbox, "pr-1", "delivery.pr.create", {"title": "Fix login", "body": "2 tests passed"})
    assert pr["status"] == "SUCCEEDED" and pr["remote_id"] == "42", pr
    artifacts = (await runs.client.get(f"{runs.path}/{run_id}/artifacts")).json()["items"]
    assert {item["kind"] for item in artifacts} >= {"file.write", "command", "diff"}
    deliveries = (await runs.client.get(f"{runs.path}/{run_id}/deliveries")).json()["items"]
    assert deliveries[0]["status"] == "SUCCEEDED" and deliveries[0]["plan_id"] == plan_id
    assert await executions.finish(
        execution_lease, status="SUCCEEDED", output="已完成修复，真实 Fake 证据用于契约测试。"
    )


@pytest.mark.anyio
async def test_rejected_run_cannot_be_resurrected_or_reapproved(delivery_runs: RunsHarness) -> None:
    runs = delivery_runs
    run_id, _, service, executions = await configure(runs)
    from weora.modules.runs.application.ports import RunActor

    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    plan = await service.propose(actor, run_id, scope(), "修复", "风险")
    rejected = await runs.client.post(
        f"{runs.path}/{run_id}/plan/{plan.id}/decision",
        json={"decision": "reject", "reason": "不允许修改"},
        headers=runs.headers(),
    )
    assert rejected.status_code == 200
    assert (await runs.client.get(f"{runs.path}/{run_id}")).json()["status"] == "FAILED"
    assert (
        await executions.claim(workspace_id=actor.workspace_id, run_id=run_id, owner="late", lease_seconds=30) is None
    )
    again = await runs.client.post(
        f"{runs.path}/{run_id}/plan/{plan.id}/decision", json={"decision": "approve"}, headers=runs.headers()
    )
    assert again.status_code == 409


@pytest.mark.anyio
async def test_material_change_needs_new_approval_and_read_only_recovery_never_writes(
    delivery_runs: RunsHarness,
) -> None:
    runs = delivery_runs
    run_id, gateway, service, executions = await configure(runs)
    from weora.modules.runs.application.ports import RunActor

    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    first = await service.propose(actor, run_id, scope(), "修复", "风险")
    await service.decide(actor, run_id, first.id, "approve")
    second = await service.propose(actor, run_id, replace(scope(), paths=("tests",)), "只改测试", "低风险")
    assert second.version == 2 and second.status == "PENDING"
    stale = await runs.client.post(
        f"{runs.path}/{run_id}/plan/{first.id}/decision", json={"decision": "approve"}, headers=runs.headers()
    )
    assert stale.status_code == 409
    await service.decide(actor, run_id, second.id, "approve")
    lease = await executions.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="lost-worker", lease_seconds=30
    )
    assert lease
    assert await executions.transition(actor, run_id, "RECOVERY_REQUIRED")
    assert await executions.recover_read_only(actor, run_id)
    read_lease = await executions.claim(
        workspace_id=actor.workspace_id, run_id=run_id, owner="read-only", lease_seconds=30
    )
    assert read_lease and read_lease.run.recovery_read_only
    attempted = await gateway.invoke(
        ToolScope(run_id, actor, read_lease.owner, read_lease.fencing_token, "sandbox"),
        "illegal-write",
        "sandbox.file.write",
        {"path": "tests/a.py", "content": "x"},
    )
    assert attempted["ok"] is False, attempted
    assert cast(Sandbox, service.sandbox).calls == 0
