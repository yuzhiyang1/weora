"""测试原生跨日趋势与请求估算在重放时只计一次。"""

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli import knowledge_activity as activity
from weora_cli.collab_client import Client
from weora_cli.commands import JsonValue
from weora_cli.knowledge_request_cost import request_cost
from weora_cli.knowledge_session_trends import trend_payload


class SessionTrendTests(unittest.TestCase):
    def test_active_intervals_merge_across_rollouts_and_hooks_without_double_counting(self) -> None:
        today = datetime.now(UTC)
        row: dict[str, JsonValue] = {
            "first_stop_day": today.date().isoformat(), "hook_spans": [[0, 100000], [900000, 910000]],
        }
        rollouts: list[dict[str, JsonValue]] = [
            {"trend": {"spans": [[80000, 350000]], "has_error": True}},
            {"trend": {"spans": [[650000, 700000], [1_300_000, 1_304_000]]}},
        ]
        self.assertEqual(trend_payload(row, rollouts, today), {"trend": {
            "first_stop_day": today.date().isoformat(), "active_ms": 914000, "has_error": True, "request_days": [],
        }})

    def test_reported_cost_and_cache_write_pricing(self) -> None:
        units = {"input": 10, "output": 2, "cache_read": 3, "cache_creation": 4}
        self.assertEqual(request_cost("claude-opus-4-8", units, tier="fast", hour_write=1), 261)
        self.assertEqual(request_cost("unknown", units, tier=None, reported=0.0000015), 2)
        self.assertIsNone(request_cost("unknown", units, tier=None))
        self.assertEqual(request_cost("gpt-6.2-sol", units, tier=None), 50)

    def test_codex_native_cost_uses_request_day_and_deduplicates_legacy_and_new_records(self) -> None:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        old_day = (today - timedelta(days=1)).date().isoformat()
        session = str(uuid4())

        def row(kind: str, seconds: int, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
            return {"type": kind, "timestamp": (today + timedelta(seconds=seconds)).isoformat(), "payload": payload}

        first: dict[str, JsonValue] = {"input_tokens": 100, "output_tokens": 5, "cached_input_tokens": 20}
        second: dict[str, JsonValue] = {
            "input_tokens": 160, "output_tokens": 10, "cached_input_tokens": 40, "cache_write_input_tokens": 20,
        }
        legacy = row("event_msg", -2, {"type": "token_count", "info": {
            "total_token_usage": first, "last_token_usage": first,
        }})
        modern = row("token_usage_record", 10, {
            "thread_id": session, "response_id": "request-beta", "usage": second, "thread_token_usage": second,
        })
        lines = [
            row("session_meta", -60, {"id": session, "source": "cli"}),
            row("turn_context", -5, {"model": "gpt-5.3-codex"}),
            row("event_msg", -4, {"type": "user_message", "message": "test content"}),
            legacy, legacy, modern, modern,
            row("event_msg", 11, {"type": "token_count", "info": {
                "total_token_usage": second, "last_token_usage": second,
            }}),
            row("event_msg", 12, {"type": "error", "message": "private native error"}),
            row("event_msg", 800, {"type": "task_complete"}),
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            transcript = root / "codex/sessions" / f"rollout-{session}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
            client = Client("https://example.invalid", str(uuid4()), "fixture")
            clock = Mock()
            clock.time.return_value = (today + timedelta(seconds=800)).timestamp()
            with (
                patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(root / "device.json"),
                                        "WEORA_USAGE_REPORT_DISABLED": "", "CODEX_HOME": str(root / "codex")}),
                patch.object(activity, "_SETTLE_SECONDS", 0),
                patch.object(activity, "time", clock),
                patch.object(activity, "call", return_value={"enabled": True}) as platform,
            ):
                activity.configure(client, root, "enable", workspace=False, project=None, shared=True)
                activity.observe(client, root, "codex", {
                    "hook_event_name": "Stop", "session_id": session, "transcript_path": str(transcript),
                })
                result = activity.publish(client, root)
                assert result is not None
                self.assertEqual(result["reported"], 1)
                payload = platform.call_args.kwargs["payload"]
                self.assertEqual(payload["trend"], {
                    "first_stop_day": today.date().isoformat(), "active_ms": 17000, "has_error": True,
                    "request_days": [
                        {"day": old_day, "priced_requests": 1, "cost_micros": 214},
                        {"day": today.date().isoformat(), "priced_requests": 1, "cost_micros": 357},
                    ],
                })
                self.assertNotIn("request-beta", str(payload))
                self.assertNotIn("test content", str(payload))
