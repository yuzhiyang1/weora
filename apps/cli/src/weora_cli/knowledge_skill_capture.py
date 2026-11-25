"""在根会话中记录已分发的 Skill，发布时按作用域拿取快照。"""

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Error
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, as_object, lock_file, read_config, write_json
from weora_cli.collab_native import profile
from weora_cli.commands import JsonValue
from weora_cli.knowledge_session_origin import subject_key
from weora_cli.knowledge_settings import session_state_root
from weora_cli.skill_delivery_capture import delivered_at
from weora_cli.skill_local import database, state_root

_MAX_SKILLS = 256
_MAX_CAPTURE_BYTES = 2_000_000


def _record_path(client: Client, runtime: str, key: str) -> Path:
    return session_state_root(client) / "skill-captures" / runtime / profile(runtime) / f"{key}.json"


def _read(path: Path) -> dict[str, JsonValue]:
    try:
        if path.stat().st_size <= _MAX_CAPTURE_BYTES:
            return as_object(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError, CollabError):
        pass
    return {}


def observe_skill_capture(client: Client, cwd: Path, runtime: str, event: Mapping[str, JsonValue]) -> None:
    session = event.get("session_id")
    if (
        runtime not in ("codex", "claude-code") or not isinstance(session, str) or event.get("agent_id")
        or event.get("hook_event_name") not in ("SessionStart", "UserPromptSubmit", "Stop")
        or event.get("stop_hook_active") is True or os.environ.get("WEORA_SCORING_DISABLED") == "1"
    ):
        return
    config = read_config()
    if config.get("workspace") != client.workspace or config.get("base") != client.base.rstrip("/"):
        return
    if not (state_root() / "skills.sqlite3").is_file():
        return
    native = UUID(session)
    path = _record_path(client, runtime, subject_key(runtime, native))
    role = "claude" if runtime == "claude-code" else runtime
    observed = datetime.now(UTC).isoformat()
    with suppress(OSError, ValueError, Error, CollabError), database() as db:
        capture = delivered_at(db, role, str(native), cwd.resolve(), observed)
        if capture is None:
            return
        with lock_file(path):
            saved = _read(path)
            previous = saved.get("items")
            rows = previous if isinstance(previous, list) else []
            observations = [
                item for item in [*rows, *capture["items"]] if isinstance(item, dict)
                and {"generation", "observed_at"} <= item.keys()
            ]
            first_seen: dict[str, dict[str, JsonValue]] = {}
            # 同一安装代次在后续 Hook 也会被看到。保留首次观察，早先的会话截止点仍然可用。
            for item in sorted(observations, key=lambda row: (str(row["observed_at"]), str(row["generation"]))):
                first_seen.setdefault(str(item["generation"]), item)
            selected = list(first_seen.values())
            scopes = saved.get("scopes")
            known = scopes if isinstance(scopes, list) else []
            scopes_at = {
                row["project"]: str(row["observed_at"])
                for row in known if isinstance(row, dict) and ("project" in row and "observed_at" in row)
                and (row["project"] is None or isinstance(row["project"], str))
            }
            for scope in capture["scopes"]:
                scopes_at[scope] = min(scopes_at.get(scope, observed), observed)
            write_json(path, {
                "items": list(selected[-_MAX_SKILLS:]),
                "scopes": [{"project": scope, "observed_at": at} for scope, at in scopes_at.items()],
                "usage": capture["usage"] or saved.get("usage"),
            })


def skill_capture(
    client: Client, project: str | None, runtime: str, key: str, captured_at: str,
) -> dict[str, JsonValue]:
    record = _read(_record_path(client, runtime, key))
    scopes = record.get("scopes")
    allowed = (None, project)
    if not isinstance(scopes, list) or not any(
        isinstance(row, dict) and row.get("project") in allowed and str(row.get("observed_at")) <= captured_at
        for row in scopes
    ):
        return {}
    rows = record.get("items")
    installations: list[JsonValue] = []
    if isinstance(rows, list):
        for row in rows:
            if (
                isinstance(row, dict) and row.get("project") in allowed and str(row.get("observed_at")) <= captured_at
                and {"generation", "observed_at", "description"} <= row.keys()
            ):
                installations.append({field: row[field] for field in ("generation", "observed_at", "description")})
    usage = record.get("usage")
    return {"skill_capture": {"installations": installations, **(usage if isinstance(usage, dict) else {})}}
