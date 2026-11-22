"""使用同一个设备身份查询知识和工作空间设置。"""

from collections.abc import Iterator
from dataclasses import replace
from typing import TypedDict
from urllib.parse import urlencode
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, items, request_json
from weora_cli.commands import JsonValue


def call(
    client: Client,
    project: str | None,
    suffix: str = "",
    *,
    method: str = "GET",
    payload: dict[str, JsonValue] | None = None,
    **params: str | int,
) -> dict[str, JsonValue]:
    url = f"{client.base.rstrip('/')}/api/v1/workspaces/{client.workspace}/knowledge{suffix}"
    if project:
        params["project_id"] = project
    return request_json(
        method,
        f"{url}?{urlencode(params)}" if params else url,
        payload,
        token=client.token,
        timeout=client.timeout,
    )


class LifecycleQuery(TypedDict, total=False):
    include_shared: int
    after: str


def lifecycle_query(project: str | None, project_only: bool) -> LifecycleQuery:
    """项目的维护视图同时读取共享根目录和生效目录。"""
    return {"include_shared": 1} if project is not None and not project_only else {}


def lifecycle_origin(item: dict[str, JsonValue], view_project: str | None) -> str | None:
    origin = item.get("project_id", view_project)
    if origin is not None and origin != view_project:
        raise CollabError("KNOWLEDGE_LIFECYCLE_SCOPE", "服务返回了当前项目之外的知识来源。")
    return view_project if origin is not None else None


def refresh_review_indexes(
    client: Client, project: str | None, run: UUID, sources: list[JsonValue],
) -> Iterator[dict[str, JsonValue]]:
    """先获取服务端确认的仓库分组，再逐仓库刷新，后一组失败不影响前一组的提交。"""
    try:
        plan = call(client, project, "/codebases/review-plan", method="POST", payload={"urls": sources})
        targets, skipped = (items({"items": plan.get(key)}) for key in ("targets", "skipped"))
    except CollabError as error:
        yield {"status": "failed", "code": error.code, "urls": sources}
        return
    for row in skipped:
        yield {"status": "skipped", "code": row.get("code"), "urls": [row.get("url")]}
    reader = replace(client, timeout=180)
    for target in targets:
        urls, repository = target.get("urls"), target.get("repository_id")
        if not isinstance(urls, list) or not isinstance(repository, str):
            yield {"status": "failed", "code": "INVALID_RESPONSE", "urls": sources}
            return
        try:
            repo = UUID(repository)
            result = call(reader, project, f"/codebases/{repo}/refresh-reviews", method="POST", payload={
                "run_id": str(run), "urls": urls,
            })
        except (CollabError, ValueError) as error:
            code = error.code if isinstance(error, CollabError) else "INVALID_RESPONSE"
            yield {"status": "failed", "repository_id": repository, "code": code, "urls": urls}
        else:
            yield result
