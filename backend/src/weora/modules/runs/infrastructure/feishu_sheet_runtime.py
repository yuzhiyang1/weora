"""显式要求用飞书 CLI 读表时，用真实工具轨迹核对 Agent 是否尝试读取正文。"""

import re
from collections.abc import Awaitable, Callable
from typing import cast

from weora.modules.runs.application.execution import RuntimeInput, TextRuntime
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.tools.application.gateway import ToolInvocation

_SHEET_LINK = re.compile(r"https://(?:[\w-]+\.)*(?:feishu\.cn|larksuite\.com)/sheets/([A-Za-z0-9]+)\b")
_CLI_READ = re.compile(r"(?:^|[。！？\n])\s*(?:请\s*)?(?:用|使用|通过|调用)\s*"
                       r"(?:飞书\s*)?(?:lark[.-]cli(?:\.exec)?|cli)\s*(?:来\s*)?(?:读|查)", re.I)
_CLI_TOOLS = {"lark.cli.skill", "lark.cli.exec"}
_CONTEXT_READS = {"context.sources.list", "feishu.messages.list", "feishu.message.get", "feishu.thread.expand"}


def explicit_sheet_cli_read(instruction: str) -> str | None:
    """仅匹配本轮直接指定链接和 CLI 读表的请求，不从历史回答推断意图。"""
    targets = {match[1] for match in _SHEET_LINK.finditer(instruction)}
    return instruction if len(targets) == 1 and _CLI_READ.search(instruction) else None


def _argv(call: ToolInvocation) -> list[str]:
    value = call.arguments.get("argv")
    return cast(list[str], value) if isinstance(value, list) and all(isinstance(arg, str) for arg in value) else []


def _discovery(argv: list[str]) -> bool:
    return "--help" in argv or "-h" in argv or "--dry-run" in argv or argv[:1] == ["schema"]


def _read_cells(call: ToolInvocation, token: str) -> bool:
    if call.name != "lark.cli.exec":
        return False
    argv = _argv(call)
    if not argv or _discovery(argv):
        return False
    if argv[:2] in (["sheets", "+cells-get"], ["sheets", "+csv-get"]):
        return any(re.search(rf"(?<![A-Za-z0-9]){token}(?![A-Za-z0-9])", arg) for arg in argv[2:])
    if [arg.lower() for arg in argv[:2]] == ["api", "get"]:
        return any(re.search(rf"/sheets/v2/spreadsheets/{token}/values(?:/|_batch_get\b)", arg) for arg in argv[2:])
    return False


def _safe_to_retry(call: ToolInvocation) -> bool:
    if call.name in _CONTEXT_READS or call.name == "lark.cli.skill":
        return True
    if call.name != "lark.cli.exec":
        return False
    argv = _argv(call)
    return _discovery(argv) or argv[:2] in (["sheets", "+workbook-info"], ["sheets", "+sheet-info"])


class FeishuSheetReadRuntime:
    """在同一租约内最多纠正一次，保持官方 CLI 的原生命令与原有 Bot 授权。"""

    def __init__(
        self, runtime: TextRuntime, list_calls: Callable[[], Awaitable[list[ToolInvocation]]],
    ) -> None:
        self._runtime, self._list_calls = runtime, list_calls

    async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
        instruction = payload.explicit_feishu_sheet_read
        cli_tools = tuple(tool for tool in payload.tools if tool.name in _CLI_TOOLS)
        if not instruction or {tool.name for tool in cli_tools} != _CLI_TOOLS:
            return await self._runtime.run(payload, profile)
        target = _SHEET_LINK.search(instruction)
        assert target is not None
        existing = {call.call_id for call in await self._list_calls()}
        output = await self._runtime.run(payload, profile)
        calls = await self._list_calls()
        if any(_read_cells(call, target[1]) for call in calls if call.call_id not in existing):
            return output
        if all(_safe_to_retry(call) or _read_cells(call, target[1]) for call in calls):
            # 只补做遗漏的读取；遇到其他工具或不明 CLI 命令时，避免重放潜在写操作。
            output = await self._runtime.run(
                RuntimeInput(
                    payload.system_prompt
                    + "\n上一轮没有调用 CLI 读取指定表格的单元格。现在只处理下面这条用户请求。"
                    "先查 lark-sheets Skill，再使用 CLI 的 sheets +cells-get 或 +csv-get 读取指定工作表。"
                    "工具在当前模型接口中可能显示为带 weora_ 前缀的名称，请按工具说明识别。"
                    "看到真实单元格结果后再总结。若调用失败，说明实际错误和尝试过的命令。",
                    instruction,
                    cli_tools,
                ),
                profile,
            )
            if any(_read_cells(call, target[1]) for call in await self._list_calls() if call.call_id not in existing):
                return output
        return "这次没有完成表格正文读取。运行记录中没有针对指定表格的单元格读取调用。"
