"""根会话摘要只用于同一成员、工作空间与项目中的关联。"""

import hashlib
from uuid import UUID


def score_source_key(runtime: str, session: UUID) -> str:
    return hashlib.sha256(f"weora-score-subject:{runtime}:{session}".encode()).hexdigest()


def learning_session_key(source: dict[str, str]) -> str | None:
    if source.get("type") != "session":
        return None
    runtime, _, value = source.get("ref", "").partition(":")
    if runtime not in ("codex", "claude-code"):
        return None
    try:
        return score_source_key(runtime, UUID(value))
    except ValueError:
        return None
