"""在原设备冻结后台评分的脱敏证据，服务端只登记指纹。"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from weora_cli.collab_client import Client, CollabError, as_object, lock_file, write_json
from weora_cli.collab_native import profile
from weora_cli.commands import JsonValue
from weora_cli.knowledge_local_scoring import evidence_digest, local_evidence, register_evidence
from weora_cli.knowledge_settings import session_state_root
from weora_cli.knowledge_skill_capture import skill_capture
from weora_cli.knowledge_transcript import resolve_transcript

_RUNTIMES = ("codex", "claude-code")
_IDLE = 6 * 3600
_RETENTION = 14 * 86400
_RETRY = 30 * 60
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CACHE_FILE = re.compile(r"[0-9a-f-]{36}\.[1-9][0-9]*\.[0-9a-f]{64}\.(?:turns|ref)\.json")
type ProjectForDirectory = Callable[[Path], str | None]
type EvidenceKey = tuple[str, int, str]


def _scheduled(client: Client, mode: str, runtime: str) -> Path:
    return session_state_root(client) / "scoring-candidates" / mode / runtime / profile(runtime)


def _snapshots(client: Client, project: str, mode: str) -> Path:
    return session_state_root(client) / "scoring-evidence" / str(UUID(project)) / mode


def _read(path: Path) -> dict[str, JsonValue]:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}


def _signature(path: Path) -> list[JsonValue]:
    info = path.stat()
    return [info.st_mtime_ns, info.st_size]


def note_finished(
    client: Client, project: str, cwd: Path, mode: str, runtime: str, session: str, transcript: Path | None,
    *, stopped_at: float | None = None,
) -> None:
    """只记录原生文件的位置，不在宿主 Hook 中读取或上传正文。"""
    native = UUID(session)
    found = resolve_transcript(runtime, native, transcript)
    candidate = _scheduled(client, mode, runtime) / f"{native}.json"
    signature = _signature(found)
    with lock_file(candidate):
        previous = _read(candidate)
        if (
            previous.get("captured") is True and previous.get("signature") == signature
            and previous.get("project") == project
        ):
            return
        write_json(candidate, {
            "stopped": time.time() if stopped_at is None else stopped_at, "cwd": str(cwd.resolve()),
            "project": project, "source": str(found), "signature": signature,
        })


def note_resumed(client: Client, runtime: str, session: str) -> None:
    candidate = _scheduled(client, "attended", runtime) / f"{UUID(session)}.json"
    if candidate.is_file():
        with lock_file(candidate):
            row = _read(candidate)
            if row and row.get("captured") is not True:
                write_json(candidate, row | {"resumed": True})


def _is_ready(row: dict[str, JsonValue], project: str, mode: str, now: float) -> bool:
    stopped, retry = row.get("stopped"), row.get("retry_after")
    idle = _IDLE if mode == "attended" else 0
    return (
        row.get("project") == project and row.get("captured") is not True and row.get("resumed") is not True
        and isinstance(stopped, int | float) and not isinstance(stopped, bool)
        and now - _RETENTION <= stopped <= now - idle
        and not (isinstance(retry, int | float) and now < retry)
    )


def _freeze(
    client: Client, project: str, mode: str, runtime: str, candidate: Path, project_for: ProjectForDirectory,
) -> None:
    with lock_file(candidate):
        row = _read(candidate)
        cwd, raw = row.get("cwd"), row.get("source")
        if not _is_ready(row, project, mode, time.time()) or not isinstance(cwd, str) or not isinstance(raw, str):
            return
        directory = Path(cwd)
        if project_for(directory) != project:
            return
        try:
            path = Path(raw)
            if _signature(path) != row.get("signature"):
                write_json(candidate, row | {"resumed": True})
                return
            # 闲置结束后才读取交互转录，归因仍以该转录最后一次 Stop 为界。
            captured_at = datetime.fromtimestamp(float(str(row["stopped"])), UTC)
            metadata, turns = local_evidence(runtime, candidate.stem, path, expected_cwd=directory)
            if _signature(path) != row.get("signature") or project_for(directory) != project:
                return
            # 根会话只以召回日志使用的摘要键出现在后台登记中。
            captured = captured_at.isoformat()
            delivery = skill_capture(client, project, runtime, str(metadata["subject_key"]), captured)
            identifier, revision = register_evidence(client, project, metadata | delivery | {
                "run_mode": mode, "captured_at": captured,
                "recall_session_key": hashlib.sha256(candidate.stem.encode()).hexdigest(),
            })
            digest = str(metadata["digest"])
            stored = _snapshots(client, project, mode) / f"{identifier}.{revision}.{digest}"
            # 先完整写入脱敏正文，再写可用于领取的短索引，避免宣布不完整的证据。
            write_json(stored.with_name(stored.name + ".turns.json"), {"turns": turns})
            write_json(stored.with_name(stored.name + ".ref.json"), {
                "id": identifier, "revision": revision, "digest": digest, "subject_key": metadata["subject_key"],
                "cwd": cwd, "prepared_at": time.time(),
            })
            write_json(candidate, row | {"captured": True})
        except (CollabError, OSError, ValueError, RecursionError):
            # 单项失败不会阻塞其他会话，下轮仍使用原候选重新登记。
            write_json(candidate, row | {"retry_after": time.time() + _RETRY})


def freeze_ready(client: Client, project: str, mode: str, project_for: ProjectForDirectory) -> None:
    candidates: list[tuple[float, str, Path]] = []
    now = time.time()
    for runtime in _RUNTIMES:
        for candidate in _scheduled(client, mode, runtime).glob("*.json"):
            with suppress(CollabError, OSError, ValueError, RecursionError):
                row = _read(candidate)
                stopped, cwd = row.get("stopped"), row.get("cwd")
                if (
                    isinstance(stopped, int | float) and isinstance(cwd, str)
                    and _is_ready(row, project, mode, now) and project_for(Path(cwd)) == project
                ):
                    candidates.append((stopped, runtime, candidate))
    for _, runtime, candidate in sorted(candidates)[:20]:
        with suppress(CollabError, OSError, ValueError, RecursionError):
            _freeze(client, project, mode, runtime, candidate, project_for)


@dataclass(frozen=True, slots=True)
class DeviceEvidence:
    key: EvidenceKey
    turns_path: Path
    subject_key: str
    cwd: Path

    def reference(self) -> dict[str, JsonValue]:
        identifier, revision, digest = self.key
        return {"id": identifier, "revision": revision, "digest": digest}

    def load(self) -> list[JsonValue]:
        try:
            if self.turns_path.stat().st_size <= 1_000_000:
                value = _read(self.turns_path).get("turns")
                if isinstance(value, list) and value and evidence_digest(self.subject_key, value) == self.key[2]:
                    return value
        except (OSError, ValueError, RecursionError):
            pass
        raise CollabError("KNOWLEDGE_SCORER_EVIDENCE", "本机脱敏快照缺失或指纹不一致。")


def device_evidence(
    client: Client, project: str, mode: str, project_for: ProjectForDirectory,
) -> dict[EvidenceKey, DeviceEvidence]:
    records: dict[EvidenceKey, DeviceEvidence] = {}
    cutoff = time.time() - _RETENTION
    folder = _snapshots(client, project, mode)
    for path in folder.glob("*.turns.json"):
        if not _CACHE_FILE.fullmatch(path.name):
            continue
        descriptor = path.with_name(path.name.removesuffix(".turns.json") + ".ref.json")
        with suppress(OSError):
            if path.stat().st_mtime < cutoff and not descriptor.exists():
                path.unlink()
    for path in sorted(folder.glob("*.ref.json")):
        if not _CACHE_FILE.fullmatch(path.name):
            continue
        with suppress(CollabError, OSError, ValueError, RecursionError):
            body = path.with_name(path.name.removesuffix(".ref.json") + ".turns.json")
            row = _read(path)
            prepared = row.get("prepared_at")
            if not isinstance(prepared, int | float) or isinstance(prepared, bool):
                continue
            if prepared < cutoff:
                # 只清理由 CLI 创建且已过期的本地快照，原生转录留在原位置。
                path.unlink(missing_ok=True)
                body.unlink(missing_ok=True)
                continue
            identifier, revision, digest = row.get("id"), row.get("revision"), row.get("digest")
            cwd, source = row.get("cwd"), row.get("subject_key")
            if (
                isinstance(identifier, str) and type(revision) is int
                and isinstance(digest, str) and _DIGEST.fullmatch(digest)
                and isinstance(source, str) and _DIGEST.fullmatch(source) and isinstance(cwd, str)
                and body.is_file() and project_for(Path(cwd)) == project
            ):
                key = (str(UUID(identifier)), revision, digest)
                records[key] = DeviceEvidence(key, body, source, Path(cwd))
    return records
