"""在 CI 宿主中展示评审建议并在合并后更新项目知识。"""

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple
from uuid import UUID, uuid4

from weora_code_graph.extractor import language_for

from weora_cli.collab_client import Client, CollabError, as_object, paired_client
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call, refresh_review_indexes
from weora_cli.knowledge_ci_comments import ReviewComments, comment_marker, comment_text, marker_label
from weora_cli.knowledge_classifier import draft_review
from weora_cli.knowledge_import import review_markdown
from weora_cli.knowledge_mr import (
    CiReviewState,
    ReviewTarget,
    _commit,
    ci_changed_paths,
    ci_diff_base,
    parse_target,
    read_ci_state,
    read_review,
    require_ci_reviewable,
)

_CI_PATH = "/ci/extractions"
_GRAPH_FILE_LIMIT = 50
_STALE_EVENT_CODES = {"KNOWLEDGE_CI_HEAD_CHANGED", "KNOWLEDGE_CI_REVIEW_INACTIVE"}


def _event_head(value: str) -> str:
    head = _commit(value)
    if head is None:
        raise argparse.ArgumentTypeError("评审事件需要完整的源提交 SHA。")
    return head


def arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    extract = actions.add_parser("extract-mr", help="从 PR/MR 提炼评审建议和项目知识")
    extract.add_argument("--url", required=True, help="代码托管平台的 PR/MR 链接")
    extract.add_argument("--mode", choices=("comment", "write", "both"), default="comment")
    extract.add_argument("--event-head", type=_event_head, help="事件中的源提交；自动评论只处理仍打开的评审")
    extract.add_argument("--comment-marker", type=marker_label, default="summary", help="本评审内要复用的评论标记")
    extract.add_argument("--individual-comments", action="store_true", help="分别展示和读取经验与索引的否决")
    extract.add_argument("--no-discussions", action="store_true")
    extract.add_argument("--output", type=Path, help="将经验保存为目录内的 learning.md")
    extract.add_argument("--project", type=UUID, required=True, help="登记该仓库的 Weora 项目 ID")
    extract.add_argument("--comment-author", help="代表 CI 发布建议的代码托管账号")
    extract.add_argument("--via", choices=("codex", "claude"), default="claude")
    extract.add_argument("--language", choices=("zh", "en"), default="zh")
    extract.add_argument("--dry-run", action="store_true", help="预览草稿及合并来源")


class Markers(NamedTuple):
    summary: str
    learning: str
    graph: str


@contextmanager
def _comment_lease(client: Client, project: str, extraction: UUID) -> Iterator[tuple[str, Callable[[], None]]]:
    lease = str(uuid4())
    path = f"{_CI_PATH}/{extraction}/comment-lease"

    def renew() -> None:
        call(client, project, path, method="POST", payload={"lease_id": lease})

    renew()
    try:
        yield lease, renew
    finally:
        with suppress(CollabError):
            call(client, project, path, method="POST", payload={"lease_id": lease, "release": True})


def _comment_plan(
    client: Client, project: str, target: ReviewTarget, state: CiReviewState,
    learning: dict[str, JsonValue], markers: Markers, *, individual: bool,
) -> tuple[dict[str, str], str | None]:
    signal = "👎"
    anchor = markers.learning if individual else markers.summary
    comments = {anchor: comment_text(
        anchor, str(learning["title"]), str(learning["body"]), signal if individual else "",
    )}
    try:
        paths = [path for path in ci_changed_paths(target, expected_head=state.head_commit) if language_for(path)]
    except CollabError as exc:
        # 代码来源暂不可读时仍能正常展示经验。
        return comments, exc.code
    if not paths:
        return comments, None
    warning = None
    try:
        base = ci_diff_base(target, state.head_commit)
        graph = call(replace(client, timeout=110), project, "/ci/graph-preview", method="POST", payload={
            "url": target.url, "base_commit": base, "head_commit": state.head_commit,
            "paths": list(paths[:_GRAPH_FILE_LIMIT]),
        })
        content = _graph_body(graph, paths)
    except CollabError as exc:
        # 缺少固定 Git 对象时仍显示评审文件，保留同一个可否决的图谱评论。
        warning = exc.code
        files = "\n".join(f"- {_markdown_token(path)}" for path in paths[:20])
        extra = f"\n\n另有 {len(paths) - 20} 个文件。" if len(paths) > 20 else ""
        content = "当前仅能列出评审影响的代码文件。\n\n" + files + extra
    comments[markers.graph] = comment_text(
        markers.graph, "代码知识索引更新", content, signal if individual else "",
    )
    return comments, warning


def _markdown_token(value: object) -> str:
    cleaned = " ".join(str(value).splitlines()).replace("`", "＇")
    # 长路径保留首尾，给 PR 评论的节点与关系清单共同留出空间。
    if len(cleaned) > 80:
        cleaned = f"{cleaned[:38]}…{cleaned[-41:]}"
    return f"`{cleaned}`"


def _graph_rows(
    groups: dict[str, JsonValue], counts: dict[str, JsonValue], key: str,
) -> tuple[list[JsonValue], int]:
    rows, count = groups.get(key), counts.get(key)
    if not isinstance(rows, list) or not isinstance(count, int) or count < len(rows):
        raise CollabError("KNOWLEDGE_CI_GRAPH_INVALID", "图谱预览响应无效。")
    return rows, count


def _relation_lines(graph: dict[str, JsonValue]) -> list[str]:
    if isinstance(warning := graph.get("relation_warning"), str):
        return ["", f"关系预览暂不可用（{_markdown_token(warning)}）。"]
    if "relations" not in graph:
        return []
    groups, counts = as_object(graph.get("relations")), as_object(graph.get("relation_counts"))
    kinds = {"DEPENDS_ON": "依赖", "REFERENCES": "调用引用", "IMPLEMENTS": "接口实现"}
    lines: list[str] = []
    for key, label in (("added", "新增关系"), ("removed", "移除关系")):
        rows, count = _graph_rows(groups, counts, key)
        lines.extend(["", f"### {label}（{count}）"])
        for raw in rows[:12]:
            edge = as_object(raw)
            kind = kinds.get(str(edge.get("kind")), _markdown_token(edge.get("kind")))
            lines.append(f"- {kind} {_markdown_token(edge.get('source'))} → {_markdown_token(edge.get('target'))}")
        if count > 12:
            lines.append(f"- 另有 {count - 12} 条关系。")
    if isinstance(skipped := graph.get("relation_skipped"), int) and skipped:
        lines.extend(["", f"关系对照省略 {skipped} 个无法解析的文件。"])
    if isinstance(coverage := graph.get("relation_coverage"), dict):
        limit = coverage.get("limit")
        if isinstance(limit, int):
            lines.extend(["", f"关系按仓库索引范围比较，每个提交最多 {limit} 个候选文件。"])
    return lines


def _graph_body(graph: dict[str, JsonValue], paths: list[str]) -> str:
    groups, totals = as_object(graph.get("nodes")), as_object(graph.get("counts"))
    lines = [
        f"评审基线 {_markdown_token(str(graph['base_commit'])[:12])} 到源提交 "
        f"{_markdown_token(str(graph['head_commit'])[:12])} 的代码知识节点变化。",
    ]
    for key, label in (("added", "新增"), ("updated", "变更"), ("removed", "移除")):
        rows, count = _graph_rows(groups, totals, key)
        lines.extend(["", f"### {label}（{count}）"])
        for row in rows[:20]:
            node = as_object(row)
            path, kind = _markdown_token(node["path"]), node["kind"]
            if kind == "file":
                lines.append(f"- 文件 {path}")
            else:
                lines.append(f"- {kind} {_markdown_token(node['name'])} · {path} 第 {node['line']} 行")
        if count > 20:
            lines.append(f"- 另有 {count - 20} 个节点。")
    skipped = graph.get("skipped")
    if isinstance(skipped, list) and skipped:
        lines.extend(["", "无法解析的文件 " + "、".join(_markdown_token(path) for path in skipped[:10]) + "。"])
    if len(paths) > _GRAPH_FILE_LIMIT:
        remaining = len(paths) - _GRAPH_FILE_LIMIT
        lines.extend(["", f"展示前 {_GRAPH_FILE_LIMIT} 个评审文件的结果，另有 {remaining} 个文件。"])
    return "\n".join(lines + _relation_lines(graph))


def _settle(
    client: Client, project: str, extraction: UUID, state: CiReviewState,
    *, rejected_learning: bool = False, rejected_codebase: bool = False, lease: str | None = None,
) -> dict[str, JsonValue]:
    assert state.merge_commit is not None and state.merged_at is not None
    return call(client, project, f"{_CI_PATH}/{extraction}/merge", method="POST", payload={
        "merge_commit": state.merge_commit, "merged_at": state.merged_at.isoformat(),
        "rejected_learning": rejected_learning, "rejected_codebase": rejected_codebase,
        "comment_lease_id": lease,
    })


def _artifact(output: Path, target: ReviewTarget, state: CiReviewState, learning: dict[str, JsonValue]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    source = {
        "ref": target.url, "review_date": state.merged_at.isoformat() if state.merged_at else None,
        "review_author": learning.get("review_author"),
    }
    (output / "learning.md").write_text(review_markdown(learning | {"source": source}), encoding="utf-8")


def execute(args: argparse.Namespace) -> dict[str, JsonValue]:
    """自动事件丢弃过期建议，合并重放沿用已入库的草稿及发布收据。"""
    try:
        return _execute(args)
    except CollabError as error:
        if getattr(args, "event_head", None) and error.code in _STALE_EVENT_CODES:
            return {"status": "skipped", "code": error.code, "source": args.url}
        raise


def _check_event(args: argparse.Namespace, state: CiReviewState) -> None:
    if args.event_head != state.head_commit:
        raise CollabError("KNOWLEDGE_CI_HEAD_CHANGED", "此事件的源提交已过期。")
    if args.mode == "comment" or (args.mode == "both" and state.merge_commit is None):
        require_ci_reviewable(state.reviewable)


def _execute(args: argparse.Namespace) -> dict[str, JsonValue]:
    if args.dry_run and args.output is not None:
        raise CollabError("KNOWLEDGE_CI_PREVIEW_OUTPUT", "预览通过命令返回，不写本地文件。")
    target = parse_target(args.url)
    state = read_ci_state(target)
    if args.event_head:
        _check_event(args, state)
    merged = state.merge_commit is not None and state.merged_at is not None
    if args.mode == "write" and not merged:
        raise CollabError("KNOWLEDGE_CI_NOT_MERGED", "评审尚未合并。合并后可运行写入模式。")
    client, project = paired_client(), str(args.project)
    item = call(client, project, _CI_PATH, url=target.url, head_commit=state.head_commit)
    if item.get("status") == "missing":
        facts = read_review(target, discussions=not args.no_discussions)
        if facts.get("head_commit") != state.head_commit:
            raise CollabError("KNOWLEDGE_CI_HEAD_CHANGED", "读取评审期间源提交已变化，请重新执行。")
        draft = draft_review(args.via, target.url, facts, args.language)
        # 模型提炼期间源提交可能更新，后台任务写入草稿前重新确认事件。
        if args.event_head:
            current = read_ci_state(target)
            _check_event(args, current)
        source = as_object(draft["source"])
        proposal = {
            "url": target.url,
            "head_commit": state.head_commit,
            "title": draft["title"],
            "body": draft["body"],
            "tags": draft["tags"],
            "review_author": source.get("review_author"),
        }
        item = {"learning": draft} if args.dry_run else call(client, project, _CI_PATH, method="POST", payload=proposal)

    learning = as_object(item["learning"])
    scope = f"{client.workspace}-{project}"
    label = args.comment_marker
    proposal_scope = str(item.get("id") or "preview")
    anchors = Markers(
        comment_marker(scope, label, "summary"),
        comment_marker(proposal_scope, label, "learning"),
        comment_marker(proposal_scope if args.individual_comments else scope, label, "graph"),
    )
    already_settled = item.get("status") == "merged"
    if args.event_head and args.mode == "comment" and already_settled:
        raise CollabError("KNOWLEDGE_CI_REVIEW_INACTIVE", "此评审已经结算，忽略延迟建议。")
    may_post = not args.event_head or not merged
    posting = args.mode == "comment" or (args.mode == "both" and not already_settled and may_post)
    warnings: list[JsonValue] = []
    comments, graph_warning = _comment_plan(
        client, project, target, state, learning, anchors, individual=args.individual_comments,
    ) if posting else ({}, None)
    if graph_warning is not None:
        warnings.append({"stage": "graph_comment", "code": graph_warning})
    if args.dry_run:
        return {
            "status": "preview", "source": target.url, "merge_commit": state.merge_commit, "learning": learning,
            "comments": list(comments.values()), "warnings": warnings,
        }

    extraction = UUID(str(item["id"]))
    writing = args.mode != "comment" and merged and not already_settled
    signals = writing and args.individual_comments
    result = item
    sent: list[JsonValue] = []
    if posting or signals:
        with _comment_lease(client, project, extraction) as (lease, renew):
            reviewer = ReviewComments(
                target, state.head_commit, args.comment_author,
                require_open=bool(args.event_head and posting),
            )
            if posting:
                sent = reviewer.publish(comments, renew)
            if writing:
                notes = reviewer.find({anchors.learning, anchors.graph}) if signals else {}
                result = _settle(
                    client, project, extraction, state, lease=lease,
                    rejected_learning=reviewer.rejected(notes.get(anchors.learning)) if signals else False,
                    rejected_codebase=reviewer.rejected(notes.get(anchors.graph)) if signals else False,
                )
    elif writing:
        result = _settle(client, project, extraction, state)
    if args.mode == "comment" or not merged:
        if args.output is not None:
            _artifact(args.output, target, state, learning)
        return result | {
            "status": "commented" if args.mode == "comment" else "awaiting_merge",
            "comments": sent, "warnings": warnings,
        }
    if result["merge_commit"] != state.merge_commit:
        raise CollabError("KNOWLEDGE_CI_MERGE_CHANGED", "平台已有其他合并提交的收据，请核对来源。")

    rows: list[JsonValue] = []
    if result.get("rejected_codebase") is not True:
        try:
            paths = ci_changed_paths(target, expected_head=state.head_commit)
            if any(language_for(path) for path in paths):
                rows.extend(refresh_review_indexes(client, project, extraction, [target.url]))
            else:
                rows.append({"status": "skipped", "code": "NO_SOURCE_CHANGED"})
        except CollabError as error:
            rows.append({"status": "failed", "code": error.code})
    if args.output is not None:
        _artifact(args.output, target, state, learning)
    return result | {"codebase": rows, "comments": sent, "warnings": warnings}
