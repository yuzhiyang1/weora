"""保留手工反馈的原作用域和发生顺序，网络恢复后重发。"""

import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from uuid import UUID, uuid4

from weora_cli.collab_client import Client, CollabError, as_object, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_settings import session_state_root


@dataclass(frozen=True, slots=True)
class Feedback:
    entry: str
    project: str | None
    session: str
    positive: bool
    operation: str
    occurred_at: str

    def receipt(self) -> dict[str, JsonValue]:
        return {"entry_id": self.entry, "project_id": self.project, "operation_id": self.operation}

    def payload(self) -> dict[str, JsonValue]:
        return {
            "session": self.session, "positive": self.positive, "operation_id": self.operation,
            "occurred_at": self.occurred_at,
        }


def _directory(client: Client) -> Path:
    return session_state_root(client) / "feedback-outbox"


def has_pending_feedback(client: Client) -> bool:
    return next(_directory(client).glob("*.json"), None) is not None


def _read(path: Path) -> Feedback | None:
    try:
        row = as_object(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    entry, project, session = row.get("entry_id"), row.get("project_id"), row.get("session")
    positive, operation, occurred_at = row.get("positive"), row.get("operation_id"), row.get("occurred_at")
    if (
        not isinstance(entry, str) or (project is not None and not isinstance(project, str))
        or not isinstance(session, str) or not isinstance(positive, bool)
        or not isinstance(operation, str) or not isinstance(occurred_at, str)
    ):
        raise ValueError("反馈队列格式无效。")
    clean_entry = str(UUID(entry))
    clean_project = str(UUID(project)) if project is not None else None
    timestamp = datetime.fromisoformat(occurred_at)
    if timestamp.tzinfo is None:
        raise ValueError("反馈发生时间缺少时区。")
    return Feedback(
        clean_entry, clean_project, session, positive, str(UUID(operation)), timestamp.astimezone(UTC).isoformat(),
    )


def _settled(path: Path, feedback: Feedback, *, previous: Feedback | None = None) -> None:
    # 网络返回时可能已记录同一根会话的新反馈，只清理这一次发送的快照。
    with suppress(OSError, ValueError, CollabError), lock_file(path):
        if _read(path) == feedback:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                write_json(path, previous.receipt() | previous.payload())


def _send(client: Client, feedback: Feedback) -> dict[str, JsonValue]:
    try:
        return call(
            client, feedback.project, f"/{feedback.entry}/feedback", method="POST", payload=feedback.payload(),
        )
    except (ValueError, HTTPException) as error:
        raise CollabError("KNOWLEDGE_FEEDBACK_ACK", "无法确认平台反馈回执。") from error


def _retryable(error: CollabError) -> bool:
    # 回执无法确认时必须保留原操作。代理或应用返回的业务码可能掩盖 HTTP 状态。
    return (
        (error.status is not None and (error.status == 429 or error.status >= 500))
        or error.code in {"WEORA_UNREACHABLE", "HTTP_429", "INVALID_RESPONSE", "KNOWLEDGE_FEEDBACK_ACK"}
        or error.code.startswith("HTTP_5")
    )


def submit_feedback(
    client: Client, project: str | None, entry: str, session: str, *, positive: bool, preview: bool,
) -> dict[str, JsonValue]:
    session = session.strip()
    if not 1 <= len(session) <= 256 or "\x00" in session:
        raise CollabError("KNOWLEDGE_FEEDBACK_SESSION", "请指定有效的根会话标识。")
    if preview:
        return {"preview": True, "entry_id": entry, "project_id": project, "positive": positive}
    key = hashlib.sha256(json.dumps([project, entry, session], ensure_ascii=False).encode()).hexdigest()
    path = _directory(client) / f"{key}.json"
    try:
        with lock_file(path):
            previous = _read(path)
            feedback = Feedback(entry, project, session, positive, str(uuid4()), datetime.now(UTC).isoformat())
            write_json(path, feedback.receipt() | feedback.payload())
    except (OSError, ValueError, CollabError) as error:
        raise CollabError("KNOWLEDGE_FEEDBACK_QUEUE", "无法保存本地反馈，请检查队列或重试。") from error
    try:
        result = _send(client, feedback)
    except CollabError as error:
        if _retryable(error):
            return feedback.receipt() | {"queued": True}
        _settled(path, feedback, previous=previous)
        raise
    _settled(path, feedback)
    return result


def pending_for_recall(client: Client, project: str | None, *, exact_scope: bool = False) -> list[JsonValue]:
    """只读取当前设备和范围里的待送票，按时间选取本次检索可携带的记录。"""
    pending: list[Feedback] = []
    scopes = {project} if exact_scope else {None, project}
    for path in _directory(client).glob("*.json"):
        with suppress(OSError, ValueError, CollabError):
            if (item := _read(path)) is not None and item.project in scopes:
                pending.append(item)
    pending.sort(key=lambda item: (item.occurred_at, item.operation), reverse=True)
    return [item.receipt() | item.payload() for item in pending[:100]]


def sync_feedback(
    client: Client, project: str | None, *, for_search: bool = False, exact_scope: bool = False,
) -> tuple[list[JsonValue], list[JsonValue]]:
    sent: list[JsonValue] = []
    failures: list[JsonValue] = []
    for path in sorted(_directory(client).glob("*.json")):
        try:
            feedback = _read(path)
        except (OSError, ValueError, CollabError):
            if not for_search:
                failures.append({"record": path.stem, "code": "KNOWLEDGE_FEEDBACK_QUEUE_INVALID"})
            continue
        if feedback is None:
            continue
        if exact_scope or (not for_search and project is not None):
            if feedback.project != project:
                continue
        elif for_search and feedback.project not in {None, project}:
            continue
        try:
            _send(client, feedback)
        except CollabError as error:
            failures.append(feedback.receipt() | {"code": error.code})
            if for_search and _retryable(error):
                break
        else:
            _settled(path, feedback)
            sent.append(feedback.receipt())
    return sent, failures
