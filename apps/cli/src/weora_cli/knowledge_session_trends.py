"""把原生时间与请求证据变为团队周报需要的根会话累计量。"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from weora_cli.commands import JsonValue
from weora_cli.knowledge_request_cost import request_cost

_IDLE_MS = 300_000


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def timestamp(value: JsonValue) -> datetime | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(UTC) if parsed.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def _spans(value: JsonValue) -> list[tuple[int, int]]:
    if not isinstance(value, list):
        return []
    return [
        (start, end) for pair in value if isinstance(pair, list) and len(pair) == 2
        and type(start := pair[0]) is int and type(end := pair[1]) is int and 0 <= start <= end
    ]


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start - merged[-1][1] <= _IDLE_MS:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _encoded(spans: list[tuple[int, int]]) -> list[JsonValue]:
    return [[start, end] for start, end in spans]


@dataclass(slots=True)
class NativeTrend:
    events: set[int] = field(default_factory=set)
    requests: dict[str, JsonValue] = field(default_factory=dict)
    has_error: bool = False

    def event(self, at: datetime | None, *, error: bool = False) -> None:
        self.has_error |= error
        if at is not None:
            self.events.add(int(at.timestamp() * 1000))

    def price(
        self, key: str, at: datetime | None, model: JsonValue, tokens: dict[str, int],
        *, tier: JsonValue = None, hour: int = 0, reported: JsonValue = None,
    ) -> None:
        if at is None:
            return
        identifier = hashlib.sha256(key.encode()).hexdigest()
        if identifier not in self.requests:
            amount = request_cost(model, tokens, tier=tier, hour_write=hour, reported=reported)
            if amount is not None:
                self.requests[identifier] = {"day": at.date().isoformat(), "cost_micros": amount}

    def view(self) -> dict[str, JsonValue]:
        spans = _merge_spans((stamp, stamp) for stamp in self.events)
        return {"spans": _encoded(spans), "has_error": self.has_error}


def note_time(row: dict[str, JsonValue], now: float) -> None:
    stamp = int(now * 1000)
    spans = _spans(row.get("hook_spans"))
    row["hook_spans"] = _encoded(_merge_spans((*spans, (stamp, stamp))))


def remember_requests(row: dict[str, JsonValue], samples: dict[str, JsonValue], now: datetime) -> None:
    earliest = (now.date() - timedelta(days=30)).isoformat()
    existing = _object(row.get("priced_requests"))
    retained = {
        key: sample for key, sample in existing.items()
        if isinstance(day := _object(sample).get("day"), str) and day >= earliest
    }
    for key, sample in samples.items():
        if isinstance(day := _object(sample).get("day"), str) and day >= earliest:
            retained.setdefault(key, sample)
    row["priced_requests"] = retained


def trend_payload(
    row: dict[str, JsonValue], rollouts: list[dict[str, JsonValue]], now: datetime,
) -> dict[str, JsonValue]:
    first = row.get("first_stop_day")
    if not isinstance(first, str):
        return {}
    observations = [_object(snapshot.get("trend")) for snapshot in rollouts]
    spans = _spans(row.get("hook_spans"))
    for observation in observations:
        spans.extend(_spans(observation.get("spans")))
    # 同一时段可能同时出现在 Hook 和多个转录中，只对合并后的活动区间计时。
    duration = sum(end - start for start, end in _merge_spans(spans))
    dates: dict[str, dict[str, int]] = {}
    earliest = (now.date() - timedelta(days=30)).isoformat()
    today = now.date().isoformat()
    for raw in _object(row.get("priced_requests")).values():
        sample = _object(raw)
        day, cost = sample.get("day"), sample.get("cost_micros")
        if not isinstance(day, str) or not earliest <= day <= today or type(cost) is not int:
            continue
        bucket = dates.setdefault(day, {"priced_requests": 0, "cost_micros": 0})
        bucket["priced_requests"] += 1
        bucket["cost_micros"] += cost
    daily: list[JsonValue] = [
        {"day": day, "priced_requests": bucket["priced_requests"], "cost_micros": bucket["cost_micros"]}
        for day, bucket in sorted(dates.items())
    ]
    return {"trend": {
        "first_stop_day": first, "active_ms": duration,
        "has_error": row.get("trend_error") is True or any(item.get("has_error") is True for item in observations),
        "request_days": daily,
    }}
