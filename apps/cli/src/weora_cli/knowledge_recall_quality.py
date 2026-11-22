"""为 Stop 提醒保留最近 24 小时的命中质量，原始问题留在调用方。"""

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from weora_cli.collab_client import Client, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_settings import session_state_root

_WINDOW = 24 * 60 * 60
_WEAK_SCORE = 5.0


@dataclass(frozen=True, slots=True)
class RecallQuality:
    hits: int = 0
    misses: int = 0
    top_score: float = 0.0
    last_run: str = ""

    def adjustment(self) -> tuple[int, str]:
        if self.misses and self.hits == 0:
            return 20, "知识召回均无结果"
        if self.hits and self.top_score < _WEAK_SCORE:
            return 10, "知识召回只有弱匹配"
        return 0, ""


def _file(client: Client, session: str) -> Path:
    filename = hashlib.sha256(session.encode()).hexdigest() + ".json"
    return session_state_root(client) / "recall-quality" / filename


def _load(path: Path, now: int) -> RecallQuality:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last = data["updated_at"]
        if type(last) is int and 0 <= now - last <= _WINDOW:
            hits, misses, score, run = data["hits"], data["misses"], data["top_score"], data["last_run"]
            if (
                type(hits) is int and hits >= 0 and type(misses) is int and misses >= 0
                and isinstance(score, int | float) and math.isfinite(score) and isinstance(run, str)
            ):
                return RecallQuality(hits, misses, score, run)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        pass
    return RecallQuality()


def recall_quality(client: Client, session: str) -> RecallQuality:
    return _load(_file(client, session), int(time.time()))


def record_quality(client: Client, session: str, result: dict[str, JsonValue]) -> None:
    """只取成功召回的计数及首位分数，缓存故障不影响返回结果。"""
    run_id, items = result.get("run_id"), result.get("items")
    if not isinstance(run_id, str) or not isinstance(items, list):
        return
    try:
        score = items[0].get("score") if items and isinstance(items[0], dict) else None
        top_score = float(score) if isinstance(score, int | float) and math.isfinite(score) else 0.0
        path = _file(client, session)
        now = int(time.time())
        with lock_file(path):
            quality = _load(path, now)
            if quality.last_run == run_id:
                return
            write_json(path, {
                "updated_at": now, "hits": quality.hits + int(bool(items)),
                "misses": quality.misses + int(not items), "top_score": max(quality.top_score, top_score),
                "last_run": run_id,
            })
    except (OSError, OverflowError):
        return
