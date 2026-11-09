"""对照上游 background/runtime-concurrency：活动生命周期和原生执行边界。"""

import asyncio
from dataclasses import replace

import pytest

from weora.modules.coordination.activities import RunActivities
from weora.modules.coordination.domain import Activity, Json
from weora.modules.runs.application.execution import RuntimeFailure


@pytest.mark.anyio
async def test_background_retains_slot_reports_completion_and_never_replays() -> None:
    """返回活动 ID 后仍占有执行槽；同 ID 再请求不重复调用工具。"""
    saved: list[Activity] = []

    async def save(item: Activity) -> None:
        saved.append(replace(item))

    activities = RunActivities("run", "owner", save)
    finish = asyncio.Event()
    calls = 0

    async def work() -> Json:
        nonlocal calls
        calls += 1
        await finish.wait()
        return {"ok": True, "result": "构建完成"}

    result = await activities.start("job", "command", work, foreground_seconds=0)
    assert result["state"] == "running" and activities.running
    await activities.start("job", "command", work, foreground_seconds=0)
    assert calls == 1 and len(saved) == 1
    finish.set()
    await asyncio.wait_for(activities.changed.wait(), timeout=1)
    assert activities.describe("job")["state"] == "completed"
    assert not activities.running and len(saved) == 2
    assert (await activities.start("job", "command", work))["result"] == "构建完成"
    assert calls == 1


@pytest.mark.anyio
async def test_shutdown_cancels_all_children_and_waits_for_cleanup() -> None:
    """退出监督范围后，命令及子进程清理必须完成，不能留下脱离租约的任务。"""
    states: list[Activity] = []
    cleaned: set[str] = set()

    async def save(item: Activity) -> None:
        states.append(item)

    async def work(identity: str) -> Json:
        try:
            await asyncio.Event().wait()
            return {"ok": True}
        finally:
            cleaned.add(identity)

    activities = RunActivities("run", "owner", save)
    await activities.start("command", "command", lambda: work("command"), foreground_seconds=0)
    await activities.start("agent", "subagent", lambda: work("agent"), foreground_seconds=0)
    await activities.close()
    assert cleaned == {"command", "agent"}
    assert not activities.running
    assert all(activities.describe(identity)["state"] == "cancelled" for identity in cleaned)


@pytest.mark.anyio
async def test_capacity_and_uncertain_recovery_fail_closed() -> None:
    """进程重启没有在途工作句柄时只要求恢复，不根据旧参数重跑命令。"""

    async def save(item: Activity) -> None:
        pass

    activities = RunActivities("run", "owner", save, limit=1)

    async def wait() -> Json:
        await asyncio.Event().wait()
        return {"ok": True}

    await activities.start("first", "subagent", wait, foreground_seconds=0)
    try:
        assert (await activities.start("second", "command", wait))["code"] == "BACKGROUND_CAPACITY_WAIT"
        with pytest.raises(RuntimeFailure, match="BACKGROUND_RECOVERY_REQUIRED"):
            activities.require_clean_recovery([Activity("unknown", "run", "command", "previous-owner", "at")])
    finally:
        await activities.close()


@pytest.mark.anyio
async def test_cancel_stops_descendants_without_touching_other_work() -> None:
    """父 Agent 被取消时清理它的后代，其他独立活动仍由原生 Run 监督。"""

    async def save(item: Activity) -> None:
        pass

    async def wait() -> Json:
        await asyncio.Event().wait()
        return {"ok": True}

    activities = RunActivities("run", "owner", save)
    for identity, parent in (("parent", None), ("child", "parent"), ("nested", "child"), ("other", None)):
        await activities.start(identity, "subagent", wait, foreground_seconds=0, parent_id=parent)
    await activities.cancel("parent")
    assert all(activities.describe(identity)["state"] == "cancelled" for identity in ("parent", "child", "nested"))
    assert activities.describe("other")["state"] == "running"
    await activities.close()


@pytest.mark.anyio
async def test_unconfirmed_shutdown_is_bounded_and_never_reported_as_clean() -> None:
    """底层忽略一次取消时进入未确认恢复，不无限等待，也不声称进程已停止。"""

    async def save(item: Activity) -> None:
        pass

    stopped = asyncio.Event()

    async def stubborn() -> Json:
        while not stopped.is_set():
            try:
                await stopped.wait()
            except asyncio.CancelledError:
                continue
        return {"ok": True}

    activities = RunActivities("run", "owner", save, shutdown_seconds=0.01)
    await activities.start("job", "command", stubborn, foreground_seconds=0)
    try:
        with pytest.raises(RuntimeFailure, match="BACKGROUND_SHUTDOWN_UNCONFIRMED"):
            await activities.close()
    finally:
        stopped.set()
        await asyncio.gather(*activities.tasks.values(), return_exceptions=True)
