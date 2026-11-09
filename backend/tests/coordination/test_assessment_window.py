"""对照 admission-context.ts 的开头与触发邻居，跨日/坏游标不能静默缩成最近一小时。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from tests.context.test_context import setup
from weora.modules.context.domain import ContextError, Json, MessagePage


@pytest.mark.anyio
async def test_window_keeps_first_five_and_five_neighbours_across_days() -> None:
    f = await setup()
    await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
    cutoff = datetime.now(UTC)
    messages = tuple(
        replace(f.message(i, thread="omt_long"), created_at=cutoff - timedelta(hours=35 - i)) for i in range(35)
    )
    f.reader.pages = {None: MessagePage(messages[:20], "page2"), "page2": MessagePage(messages[20:])}
    result = await f.service.channel_messages(
        f.actor, f.source, "ou_user", cutoff=cutoff, thread_id="omt_long", anchor_id="om_22", request_key="eval"
    )
    ids = [item["id"] for item in cast(list[Json], result["items"])]
    assert ids == [f"om_{i}" for i in list(range(5)) + list(range(17, 28))]
    assert result["source_found"] is True and result["omitted_messages"] == 19
    assert result["next_cursor"] is None and result["partial"] is False
    assert {item["id"] for item in cast(list[Json], result["stubs"])} | set(ids) == {f"om_{i}" for i in range(35)}


@pytest.mark.anyio
async def test_missing_anchor_and_unread_pages_remain_explicit_and_cursor_binds_source_version() -> None:
    f = await setup()
    await f.service.bind_identity(f.actor, f.connection, "ou_user", f.actor.user_id)
    f.reader.pages = {
        None: MessagePage((f.message(0, thread="omt_long"),), "1"),
        "1": MessagePage((), "2"),
        "2": MessagePage((), "3"),
        "3": MessagePage((), "4"),
        "4": MessagePage(()),
    }
    cutoff = datetime.now(UTC)
    result = await f.service.channel_messages(
        f.actor, f.source, "ou_user", cutoff=cutoff, thread_id="omt_long", anchor_id="missing", request_key="eval"
    )
    assert result["source_found"] is False and result["partial"] is True and result["next_cursor"]
    with pytest.raises(ContextError):
        await f.service.channel_messages(
            f.actor,
            f.source,
            "ou_user",
            cutoff=cutoff,
            thread_id="omt_long",
            anchor_id="different",
            cursor=str(result["next_cursor"]),
            request_key="eval",
        )
    f.reader.pages = {None: MessagePage((), "stuck"), "stuck": MessagePage((), "stuck")}
    with pytest.raises(ContextError, match="分页重复"):
        await f.service.channel_messages(
            f.actor, f.source, "ou_user", cutoff=cutoff, thread_id="omt_long", anchor_id="missing"
        )
