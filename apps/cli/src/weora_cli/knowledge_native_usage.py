"""按原生转录口径计数，统计快照不包含正文、模型请求标识或路径。"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from weora_cli.collab_client import CollabError
from weora_cli.commands import JsonValue
from weora_cli.knowledge_request_cost import reported_micros
from weora_cli.knowledge_session_trends import NativeTrend, timestamp
from weora_cli.knowledge_transcript_text import _native_rows

_KEYS = ("input", "output", "cache_read", "cache_creation")
_INJECTED = (
    "<system-reminder>", "<task-notification>", "<environment_context>",
    "<local-command-caveat>", "<local-command-stdout>", "[Request interrupted by user",
)
_MAX_CHILD_BYTES = 50 * 1024 * 1024


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def count(value: JsonValue) -> int:
    return value if type(value) is int and value >= 0 else 0


def human_prompt(value: JsonValue) -> bool:
    if isinstance(value, str):
        text = value.strip()
        return bool(text) and not text.startswith(_INJECTED)
    if isinstance(value, list):
        return any(
            human_prompt(segment) for part in value
            if isinstance(part, dict) and part.get("type") in ("text", "input_text")
            and isinstance(segment := part.get("text"), str)
        )
    return False


def _tokens(value: JsonValue, *, codex: bool) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    read = "cached_input_tokens" if codex else "cache_read_input_tokens"
    write = "cache_write_input_tokens" if codex else "cache_creation_input_tokens"
    fields = ("input_tokens", "output_tokens", read, write)
    raw = [value.get(key) for key in fields]
    if not any(type(number) is int and number >= 0 for number in raw):
        return None
    observed = [count(number) for number in raw]
    if codex:
        # Codex 输入包含缓存读写，输出已经包含推理用量。
        observed[0] = max(0, observed[0] - observed[2] - observed[3])
    return dict(zip(_KEYS, observed, strict=True))


@dataclass(slots=True)
class NativeUsage:
    prompts: int = 0
    codex_message_prompts: int = 0
    thread_tokens: dict[str, int] | None = None
    transcript_tokens: dict[str, int] | None = None
    requests: set[str] = field(default_factory=set)
    trend: NativeTrend = field(default_factory=NativeTrend)
    model: JsonValue = None
    tier: JsonValue = None
    per_response: bool = False
    legacy_total: str | None = None

    def codex(self, row: dict[str, JsonValue], session: str, rollout: str) -> None:
        kind = row.get("type")
        payload = _object(row.get("payload"))
        at = timestamp(row.get("timestamp"))
        event = payload.get("type")
        if kind in ("turn_context", "response_item", "token_usage_record") or (
            kind == "event_msg" and event in ("user_message", "task_started", "task_complete", "turn_aborted",
                                               "exec_command_end", "error", "turn_failed")
        ):
            self.trend.event(at, error=event in ("error", "turn_failed") or row.get("status") == "error")
        if kind == "turn_context" and isinstance(payload.get("model"), str):
            self.model = payload["model"]
        if event == "thread_settings_applied":
            self.tier = _object(payload.get("thread_settings")).get("service_tier")
        if kind == "token_usage_record" and payload.get("thread_id") in (None, session):
            self.per_response = True
            totals = _tokens(payload.get("thread_token_usage"), codex=True)
            if totals is not None:
                self.thread_tokens = totals
            key = payload.get("response_id")
            sample = _tokens(payload.get("usage"), codex=True)
            if isinstance(key, str) and sample is not None:
                self.trend.price("codex:" + key, at, payload.get("model") or self.model, sample, tier=self.tier)
        elif kind == "event_msg":
            if payload.get("type") == "user_message" and human_prompt(payload.get("message")):
                self.prompts += 1
            if event == "token_count":
                info = _object(payload.get("info"))
                totals = _tokens(info.get("total_token_usage"), codex=True)
                if totals is not None:
                    self.transcript_tokens = totals
                    snapshot_key = json.dumps(totals, sort_keys=True)
                    request = _tokens(info.get("last_token_usage"), codex=True)
                    if not self.per_response and snapshot_key != self.legacy_total and request is not None:
                        self.trend.price(f"codex:{rollout}:{snapshot_key}", at, self.model, request, tier=self.tier)
                    self.legacy_total = snapshot_key
        elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            if human_prompt(payload.get("content")):
                self.codex_message_prompts += 1

    def _add(self, tokens: dict[str, int]) -> None:
        if self.transcript_tokens is None:
            self.transcript_tokens = tokens
        else:
            for key, value in tokens.items():
                self.transcript_tokens[key] += value

    def claude(self, row: dict[str, JsonValue], *, count_prompts: bool = True) -> None:
        kind, message = row.get("type"), _object(row.get("message"))
        at = timestamp(row.get("timestamp"))
        if count_prompts and kind in ("user", "assistant", "system"):
            self.trend.event(at, error=row.get("isApiErrorMessage") is True or row.get("status") == "error"
                             or row.get("subtype") == "api_error")
        if kind == "user" and count_prompts and row.get("isMeta") is not True and human_prompt(message.get("content")):
            self.prompts += 1
        if kind != "assistant":
            return
        message_id = message.get("id")
        identifier = message_id if isinstance(message_id, str) and message_id else row.get("requestId")
        usage = _object(message.get("usage"))
        tokens = _tokens(usage, codex=False)
        reported = row.get("costUSD")
        if (
            not isinstance(identifier, str) or not identifier or identifier in self.requests
            or (tokens is None and reported_micros(reported) is None)
        ):
            return
        # 一个回复写出多个内容块时，每块重复相同的用量，只累计一次。
        self.requests.add(identifier)
        if tokens is not None:
            self._add(tokens)
        hour = count(_object(usage.get("cache_creation")).get("ephemeral_1h_input_tokens"))
        self.trend.price("claude:" + identifier, at, message.get("model"), tokens or dict.fromkeys(_KEYS, 0),
                         tier=usage.get("speed"), hour=hour, reported=reported)
        iterations = usage.get("iterations")
        if isinstance(iterations, list):
            for index, item in enumerate(iterations):
                if (
                    isinstance(item, dict) and item.get("type") == "advisor_message"
                    and isinstance(item.get("model"), str) and (extra := _tokens(item, codex=False)) is not None
                ):
                    self._add(extra)
                    self.trend.price(f"claude:{identifier}:advisor:{index}", at, item["model"], extra,
                                     tier=usage.get("speed"), reported=item.get("costUSD"))

    def claude_children(self, transcript: Path) -> None:
        directory = transcript.with_suffix("") / "subagents"
        try:
            if not directory.exists() or directory.resolve() != directory:
                return
            remaining = _MAX_CHILD_BYTES
            for child in sorted(directory.glob("*.jsonl")):
                if child.resolve() != child or not child.is_file():
                    continue
                size = child.stat().st_size
                if size == 0:
                    continue
                remaining -= size
                if remaining < 0:
                    raise ValueError("child transcripts exceed the read budget")
                for row in _native_rows(child):
                    self.claude(row, count_prompts=False)
        except (OSError, ValueError, CollabError):
            # 子会话记录不完整时，本次仍可上报根会话轮次，既有 Token 保持原值。
            self.transcript_tokens = None

    def snapshot(self) -> dict[str, JsonValue]:
        tokens = self.thread_tokens if self.thread_tokens is not None else self.transcript_tokens
        encoded: dict[str, JsonValue] | None = (
            {key: value for key, value in tokens.items()} if tokens is not None else None
        )
        return {
            "prompts": self.prompts or self.codex_message_prompts,
            "tokens": encoded,
            "session_wide": self.thread_tokens is not None,
        }


def session_usage(rollouts: list[dict[str, JsonValue]], prompts: int) -> dict[str, JsonValue]:
    # 新版 Codex 的 thread 计数已经包含旧 rollout，不能再与各文件累计相加。
    latest_thread = next((rollout for rollout in reversed(rollouts) if rollout.get("session_wide") is True), None)
    sources = [latest_thread] if latest_thread is not None else rollouts
    meters = [_object(row.get("tokens")) for row in sources if isinstance(row.get("tokens"), dict)]
    return {
        "prompts": max(prompts, sum(count(rollout.get("prompts")) for rollout in rollouts)),
        "tokens": {key: sum(count(meter.get(key)) for meter in meters) for key in _KEYS} if meters else None,
    }
