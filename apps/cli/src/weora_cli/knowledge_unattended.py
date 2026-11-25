"""汇集配对设备上的无人值守根会话，在任务结束时统一处理。"""

import argparse
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, as_object, lock_file, write_json
from weora_cli.collab_native import NATIVE, profile
from weora_cli.commands import JsonValue
from weora_cli.knowledge_activity import _active as activity_binding
from weora_cli.knowledge_activity import drain as stage_activity
from weora_cli.knowledge_activity import publish as publish_activity
from weora_cli.knowledge_finished_sessions import drain as archive_session
from weora_cli.knowledge_finished_sessions import unattended_binding
from weora_cli.knowledge_run_mode import TASK_ENV, is_unattended, task_identifier
from weora_cli.knowledge_score_work import _bound_project, stage_drained_session
from weora_cli.knowledge_score_work import drain as score_work
from weora_cli.knowledge_settings import session_state_root
from weora_cli.knowledge_transcript import resolve_transcript
from weora_cli.knowledge_transcript_text import native_text

_RETENTION = 14 * 86400
_RUNTIMES = ("codex", "claude-code")


def _queue(client: Client, runtime: str) -> Path:
    return session_state_root(client) / "unattended-sessions" / runtime / profile(runtime)


def _read(path: Path) -> dict[str, JsonValue]:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}


def _targets(client: Client, directory: Path) -> dict[str, JsonValue]:
    archive = unattended_binding(client, directory)
    score = _bound_project(client, directory, "unattended")
    activity = activity_binding(client, directory)
    scopes: dict[str, JsonValue] = {}
    if archive is not None and archive.project is not None:
        scopes["archive"] = archive.project
    if score is not None and os.environ.get("WEORA_SCORING_DISABLED") != "1":
        scopes["scoring"] = score
    if activity:
        scopes["activity"] = activity.get("project")
    return scopes


def observe(client: Client, cwd: Path, runtime: str, event: Mapping[str, JsonValue]) -> None:
    """原生 Hook 只登记已选择的项目范围和转录位置，即使在 Stop 也不发布。"""
    if (
        runtime not in NATIVE or not is_unattended(runtime, event)
        or event.get("hook_event_name") not in ("SessionStart", "UserPromptSubmit", "Stop")
        or event.get("agent_id") or event.get("stop_hook_active") is True
    ):
        return
    scopes = _targets(client, cwd)
    if not scopes:
        return
    identifier = str(UUID(str(event.get("session_id"))))
    task = task_identifier()
    path = _queue(client, runtime) / f"{identifier}.json"
    directory = str(cwd.resolve())
    with lock_file(path):
        row = _read(path)
        if row and (row.get("ended") is not None or row.get("task") != task or row.get("cwd") != directory):
            return
        if not row:
            row = {"cwd": directory, "task": task, "scopes": scopes, "seen": time.time()}
        raw = event.get("transcript_path")
        if isinstance(raw, str) and Path(raw).is_absolute():
            row["source"] = raw
        write_json(path, row)


def _signature(source: Path) -> list[JsonValue]:
    stat = source.stat()
    return [stat.st_size, stat.st_mtime_ns]


def _process(
    client: Client, runtime: str, path: Path, row: dict[str, JsonValue], target: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    cwd, native = Path(str(row["cwd"])), str(UUID(path.stem))
    ended = float(str(row.setdefault("ended", time.time())))
    # 先持久化结束时点，再发起副作用。断线续做不会把之后的召回归入旧会话。
    write_json(path, row)
    result: dict[str, JsonValue] = {"runtime": runtime, "session": native}
    raw = row.get("source")
    source = resolve_transcript(runtime, UUID(native), Path(raw) if isinstance(raw, str) else None)
    signature = _signature(source)
    observed_cwd, _ = native_text(source, runtime, UUID(native))
    if not observed_cwd or not Path(observed_cwd).is_absolute() or Path(observed_cwd).resolve() != cwd:
        raise CollabError("KNOWLEDGE_ARCHIVE_DIRECTORY", "转录的工作目录与任务目录不一致。")
    if _signature(source) != signature:
        raise CollabError("KNOWLEDGE_ARCHIVE_ACTIVE", "会话转录仍在变化，请稍后重试。")
    if "signature" in row and row["signature"] != signature:
        raise CollabError("KNOWLEDGE_DRAIN_TRANSCRIPT_CHANGED", "转录在首次结束登记后已变化。")
    row["signature"] = signature
    write_json(path, row)
    done = as_object(row.get("done", {}))
    errors: list[JsonValue] = []

    for action, project in target.items():
        if action in done:
            continue
        try:
            current = _targets(client, cwd)
            if action not in current or current[action] != project:
                raise CollabError("KNOWLEDGE_DRAIN_BINDING_CHANGED", "目录的项目绑定已变化。")
            if _signature(source) != signature:
                raise CollabError("KNOWLEDGE_DRAIN_TRANSCRIPT_CHANGED", "转录在首次结束登记后已变化。")
            if action == "archive" and isinstance(project, str):
                result["archive"] = archive_session(
                    client, cwd, runtime, native, source, apply=True, stopped_at=ended, expected_project=project,
                )["items"]
            elif action == "scoring" and isinstance(project, str):
                stage_drained_session(
                    client, cwd, runtime, native, source, stopped_at=ended, expected_project=project,
                )
                result["scoring_staged"] = True
            elif action == "activity":
                stage_activity(
                    client, cwd, runtime, native, source, stopped_at=ended, report_now=False,
                    expected_binding={"enabled": True, "project": project},
                )
            else:
                raise ValueError("无人值守记录的处理范围无效。")
            done[action] = True
            write_json(path, row | {"done": done})
        except (CollabError, OSError, ValueError) as error:
            code = error.code if isinstance(error, CollabError) else "KNOWLEDGE_DRAIN_PENDING"
            errors.append({"action": action, "code": code})
    if errors:
        result["errors"] = errors
    return result


def _eligible(row: dict[str, JsonValue], directory: Path, task: str | None) -> Path | None:
    raw, seen = row.get("cwd"), row.get("seen")
    if not isinstance(raw, str) or not isinstance(seen, int | float) or time.time() - seen > _RETENTION:
        return None
    checkout = Path(raw)
    if not checkout.is_relative_to(directory):
        return None
    if task is not None:
        return checkout if row.get("task") == task else None
    # 启动器还没有登记退出时，其他任务的手动收尾不能结束它。
    return checkout if row.get("task") is None or row.get("ended") is not None else None


def drain(
    client: Client, cwd: Path, args: argparse.Namespace, *, task: str | None = None,
) -> dict[str, JsonValue]:
    """结束本次任务或本目录的捕获记录，之后同步完成评分和用量上报。"""
    if not 1 <= args.limit <= 20:
        raise CollabError("INVALID_LIMIT", "后台评分一次可领取 1 至 20 项。")
    directory = cwd.resolve()
    sessions: list[JsonValue] = []
    errors: list[JsonValue] = []
    score_projects: set[str | None] = set()
    reports: set[Path] = set()
    for runtime in _RUNTIMES:
        for path in sorted(_queue(client, runtime).glob("*.json")):
            try:
                if _eligible(_read(path), directory, task) is None:
                    continue
                with lock_file(path):
                    row = _read(path)
                    checkout = _eligible(row, directory, task)
                    if checkout is None:
                        continue
                    scoped = as_object(row["scopes"])
                    target = {name: project for name, project in scoped.items()
                              if args.project is None or project == args.project}
                    if not target:
                        continue
                    if isinstance(project := target.get("scoring"), str) and _bound_project(
                        client, checkout, "unattended",
                    ) == project:
                        score_projects.add(project)
                    active_usage = activity_binding(client, checkout)
                    if "activity" in target and active_usage and active_usage.get("project") == target["activity"]:
                        reports.add(checkout)
                    done = as_object(row.get("done", {}))
                    if target.keys() <= done.keys():
                        continue
                    result = _process(client, runtime, path, row, target)
                    sessions.append(result)
                    if isinstance(failures := result.get("errors"), list):
                        errors.extend(failures)
            except (CollabError, OSError, ValueError, KeyError) as error:
                code = error.code if isinstance(error, CollabError) else "KNOWLEDGE_DRAIN_PENDING"
                errors.append({"runtime": runtime, "session": path.stem, "code": code})

    fallback = args.project if args.project is not None else _bound_project(client, cwd, "unattended")
    if fallback is not None or task is None and not score_projects:
        score_projects.add(fallback)
    scores: list[JsonValue] = []
    for project in sorted(score_projects, key=lambda item: item or ""):
        try:
            options = argparse.Namespace(project=project, run_mode="unattended", limit=args.limit, bound=False)
            scored = score_work(client, cwd, options)
            scores.append(scored | {"project_id": project})
            judged = scored.get("items")
            if isinstance(judged, list) and any(
                isinstance(item, dict) and item.get("status") == "failed" for item in judged
            ):
                errors.append({"project_id": project, "code": "KNOWLEDGE_DRAIN_SCORE_FAILED"})
        except (CollabError, OSError, ValueError) as error:
            code = error.code if isinstance(error, CollabError) else "KNOWLEDGE_DRAIN_SCORE_FAILED"
            errors.append({"project_id": project, "code": code})

    activity: list[JsonValue] = []
    for checkout in sorted(reports):
        try:
            report = publish_activity(client, checkout)
            if report is not None:
                activity.append(report)
                if isinstance(failures := report.get("errors"), list):
                    errors.extend(failures)
        except (CollabError, OSError, ValueError) as error:
            code = error.code if isinstance(error, CollabError) else "KNOWLEDGE_DRAIN_ACTIVITY_FAILED"
            errors.append({"code": code})
    response: dict[str, JsonValue] = {"status": "empty", "items": []}
    if len(scores) == 1:
        response = {key: value for key, value in as_object(scores[0]).items() if key != "project_id"}
    elif scores:
        response.update({"status": "processed", "scores": scores})
    if sessions:
        response["sessions"] = sessions
    if activity:
        response["activity"] = activity
    if errors:
        response.update({"drain_failed": True, "errors": errors})
    return response


def task_environment(task: UUID) -> dict[str, str]:
    return os.environ | {"WEORA_RUN_MODE": "unattended", TASK_ENV: str(task)}
