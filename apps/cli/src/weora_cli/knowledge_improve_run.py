"""根据评分失败生成同一提案的资源修改和回归任务。"""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, as_object, items, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_benchmark_executor import Sandbox, _assets, _clone
from weora_cli.knowledge_improvement import plan
from weora_cli.knowledge_improvement_delivery import publish_revision
from weora_cli.knowledge_repo_import import repository_plan
from weora_cli.knowledge_session_origin import checkout_remote, resolve_commit
from weora_cli.knowledge_settings import session_state_root

_MAX_CONTEXT = 400_000
_PATH = re.compile(r"(?:(?:rules|skills|agents)(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})+|AGENTS\.md|CLAUDE\.md)")


def _sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


def _context(
    client: Client, project: str, failures: list[dict[str, JsonValue]], scorer: str,
) -> dict[str, JsonValue]:
    rubric = items(call(client, project, "/session-scorers/plan", method="POST", payload={"scorers": [scorer]}))
    if len(rubric) != 1 or rubric[0].get("name") != scorer:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_RUBRIC", "未找到固定的评分标准。")
    notes: dict[tuple[str, int], dict[str, JsonValue]] = {}
    cases: list[JsonValue] = []
    for failure in failures:
        citations: list[JsonValue] = []
        for note in items({"items": failure.get("learnings")}):
            ident, revision = note.get("id"), note.get("revision")
            if not isinstance(ident, str) or type(revision) is not int:
                raise CollabError("KNOWLEDGE_IMPROVEMENT_EVIDENCE", "经验标识或版本无效。")
            key = str(UUID(ident)), revision
            if key not in notes:
                row = call(client, project, f"/{key[0]}")
                if row.get("revision") != revision or not isinstance(row.get("body"), str):
                    raise CollabError("KNOWLEDGE_IMPROVEMENT_EVIDENCE", "关联经验版本已变化。")
                notes[key] = {"title": row.get("title"), "body": row["body"], "revision": revision}
            citations.append(notes[key])
        cases.append({
            "label_id": failure["label_id"], "label": failure["label"], "tool": failure["runtime"],
            "learnings": citations,
        })
    return {"rubric": rubric[0], "failures": cases}


def _sources(
    args: argparse.Namespace, base_commit: str, prior: dict[str, JsonValue],
) -> tuple[dict[str, str], set[str]]:
    texts: dict[str, str] = {}
    skipped: set[str] = set()
    with TemporaryDirectory(prefix="weora-improve-") as temporary:
        root = Path(temporary)
        checkout, assets = root / "resources", root / "selected"
        _clone(args.resources, base_commit, checkout)
        _assets(checkout, assets)
        for entry in assets.rglob("*"):
            if not entry.is_file():
                continue
            name = entry.relative_to(assets).as_posix()
            if not _PATH.fullmatch(name) or entry.stat().st_size > 64_000:
                skipped.add(name)
                continue
            try:
                contents = entry.read_bytes().decode("utf-8")
                if "\0" in contents:
                    skipped.add(name)
                else:
                    texts[name] = contents
            except UnicodeError:
                skipped.add(name)
    for item in items({"items": prior.get("changes", [])}):
        path, content = item.get("path"), item.get("content")
        if not isinstance(path, str) or path in skipped or _sha(texts.get(path)) != item.get("before_sha256"):
            raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "本地资源基线和开放提案不一致。")
        if content is None:
            texts.pop(path, None)
        elif isinstance(content, str):
            texts[path] = content
        else:
            raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "开放提案的资源正文无效。")
    return texts, skipped


def _validated(
    answer: dict[str, JsonValue], failures: list[dict[str, JsonValue]], files: dict[str, str], skipped: set[str],
) -> tuple[list[JsonValue], list[JsonValue]]:
    tasks = items({"items": answer.get("tasks")})
    edits = items({"items": answer.get("changes")})
    if answer.get("change") is False and not tasks and not edits:
        return [], []
    if answer.get("change") is not True or not tasks or not edits:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GENERATION", "模型未同时给出回归任务和资源修改。")
    pins = {
        label: as_object(failure.get("git_start")).get("commit")
        for failure in failures if type(label := failure.get("label_id")) is int
    }
    results: list[JsonValue] = []
    for task in tasks:
        label = task.get("label_id")
        if (
            type(label) is not int or label not in pins
            or set(task) != {"label_id", "name", "title", "prompt", "success_criteria"}
            or any(not isinstance(task.get(key), str) or not str(task[key]).strip()
                   for key in ("name", "title", "prompt", "success_criteria"))
        ):
            raise CollabError("KNOWLEDGE_IMPROVEMENT_GENERATION", "模型返回了无法绑定原失败的任务。")
        results.append(task | {"starting_commit": pins.pop(label)})
    if pins:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GENERATION", "每个选中失败都需要一个回归任务。")
    changes: list[JsonValue] = []
    paths: set[str] = set()
    existing = {path.casefold(): path for path in (*files, *skipped)}
    for item in edits:
        path, content = item.get("path"), item.get("content")
        if (
            set(item) != {"path", "content"} or not isinstance(path, str) or not _PATH.fullmatch(path)
            or path.casefold() in paths or path in skipped or existing.get(path.casefold(), path) != path
            or (content is not None and not isinstance(content, str)) or files.get(path) == content
        ):
            raise CollabError("KNOWLEDGE_IMPROVEMENT_GENERATION", "模型返回了无效或重复的资源修改。")
        changes.append({"path": path, "before_sha256": _sha(files.get(path)), "content": content})
        paths.add(path.casefold())
    tree = {path.casefold() for path in (*files, *skipped)}
    for change in edits:
        path = str(change["path"]).casefold()
        if change["content"] is None:
            tree.discard(path)
        else:
            tree.add(path)
    if any("/".join(path.split("/")[:depth]) in tree for path in tree for depth in range(1, len(path.split("/")))):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GENERATION", "模型修改导致资源文件和目录冲突。")
    return results, changes


def _prepare(client: Client, project: str, args: argparse.Namespace) -> dict[str, JsonValue]:
    selection = plan(client, project, args.scorer, args.checkout, suite=args.suite)
    if selection.get("ready") is not True:
        return {"status": "skipped", "plan": selection}
    failures = items({"items": selection["failures"]})
    remote = checkout_remote(args.resources)
    commit = resolve_commit(args.resources, args.base_ref)
    if not remote or not commit:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "资源仓库须包含基准提交及已登记的 origin。")
    binding = repository_plan(client, project, [remote])[0]
    repository = binding.get("repository_id")
    if binding.get("status") != "ready" or not isinstance(repository, str):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "项目尚未登记此资源仓库。")
    proposal_id = selection.get("proposal_id")
    prior: dict[str, JsonValue] = {"revision": 0, "changes": []}
    if isinstance(proposal_id, str):
        prior = call(client, project, f"/self-improvement/proposals/{proposal_id}/revision", suite=args.suite)
    if prior.get("revision") != 0 and (
        prior.get("base_commit") != commit or prior.get("resource_repository_id") != repository
        or prior.get("connection_version") != binding.get("connection_version")
    ):
        raise CollabError("KNOWLEDGE_IMPROVEMENT_BASE", "开放提案所用资源仓库或基准提交已变化。")

    sandbox = Sandbox.prepare(args.image, args.network, args.gateway)
    files, skipped = _sources(args, commit, prior)
    evidence = _context(client, project, failures, args.scorer)
    evidence["resources"] = {key: value for key, value in files.items()}
    evidence["covered"] = selection.get("covered")
    if len(json.dumps(evidence, ensure_ascii=False).encode()) > _MAX_CONTEXT:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_CONTEXT", "所选证据超过单次生成容量。")
    try:
        answer = sandbox.invoke("improve", {"tool": args.via, "model": args.model, "evidence": evidence}, args.timeout)
        tasks, changes = _validated(answer, failures, files, skipped)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GENERATION", "沙箱未返回有效的改进草稿。") from error
    reason = answer.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GENERATION", "改进草稿缺少共同原因或跳过说明。")
    if not tasks:
        return {"status": "skipped", "reason": reason, "plan": selection}
    return {"status": "draft", "draft": {
        "operation_id": args.key, "scorer": args.scorer, "expected_proposal_id": proposal_id,
        "base_revision": prior["revision"], "resource_repository_id": repository,
        "base_commit": commit, "reason": reason, "tasks": tasks, "changes": changes,
    }}


def stage(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    project = args.project
    if project is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "自我改进需要指定 --project。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,159}", args.model) or not 1 <= args.timeout <= 3600:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_GENERATION", "请指定有效模型和 1 至 3600 秒的生成超时。")
    record = session_state_root(client) / "self-improvement" / f"{UUID(args.key)}.json"
    try:
        with lock_file(record):
            binding = {"project": project, "suite": args.suite, "scorer": args.scorer}
            if record.is_file():
                saved = as_object(json.loads(record.read_text(encoding="utf-8")))
                if saved.get("binding") != binding:
                    raise CollabError("KNOWLEDGE_IMPROVEMENT_OPERATION", "原操作已绑定其他项目、套件或标准。")
                draft = as_object(saved.get("draft"))
            else:
                result = _prepare(client, project, args)
                if result.get("status") != "draft":
                    return result
                draft = as_object(result["draft"])
                # 保留完整请求，网络中断时以原请求重试而不用再次调用生成器。
                write_json(record, {"binding": binding, "draft": draft})
            if args.preview:
                return {"status": "draft", "draft": draft}
            return call(client, project, "/self-improvement/proposals", method="POST", suite=args.suite, payload=draft)
    except (OSError, ValueError) as error:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_OPERATION", "无法读取或保存本机改进草稿。") from error


def recover(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    return call(client, args.project, "/self-improvement/resume",
                suite=args.suite, scorer=args.scorer, operation_id=args.key)


def finish(
    client: Client, args: argparse.Namespace, receipt: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    if receipt.get("status") == "missing":
        receipt = stage(client, args)
    if receipt.get("status") not in (None, "pending_delivery"):
        return receipt
    return publish_revision(client, args, receipt)


def execute(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    if not args.publish:
        return stage(client, args)
    if args.project is None:
        raise CollabError("KNOWLEDGE_IMPROVEMENT_PROJECT_REQUIRED", "自我改进需要指定 --project。")
    return finish(client, args, recover(client, args))
