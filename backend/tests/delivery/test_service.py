"""批准不是永久通行证；撤权、计划变化、未知远端结果必须关闭写入路径。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.delivery.fakes import Access, Browser, MemoryStore, Remote, Sandbox
from weora.modules.delivery.application.ports import BrowserResult, RemoteResult, SandboxResult
from weora.modules.delivery.application.service import DeliveryService
from weora.modules.delivery.application.tools import _artifact_result
from weora.modules.delivery.domain.models import DeliveryError, PlanScope, safe_path
from weora.modules.runs.application.ports import RunActor


def scope(*, remote: bool = False) -> PlanScope:
    actions = ("file.write", "command.run", "git.diff", "browser.verify")
    return PlanScope(
        "test-repo",
        "a" * 40,
        ("src", "tests"),
        (("python", "-m", "pytest"),),
        actions + (("git.commit", "git.push", "pr.create") if remote else ()),
        "test-remote" if remote else None,
        "main" if remote else None,
    )


def harness() -> tuple[DeliveryService, RunActor, Access, MemoryStore, Sandbox, Remote]:
    actor, run_id = RunActor(uuid4(), uuid4()), uuid4()
    access, store, sandbox, remote = Access(actor, run_id), MemoryStore(), Sandbox(), Remote()
    return DeliveryService(store, access, sandbox, Browser(), remote), actor, access, store, sandbox, remote


@pytest.mark.parametrize(
    "path", ["../a", "/etc/passwd", "C:/a", "src\\a", ".git/config", "src/../../a", "http://x", "-x", "src/./a"]
)
def test_path_escape_is_rejected(path: str) -> None:
    with pytest.raises(DeliveryError, match="路径"):
        safe_path(path)


@pytest.mark.anyio
async def test_pending_cannot_execute_and_approve_is_idempotent() -> None:
    service, actor, access, store, sandbox, _ = harness()
    plan = await service.propose(actor, access.run_id, scope(), "修复登录", "影响认证")
    with pytest.raises(DeliveryError) as error:
        await service.command(actor, access.run_id, ("python", "-m", "pytest"), 7)
    assert error.value.code == "APPROVAL_REQUIRED"
    approved = await service.decide(actor, access.run_id, plan.id, "approve")
    replay = await service.decide(actor, access.run_id, plan.id, "approve")
    assert approved == replay
    assert access.states == ["WAITING_APPROVAL", "QUEUED", "QUEUED"]
    evidence = await service.command(actor, access.run_id, ("python", "-m", "pytest"), 7)
    assert evidence.status == "PASSED" and evidence.exit_code == 0
    assert sandbox.calls == 1 and len(store.evidence) == 1


@pytest.mark.anyio
async def test_reply_screenshots_require_valid_owned_unexpired_evidence() -> None:
    service, actor, access, store, _, _ = harness()
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7uoAAAAASUVORK5CYII="
    first = await service.record_computer_screenshot(actor, access.run_id, png)
    second = await service.record_computer_screenshot(actor, access.run_id, png)
    selected = await service.reply_screenshots(actor, access.run_id, artifact_ids=(str(first.id), str(second.id)))
    assert [item.id for item in selected] == [first.id, second.id]
    store.evidence.append(replace(first, id=uuid4(), run_id=uuid4()))
    store.evidence[0] = replace(first, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    for identity in (str(first.id), str(store.evidence[-1].id), str(uuid4())):
        with pytest.raises(DeliveryError, match="截图"):
            await service.reply_screenshots(actor, access.run_id, artifact_ids=(identity,))
    store.evidence[1] = replace(second, sha256="tampered")
    with pytest.raises(DeliveryError, match="截图"):
        await service.reply_screenshots(actor, access.run_id, artifact_ids=(str(second.id),))


@pytest.mark.anyio
async def test_material_change_revokes_old_approval_and_supplement_creates_version() -> None:
    service, actor, access, store, _, _ = harness()
    first = await service.propose(actor, access.run_id, scope(), "修复登录", "影响认证")
    await service.decide(actor, access.run_id, first.id, "approve")
    second = await service.propose(actor, access.run_id, replace(scope(), paths=("tests",)), "只改测试", "低风险")
    assert second.version == 2 and second.status == "PENDING"
    assert store.plans[0].status == "SUPERSEDED"
    with pytest.raises(DeliveryError) as error:
        await service.decide(actor, access.run_id, first.id, "approve")
    assert error.value.code == "PLAN_CHANGED"
    third = await service.decide(actor, access.run_id, second.id, "supplement", reason="补充并发测试")
    assert third.version == 3 and third.reason == "补充并发测试" and third.status == "REVISION_REQUIRED"
    assert access.states[-1] == "QUEUED"
    with pytest.raises(DeliveryError) as error:
        await service.decide(actor, access.run_id, third.id, "approve")
    assert error.value.code == "INVALID_DECISION"
    with pytest.raises(DeliveryError) as error:
        await service.command(actor, access.run_id, ("python", "-m", "pytest"), 7)
    assert error.value.code == "APPROVAL_REQUIRED"
    revised = await service.propose(actor, access.run_id, third.scope, "新增并发测试后的计划", third.risk)
    assert revised.version == 4 and revised.status == "PENDING" and revised.reason is None
    assert access.states[-1] == "WAITING_APPROVAL"


@pytest.mark.anyio
async def test_expired_plan_cannot_execute_and_same_proposal_can_be_reapproved() -> None:
    service, actor, access, store, _, _ = harness()
    plan = await service.propose(actor, access.run_id, scope(), "修复登录", "影响认证")
    approved = await service.decide(actor, access.run_id, plan.id, "approve")
    store.plans[-1] = replace(approved, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(DeliveryError) as error:
        await service.write(actor, access.run_id, "src/a.py", "fixed", 7)
    assert error.value.code == "APPROVAL_EXPIRED"
    new = await service.propose(actor, access.run_id, scope(), "修复登录", "影响认证")
    assert new.version == 2 and new.status == "PENDING"


@pytest.mark.anyio
async def test_rejection_cancels_attempt_and_revoke_blocks_tools() -> None:
    service, actor, access, _, _, _ = harness()
    plan = await service.propose(actor, access.run_id, scope(), "修复登录", "影响认证")
    rejected = await service.decide(actor, access.run_id, plan.id, "reject", reason="不接受")
    assert rejected.status == "REJECTED" and access.states[-1] == "FAILED"
    new = await service.propose(actor, access.run_id, scope(), "改进方案", "低风险")
    await service.decide(actor, access.run_id, new.id, "approve")
    await service.decide(actor, access.run_id, new.id, "revoke")
    with pytest.raises(DeliveryError) as error:
        await service.diff(actor, access.run_id, 7)
    assert error.value.code == "APPROVAL_REQUIRED"


@pytest.mark.anyio
async def test_fencing_actor_commands_and_paths_are_checked_before_sandbox() -> None:
    service, actor, access, _, sandbox, _ = harness()
    plan = await service.propose(actor, access.run_id, scope(), "修复登录", "影响认证")
    await service.decide(actor, access.run_id, plan.id, "approve")
    for argv, fence, code in [
        (("sh", "-c", "echo x"), 7, "COMMAND_DENIED"),
        (("python", "-m", "pytest"), 6, "LEASE_LOST"),
    ]:
        with pytest.raises(DeliveryError) as error:
            await service.command(actor, access.run_id, argv, fence)
        assert error.value.code == code
    with pytest.raises(DeliveryError):
        await service.write(actor, access.run_id, "config/secret", "x", 7)
    with pytest.raises(DeliveryError):
        await service.get_plan(RunActor(uuid4(), actor.workspace_id), access.run_id)
    assert sandbox.calls == 0


@pytest.mark.anyio
async def test_source_revoked_during_command_stops_entire_sandbox() -> None:
    service, actor, access, store, sandbox, _ = harness()
    plan = await service.propose(actor, access.run_id, scope(), "修复登录", "影响认证")
    await service.decide(actor, access.run_id, plan.id, "approve")
    sandbox.block = True
    command = asyncio.create_task(service.command(actor, access.run_id, ("python", "-m", "pytest"), 7))
    await sandbox.started.wait()
    access.allowed = False
    with pytest.raises(DeliveryError):
        await asyncio.wait_for(command, 2)
    assert sandbox.cleanups >= 1 and store.evidence == []


@pytest.mark.anyio
async def test_failed_test_exit_is_not_success_and_expired_body_is_not_returned() -> None:
    service, actor, access, store, sandbox, _ = harness()
    plan = await service.propose(actor, access.run_id, scope(), "修复登录", "影响认证")
    await service.decide(actor, access.run_id, plan.id, "approve")
    sandbox.result = SandboxResult("FAILED", "AssertionError", 1)
    artifact = await service.command(actor, access.run_id, ("python", "-m", "pytest"), 7)
    assert artifact.status == "FAILED" and artifact.exit_code == 1
    store.evidence[-1] = replace(artifact, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    expired = (await service.artifacts(actor, access.run_id))[0]
    assert expired.body is None and expired.status == "EXPIRED"
    assert await service.purge_expired() == 1


@pytest.mark.anyio
async def test_remote_unknown_is_reconciled_without_second_create() -> None:
    service, actor, access, _, _, remote = harness()
    plan = await service.propose(actor, access.run_id, scope(remote=True), "修复登录并提交草稿", "测试远端")
    await service.decide(actor, access.run_id, plan.id, "approve")
    remote.unknown = True
    with pytest.raises(DeliveryError) as error:
        await service.create_pr(actor, access.run_id, "Fix login", "Test report", 7)
    assert error.value.code == "RECOVERY_REQUIRED"
    assert (await service.deliveries(actor, access.run_id))[0].status == "UNKNOWN"
    with pytest.raises(DeliveryError):
        await service.create_pr(actor, access.run_id, "Fix login", "Test report", 7)
    assert remote.calls == 1
    remote.found = RemoteResult("42", "https://github.com/example/test/pull/42")
    result = await service.create_pr(actor, access.run_id, "Fix login", "Test report", 7)
    assert result.status == "SUCCEEDED" and remote.calls == 1


@pytest.mark.anyio
async def test_fix_does_not_imply_push_or_pr_authority() -> None:
    service, actor, access, _, _, remote = harness()
    ordinary = replace(scope(remote=True), actions=("file.write", "pr.create"))
    plan = await service.propose(actor, access.run_id, ordinary, "修复", "风险")
    await service.decide(actor, access.run_id, plan.id, "approve")
    with pytest.raises(DeliveryError) as error:
        await service.create_pr(actor, access.run_id, "fix", "report", 7)
    assert error.value.code == "ACTION_DENIED" and remote.calls == 0


@pytest.mark.anyio
async def test_browser_not_configured_is_not_passed() -> None:
    service, actor, access, _, _, _ = harness()
    plan = await service.propose(actor, access.run_id, scope(), "修复", "风险")
    await service.decide(actor, access.run_id, plan.id, "approve")
    result = await service.verify_browser(actor, access.run_id, "/", "Hello", 7)
    assert result.verification.status == "NOT_CONFIGURED" and result.screenshot is None
    assert _artifact_result(result.verification)["screenshot_artifact_id"] is None


@pytest.mark.anyio
async def test_browser_returns_separate_real_screenshot_reference_without_image_body() -> None:
    service, actor, access, store, _, _ = harness()
    browser = Browser()
    browser.result = BrowserResult("PASSED", "真实页面断言通过", "iVBORw0KGgo-test-image")
    service.browser = browser
    plan = await service.propose(actor, access.run_id, scope(), "验收截图", "无外部资源")
    await service.decide(actor, access.run_id, plan.id, "approve")
    result = await service.verify_browser(actor, access.run_id, "/", "Hello", 7)
    assert result.screenshot is not None
    assert result.verification.id != result.screenshot.id
    assert result.verification.kind == "browser" and result.screenshot.kind == "screenshot"
    assert result.verification.plan_id == result.screenshot.plan_id == plan.id
    assert {record.id for record in store.evidence} == {result.verification.id, result.screenshot.id}
    payload = _artifact_result(result.verification, result.screenshot)
    assert payload["artifact_id"] == str(result.verification.id)
    assert payload["screenshot_artifact_id"] == str(result.screenshot.id)
    assert payload["screenshot_status"] == "PASSED"
    assert browser.result.screenshot_base64 is not None
    assert browser.result.screenshot_base64 not in str(payload)


@pytest.mark.anyio
async def test_read_only_remote_reconcile_works_after_approval_expiry_without_create() -> None:
    service, actor, access, store, _, remote = harness()
    plan = await service.propose(actor, access.run_id, scope(remote=True), "修复并交付", "测试仓库")
    approved = await service.decide(actor, access.run_id, plan.id, "approve")
    remote.unknown = True
    with pytest.raises(DeliveryError):
        await service.create_pr(actor, access.run_id, "Fix", "report", 7)
    store.plans[-1] = replace(approved, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    access.fence = 8  # 原执行租约已失效；核对只读路径不使用这个租约。
    not_found = await service.reconcile_pr(actor, access.run_id)
    assert not_found and not_found.status == "RECOVERY_REQUIRED" and remote.calls == 1
    remote.found = RemoteResult("42", "https://github.com/example/test/pull/42")
    found = await service.reconcile_pr(actor, access.run_id)
    assert found and found.status == "SUCCEEDED" and remote.calls == 1
    access.allowed = False
    with pytest.raises(DeliveryError):
        await service.reconcile_pr(actor, access.run_id)


@pytest.mark.anyio
async def test_cleanup_failure_is_persisted_for_compensation() -> None:
    service, actor, access, store, sandbox, _ = harness()

    async def fail_cleanup(run_id: object) -> None:
        raise DeliveryError("CLEANUP_REQUIRED", "SDK 敏感错误不能复制")

    sandbox.cleanup = fail_cleanup  # type: ignore[method-assign]
    assert not await service.cleanup(actor, access.run_id)
    evidence = store.evidence[-1]
    assert evidence.kind == "cleanup" and evidence.status == "CLEANUP_REQUIRED"
    assert evidence.body and "敏感错误" not in evidence.body


@pytest.mark.anyio
async def test_completion_requires_post_write_tests_and_true_diff() -> None:
    service, actor, access, _, sandbox, _ = harness()
    await service.validate_completion(actor, access.run_id, 7)  # 无计划的咨询不要求研发证据。
    coding_scope = replace(scope(), actions=("file.write", "command.run", "git.diff"))
    plan = await service.propose(actor, access.run_id, coding_scope, "修复登录", "影响认证")
    await service.decide(actor, access.run_id, plan.id, "approve")
    with pytest.raises(DeliveryError) as error:
        await service.validate_completion(actor, access.run_id, 7)
    assert error.value.code == "MISSING_EVIDENCE"
    await service.command(actor, access.run_id, ("python", "-m", "pytest"), 7)
    await service.write(actor, access.run_id, "src/login.py", "fixed", 7)
    with pytest.raises(DeliveryError) as error:
        await service.validate_completion(actor, access.run_id, 7)
    assert error.value.code == "MISSING_EVIDENCE"  # 改码之前通过的测试不能作为当前修改的证据。
    sandbox.result = SandboxResult("FAILED", "one failing test", 1)
    await service.command(actor, access.run_id, ("python", "-m", "pytest"), 7)
    with pytest.raises(DeliveryError) as error:
        await service.validate_completion(actor, access.run_id, 7)
    assert error.value.code == "FAILED_EVIDENCE"
    sandbox.result = SandboxResult("PASSED", "2 passed", 0)
    await service.command(actor, access.run_id, ("python", "-m", "pytest"), 7)
    await service.validate_completion(actor, access.run_id, 7)


@pytest.mark.anyio
async def test_completion_does_not_treat_unconfigured_browser_as_success() -> None:
    service, actor, access, _, _, _ = harness()
    browser_scope = replace(scope(), actions=("browser.verify",), commands=())
    plan = await service.propose(actor, access.run_id, browser_scope, "验证页面", "页面风险")
    await service.decide(actor, access.run_id, plan.id, "approve")
    await service.verify_browser(actor, access.run_id, "/", "Hello", 7)
    with pytest.raises(DeliveryError) as error:
        await service.validate_completion(actor, access.run_id, 7)
    assert error.value.code == "MISSING_EVIDENCE"


@pytest.mark.anyio
async def test_command_mutation_invalidates_earlier_tests_until_final_state_is_verified() -> None:
    service, actor, access, _, sandbox, _ = harness()
    test_command = ("python", "-m", "pytest")
    format_command = ("python", "format.py")
    coding_scope = replace(
        scope(), actions=("file.write", "command.run", "git.diff"), commands=(test_command, format_command)
    )
    plan = await service.propose(actor, access.run_id, coding_scope, "修复并格式化", "格式化也可能改变源码")
    await service.decide(actor, access.run_id, plan.id, "approve")
    await service.write(actor, access.run_id, "src/login.py", "fixed", 7)
    tested = await service.command(actor, access.run_id, test_command, 7)
    assert tested.repository_sha256
    sandbox.command_changes_diff = "+ fixed and formatted"
    formatted = await service.command(actor, access.run_id, format_command, 7)
    assert formatted.exit_code == 0 and formatted.repository_sha256 is None
    sandbox.command_changes_diff = None
    # 即使格式化命令第二次已幂等，旧测试仍然属于格式化之前的代码。
    await service.command(actor, access.run_id, format_command, 7)
    with pytest.raises(DeliveryError) as error:
        await service.validate_completion(actor, access.run_id, 7)
    assert error.value.code == "MISSING_EVIDENCE"
    current_test = await service.command(actor, access.run_id, test_command, 7)
    assert current_test.repository_sha256 and current_test.repository_sha256 != tested.repository_sha256
    await service.validate_completion(actor, access.run_id, 7)


@pytest.mark.anyio
async def test_legacy_command_without_repository_binding_cannot_complete() -> None:
    service, actor, access, store, _, _ = harness()
    coding_scope = replace(scope(), actions=("file.write", "command.run", "git.diff"))
    plan = await service.propose(actor, access.run_id, coding_scope, "修复", "风险")
    await service.decide(actor, access.run_id, plan.id, "approve")
    await service.write(actor, access.run_id, "src/login.py", "fixed", 7)
    result = await service.command(actor, access.run_id, ("python", "-m", "pytest"), 7)
    store.evidence[-1] = replace(result, repository_sha256=None)
    with pytest.raises(DeliveryError) as error:
        await service.validate_completion(actor, access.run_id, 7)
    assert error.value.code == "MISSING_EVIDENCE"
