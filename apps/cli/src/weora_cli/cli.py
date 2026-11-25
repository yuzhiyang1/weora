"""命令解析与进程输出边界；业务鉴权和执行始终由平台负责。"""

import argparse
import io
import json
import sys
from importlib.metadata import version
from typing import NoReturn

from keyring.errors import KeyringError

from weora_cli import cli_auth, collaboration, knowledge, knowledge_ci, resources, skill_telemetry, skills
from weora_cli.collab_client import CollabError
from weora_cli.commands import COMMANDS, JsonValue
from weora_cli.rule_adapters import RULE_TOOLS

PROTOCOL_VERSION = 1


class UsageError(Exception):
    """参数或能力发现错误，与业务执行失败使用不同退出码。"""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse 默认混排 usage 和错误；Agent 需要单个可解析的错误对象。
        raise UsageError(message)


def build_parser() -> Parser:
    parser = Parser(prog="weora", description="Weora 平台 CLI，支持本地 Agent 协作。", allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="operation", required=True, parser_class=Parser)
    hook = subparsers.add_parser("_resource-hook", help=argparse.SUPPRESS)
    hook.add_argument("--agent", choices=sorted(RULE_TOOLS), default="codex")
    for command in COMMANDS:
        subparser = subparsers.add_parser(command.name, help=command.description, description=command.description)
        for argument in command.arguments:
            subparser.add_argument(argument.name, nargs="*", help=argument.description)
        if command.name == "collab":
            collaboration.arguments(subparser)
        elif command.name == "skill":
            skills.arguments(subparser)
        elif command.name == "context":
            knowledge.arguments(subparser)
        elif command.name == "ci":
            knowledge_ci.arguments(subparser)
        elif command.name in {"auth", "login", "logout"}:
            cli_auth.arguments(subparser, command.name)
        elif command.name in resources.COMMANDS:
            resources.arguments(subparser, command.name)
    return parser


def execute(args: argparse.Namespace) -> JsonValue:
    if args.operation == "version":
        return {"version": version("weora-cli"), "protocol_version": PROTOCOL_VERSION}
    if args.operation == "commands":
        return {
            "commands": [command.describe() for command in COMMANDS],
            "business_capabilities": ["collab", "skill", "context", "ci", "auth", "team-resources"],
        }
    if args.operation == "schema":
        target = " ".join(args.command)
        commands = [command for command in COMMANDS if not target or command.name == target]
        if not commands:
            raise UsageError("未找到已实现的命令；请运行 weora commands 查看可用命令。")
        return {"commands": [command.describe() for command in commands]}
    if args.operation == "collab":
        return collaboration.execute(args)
    if args.operation == "skill":
        return skills.execute(args)
    if args.operation == "context":
        return knowledge.execute(args)
    if args.operation == "ci":
        return knowledge_ci.execute(args)
    if args.operation in {"auth", "login", "logout"}:
        return cli_auth.execute(args)
    if args.operation in resources.COMMANDS:
        return resources.execute(args)
    raise UsageError("未找到命令。")


def emit(payload: dict[str, JsonValue], *, error: bool = False) -> None:
    # 显式帮助保留可读文本；执行结果每次仅输出一行 JSON，不夹带日志。
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr if error else sys.stdout)


def main(argv: list[str] | None = None) -> int:
    """执行本地命令：成功返回 0，参数错误返回 2，内部异常返回 1。"""
    # 重定向到管道时仍输出 UTF-8，避免 Windows 本地代码页改变 Agent 的解析结果。
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
        if args.operation == "_resource-hook":
            if isinstance(sys.stdin, io.TextIOWrapper):
                sys.stdin.reconfigure(encoding="utf-8")
            print(json.dumps(resources.hook(args.agent), ensure_ascii=False))
            return 0
        if args.operation == "skill" and args.action == "hook":
            if isinstance(sys.stdin, io.TextIOWrapper):
                sys.stdin.reconfigure(encoding="utf-8")
            skill_telemetry.hook(args.runtime, args.event)
            if args.runtime == "zcode":
                # ZCode process Hook 需要一行 JSON，空对象表示不干预会话。
                print("{}")
            return 0
        if args.operation == "skill" and args.action == "launch":
            return skills.launch_bound(args)
        if args.operation == "collab" and args.action == "hook":
            # 原生 hook 使用客户端自己的协议，不能包一层 CLI 响应。
            print(json.dumps(collaboration.hook_output(args), ensure_ascii=False))
            return 0
        data = execute(args)
    except UsageError as exc:
        emit({"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": str(exc)}}, error=True)
        return 2
    except CollabError as exc:
        emit({"ok": False, "error": {"code": exc.code, "message": str(exc), "data": exc.data}}, error=True)
        return 1
    except KeyringError:
        emit(
            {"ok": False, "error": {"code": "KEYRING_UNAVAILABLE", "message": "系统钥匙串不可用，请先解锁或配置它。"}},
            error=True,
        )
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        # 不把异常文本或 traceback 暴露给调用 Agent，避免以后泄漏请求或凭据。
        emit({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "CLI 内部错误。"}}, error=True)
        return 1
    if args.operation == "collab" and args.action == "mcp":
        # stdio MCP 客户端只能收到 MCP 帧，退出时也不附加 CLI 响应。
        return 0

    if args.operation != "pull" or not args.silent:
        emit({"ok": True, "data": data})
    if (
        getattr(args, "action", None) == "codebase" and args.output and args.lint and isinstance(data, dict)
        and isinstance(summary := data.get("summary"), dict) and summary.get("high")
    ):
        return 1
    if getattr(args, "action", None) == "score-drain" and isinstance(data, dict) and data.get("drain_failed") is True:
        return 1
    if getattr(args, "require_ci_success", False) and isinstance(data, dict) and data.get("check_state") in (
        "failure", "error",
    ):
        return 1
    if getattr(args, "fail_on_regression", False) and isinstance(data, dict) and data.get("gate") == "regressed":
        # CI 读取正常的报告 JSON，并通过退出码识别已验证的回退。
        return 1
    return 0
