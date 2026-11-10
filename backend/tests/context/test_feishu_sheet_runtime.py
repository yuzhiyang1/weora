"""回归：续接里的旧失败答复不能替代当前 CLI 读取。"""

import asyncio
from datetime import UTC, datetime

import pytest

from weora.modules.runs.application.execution import RuntimeInput
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.runs.infrastructure.feishu_sheet_runtime import FeishuSheetReadRuntime, explicit_sheet_cli_read
from weora.modules.tools.application.gateway import ToolInvocation, ToolSpec

REQUEST = "请用飞书 CLI 读取 https://example.feishu.cn/sheets/Workbook123?sheet=abc 的正文，总结后回复。"
PROFILE = ExecutionProfile("test", "test", "test", None, 30)
TOOLS = tuple(ToolSpec(name, "", {}) for name in ("context.sources.list", "lark.cli.skill", "lark.cli.exec"))


def cli_call(call_id: str, argv: list[str]) -> ToolInvocation:
    return ToolInvocation(call_id, "lark.cli.exec", "SUCCEEDED", {"argv": argv}, None, None, datetime.now(UTC))


@pytest.mark.parametrize("first_reads_cells", [False, True])
def test_sheet_followup_checks_current_cli_call(first_reads_cells: bool) -> None:
    async def scenario() -> None:
        # 历史中存在读表回执也不能替代本轮证据。
        read = ["sheets", "+cells-get", "--spreadsheet-token", "Workbook123"]
        calls = [cli_call("old-read", read)]
        inputs: list[RuntimeInput] = []

        class Runtime:
            async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
                inputs.append(payload)
                if first_reads_cells or len(inputs) == 2:
                    calls.append(cli_call("current-read", read))
                    return "已读取正文"
                calls.append(cli_call("help", ["sheets", "+cells-get", "--help"]))
                return "没有 CLI"

        async def list_calls() -> list[ToolInvocation]:
            return calls

        payload = RuntimeInput(
            "当前工具已开放", REQUEST + "\n旧回复：没有 CLI", TOOLS, explicit_sheet_cli_read(REQUEST)
        )
        assert await FeishuSheetReadRuntime(Runtime(), list_calls).run(payload, PROFILE) == "已读取正文"
        assert len(inputs) == (1 if first_reads_cells else 2)
        if not first_reads_cells:
            assert inputs[-1].instruction == REQUEST
            assert {tool.name for tool in inputs[-1].tools} == {"lark.cli.exec", "lark.cli.skill"}

    asyncio.run(scenario())


def test_sheet_correction_never_repeats_unknown_side_effect() -> None:
    async def scenario() -> None:
        calls: list[ToolInvocation] = []

        class Runtime:
            async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
                assert not calls
                calls.append(cli_call("unknown", ["sheets", "+cells-set", "--spreadsheet-token", "Workbook123"]))
                return "没有 CLI"

        async def list_calls() -> list[ToolInvocation]:
            return calls

        payload = RuntimeInput("", REQUEST, TOOLS, explicit_sheet_cli_read(REQUEST))
        output = await FeishuSheetReadRuntime(Runtime(), list_calls).run(payload, PROFILE)
        assert "没有针对指定表格的单元格读取调用" in output

    asyncio.run(scenario())
