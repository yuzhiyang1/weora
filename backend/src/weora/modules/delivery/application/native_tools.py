"""原生 Coding SDK 在受管沙箱里运行，平台仍负责批准命令与可修改路径。"""

import json
from typing import cast

from weora.modules.delivery.domain.models import DeliveryError, PlanScope, safe_path
from weora.modules.runs.application.harness import OMP_RUNTIME, PI_RUNTIME

NATIVE_TOOL_ENTRY = "/usr/local/bin/weora-native-tool"
NATIVE_TOOLS = {
    "pi": frozenset({"read", "ls", "find", "grep", "edit", "write", "bash"}),
    "omp": frozenset({"read", "glob", "grep", "edit", "write", "bash", "ast_grep", "ast_edit"}),
}
NATIVE_MUTATIONS = frozenset({"edit", "write", "ast_edit"})
NATIVE_RUNTIME_IDS = {"pi": PI_RUNTIME, "omp": OMP_RUNTIME}


def command_kind(argv: tuple[str, ...]) -> str:
    """原生查找/编辑留下审计记录，但只有命令可作为验证证据。"""
    if len(argv) != 4 or argv[0] != NATIVE_TOOL_ENTRY or argv[2] == "bash":
        return "command"
    return "file.write" if argv[2] in NATIVE_MUTATIONS else "native_tool"


def validate_native_command(argv: tuple[str, ...], scope: PlanScope, runtime_id: str | None) -> None:
    if not argv or argv[0] != NATIVE_TOOL_ENTRY:
        return
    if len(argv) != 4 or argv[2] not in NATIVE_TOOLS.get(argv[1], ()):
        raise DeliveryError("TOOL_ARGUMENTS_INVALID", "未知的原生工具或参数")
    if NATIVE_RUNTIME_IDS[argv[1]] != runtime_id:
        raise DeliveryError("TOOL_NOT_ALLOWED", "原生工具必须与本轮运行时一致")
    try:
        arguments = json.loads(argv[3])
    except (ValueError, RecursionError):
        arguments = None
    if not isinstance(arguments, dict):
        raise DeliveryError("TOOL_ARGUMENTS_INVALID", "原生工具参数必须是 JSON 对象")
    if argv[2] not in NATIVE_MUTATIONS:
        return
    if "file.write" not in scope.actions:
        raise DeliveryError("ACTION_DENIED", "原生编辑需要 file.write 审批")
    paths = arguments.get("paths") if argv[2] == "ast_edit" else [arguments.get("path")]
    if not isinstance(paths, list) or not paths:
        raise DeliveryError("PATH_DENIED", "原生编辑必须提供明确的文件路径")
    for path in cast(list[object], paths):
        if not isinstance(path, str) or any(character in path for character in "*?[];"):
            raise DeliveryError("PATH_DENIED", "原生编辑仅接受批准范围内的明确文件路径")
        if not scope.allows_path(safe_path(path)):
            raise DeliveryError("PATH_DENIED", "原生编辑超出已批准的路径")
