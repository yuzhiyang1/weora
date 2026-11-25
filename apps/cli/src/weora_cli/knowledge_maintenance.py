"""按批次预演或执行知识维护，失败时保留已处理条目和续做位置。"""

from math import isfinite
from typing import TypedDict

from weora_cli.collab_client import Client, CollabError, items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call, lifecycle_query


class MaintenanceQuery(TypedDict, total=False):
    threshold: str
    candidates_only: int
    after: str
    include_shared: int


def maintain(
    client: Client, project: str | None, *, confidence: bool, preview: bool,
    threshold: float | None, after: str | None, project_only: bool = False,
) -> dict[str, JsonValue]:
    if threshold is not None and not isfinite(threshold):
        raise CollabError("KNOWLEDGE_PRUNE_THRESHOLD", "--threshold 必须是有限数字。")
    query: MaintenanceQuery = {}
    if lifecycle_query(project, project_only):
        query["include_shared"] = 1
    if threshold is not None:
        query["threshold"] = str(threshold)
    if not confidence:
        query["candidates_only"] = 1
    method = "GET" if preview else "POST"
    endpoint = "/maintenance/confidence" if confidence else "/maintenance" if preview else "/maintenance/archive"
    candidates: list[dict[str, JsonValue]] = []
    visited = {after} if after else set()
    while True:
        if after:
            query["after"] = after
        try:
            page = call(client, project, endpoint, method=method, **query)
        except CollabError as error:
            if not candidates and not after:
                raise
            raise CollabError(error.code, str(error), {
                "items": list(candidates), "next_after": after, "preview": preview,
            }) from error
        candidates.extend(items(page))
        cursor = page.get("next_after")
        if cursor is None:
            break
        if not isinstance(cursor, str) or cursor in visited:
            raise CollabError("KNOWLEDGE_MAINTENANCE_RESPONSE", "服务端返回的维护续做位置无效。")
        visited.add(cursor)
        after = cursor
    if not confidence:
        candidates.sort(key=lambda row: float(str(row["confidence"])))
    return {"items": list(candidates), "preview": preview, "next_after": None}
