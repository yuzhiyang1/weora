"""把经过确认的回归 YAML 登记到项目套件。"""

import argparse
import json
import re
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from weora_cli.collab_client import Client, CollabError, as_object, items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_benchmark_executor import Sandbox
from weora_cli.knowledge_benchmark_executor import execute as execute_run
from weora_cli.knowledge_repo_import import repository_plan, repository_reference
from weora_cli.knowledge_session_origin import checkout_remote, commits_in_checkout, resolve_commit

DEFAULT_SUITE = "benchmarks/regressions"


def _yaml(
    path: Path, required: set[str], optional: set[str],
) -> dict[str, JsonValue]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("file too large")
        document = as_object(YAML(typ="safe").load(raw.decode("utf-8-sig")))
    except (OSError, ValueError, YAMLError, RecursionError, CollabError) as error:
        raise CollabError("KNOWLEDGE_BENCHMARK_FILE", "无法读取不超过 64 KiB 的回归 YAML。") from error
    if document.get("schemaVersion") != "v1alpha1" or required - document.keys() or document.keys() - (
        required | optional | {"schemaVersion"}
    ):
        raise CollabError("KNOWLEDGE_BENCHMARK_FILE", "回归 YAML 版本或字段不匹配。")
    return document


def _timeout(value: JsonValue) -> int:
    if not isinstance(value, str) or not (match := re.fullmatch(r"([1-9][0-9]{0,5})(s|m|h)", value)):
        raise CollabError("KNOWLEDGE_BENCHMARK_TIMEOUT", "请使用带 s、m 或 h 的超时值，例如 2h。")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]


def _suite(client: Client, project: str, args: argparse.Namespace) -> dict[str, JsonValue]:
    spec = _yaml(args.file, {"name", "repo", "matrix", "timeout"}, {"description", "defaults", "repetitions"})
    name, repo = spec["name"], spec["repo"]
    description, repetitions = spec.get("description", ""), spec.get("repetitions", 3)
    matrix = as_object(spec["matrix"])
    defaults = as_object(spec.get("defaults", {}))
    tools = matrix.get("tool")
    if (
        not isinstance(name, str) or not isinstance(repo, str) or set(matrix) != {"tool", "team"}
        or matrix.get("team") != ["main", "pulled"] or not isinstance(tools, list)
        or any(not isinstance(tool, str) or tool not in {"claude", "codex"} for tool in tools)
        or not isinstance(description, str)
        or type(repetitions) is not int
        or defaults.keys() - {"tool"} or ("tool" in defaults and defaults["tool"] not in tools)
    ):
        raise CollabError("KNOWLEDGE_BENCHMARK_SUITE", "套件须指定仓库、工具和 [main, pulled] 对比矩阵。")
    path = args.suite or f"benchmarks/{name}"
    if path.rsplit("/", 1)[-1] != name:
        raise CollabError("KNOWLEDGE_BENCHMARK_SUITE", "套件路径末段须与 YAML 名称一致。")
    registered = repository_plan(client, project, [repository_reference(repo)])[0]
    if registered.get("status") != "ready" or not isinstance(registered.get("repository_id"), str):
        raise CollabError("KNOWLEDGE_BENCHMARK_REPOSITORY", "套件仓库未唯一登记到该项目。")
    return call(client, project, "/benchmark/suites", method="POST", payload={
        "path": path, "repository_id": registered["repository_id"], "description": description,
        "tools": tools, "repetitions": repetitions, "timeout_seconds": _timeout(spec["timeout"]),
    })


def _task(client: Client, project: str, args: argparse.Namespace) -> dict[str, JsonValue]:
    spec = _yaml(args.file, {"title", "prompt", "startingCommit", "successCriteria"}, {"source"})
    commit = spec["startingCommit"]
    source = spec.get("source")
    if any(not isinstance(spec[key], str) for key in ("title", "prompt", "successCriteria")) or (
        source is not None and (
            not isinstance(source, dict) or set(source) != {"platform", "sessionId"}
            or any(not isinstance(value, str) for value in source.values())
        )
    ):
        raise CollabError("KNOWLEDGE_BENCHMARK_FILE", "任务须包含文本描述和判定标准，来源可指定原生会话。")
    # 任务登记不拉取缺失对象。回归开始提交应由当前项目检出提供。
    if not isinstance(commit, str) or commit not in commits_in_checkout(Path.cwd(), {commit}):
        raise CollabError("KNOWLEDGE_BENCHMARK_START", "当前检出找不到任务的开始提交。")
    payload: dict[str, JsonValue] = {
        "name": args.file.stem, "scorer": args.scorer, "label_id": args.label, "title": spec["title"],
        "prompt": spec["prompt"], "starting_commit": commit, "success_criteria": spec["successCriteria"],
        "proposed": not args.publish, "source": spec.get("source"),
    }
    payload["operation_id"] = args.key or str(uuid5(
        NAMESPACE_URL, json.dumps([project, args.suite, payload], ensure_ascii=False, sort_keys=True),
    ))
    return call(client, project, "/benchmark/tasks", method="POST", suite=args.suite, payload=payload)


def _run_plan(client: Client, project: str, args: argparse.Namespace) -> dict[str, JsonValue]:
    base = resolve_commit(args.resources, args.base_ref)
    proposed = resolve_commit(args.resources, args.proposal_ref)
    remote = checkout_remote(args.resources)
    if base is None or proposed is None or remote is None:
        raise CollabError("KNOWLEDGE_BENCHMARK_RESOURCES", "资源仓库需包含两侧提交并配置已登记的 origin。")
    match = repository_plan(client, project, [remote])[0]
    repo = match.get("repository_id")
    if match.get("status") != "ready" or not isinstance(repo, str):
        raise CollabError("KNOWLEDGE_BENCHMARK_REPOSITORY", "资源仓库未唯一登记到该项目。")
    payload: dict[str, JsonValue] = {
        "proposal_id": args.proposal, "resource_repository_id": repo,
        "base_resources": base, "proposal_resources": proposed, "tasks": sorted(args.task),
    }
    # 同样的版本和任务可显式重跑。给定操作键时服务端复用既有运行。
    payload["operation_id"] = args.key
    return call(client, project, "/benchmark/runs", method="POST", suite=args.suite, payload=payload)


def _record(client: Client, project: str, run: str, path: Path) -> dict[str, JsonValue]:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("file too large")
        records = items({"items": json.loads(raw)})
        if not records:
            raise ValueError("empty results")
    except (OSError, ValueError, RecursionError, CollabError) as error:
        raise CollabError("KNOWLEDGE_BENCHMARK_RESULTS", "结果文件须为包含试验判定的 JSON 数组。") from error
    return call(client, project, f"/benchmark/runs/{run}/results", method="POST", payload={"items": list(records)})


def execute(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    project = args.project
    if project is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "回归套件需要指定 --project。")
    if args.benchmark_action == "suite-add":
        return _suite(client, project, args)
    if args.benchmark_action == "task-add":
        return _task(client, project, args)
    if args.benchmark_action == "run-execute":
        return execute_run(client, project, args)
    if args.benchmark_action == "run":
        sandbox = Sandbox.prepare(args.image, args.network, args.gateway)
        planned = _run_plan(client, project, args)
        args.run = planned.get("id")
        if not isinstance(args.run, str):
            raise CollabError("KNOWLEDGE_BENCHMARK_INPUT", "平台未返回基准运行标识。")
        print(f"基准运行 {args.run}，续做时可用 run-execute。", file=sys.stderr)
        return execute_run(client, project, args, sandbox=sandbox)
    if args.benchmark_action == "run-plan":
        return _run_plan(client, project, args)
    if args.benchmark_action == "run-record":
        return _record(client, project, args.run, args.file)
    if args.benchmark_action in ("run-report", "run-inputs"):
        path = f"/benchmark/runs/{args.run}"
        return call(client, project, f"{path}/inputs" if args.benchmark_action == "run-inputs" else path)
    if args.benchmark_action == "tasks":
        params = {"proposal_id": args.proposal} if args.proposal else {}
        return call(client, project, "/benchmark/suites", suite=args.suite, **params)
    return call(client, project, f"/benchmark/proposals/{args.proposal}", method="PUT", suite=args.suite,
                payload={"status": args.status})
