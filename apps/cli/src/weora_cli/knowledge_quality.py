"""生成可编辑的质量草稿，明确提交后进入平台版本审核。"""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import NAMESPACE_URL, UUID, uuid5

from weora_cli.collab_client import Client, CollabError, as_object, config_path, items, lock_file
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import LifecycleQuery, call, lifecycle_origin, lifecycle_query
from weora_cli.knowledge_classifier import draft_quality
from weora_cli.knowledge_settings import session_state_root

_API = "/maintenance/quality"


def _baseline(record: dict[str, JsonValue], *, draft: bool = False) -> int | str:
    """普通知识按序号绑定，Registry Skill 按不可变版本 ID 绑定。"""
    number = record.get("base_revision" if draft else "revision")
    skill_version = record.get("base_skill_version_id" if draft else "skill_version_id")
    if type(number) is int and number > 0 and skill_version is None:
        return number
    if number is None and isinstance(skill_version, str):
        return str(UUID(skill_version))
    raise ValueError("invalid baseline")


def _base_field(baseline: int | str) -> dict[str, JsonValue]:
    key = "base_revision" if isinstance(baseline, int) else "base_skill_version_id"
    return {key: baseline}


def generate(
    client: Client,
    project: str | None,
    *,
    driver: str,
    preview: bool,
    output: Path | None,
    after: str | None,
    project_only: bool = False,
) -> dict[str, JsonValue]:
    scope = lifecycle_query(project, project_only)
    candidates: list[dict[str, JsonValue]] = []
    cursors: set[str] = set()
    while True:
        query = scope.copy()
        if after is not None:
            query["after"] = after
        page = call(client, project, _API, **query)
        candidates.extend(items(page))
        cursor = page.get("next_after")
        if cursor is None:
            break
        if not isinstance(cursor, str) or cursor in cursors:
            raise CollabError("KNOWLEDGE_QUALITY_RESPONSE", "服务端返回的质量候选分页无效。")
        cursors.add(cursor)
        after = cursor
    candidates.sort(key=lambda row: -int(str(row.get("recalls", 0))))
    if preview:
        return {"preview": True, "items": list(candidates)}
    directory = output or config_path().parent / "knowledge-quality" / session_state_root(client).name
    results: list[JsonValue] = []
    failures: list[JsonValue] = []
    for row in candidates:
        try:
            entry = str(UUID(str(row["id"])))
            baseline = _baseline(row)
            prefix = "r" if isinstance(baseline, int) else "v"
            path = directory / f"{entry}.{prefix}{baseline}.draft.md"
            origin = lifecycle_origin(row, project)
            if not scope and origin != project:
                raise CollabError("KNOWLEDGE_LIFECYCLE_SCOPE", "候选不属于所选项目。")
            status = _create_draft(client, project, driver, entry, baseline, path, origin=origin, query=scope)
            results.append({
                "id": entry, **_base_field(baseline), "draft": str(path), "status": status,
                **({"project_id": origin} if scope else {}),
            })
        except (KeyError, ValueError, OSError, CollabError) as exc:
            code = exc.code if isinstance(exc, CollabError) else "KNOWLEDGE_QUALITY_DRAFT_FAILED"
            failures.append({"id": row.get("id"), "title": row.get("title"), "code": code})
    if failures:
        raise CollabError(
            "KNOWLEDGE_QUALITY_INCOMPLETE",
            "部分条目未能生成质量草稿。",
            {
                "items": results,
                "failed": failures,
            },
        )
    return {"items": results, "failed": failures}


def _create_draft(
    client: Client,
    project: str | None,
    driver: str,
    entry: str,
    baseline: int | str,
    path: Path, *,
    origin: str | None, query: LifecycleQuery,
) -> str:
    if path.exists():
        return "exists"
    context = call(client, project, f"{_API}/{entry}", **query)
    if lifecycle_origin(context, project) != origin:
        raise CollabError("KNOWLEDGE_LIFECYCLE_SCOPE", "条目的归属与候选视图不一致。")
    if _baseline(context) != baseline:
        raise CollabError("KNOWLEDGE_QUALITY_BASE_CHANGED", "正式版本已变化，请重新查找质量候选。")
    body = draft_quality(driver, context)
    metadata = {
        "workspace": client.workspace,
        "project": origin,
        "id": entry,
        **_base_field(baseline),
        "title": context.get("title"),
    }
    # 已经落盘的草稿可能有人编辑，只为同一条目与基准创建一份文件。
    with lock_file(path):
        if path.exists():
            return "exists"
        with NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
            stream.write(f"---\nweora_quality: {json.dumps(metadata, ensure_ascii=False)}\n---\n\n{body.rstrip()}\n")
            staged = Path(stream.name)
        try:
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
    return "drafted"


def submit(client: Client, path: Path, project: str | None) -> dict[str, JsonValue]:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            text = stream.read(70_001)
        header, metadata, end, body = text.split("\n", 3)
        if len(text) > 70_000 or header != "---" or end != "---" or not metadata.startswith("weora_quality: "):
            raise ValueError("invalid draft")
        draft = as_object(json.loads(metadata.removeprefix("weora_quality: ")))
        entry = str(UUID(str(draft["id"])))
        baseline, scope = _baseline(draft, draft=True), draft["project"]
        if scope is not None and not isinstance(scope, str):
            raise ValueError("invalid metadata")
        if scope is not None:
            scope = str(UUID(scope))
    except (OSError, ValueError, KeyError, CollabError) as exc:
        raise CollabError("KNOWLEDGE_QUALITY_DRAFT_INVALID", "无法读取草稿或草稿的版本信息无效。") from exc
    if draft.get("workspace") != client.workspace or (project is not None and project != scope):
        raise CollabError("KNOWLEDGE_QUALITY_SCOPE", "草稿的工作空间或项目与当前提交不符。")
    # 生成时在元数据后加一行空行，保留用户正文内部的 Markdown 空白和缩进。
    body = body.removeprefix("\n")
    if not body.strip() or len(body) > 64_000 or "\x00" in body:
        raise CollabError("KNOWLEDGE_QUALITY_DRAFT_INVALID", "请提供 64,000 字内的非空草稿正文。")
    request = json.dumps([client.workspace, scope, entry, baseline, body], ensure_ascii=False)
    operation = str(uuid5(NAMESPACE_URL, f"weora:quality:{request}"))
    return call(
        client,
        scope,
        f"{_API}/{entry}",
        method="POST",
        payload={
            **_base_field(baseline),
            "body": body,
            "operation_id": operation,
        },
    )
