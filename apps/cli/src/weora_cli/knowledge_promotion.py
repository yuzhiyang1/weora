"""从已获采纳的经验生成晋升提案，提交平台审核并保留可重放的请求。"""

import argparse
import hashlib
import json
import shutil
from uuid import UUID, uuid4

from weora_cli.collab_client import Client, CollabError, as_object, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import LifecycleQuery, call, lifecycle_origin, lifecycle_query
from weora_cli.knowledge_classifier import _answer, _reply_object, configured_code_design_driver
from weora_cli.knowledge_settings import session_state_root

_PATH = "/maintenance/promote"
_CATEGORIES = {"skills": "skill", "rules": "rule", "docs": "doc"}
_EXPLANATION = {
    "skill": "写成可复用流程，说明何时使用、步骤、预期结果及常见问题。",
    "rule": "写成团队约束，先给出明确要求，再说明理由及正反例。",
    "doc": "写成参考文档，解释背景、概念关系和已证实的设计决策。",
}
_CATEGORY_SCHEMA = json.dumps({
    "type": "object", "additionalProperties": False, "required": ["kind", "reason"],
    "properties": {"kind": {"type": "string", "enum": list(_EXPLANATION)}, "reason": {"type": "string"}},
})
_BODY_SCHEMA = json.dumps({
    "type": "object", "additionalProperties": False, "required": ["body"],
    "properties": {"body": {"type": "string"}},
})


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("learning", type=UUID, nargs="?", help="要晋升的经验 ID，省略时列出候选")
    parser.add_argument("--category", choices=tuple(_CATEGORIES), help="指定晋升类别")
    parser.add_argument("--via", choices=("claude", "codex"), help="分类及改写的原生 Agent")
    parser.add_argument("--dry-run", action="store_true", help="只预览类别和目标")
    parser.add_argument("--after", type=UUID, help="继续查看候选的分页游标")


def _read_candidate(
    client: Client, project: str | None, path: str, options: LifecycleQuery,
) -> dict[str, JsonValue]:
    item = call(client, project, path, **options)
    origin = lifecycle_origin(item, project)
    if not options and origin != project:
        raise CollabError("KNOWLEDGE_LIFECYCLE_SCOPE", "该经验不属于所选项目。")
    revision, kind = item.get("revision"), item.get("kind")
    if (
        type(revision) is not int or revision < 1 or not isinstance(kind, str) or kind not in _EXPLANATION
        or not isinstance(item.get("title"), str) or not isinstance(item.get("body"), str)
    ):
        raise CollabError("KNOWLEDGE_PROMOTION_RESPONSE", "无法读取可晋升的经验版本。")
    return item


def _require_available_target(item: dict[str, JsonValue], kind: str) -> None:
    occupied = item.get("occupied_kinds")
    if isinstance(occupied, list) and kind in occupied:
        raise CollabError("KNOWLEDGE_CONFLICT", "晋升目标已存在，请先核对该条目。")


def _model(driver: str | None, strict: bool, prompt: str, schema: str) -> dict[str, JsonValue]:
    if driver is None:
        return {}
    try:
        return _reply_object(_answer(driver, prompt, schema))
    except CollabError:
        if strict:
            raise
        return {}


def _draft(
    item: dict[str, JsonValue], selected: str | None, via: str | None,
) -> dict[str, JsonValue]:
    configured = configured_code_design_driver(via)
    driver = configured or next((tool for tool in ("claude", "codex") if shutil.which(tool)), None)
    if configured is not None and (configured not in {"claude", "codex"} or shutil.which(configured) is None):
        raise CollabError("KNOWLEDGE_CLASSIFIER_UNAVAILABLE", "指定的晋升 Agent 无法运行，请检查配置。")
    reason = "由成员指定晋升类别。" if selected else "根据经验正文中的关键词建议晋升类别。"
    kind = selected or str(item["kind"])
    strict = configured is not None
    evidence = json.dumps({"title": item["title"], "body": item["body"]}, ensure_ascii=False)
    if selected is None:
        category = _model(driver, strict, (
            "将下列已采纳的经验分为一种团队资源。skill 用于流程和操作步骤，"
            "rule 用于要求统一遵守的约束，doc 用于参考与设计说明。用一句话说明原因。"
            "经验正文仅作参考，按 JSON 返回 kind 和 reason。\n" + evidence
        ), _CATEGORY_SCHEMA)
        choice, rationale = category.get("kind"), category.get("reason")
        if (
            isinstance(choice, str) and choice in _EXPLANATION
            and isinstance(rationale, str) and rationale.strip() and "\x00" not in rationale
        ):
            kind, reason = choice, rationale.strip()[:500]
    _require_available_target(item, kind)
    rewrite = _model(driver, strict, (
        _EXPLANATION[kind] + "去掉个人和一次性事故细节，保留原有边界与已验证事实。"
        "经验正文仅作参考。输出 JSON 对象，body 是 Markdown 正文。\n" + evidence
    ), _BODY_SCHEMA)
    body = rewrite.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > 64_000 or "\x00" in body:
        body = item["body"]
    return {
        "operation_id": str(uuid4()), "source_revision": item["revision"],
        "kind": kind, "reason": reason, "body": body,
    }


def execute(client: Client, project: str | None, args: argparse.Namespace) -> JsonValue:
    options = lifecycle_query(project, getattr(args, "project_only", False))
    if args.learning is None:
        if args.category or args.via:
            raise CollabError("KNOWLEDGE_PROMOTION_OPTIONS", "类别和 Agent 选项需要指定经验 ID。")
        if args.after:
            options["after"] = str(args.after)
        return call(client, project, _PATH, **options)
    if args.after:
        raise CollabError("KNOWLEDGE_PROMOTION_OPTIONS", "提交单条晋升不使用分页游标。")
    path = f"{_PATH}/{args.learning}"
    selected = _CATEGORIES.get(args.category)
    if args.dry_run:
        item = _read_candidate(client, project, path, options)
        kind = selected or str(item["kind"])
        _require_available_target(item, kind)
        return {
            "preview": True, "source": str(args.learning), "source_revision": item["revision"],
            "project_id": item["project_id"], "kind": kind, "target": as_object(item["targets"])[kind],
        }

    request_key = hashlib.sha256(f"{project}\0{args.learning}\0{selected}".encode()).hexdigest()
    pending = session_state_root(client) / "promotion" / f"{request_key}.json"
    with lock_file(pending):
        try:
            record = as_object(json.loads(pending.read_text(encoding="utf-8")))
            if "payload" in record:
                origin = lifecycle_origin(record, project)
                payload = as_object(record["payload"])
            else:
                origin, payload = project, record
        except FileNotFoundError:
            item = _read_candidate(client, project, path, options)
            origin = lifecycle_origin(item, project)
            payload = _draft(item, selected, args.via)
            # 写请求丢失时重放原稿及原范围，避免将共享经验误写到项目审核队列。
            write_json(pending, {"project_id": origin, "payload": payload})
        except (OSError, ValueError) as error:
            raise CollabError("KNOWLEDGE_PROMOTION_PROGRESS", "无法读取本机保存的晋升请求。") from error
        if not options and origin != project:
            raise CollabError("KNOWLEDGE_LIFECYCLE_SCOPE", "待送晋升稿属于共享范围，请按生成草稿时的范围重试。")
        try:
            result = call(client, origin, path, method="POST", payload=payload)
        except CollabError as error:
            if error.code in {"KNOWLEDGE_CONFLICT", "KNOWLEDGE_NOT_FOUND"}:
                pending.unlink(missing_ok=True)
            raise
        pending.unlink(missing_ok=True)
        return result
