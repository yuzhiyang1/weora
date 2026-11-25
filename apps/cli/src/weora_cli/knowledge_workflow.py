"""查看、上传或恢复工作空间的知识工作流定义。"""

import argparse
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_run_mode import configured_mode

SHARE_LEARNINGS = "teamai-share-learnings"
SHARE_SESSION = "teamai-share-session"
WORKFLOW_NAMES = (SHARE_LEARNINGS, SHARE_SESSION)


def arguments(parser: argparse.ArgumentParser, identifier: Callable[[str], str]) -> None:
    commands = parser.add_subparsers(dest="workflow_command", required=True)
    describe = commands.add_parser("describe", help="查看生效工作流及可用审批模式")
    describe.add_argument("name", help="工作流名称或团队别名")
    start = commands.add_parser("start", help="启动知识工作流或申请本人确认")
    start.add_argument("name", help="工作流名称或团队别名")
    start.add_argument("--runtime", required=True, choices=("codex", "claude-code"))
    start.add_argument("--session", required=True, type=identifier)
    start.add_argument("--operation", dest="workflow_operation", type=identifier, help="重复发起请求时使用原标识")
    start.add_argument("--human", action="store_true", help="让本人从浏览器启动工作流")
    approval = start.add_mutually_exclusive_group()
    approval.add_argument("--hitl", choices=("recommend", "person"), default="recommend")
    approval.add_argument("--auto-approve", action="store_true", help="自动同意可选审批")
    start.add_argument("--publish", action="store_true", help="公开脱敏会话")
    start.add_argument("--scorer", action="append", default=[], help="要运行的会话评分标准")
    resume = commands.add_parser("resume", help="读取已保存的待办步骤和发布结果")
    draft = commands.add_parser("draft", help="提交已经与本人核对的经验草稿")
    draft.add_argument("--file", action="append", type=Path, required=True)
    draft.add_argument("--namespace", help="草稿所属经验目录")
    decide = commands.add_parser("decide", help="回答允许设备代答的可选审批")
    decide.add_argument("step", help="运行返回的审批步骤标识")
    decide.add_argument("decision", choices=("approve", "reject"))
    decide.add_argument("--reason", required=True, help="说明设备代答的依据")
    session = commands.add_parser("session-complete", help="登记本次会话的归档及评分收据")
    session.add_argument("--archive", required=True, type=identifier)
    session.add_argument("--revision", required=True, type=int)
    for command in (resume, draft, decide, session):
        command.add_argument("run", type=identifier)
    for command in (describe, start, resume, draft, decide, session):
        command.add_argument("--project", type=identifier, help="运行所属项目")


def execute(
    client: Client, args: argparse.Namespace,
    read_markdown: Callable[[Path], tuple[str | None, list[str], str]],
) -> dict[str, JsonValue]:
    mode = args.workflow_command
    project = args.project
    params = {"workspace": client.workspace}
    if project:
        params["project"] = project
    name = args.name if mode in {"describe", "start"} else None
    if name is not None and (mode == "describe" or name not in WORKFLOW_NAMES):
        candidates = (name,) if name in WORKFLOW_NAMES else WORKFLOW_NAMES
        for canonical in candidates:
            details = call(client, project, f"/workflows/{canonical}")
            definition = as_object(details["definition"])
            if name in (definition.get("name"), definition.get("alias")):
                name = canonical
                if mode == "describe":
                    return details
                break
        else:
            raise CollabError("KNOWLEDGE_WORKFLOW_UNKNOWN", "当前工作空间没有这个知识工作流。")
    if mode == "start":
        unattended = configured_mode() == "unattended"
        if unattended and args.hitl == "person":
            raise CollabError("KNOWLEDGE_WORKFLOW_OPTIONS", "无人值守运行不能启用本人回答模式。")
        if args.human:
            intent = dict(
                params, runtime=args.runtime, session=args.session, operation=args.workflow_operation or str(uuid4()),
            )
            intent["publish"] = "yes" if args.publish else "no"
            intent["hitl"] = "auto" if args.auto_approve else "person"
            intent["workflow"] = name or SHARE_LEARNINGS
            query = urlencode([*intent.items(), *(("scorer", name) for name in args.scorer)])
            url = f"{client.base.rstrip('/')}/knowledge/workflows/start?{query}"
            return {"launch": "person", "approval_url": url}
        result = call(client, project, f"/workflows/{name}/runs", method="POST", payload={
            "operation_id": args.workflow_operation or str(uuid4()),
            "runtime": args.runtime, "session": args.session,
            "hitl": "auto" if unattended or args.auto_approve else args.hitl,
            "unattended": unattended, "publish": args.publish, "scorers": [name for name in args.scorer],
        })
    else:
        endpoint = f"/workflow-runs/{args.run}"
        if mode == "draft":
            drafts: list[JsonValue] = []
            for path in args.file:
                title, tags, body = read_markdown(path)
                encoded_tags: list[JsonValue] = list(tags)
                drafts.append({
                    "title": title or path.stem, "tags": encoded_tags, "body": body, "namespace": args.namespace,
                })
            result = call(client, project, f"{endpoint}/drafts", method="PUT", payload={"items": drafts})
        elif mode == "decide":
            step = quote(args.step, safe="")
            result = call(client, project, f"{endpoint}/approvals/{step}", method="PUT", payload={
                "decision": args.decision, "reason": args.reason,
            })
        elif mode == "session-complete":
            result = call(client, project, f"{endpoint}/session", method="PUT", payload={
                "archive_id": args.archive, "revision": args.revision,
            })
        else:
            result = call(client, project, endpoint)
    approval_page = f"{client.base.rstrip('/')}/knowledge/workflows/{result['id']}"
    return result | {"approval_url": f"{approval_page}?{urlencode(params)}"}


def configure(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    """覆盖和重置都要求调用者明确指定正在编辑的版本。"""
    endpoint = f"/workflows/{args.name}"
    if args.mode == "status":
        if args.file is not None or args.revision is not None:
            raise CollabError("KNOWLEDGE_WORKFLOW_OPTIONS", "查看定义时无需指定文件或版本。")
        return call(client, None, endpoint)
    if args.revision is None or args.revision < 0 or (args.mode == "apply") != (args.file is not None):
        raise CollabError("KNOWLEDGE_WORKFLOW_OPTIONS", "请指定基准版本。上传覆盖时同时指定 YAML 文件。")
    definition = None
    if args.file is not None:
        try:
            with args.file.open("rb") as stream:
                raw = stream.read(131_073)
            if len(raw) > 131_072:
                raise ValueError("file too large")
            definition = as_object(YAML(typ="safe").load(raw.decode("utf-8-sig")))
        except (OSError, ValueError, YAMLError, RecursionError, CollabError) as exc:
            raise CollabError("KNOWLEDGE_WORKFLOW_FILE", "请提供不超过 128 KiB 的知识工作流 YAML。") from exc
    return call(client, None, endpoint, method="PUT", payload={
        "base_revision": args.revision,
        "definition": definition,
    })
