"""为本机脱敏会话生成可与团队标签关联的证据指纹。"""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from weora_cli.collab_client import Client, CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_session_origin import start_metadata, subject_key
from weora_cli.knowledge_transcript import captured_snapshot


def current_session(runtime: str | None) -> tuple[str, str]:
    """只在宿主环境能确定唯一根会话时自动补齐来源。"""
    claude = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not claude and not os.environ.get("CODEBUDDY_SESSION_ID"):
        claude = os.environ.get("CLAUDE_SESSION_ID")
    candidates = [("claude-code", claude), ("codex", os.environ.get("CODEX_SESSION_ID"))]
    matches: dict[str, str] = {}
    for name, value in candidates:
        if value and (runtime is None or runtime == name):
            try:
                matches.setdefault(str(UUID(value)), name)
            except ValueError:
                continue
    if len(matches) != 1:
        raise CollabError("KNOWLEDGE_SCORER_SESSION", "无法确定唯一根会话，请显式指定 --session。")
    session, name = next(iter(matches.items()))
    return name, session


def evidence_digest(subject_key: str, turns: list[JsonValue]) -> str:
    body = json.dumps(turns, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(f"weora-score-evidence:{subject_key}\0".encode() + body).hexdigest()


def local_evidence(
    runtime: str | None, session: str, transcript: Path | None, *, expected_cwd: Path | None = None,
    with_recall: bool = False,
) -> tuple[dict[str, JsonValue], list[JsonValue]]:
    """沿用归档脱敏规则。标识和指纹用于上传，转录单独留在调用方。"""
    native = UUID(session)
    if runtime is None:
        matches: list[tuple[str, dict[str, JsonValue]]] = []
        for candidate in ("codex", "claude-code"):
            try:
                matches.append((candidate, captured_snapshot(candidate, native, transcript, expected_cwd=expected_cwd)))
            except CollabError as error:
                if error.code != "KNOWLEDGE_TRANSCRIPT_SCOPE":
                    raise
        if not matches:
            raise CollabError("KNOWLEDGE_TRANSCRIPT_SCOPE", "找不到 Codex 或 Claude Code 的根会话转录。")
        if len(matches) > 1:
            raise CollabError("KNOWLEDGE_TRANSCRIPT_AMBIGUOUS", "两个宿主均有此会话，请用 --runtime 指定。")
        runtime, snapshot = matches[0]
    else:
        snapshot = captured_snapshot(runtime, native, transcript, expected_cwd=expected_cwd)
    turns = snapshot.get("turns")
    if not isinstance(turns, list) or not turns:
        raise CollabError("KNOWLEDGE_SCORER_EVIDENCE", "没有可评分的本机脱敏证据。")
    key = subject_key(runtime, native)
    metadata: dict[str, JsonValue] = {"runtime": runtime, "subject_key": key, "digest": evidence_digest(key, turns)}
    if with_recall:
        metadata.update({
            "recall_session_key": hashlib.sha256(str(native).encode()).hexdigest(),
            "captured_at": snapshot["captured_at"],
        })
    return metadata, turns


def register_evidence(
    client: Client, project: str | None, metadata: dict[str, JsonValue],
) -> tuple[str, int]:
    origin = start_metadata(client, project, metadata.get("runtime"), metadata.get("subject_key"))
    reply = call(client, project, "/session-scoring/local-subjects", method="POST", payload=metadata | origin)
    identifier, revision = reply.get("id"), reply.get("revision")
    if (
        not isinstance(identifier, str) or type(revision) is not int or revision < 1
        or reply.get("digest") != metadata.get("digest") or reply.get("evidence") != "local"
        or (metadata.get("run_mode") is not None and reply.get("run_mode") != metadata.get("run_mode"))
    ):
        raise CollabError("KNOWLEDGE_SCORER_EVIDENCE", "本机评分证据的登记收据无效。")
    try:
        return str(UUID(identifier)), revision
    except ValueError as error:
        raise CollabError("KNOWLEDGE_SCORER_EVIDENCE", "本机评分证据的登记标识无效。") from error
