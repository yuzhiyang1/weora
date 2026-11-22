"""覆盖原生用量格式、缓存去重及同一根会话的多个 Codex rollout。"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge_activity as activity
from weora_cli.collab_client import Client
from weora_cli.commands import JsonValue
from weora_cli.knowledge_friction import scan


def stream(path: Path, rows: list[dict[str, JsonValue]], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row) + "\n")


class NativeUsageTests(unittest.TestCase):
    def test_claude_counts_human_turns_once_and_rolls_up_owned_child_usage(self) -> None:
        session = str(uuid4())
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / f"{session}.jsonl"
            user: dict[str, JsonValue] = {"type": "user", "sessionId": session}
            assistant: dict[str, JsonValue] = {"type": "assistant", "sessionId": session}
            primary: dict[str, JsonValue] = {
                "id": "real-request",
                "usage": {"input_tokens": 10, "output_tokens": 8, "cache_read_input_tokens": 6,
                          "cache_creation_input_tokens": 4, "iterations": [
                              {"type": "advisor_message", "model": "advisor", "input_tokens": 2, "output_tokens": 1},
                          ]},
            }
            stream(root, [
                user | {"message": {"content": "private fixture"}},
                user | {"message": {"content": [{"type": "tool_result", "content": "fake tool payload"}]}},
                user | {"message": {"content": "<system-reminder>injected</system-reminder>"}},
                user | {"sessionId": "someone-else", "message": {"content": "ignore"}},
                user | {"isMeta": True, "message": {"content": "ignore"}},
                assistant | {"message": primary},
                assistant | {"message": primary | {"content": [{"type": "text", "text": "repeated block"}]}},
            ])
            child = root.with_suffix("") / "subagents" / "agent-fixture.jsonl"
            stream(child, [
                user | {"message": {"content": "automated tool prompt"}},
                assistant | {"message": primary},
                assistant | {"requestId": "owned-child", "message": {"usage": {
                    "input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 1,
                }}},
            ])
            result = scan(root, "claude-code", session, collect_usage=True)
            assert result is not None and result.usage is not None
            self.assertEqual(result.usage.snapshot(), {
                "prompts": 1, "tokens": {"input": 15, "output": 11, "cache_read": 7, "cache_creation": 4},
                "session_wide": False,
            })

    def test_codex_resume_uses_current_thread_snapshot_without_resumming_old_rollouts(self) -> None:
        session = str(uuid4())
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            scratch = root / "project"
            scratch.mkdir()
            codex = root / "codex"
            first = codex / "sessions" / f"rollout-a-{session}.jsonl"
            second = codex / "sessions" / f"rollout-b-{session}.jsonl"
            meta: dict[str, JsonValue] = {"type": "session_meta", "payload": {"id": session, "source": "cli"}}
            prompt: dict[str, JsonValue] = {
                "type": "event_msg", "payload": {"type": "user_message", "message": "test prompt"},
            }
            duplicate: dict[str, JsonValue] = {"type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "test prompt"}],
            }}
            legacy: dict[str, JsonValue] = {"type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 40,
                }},
            }}
            projection: dict[str, JsonValue] = {"type": "token_usage_record", "payload": {
                "thread_id": session, "thread_token_usage": {
                    "input_tokens": 140, "output_tokens": 30, "cached_input_tokens": 55, "cache_write_input_tokens": 5,
                },
            }}
            client = Client("https://example.invalid", str(uuid4()), "fixture")
            with (
                patch.dict(os.environ, {"CODEX_HOME": str(codex), "WEORA_COLLAB_CONFIG": str(root / "device.json"),
                                        "WEORA_USAGE_REPORT_DISABLED": ""}),
                patch.object(activity, "_SETTLE_SECONDS", 0),
                patch.object(activity, "call", return_value={"enabled": True}) as platform,
            ):
                activity.configure(client, scratch, "enable", workspace=False, project=str(uuid4()), shared=False)
                for path in (first, second):
                    stream(path, [meta, prompt, duplicate, legacy, legacy])
                    # 文件尺寸和时间相同仍是两个独立的 rollout。
                    same_time = 1_790_000_000_000_000_000
                    os.utime(path, ns=(same_time, same_time))
                    activity.observe(client, scratch, "codex", {
                        "session_id": session, "transcript_path": str(path), "hook_event_name": "Stop",
                    })
                    published = activity.publish(client, scratch)
                    assert published is not None
                    self.assertEqual(published["reported"], 1)
                payload = platform.call_args.kwargs["payload"]
                self.assertEqual((payload["prompts"], payload["tokens"]), (
                    2, {"input": 120, "output": 20, "cache_read": 80, "cache_creation": 0},
                ))
                # 使用新线程级计数测试优先级，同时模拟 Stop 后追加的宿主写入。
                stream(second, [prompt], append=True)
                activity.observe(client, scratch, "codex", {
                    "session_id": session, "transcript_path": str(second), "hook_event_name": "Stop",
                })
                stream(second, [projection, legacy], append=True)
                activity.publish(client, scratch)
                payload = platform.call_args.kwargs["payload"]
                self.assertEqual((payload["prompts"], payload["tokens"]), (
                    3, {"input": 80, "output": 30, "cache_read": 55, "cache_creation": 5},
                ))
                self.assertNotIn("test prompt", str(payload))
                self.assertNotIn(str(codex), str(payload))
