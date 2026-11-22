"""发现这台设备上已绑定项目的结束会话，按发布策略处理。"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, lock_file, write_json
from weora_cli.collab_native import native_transcript, profile
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_run_mode import RunMode, is_unattended
from weora_cli.knowledge_score_work import observe_scoring
from weora_cli.knowledge_score_work import start_if_bound as start_scoring
from weora_cli.knowledge_session_origin import start_metadata, subject_key
from weora_cli.knowledge_settings import session_state_root
from weora_cli.knowledge_skill_capture import observe_skill_capture, skill_capture
from weora_cli.knowledge_transcript import captured_snapshot

_MODES: tuple[RunMode, ...] = ("attended", "unattended")
_RUNTIMES = ("codex", "claude-code")
_ACTIONS = ("off", "ask", "auto")
_API = "/settings/session-archive"
_IDLE = 6 * 3600
_RETRY_AFTER = 30 * 60


def _object(path: Path) -> dict[str, JsonValue]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("本机归档设置或会话记录格式无效。")
    return data


def _settings(client: Client) -> Path:
    return session_state_root(client) / "archive-settings.json"


@dataclass(frozen=True)
class Binding:
    directory: Path
    project: str | None
    action: str


def _bindings(client: Client, cwd: Path) -> dict[RunMode, Binding]:
    stored = _object(_settings(client))
    root = cwd.resolve()
    bindings: dict[RunMode, Binding] = {}
    for directory in (root, *root.parents):
        entry = stored.get(str(directory))
        if not isinstance(entry, dict):
            continue
        for mode in _MODES:
            value = entry.get(mode)
            if mode in bindings or not isinstance(value, dict):
                continue
            action, project = value.get("action"), value.get("project")
            if action == "off":
                bindings[mode] = Binding(directory, None, action)
            elif isinstance(action, str) and action in _ACTIONS and isinstance(project, str):
                bindings[mode] = Binding(directory, str(UUID(project)), action)
    return bindings


def _team_action(team: dict[str, JsonValue], mode: RunMode) -> str:
    value = team.get(mode)
    action = value.get("action") if isinstance(value, dict) else None
    if not isinstance(action, str) or action not in _ACTIONS:
        raise CollabError("KNOWLEDGE_SETTINGS_INVALID", "工作空间返回的归档策略格式无效。")
    return action


def configure(
    client: Client, cwd: Path, mode: RunMode, action: str, *, workspace: bool, project: str | None,
) -> dict[str, JsonValue]:
    """成员为目录明确绑定目标项目，工作空间管理员独立授权自动归档。"""
    if workspace:
        if project is not None or action == "inherit":
            raise CollabError("KNOWLEDGE_SETTINGS_SCOPE", "工作空间归档支持 off、ask、auto 和 status。")
        if action != "status":
            return call(client, None, f"{_API}/{mode}", method="PUT", payload={"action": action})
        team = call(client, None, _API)
        value = team.get(mode)
        return {"action": _team_action(team, mode), "source": value.get("source") if isinstance(value, dict) else None}

    if action == "status" and project is not None:
        raise CollabError("KNOWLEDGE_SETTINGS_SCOPE", "查看当前目录时无需指定 --project。")
    if action != "status":
        if (action in ("ask", "auto")) != (project is not None):
            raise CollabError("KNOWLEDGE_SETTINGS_SCOPE", "目录选择 ask 或 auto 时请指定 --project。")
        if project is not None:
            call(client, project, query="", limit=1)
        if action == "auto" and _team_action(call(client, None, _API), mode) != "auto":
            raise CollabError("KNOWLEDGE_ARCHIVE_AUTOMATIC_DISABLED", "工作空间当前未允许此模式自动归档。")
        path = _settings(client)
        key = str(cwd.resolve())
        with lock_file(path):
            entries = _object(path)
            existing = entries.get(key)
            entry = existing if isinstance(existing, dict) else {}
            if action == "inherit":
                entry.pop(mode, None)
            else:
                entry[mode] = {"action": action, "project": project}
            entries[key] = entry
            write_json(path, entries)

    binding = _bindings(client, cwd).get(mode)
    if os.environ.get("WEORA_ARCHIVE_DISABLED") == "1":
        return {"action": "off", "source": "environment"}
    if binding is None:
        return {"action": "off", "source": "directory-default"}
    result: dict[str, JsonValue] = {"action": binding.action, "source": "directory", "project_id": binding.project}
    if binding.action == "auto":
        team_action = _team_action(call(client, None, _API), mode)
        result["workspace_action"] = team_action
        result["action"] = "auto" if team_action == "auto" else team_action
    return result


def _records(client: Client, runtime: str) -> Path:
    return session_state_root(client) / "archive-queue" / profile(runtime) / runtime


def _signature(path: Path) -> list[JsonValue]:
    info = path.stat()
    return [info.st_mtime_ns, info.st_size]


def unattended_binding(client: Client, cwd: Path) -> Binding | None:
    if os.environ.get("WEORA_ARCHIVE_DISABLED") == "1":
        return None
    return _bindings(client, cwd).get("unattended")


def observe(client: Client, cwd: Path, runtime: str, event: Mapping[str, JsonValue]) -> str:
    """随着受支持的原生 Hook 记录交互会话的活动，并在新会话启动时处理已结束会话。"""
    if runtime not in _RUNTIMES or event.get("agent_id") or event.get("stop_hook_active") is True:
        return ""
    with suppress(OSError, ValueError, CollabError):
        observe_skill_capture(client, cwd, runtime, event)
    if is_unattended(runtime, event):
        return ""
    record = _records(client, runtime) / f"{UUID(str(event.get('session_id')))}.json"
    name = event.get("hook_event_name")
    with suppress(OSError, ValueError, CollabError):
        observe_scoring(client, cwd, runtime, event)
    if name == "SessionStart":
        with suppress(OSError, ValueError, CollabError):
            start_scoring(client, cwd, "attended")
    if os.environ.get("WEORA_ARCHIVE_DISABLED") == "1":
        return ""
    if name in ("SessionStart", "UserPromptSubmit") and record.is_file():
        # 恢复旧会话时先取消待归档资格，直到下次 Stop 并再次闲置。
        with lock_file(record):
            row = _object(record)
            if row and row.get("published") is not True:
                write_json(record, row | {"ready": False})
    binding = _bindings(client, cwd).get("attended")
    if binding is None or binding.project is None:
        return ""
    if name == "SessionStart":
        return _at_start(client, cwd, binding)
    raw = event.get("transcript_path")
    if name != "Stop" or not isinstance(raw, str):
        return ""
    found = native_transcript(runtime, UUID(record.stem), raw)
    if found is not None:
        signature = _signature(found)
        with lock_file(record):
            old = _object(record)
            stopped_row: dict[str, JsonValue] = {
                "cwd": str(cwd.resolve()), "directory": str(binding.directory), "project": binding.project,
                "source": str(found), "ready": True,
                "stopped": time.time(), "signature": signature,
            }
            if (
                old.get("published") is True and old.get("signature") == signature
                and old.get("project") == binding.project
            ):
                stopped_row["published"] = True
            write_json(record, stopped_row)
    return ""


def _at_start(client: Client, cwd: Path, binding: Binding) -> str:
    # 会话 Hook 只查看本地候选。网络请求和转录读取均在后台执行。
    candidates = _attended(client, binding, limit=1)
    if not candidates:
        return ""
    if binding.action == "auto":
        subprocess.Popen(
            [sys.executable, "-m", "weora_cli", "context", "finished-sessions", "--apply"], cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, start_new_session=os.name != "nt",
        )
        return ""
    return "本机有已结束的会话可供核对。运行 weora context finished-sessions 查看，再选择是否分享。"


def _attended(client: Client, binding: Binding, *, limit: int) -> list[tuple[float, str, Path]]:
    candidates: list[tuple[float, str, Path]] = []
    now = time.time()
    for runtime in _RUNTIMES:
        for path in _records(client, runtime).glob("*.json"):
            with suppress(OSError, ValueError, RecursionError):
                row = _object(path)
                stopped, retry = row.get("stopped"), row.get("retry_after")
                if (
                    not isinstance(stopped, int | float) or isinstance(stopped, bool) or not 0 <= stopped <= now - _IDLE
                    or row.get("project") != binding.project or row.get("ready") is not True
                    or row.get("published") is True or row.get("directory") != str(binding.directory)
                    or (isinstance(retry, int | float) and retry > now)
                ):
                    continue
                candidates.append((stopped, runtime, path))
    return sorted(candidates)[:limit]


def _apply_one(
    client: Client, runtime: str, path: Path, binding: Binding, *, apply: bool,
) -> dict[str, JsonValue] | None:
    with lock_file(path):
        row = _object(path)
        stopped, raw, cwd = row.get("stopped"), row.get("source"), row.get("cwd")
        if (
            row.get("published") is True or row.get("ready") is not True or row.get("project") != binding.project
            or not isinstance(stopped, int | float) or isinstance(stopped, bool) or time.time() - stopped < _IDLE
            or not isinstance(raw, str) or not isinstance(cwd, str)
            or _bindings(client, Path(cwd)).get("attended") != binding
        ):
            return None
        native = UUID(path.stem)
        result: dict[str, JsonValue] = {"runtime": runtime, "session": str(native), "project_id": binding.project,
                                         "stopped_at": stopped, "action": binding.action}
        try:
            found = native_transcript(runtime, native, raw)
            if found is None or _signature(found) != row.get("signature"):
                if apply:
                    write_json(path, row | {"ready": False})
                return None
            if not apply or binding.action != "auto":
                return result
            payload = captured_snapshot(runtime, native, found, expected_cwd=Path(cwd))
            key = subject_key(runtime, native)
            payload.update(start_metadata(client, binding.project, runtime, key))
            captured = datetime.fromtimestamp(float(stopped), UTC).isoformat()
            payload.update(skill_capture(client, binding.project, runtime, key, captured))
            if _signature(found) != row.get("signature") or _bindings(client, Path(cwd)).get("attended") != binding:
                return None
            published = call(client, binding.project, "/shared-sessions", method="POST",
                             payload=payload | {"automatic_mode": "attended"})
        except (CollabError, OSError):
            # 坏转录或短时故障推迟到下次处理，后续候选仍可归档。
            if apply and binding.action == "auto":
                write_json(path, row | {"retry_after": time.time() + _RETRY_AFTER})
            raise
        write_json(path, row | {"published": True})
        return result | {"archive": published}


def finished(client: Client, cwd: Path, limit: int, *, apply: bool) -> dict[str, JsonValue]:
    """预览已结束的交互会话，或发布当前两级策略都授权的候选。"""
    if not 1 <= limit <= 100:
        raise CollabError("INVALID_LIMIT", "会话条数必须在 1 至 100 之间。")
    binding = _bindings(client, cwd).get("attended")
    if binding is None or binding.project is None or os.environ.get("WEORA_ARCHIVE_DISABLED") == "1":
        return {"items": []}
    candidates = _attended(client, binding, limit=limit)
    if not candidates:
        return {"items": []}
    action = binding.action
    if action == "auto":
        action = _team_action(call(client, None, _API), "attended")
        if apply and action != "auto":
            raise CollabError("KNOWLEDGE_ARCHIVE_AUTOMATIC_DISABLED", "工作空间当前未允许此模式自动归档。")
        if action == "off":
            return {"items": []}
    items: list[JsonValue] = []
    for _, runtime, path in candidates:
        try:
            result = _apply_one(client, runtime, path, binding, apply=apply)
            if result is not None:
                items.append(result | {"action": action})
        except (OSError, ValueError, CollabError) as error:
            code = error.code if isinstance(error, CollabError) else "KNOWLEDGE_ARCHIVE_UNAVAILABLE"
            items.append({"session": path.stem, "runtime": runtime, "error": code})
    return {"items": items}


def drain(
    client: Client, cwd: Path, runtime: str, session: str, transcript: Path | None, *, apply: bool,
    stopped_at: float | None = None, expected_project: str | None = None,
) -> dict[str, JsonValue]:
    """无人值守启动器在进程退出后明确登记结束，避免在中途 Stop 分享。"""
    binding = unattended_binding(client, cwd)
    if expected_project is not None and (binding is None or binding.project != expected_project):
        raise CollabError("KNOWLEDGE_DRAIN_BINDING_CHANGED", "目录的归档项目已变化。")
    if binding is None or binding.project is None:
        return {"items": []}
    project = binding.project
    action = binding.action
    if action == "auto":
        action = _team_action(call(client, None, _API), "unattended")
    if action == "off":
        return {"items": []}
    planned: dict[str, JsonValue] = {"runtime": runtime, "session": session, "project_id": project, "action": action}
    if apply and action == "auto":
        native = UUID(session)
        payload = captured_snapshot(runtime, native, transcript, expected_cwd=cwd)
        if stopped_at is not None:
            payload["captured_at"] = datetime.fromtimestamp(stopped_at, UTC).isoformat()
        key = subject_key(runtime, native)
        payload.update(start_metadata(client, project, runtime, key))
        payload.update(skill_capture(client, project, runtime, key, str(payload["captured_at"])))
        if _bindings(client, cwd).get("unattended") != binding:
            raise CollabError("KNOWLEDGE_ARCHIVE_BINDING_CHANGED", "归档期间目录绑定已变化，请重试。")
        published = call(client, project, "/shared-sessions", method="POST",
                         payload=payload | {"automatic_mode": "unattended"})
        planned["archive"] = published
    return {"items": [planned]}
