"""与上游输入投递退避对齐：仅重试确定尚未执行工具或保存会话的运输失败。"""

import asyncio
import json
from collections.abc import Awaitable, Callable

from weora.modules.runs.application.execution import RuntimeFailure


def session_history_window(
    messages: list[dict[str, object]], *, limit_bytes: int = 96000
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """保留完整审计前缀，模型只恢复预算内的完整调用轮次，避免切断工具调用与结果。

    weoraInputIds 标记运行入口，单条插话的 weoraInputId 不作为裁剪边界。
    大型旧日志缺少轮次标签时，模型从本轮平台事实重新读取，持久历史仍完整保存。
    """
    size = 2
    start = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        size += len(json.dumps(message, ensure_ascii=False).encode()) + 1
        if size > limit_bytes:
            break
        if message.get("role") == "user" and isinstance(message.get("weoraInputIds"), list):
            start = index
    else:
        return [], messages
    return messages[:start], messages[start:]


async def deliver_session(
    operation: Callable[[], Awaitable[str]],
    status: Callable[[dict[str, object] | None], Awaitable[None]],
    *,
    base_delay_seconds: float = 15,
) -> str:
    """模型错误由 SDK 自己重试；已进入工具或检查点的失败绝不在这里重放整轮。"""
    changed = False
    try:
        for attempt in range(4):
            try:
                return await operation()
            except RuntimeFailure as error:
                if error.code != "SESSION_DELIVERY_FAILED" or attempt == 3:
                    raise
                delay = min(60, base_delay_seconds * 2**attempt)
                changed = True
                await status({"attempt": attempt + 1, "delay_ms": int(delay * 1000)})
                await asyncio.sleep(delay)
    finally:
        if changed:
            await status(None)
    raise RuntimeFailure("SESSION_DELIVERY_FAILED")
