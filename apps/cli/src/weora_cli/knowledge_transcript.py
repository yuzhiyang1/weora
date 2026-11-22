"""生成可核对的会话归档预览，并发布同一份本地快照。"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, as_object, write_json
from weora_cli.collab_native import _home, native_transcript
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_session_origin import start_metadata, subject_key
from weora_cli.knowledge_settings import session_state_root
from weora_cli.knowledge_skill_capture import skill_capture
from weora_cli.knowledge_transcript_text import native_text, scrub_text

_CONFIRM = re.compile(r"^[0-9a-f]{64}$")
_VALID_FOR = 24 * 3600
_MAX_BYTES = 512_000
_MAX_TURNS = 500
_MAX_TURN = 12_000


def resolve_transcript(runtime: str, session: UUID, chosen: Path | None) -> Path:
    try:
        if chosen is not None:
            found = native_transcript(runtime, session, str(chosen.resolve(strict=True)))
            if found is not None:
                return found
        else:
            roots = ("sessions", "archived_sessions") if runtime == "codex" else ("projects",)
            matches: set[Path] = set()
            for root in roots:
                for candidate in (_home(runtime) / root).rglob(f"*{session}.jsonl"):
                    path = native_transcript(runtime, session, str(candidate.absolute()))
                    if path is not None:
                        matches.add(path)
                        if len(matches) > 1:
                            raise CollabError(
                                "KNOWLEDGE_TRANSCRIPT_AMBIGUOUS", "找到多个转录，请用 --transcript 指定。",
                            )
            if len(matches) == 1:
                return matches.pop()
    except OSError:
        pass
    raise CollabError("KNOWLEDGE_TRANSCRIPT_SCOPE", "找不到所选客户端会话目录中的根会话转录。")


def _checksum(device: str, project: str | None, payload: dict[str, JsonValue]) -> str:
    checked = json.dumps([device, project, payload], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(checked.encode()).hexdigest()


def _snapshot(
    runtime: str, session: UUID, transcript: Path | None, title: str | None,
    expected_cwd: Path | None,
) -> dict[str, JsonValue]:
    source = resolve_transcript(runtime, session, transcript)
    before = source.stat() if expected_cwd is not None else None
    cwd, turns = native_text(source, runtime, session)
    if expected_cwd is not None:
        # 自动处理只接受与目录绑定一致、读取期间没有继续写入的原生记录。
        after = source.stat()
        if not cwd or not Path(cwd).is_absolute() or Path(cwd).resolve() != expected_cwd.resolve():
            raise CollabError("KNOWLEDGE_ARCHIVE_DIRECTORY", "转录的工作目录与自动归档目录不一致。")
        if before is None or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise CollabError("KNOWLEDGE_ARCHIVE_ACTIVE", "会话转录仍在变化，请稍后重试。")
    raw_title = title or next((text for role, text in turns if role == "user"), "已分享会话")
    safely_named = scrub_text(cwd, [("user", raw_title)])
    name = str(safely_named[0]["text"]) if safely_named else "已分享会话"
    name = " ".join(name.split())[:160]
    cleaned = scrub_text(cwd, turns)
    if not cleaned:
        raise CollabError("KNOWLEDGE_TRANSCRIPT_SCOPE", "没有可分享的会话正文。")
    if len(cleaned) > _MAX_TURNS or any(len(str(row["text"])) > _MAX_TURN for row in cleaned):
        raise CollabError("KNOWLEDGE_TRANSCRIPT_LIMIT", "会话超过归档条数或单条长度限制。")
    archive = json.dumps([name, cleaned], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(archive) > _MAX_BYTES:
        raise CollabError("KNOWLEDGE_TRANSCRIPT_LIMIT", "会话超过归档大小限制。")
    return {"runtime": runtime, "session": str(session), "title": name, "turns": list(cleaned)}


def captured_snapshot(
    runtime: str, session: UUID, transcript: Path | None, title: str | None = None, *, expected_cwd: Path | None = None,
) -> dict[str, JsonValue]:
    """在读取前固定采集时点，后续判定只可使用更早的召回。"""
    started = datetime.now(UTC)
    return _snapshot(runtime, session, transcript, title, expected_cwd) | {"captured_at": started.isoformat()}


def share_session(
    client: Client, project: str | None, runtime: str, session: str, transcript: Path | None,
    title: str | None, confirmation: str | None,
) -> dict[str, JsonValue]:
    directory = session_state_root(client)
    pending = directory / "transcript-previews"
    now = time.time()
    if confirmation is None:
        native = UUID(session)
        payload = captured_snapshot(runtime, native, transcript, title)
        key = subject_key(runtime, native)
        payload.update(start_metadata(client, project, runtime, key))
        payload.update(skill_capture(client, project, runtime, key, str(payload["captured_at"])))
        digest = _checksum(directory.name, project, payload)
        try:
            write_json(pending / f"{digest}.json", {"created_at": now, "project": project, "payload": payload})
        except OSError as exc:
            raise CollabError("KNOWLEDGE_TRANSCRIPT_PREVIEW_FAILED", "无法保存本机归档预览。") from exc
        preview = {key: value for key, value in payload.items() if key not in ("session", "skill_capture")}
        delivery = payload.get("skill_capture")
        installations = delivery.get("installations") if isinstance(delivery, dict) else None
        if isinstance(installations, list):
            preview["skill_count"] = len(installations)
        return {
            "preview": True, "workspace_id": client.workspace, "project_id": project, "confirm": digest, **preview,
        }
    if not _CONFIRM.fullmatch(confirmation) or transcript is not None or title is not None:
        raise CollabError("KNOWLEDGE_TRANSCRIPT_CONFIRM", "确认时只提交预览码、来源、会话和已核对的项目范围。")
    try:
        stored = as_object(json.loads((pending / f"{confirmation}.json").read_text(encoding="utf-8")))
        payload = as_object(stored.get("payload"))
        created = stored.get("created_at")
        valid = (
            isinstance(created, int | float) and not isinstance(created, bool) and 0 <= now - created <= _VALID_FOR
            and runtime == payload.get("runtime") and session == payload.get("session")
            and project == stored.get("project") and _checksum(directory.name, project, payload) == confirmation
        )
    except (OSError, ValueError, RecursionError, CollabError):
        valid = False
        payload = {}
    if not valid:
        raise CollabError("KNOWLEDGE_TRANSCRIPT_CONFIRM", "找不到匹配且未过期的预览，请重新核对归档。")
    return call(client, project, "/shared-sessions", method="POST", payload=payload)
