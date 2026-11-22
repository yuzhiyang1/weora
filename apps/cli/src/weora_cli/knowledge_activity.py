"""上报根会话的人工干预计数，正文和原始会话标识留在本机。"""

import hashlib
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, as_object, lock_file, write_json
from weora_cli.collab_native import profile
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_friction import correction_prompt, scan
from weora_cli.knowledge_native_usage import human_prompt, session_usage
from weora_cli.knowledge_run_mode import is_unattended
from weora_cli.knowledge_session_origin import subject_key
from weora_cli.knowledge_session_outcomes import eligible, finished, git_outcomes, git_start, git_stop
from weora_cli.knowledge_session_trends import note_time, remember_requests, trend_payload
from weora_cli.knowledge_settings import session_state_root
from weora_cli.knowledge_transcript import resolve_transcript

_POLICY_API = "/settings/usage-report"
_RUNTIMES = ("codex", "claude-code")
_CORRECTION_SECONDS = 60
_SETTLE_SECONDS = 2
_RETRY_SECONDS = 30 * 60


def _read(path: Path) -> dict[str, JsonValue]:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}


def _config(client: Client) -> Path:
    return session_state_root(client) / "activity-settings.json"


def _local(client: Client, cwd: Path) -> dict[str, JsonValue]:
    path = cwd.resolve()
    values = _read(_config(client))
    for directory in (path, *path.parents):
        cell = values.get(str(directory))
        if isinstance(cell, dict):
            return cell
    return {}


def _active(client: Client, cwd: Path) -> dict[str, JsonValue]:
    if os.environ.get("WEORA_USAGE_REPORT_DISABLED") == "1":
        return {}
    local = _local(client, cwd)
    return local if local.get("enabled") is True else {}


def configure(
    client: Client, cwd: Path, mode: str, *, workspace: bool, project: str | None, shared: bool,
) -> dict[str, JsonValue]:
    if workspace:
        if mode == "status":
            return call(client, None, _POLICY_API)
        if mode == "inherit":
            raise CollabError("KNOWLEDGE_USAGE_SCOPE", "工作空间只支持启用、关闭和查看。")
        return call(client, None, _POLICY_API, method="PUT", payload={"enabled": mode == "enable"})
    if mode != "enable" and (project is not None or shared):
        raise CollabError("KNOWLEDGE_USAGE_SCOPE", "只在启用上报时选择范围。")
    if mode == "enable" and project is None and not shared:
        raise CollabError("KNOWLEDGE_USAGE_SCOPE", "请使用 --project 指定项目，或用 --shared 选择共享范围。")
    if mode == "enable" and project is not None:
        call(client, project, limit=1)
    if mode != "status":
        path = _config(client)
        key = str(cwd.resolve())
        with lock_file(path):
            values = _read(path)
            if mode == "inherit":
                values.pop(key, None)
            else:
                values[key] = {"enabled": mode == "enable", "project": project}
            write_json(path, values)
    local = _active(client, cwd)
    team = call(client, None, _POLICY_API)
    return {"enabled": bool(local) and team.get("enabled") is True, "project": local.get("project"),
            "workspace_enabled": team.get("enabled")}


def _queue(client: Client, runtime: str) -> Path:
    return session_state_root(client) / "activity" / runtime / profile(runtime)


def _signature(path: Path) -> list[JsonValue]:
    stamp = path.stat()
    return [stamp.st_mtime_ns, stamp.st_size]


def _count(value: JsonValue) -> int:
    return value if type(value) is int and value >= 0 else 0


def _stop(
    row: dict[str, JsonValue], now: float, source: Path, cwd: Path, binding: dict[str, JsonValue], mode: str,
) -> dict[str, JsonValue]:
    note_time(row, now)
    row.setdefault("first_stop_day", datetime.fromtimestamp(now, UTC).date().isoformat())
    if row.get("source") != str(source):
        # 不同 rollout 即使大小和修改时间相同，也要重新扫描并分别归入根会话。
        row.pop("counters", None)
    start = row.get("outcome_start")
    if isinstance(start, dict) and row.get("outcome_recorded") is not True:
        row["outcome_stop"] = git_stop(cwd, start, row.get("outcome_stop"))
    return row | {"stopped": now, "cwd": str(cwd.resolve()), "source": str(source), "running": False,
                  "signature": _signature(source), "project": binding.get("project"), "mode": mode,
                  "pending": True, "retry_after": 0}


def observe(client: Client, cwd: Path, runtime: str, event: Mapping[str, JsonValue]) -> None:
    """Stop 只记转录的位置。下次提示词只保存是否纠正，不保存提示词。"""
    name = event.get("hook_event_name")
    if name not in ("SessionStart", "Stop", "UserPromptSubmit") or runtime not in _RUNTIMES:
        return
    if event.get("agent_id") or event.get("stop_hook_active"):
        return
    if is_unattended(runtime, event) and name != "SessionStart":
        return
    binding = _active(client, cwd)
    if not binding:
        return
    native = UUID(str(event.get("session_id")))
    path = _queue(client, runtime) / f"{native}.json"
    now = time.time()
    with lock_file(path):
        row = _read(path)
        previous = row.get("stopped")
        # 目录重新绑定时，旧项目累积的计数不能随这次会话发送到新项目。
        if row and row.get("project") != binding.get("project"):
            return
        if name == "SessionStart":
            if event.get("source") == "startup" and "outcome_start" not in row:
                row["outcome_start"] = git_start(cwd, now)
            if "outcome_start" in row:
                note_time(row, now)
                write_json(path, row | {"project": binding.get("project"), "running": True})
            return
        if name == "UserPromptSubmit":
            prompt = event.get("prompt")
            if not isinstance(prompt, str) or not human_prompt(prompt):
                return
            note_time(row, now)
            row["project"] = binding.get("project")
            row["hook_prompts"] = _count(row.get("hook_prompts")) + 1
            row["running"] = True
            if isinstance(previous, int | float):
                within_window = 0 <= now - previous <= _CORRECTION_SECONDS
                if not row.get("followed_stop") and within_window and correction_prompt(prompt):
                    row["corrections"] = _count(row.get("corrections")) + 1
                row["followed_stop"] = True
                row["pending"] = True
            write_json(path, row)
            return
        transcript = event.get("transcript_path")
        if not isinstance(transcript, str):
            return
        source = resolve_transcript(runtime, native, Path(transcript))
        signature = _signature(source)
        if row.get("running") is True or row.get("source") != str(source) or row.get("signature") != signature:
            write_json(path, _stop(row, now, source, cwd, binding, "attended") | {"followed_stop": False})


def _publish_one(
    client: Client, runtime: str, path: Path, binding: dict[str, JsonValue],
) -> bool:
    with lock_file(path):
        row = _read(path)
        directory, source, mode = row.get("cwd"), row.get("source"), row.get("mode")
        stopped = row.get("stopped")
        if row.get("pending") is not True or not isinstance(directory, str) or not isinstance(source, str):
            return False
        if not isinstance(stopped, int | float) or isinstance(stopped, bool):
            return False
        if row.get("project") != binding.get("project") or _active(client, Path(directory)) != binding:
            return False
        project = row.get("project")
        native = UUID(path.stem)
        saved = row.get("counters")
        if isinstance(saved, dict) and saved.get("signature") == row.get("signature"):
            interrupts, tool_rejections = _count(saved.get("interrupts")), _count(saved.get("tool_rejections"))
        else:
            try:
                transcript = resolve_transcript(runtime, native, Path(source))
                signature = _signature(transcript)
                stored = row.get("signature")
                if signature != stored and (
                    row.get("followed_stop") is True or not isinstance(stored, list) or len(stored) != 2
                    or type(stored[1]) is not int or type(signature[1]) is not int or signature[1] < stored[1]
                ):
                    # 新一轮对话留给下一次 Stop，允许上一轮异步追加最后的 Token 记录。
                    write_json(path, row | {"pending": False})
                    return False
                facts = scan(transcript, "codex" if runtime == "codex" else "claude-code", path.stem,
                             unattended=mode == "unattended", collect_usage=True)
                if _signature(transcript) != signature:
                    facts = None
            except (CollabError, OSError):
                facts = None
            if facts is None:
                write_json(path, row | {"retry_after": time.time() + _RETRY_SECONDS})
                raise CollabError("KNOWLEDGE_USAGE_TRANSCRIPT_UNAVAILABLE", "本次停止时的会话转录暂不可扫描。")
            interrupts, tool_rejections = facts.interrupts, len(facts.rejected)
            row["signature"] = signature
            row["counters"] = {"signature": signature, "interrupts": interrupts, "tool_rejections": tool_rejections}
            if facts.usage is not None:
                previous_usage = row.get("rollouts")
                segments = previous_usage if isinstance(previous_usage, dict) else {}
                key = hashlib.sha256(transcript.name.casefold().encode()).hexdigest()
                segments.pop(key, None)
                trend = facts.usage.trend
                segments[key] = facts.usage.snapshot() | {"trend": trend.view()}
                row["rollouts"] = segments
                row["trend_error"] = row.get("trend_error") is True or trend.has_error
                remember_requests(row, trend.requests, datetime.now(UTC))
            write_json(path, row)
    rollouts = row.get("rollouts")
    snapshots = [snapshot for snapshot in rollouts.values() if isinstance(snapshot, dict)] if isinstance(
        rollouts, dict,
    ) else []
    usage = session_usage(snapshots, _count(row.get("hook_prompts"))) if snapshots or "hook_prompts" in row else {}
    # 网络上报不能持有会话锁，否则在途的纠正或 Stop 可能被跳过。
    if _active(client, Path(directory)) != binding:
        return False
    call(client, project if isinstance(project, str) else None, "/activity/sessions", method="PUT", payload={
        "runtime": runtime, "subject_key": subject_key(runtime, native), "run_mode": mode,
        "observed_at": datetime.fromtimestamp(stopped, UTC).isoformat(),
        "interrupts": interrupts, "tool_rejections": tool_rejections, "corrections": _count(row.get("corrections")),
    } | usage | trend_payload(row, snapshots, datetime.now(UTC)))
    with lock_file(path):
        if _read(path) == row:
            write_json(path, row | {"pending": False})
    return True


def _publish_outcome(
    client: Client, runtime: str, path: Path, binding: dict[str, JsonValue],
) -> bool:
    row = _read(path)
    directory, stopped = row.get("cwd"), row.get("stopped")
    if not eligible(row, time.time()) or not isinstance(directory, str) or not isinstance(stopped, int | float):
        return False
    checkout = Path(directory)
    if _active(client, checkout) != binding:
        return False
    due = finished(row, time.time())
    observed = datetime.fromtimestamp(stopped, UTC).isoformat()
    prior = row.get("outcome_submission")
    replay = isinstance(prior, dict) and prior.get("stopped_at") == observed
    commit, pr_commit = (False, None) if replay else git_outcomes(row, checkout)
    with lock_file(path):
        if _read(path) != row:
            return False
        stop = row.get("outcome_stop")
        if pr_commit and isinstance(stop, dict):
            row["outcome_pr"] = {"branch": stop.get("branch"), "commit": pr_commit}
            write_json(path, row)
        if not due:
            return False
        # 回执丢失时复用第一次的判定。再次 Stop 后为新的结束时间重新判定。
        payload: dict[str, JsonValue]
        if replay and isinstance(prior, dict):
            payload = prior
        else:
            payload = {
                "runtime": runtime, "subject_key": subject_key(runtime, UUID(path.stem)),
                "run_mode": row.get("mode"), "stopped_at": observed, "has_commit": commit, "has_pr": bool(pr_commit),
            }
            row["outcome_submission"] = payload
            write_json(path, row)
    if _active(client, checkout) != binding:
        return False
    project = row.get("project")
    call(client, project if isinstance(project, str) else None, "/activity/session-outcomes", method="PUT",
         payload=payload)
    with lock_file(path):
        latest = _read(path)
        if latest.get("outcome_submission") == payload:
            write_json(path, latest | {"outcome_recorded": True})
    return True


def publish(client: Client, cwd: Path) -> dict[str, JsonValue] | None:
    binding = _active(client, cwd)
    if not binding:
        return None
    ready: list[tuple[str, Path, float]] = []
    outcomes: list[tuple[bool, float, str, Path]] = []
    pending = 0
    now = time.time()
    for runtime in _RUNTIMES:
        for path in sorted(_queue(client, runtime).glob("*.json")):
            try:
                row = _read(path)
            except (OSError, ValueError, CollabError):
                continue
            if row.get("project") != binding.get("project"):
                continue
            stopped = row.get("stopped")
            if isinstance(stopped, int | float) and eligible(row | {"pending": False}, now):
                outcomes.append((not finished(row, now), stopped, runtime, path))
            if row.get("pending") is True:
                pending += 1
                retry = row.get("retry_after")
                if not isinstance(retry, int | float) or retry <= now:
                    settled = (
                        stopped + _SETTLE_SECONDS
                        if row.get("mode") == "attended" and isinstance(stopped, int | float) else now
                    )
                    ready.append((runtime, path, settled))
    errors: list[JsonValue] = []
    reported = 0
    for runtime, path, settled in ready[:25]:
        try:
            # Codex 可能在 Stop 返回后才追加本轮用量，等待发生在 CLI 上报进程。
            delay = min(_SETTLE_SECONDS, settled - time.time())
            if delay > 0:
                time.sleep(delay)
            reported += _publish_one(client, runtime, path, binding)
            if _read(path).get("pending") is not True:
                pending -= 1
        except (CollabError, OSError, ValueError) as error:
            code = error.code if isinstance(error, CollabError) else "KNOWLEDGE_USAGE_PENDING"
            errors.append({"runtime": runtime, "code": code})
            if code in ("WEORA_UNREACHABLE", "KNOWLEDGE_USAGE_DISABLED"):
                break
    outcome_count = 0
    for _, _, runtime, path in sorted(outcomes)[:25]:
        try:
            outcome_count += _publish_outcome(client, runtime, path, binding)
        except (CollabError, OSError, ValueError) as error:
            code = error.code if isinstance(error, CollabError) else "KNOWLEDGE_OUTCOME_PENDING"
            errors.append({"runtime": runtime, "code": code})
            if code in ("WEORA_UNREACHABLE", "KNOWLEDGE_USAGE_DISABLED"):
                break
    result: dict[str, JsonValue] = {"reported": reported, "pending": pending, "errors": errors}
    if outcome_count:
        result["outcomes_reported"] = outcome_count
    return result


def drain(
    client: Client, cwd: Path, runtime: str, session: str, transcript: Path | None, *,
    stopped_at: float | None = None, report_now: bool = True, expected_binding: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue] | None:
    binding = _active(client, cwd)
    if expected_binding is not None and binding != expected_binding:
        raise CollabError("KNOWLEDGE_DRAIN_BINDING_CHANGED", "目录的活动上报范围已变化。")
    if not binding or runtime not in _RUNTIMES:
        return None
    native = UUID(session)
    source = resolve_transcript(runtime, native, transcript)
    path = _queue(client, runtime) / f"{native}.json"
    with lock_file(path):
        row = _read(path)
        if row and row.get("project") != binding.get("project"):
            return None
        now = time.time() if stopped_at is None else stopped_at
        write_json(path, _stop(row, now, source, cwd, binding, "unattended"))
    return publish(client, cwd) if report_now else None
