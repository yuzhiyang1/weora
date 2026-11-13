"""用真实 Pi 模型评测飞书答复。工具结果为固定样例，回帖只在内存中捕获。

从 backend 运行：uv run python evals/feishu_reply.py --repeats 2
使用 Weora 的 Pi 入口和 DeepSeek 环境配置。每行输出一条 JSON，含原文和检查结果。
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from uuid import uuid4

from weora.bootstrap.harness import harness_factory
from weora.bootstrap.settings import get_settings
from weora.modules.context.replies import reply_content
from weora.modules.coordination.tools import MAIN_PROMPT, MAIN_TOOLS, object_schema
from weora.modules.coordination.worker_runtime import WORKER_DELIVERY_GUIDANCE, WORKER_PROMPT, WORKER_TOOLS
from weora.modules.runs.application.execution import RuntimeFailure, RuntimeInput, RuntimeSession
from weora.modules.runs.application.harness import PI_RUNTIME, HarnessFactory
from weora.modules.runs.application.ports import ExecutionProfile
from weora.modules.tools.application.gateway import ToolSpec


@dataclass(frozen=True)
class Case:
    name: str
    role: str
    question: str
    facts: dict[str, object]
    expected_state: str
    expected_evidence: tuple[tuple[str, ...], ...] = ()


CASES = (
    Case("进度", "main", "订单导出跑完了吗？", {
        "trigger": {"id": "fixture-user", "text": "订单导出跑完了吗？"},
        "result": {"ticket_id": "fixture-ticket", "state": "ready", "verified": True,
                   "summary": "导出已完成。生成 orders.csv，包含 12 条订单。"},
    }, "result", (("12",), ("orders.csv",))),
    # 用虚构的库存规则覆盖同一判断：已确认的差异、已通过的规则和待验证的边界。
    Case("代码核对", "worker", "根据已经核实的对照结果，告诉我哪些库存规则需要改代码。", {
        "evidence": [
            "单批入库规则要求最多 40 件，样例分支实测仍允许 60 件。",
            "出库已按入库时间排序，先进先出测试通过。",
            "退库接口尚未验证，本轮未改代码。",
        ],
    }, "ready", (("40",), ("60",), ("入库",), ("退库",), ("未验证", "没验证", "还没"))),
    Case("权限阻塞", "worker", "读一下这份表，看看有没有写清盘点范围和重复扫码的处理方式。", {
        "input": {"sheet_id": "fixture-sheet", "tool": "sheet.read"},
    }, "blocked", (("无权", "权限", "授权", "PERMISSION_DENIED"), ("发", "贴", "提供", "授权", "分享"))),
    Case("方案", "worker", "帮我设计个手机实时字幕的首版。识别服务先留待选择。", {
        "constraints": ["Android 手机采音并显示字幕，Windows 电脑只用于开发联调", "第一版仅面对面单向转写"],
    }, "ready", (("Android", "安卓"), ("单向", "一个人说", "一人说", "说话者"))),
)

SHEET_READ = ToolSpec(
    "sheet.read",
    "读取指定表格。",
    object_schema({"sheet_id": {"type": "string"}}, "sheet_id"),
)


async def run_case(
    factory: HarnessFactory, profile: ExecutionProfile, case: Case, variant: str, trial: int,
    *, main_prompt: str = MAIN_PROMPT, worker_prompt: str = WORKER_PROMPT,
) -> dict[str, object]:
    calls: list[str] = []
    outputs: list[dict[str, object]] = []
    is_main = case.role == "main"
    output_tool = "orchestrator_reply" if is_main else "orchestrator_report"
    tools = tuple(
        tool for tool in (MAIN_TOOLS if is_main else WORKER_TOOLS)
        if tool.name in {"orchestrator_read", output_tool}
    )
    if case.expected_state == "blocked":
        tools += (SHEET_READ,)

    async def invoke(_: str, name: str, args: dict[str, object]) -> dict[str, object]:
        # 读取与提交都在内存中完成，回帖调用只捕获内容。
        calls.append(name)
        if name == "orchestrator_read":
            resource = args.get("resource")
            if resource == "root":
                return {"text": case.question}
            if resource == "deliveries":
                return {"items": []}
            return {"task": case.question, **case.facts}
        if name == SHEET_READ.name:
            return {"ok": False, "error": {"code": "PERMISSION_DENIED", "message": "应用无权读取表格。"}}
        if name == output_tool:
            outputs.append(args)
            return {"ok": True}
        return {"ok": False, "error": {"code": "UNKNOWN_TOOL"}}

    record: dict[str, object] = {"case": case.name, "variant": variant, "trial": trial, "model": profile.model_id}
    runtime = factory(RuntimeSession(f"reply-eval-{uuid4()}"), invoke)
    instruction = case.question + ("\n本轮是对已验收任务的跟进。" if is_main else "")
    try:
        await runtime.run(RuntimeInput(main_prompt if is_main else worker_prompt, instruction, tools), profile)
    except RuntimeFailure as error:
        return {**record, "error": error.code, "calls": calls}

    output = outputs[-1] if outputs else {}
    state = output.get("purpose" if is_main else "state")
    # 按实际状态取待发内容。进度报告留在运行记录，不能算作交付。
    field = "text" if is_main else {
        "ready": "summary", "blocked": "human_action", "failed": "human_action", "waiting": "human_action",
    }.get(str(state))
    text = str(output.get(field) or "").strip() if field else ""
    rendered = reply_content(text)
    evidence_tool = SHEET_READ.name if case.expected_state == "blocked" else "orchestrator_read"
    expected_node = {"tag": "md", "text": text}
    checks = {
        "evidence_read": evidence_tool in calls and calls.index(evidence_tool) < calls.index(output_tool)
        if output_tool in calls else False,
        "state": state == case.expected_state or (is_main and state == "answer"),
        "evidence_mentions": all(any(term in text for term in group) for group in case.expected_evidence),
        "rendering": bool(text) and rendered == {"zh_cn": {"content": [[expected_node]]}},
    }
    return {
        **record, "checks": checks, "calls": calls, "state": state, "field": field, "text": text,
        "report": output.get("summary"),
    }


async def evaluate(
    factory: HarnessFactory, profile: ExecutionProfile, repeats: int,
    *, main_prompt: str = MAIN_PROMPT, worker_prompt: str = WORKER_PROMPT,
) -> list[dict[str, object]]:
    limit = asyncio.Semaphore(4)
    guidance = WORKER_DELIVERY_GUIDANCE + "\n"
    if worker_prompt.count(guidance) != 1:
        raise ValueError("新版提示词应包含一处交付说明")
    baseline = worker_prompt.replace(guidance, "", 1)

    async def evaluate_one(case: Case, variant: str, trial: int) -> dict[str, object]:
        async with limit:
            return await run_case(
                factory, profile, case, variant, trial, main_prompt=main_prompt,
                worker_prompt=baseline if variant == "OpenDots 基线" else worker_prompt,
            )

    # 主 Agent 提示词相同，只跑基线；Worker 对照组仅移除交付说明。
    return await asyncio.gather(*(
        evaluate_one(case, variant, trial)
        for trial in range(1, repeats + 1)
        for case in CASES
        for variant in (("OpenDots 基线",) if case.role == "main" else ("OpenDots 基线", "先交付已确认内容"))
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--model", default="deepseek-flash")
    options = parser.parse_args()
    if options.repeats < 1:
        parser.error("--repeats 至少为 1")
    settings = get_settings()
    if not settings.deepseek_api_key or not settings.pi_runtime_entry:
        parser.error("需要 WEORA_DEEPSEEK_API_KEY 和 WEORA_PI_RUNTIME_ENTRY")
    profile = ExecutionProfile(PI_RUNTIME, "deepseek", options.model, None, 55, execution_timeout_seconds=70)
    factory = harness_factory(settings, profile, settings.deepseek_api_key.get_secret_value())
    for result in asyncio.run(evaluate(factory, profile, options.repeats)):
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
