"""验证会话提醒只在本机处理，并在复核草稿之后发布。"""

import argparse
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import collaboration, knowledge, knowledge_session_share, knowledge_transcript
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.knowledge_recall_quality import RecallQuality
from weora_cli.knowledge_settings import SessionSettings


class SessionShareTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        env = patch.dict(
            os.environ,
            {
                "WEORA_COLLAB_CONFIG": str(self.home / "device.json"),
                "CLAUDE_CONFIG_DIR": str(self.home / "claude"),
                "CODEX_HOME": str(self.home / "codex"),
                "WEORA_SHARE_REMINDER_DISABLED": "",
                "WEORA_RECALL_DISABLED": "",
                "CLAUDE_CODE_ENTRYPOINT": "cli",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.client = Client("https://example.invalid", str(uuid4()), "fixture")
        policies = patch(
            "weora_cli.knowledge_settings.call",
            return_value={
                "recall": {"enabled": True, "source": "workspace"},
                "share-reminder": {"enabled": True, "source": "default"},
            },
        )
        policies.start()
        self.addCleanup(policies.stop)
        self.session = str(uuid4())
        self.parser = argparse.ArgumentParser()
        knowledge.arguments(self.parser)

    def test_claude_stop_hints_once_after_rejected_tool_and_only_emits_counts(self) -> None:
        path = self.home / "claude/projects/demo" / f"{self.session}.jsonl"
        path.parent.mkdir(parents=True)
        tools = [{"type": "tool_use", "id": str(i), "name": "Bash"} for i in range(15)]
        rows = [
            {"type": "assistant", "sessionId": self.session, "message": {"content": tools}},
            {
                "type": "user",
                "sessionId": self.session,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "14",
                            "is_error": True,
                            "content": "The tool use was rejected. private fixture text",
                        },
                    ]
                },
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        event = {
            "hook_event_name": "Stop",
            "session_id": self.session,
            "cwd": str(self.home),
            "transcript_path": str(path),
            "stop_hook_active": False,
        }
        args = argparse.Namespace(runtime="claude-code")
        with (
            patch.object(collaboration, "paired_client", return_value=self.client),
            patch.object(Client, "request") as request,
        ):
            for expected in (True, False):
                with patch("sys.stdin", io.StringIO(json.dumps(event))):
                    result = collaboration.hook_output(args)
                self.assertEqual(bool(result), expected)
                if expected:
                    self.assertIn("工具请求被拒绝 1 次", str(result["systemMessage"]))
                    self.assertNotIn("private fixture text", str(result))
            request.assert_not_called()

    def test_codex_correction_uses_one_prompt_per_stop_and_skips_exec(self) -> None:
        path = self.home / "codex/sessions" / f"rollout-test-{self.session}.jsonl"
        path.parent.mkdir(parents=True)

        def transcript(source: str) -> None:
            tool_logs = [
                {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": str(i)}}
                for i in range(15)
            ]
            rows = [{"type": "session_meta", "payload": {"id": self.session, "source": source}}, *tool_logs]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

        event = {"hook_event_name": "Stop", "session_id": self.session, "transcript_path": str(path)}
        settings = SessionSettings(self.client, self.home)
        transcript("exec")
        self.assertEqual(knowledge_session_share.observe(settings, "codex", event), "")
        transcript("cli")
        with patch("weora_cli.knowledge_session_share.time.time", return_value=100):
            self.assertEqual(knowledge_session_share.observe(settings, "codex", event), "")
            prompt = event | {"hook_event_name": "UserPromptSubmit", "prompt": "不对，请重新处理"}
            for _ in range(2):
                knowledge_session_share.observe(settings, "codex", prompt)
            self.assertIn("用户纠正 1 次", knowledge_session_share.observe(settings, "codex", event))

    def test_stop_uses_only_recent_local_recall_outcomes_for_knowledge_gap(self) -> None:
        path = self.home / "claude/projects/gap" / f"{self.session}.jsonl"
        path.parent.mkdir(parents=True)
        tools = [{"type": "tool_use", "id": str(i), "name": "Read"} for i in range(15)]
        path.write_text(json.dumps({
            "type": "assistant", "sessionId": self.session, "message": {"content": tools},
        }), encoding="utf-8")
        event = {"hook_event_name": "Stop", "session_id": self.session, "transcript_path": str(path)}
        settings = SessionSettings(self.client, self.home)
        platform_session = str(uuid4())
        start = event | {"hook_event_name": "UserPromptSubmit", "cwd": str(self.home)}
        with (
            patch("sys.stdin", io.StringIO(json.dumps(start))),
            patch.object(collaboration, "paired_client", return_value=self.client),
            patch.object(collaboration, "_register", return_value={"id": platform_session}),
            patch.object(collaboration, "_git", return_value=""),
            patch.object(Client, "request", return_value={"items": []}),
        ):
            hook = collaboration.hook_output(argparse.Namespace(runtime="claude-code", auto=False))
        context = str(as_object(hook["hookSpecificOutput"])["additionalContext"])
        self.assertIn(f"当前 Weora 会话 ID 为 {platform_session}", context)
        self.assertEqual(context.count(f"--session {self.session} --automatic"), 2)

        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge, "_call") as request,
            patch("weora_cli.knowledge_recall_quality.time.time", return_value=200000) as clock,
        ):
            request.return_value = {"relevant": False}
            knowledge.execute(self.parser.parse_args(["recall", "sensitive-query", "--check"]))
            self.assertEqual(knowledge_session_share.observe(settings, "claude-code", event), "")
            request.return_value = {"run_id": str(uuid4()), "items": []}
            knowledge.execute(self.parser.parse_args(["recall", "sensitive-query", "--session", self.session]))
            clock.return_value += 86401
            self.assertEqual(knowledge_session_share.observe(settings, "claude-code", event), "")
            request.return_value = {"run_id": str(uuid4()), "items": []}
            knowledge.execute(self.parser.parse_args(["recall", "sensitive-query", "--session", self.session]))
            hint = knowledge_session_share.observe(settings, "claude-code", event)
            self.assertIn("知识召回均无结果", hint)
            self.assertNotIn("sensitive-query", hint)
            self.assertEqual(knowledge_session_share.observe(settings, "claude-code", event), "")
        quality = list(self.home.glob("knowledge-session/*/recall-quality/*.json"))
        self.assertEqual(len(quality), 1)
        self.assertNotIn("sensitive-query", quality[0].read_text(encoding="utf-8"))

    def test_weak_and_effective_recall_adjust_the_reminder_at_the_reference_threshold(self) -> None:
        self.assertEqual(RecallQuality(hits=1, misses=2, top_score=4.99).adjustment(), (10, "知识召回只有弱匹配"))
        self.assertEqual(RecallQuality(hits=1, misses=2, top_score=5).adjustment(), (0, ""))

    def test_transcript_preview_scrubs_and_confirmation_publishes_the_same_snapshot(self) -> None:
        project = str(uuid4())
        workspace = self.home / "fixture-repo"
        (workspace / ".git").mkdir(parents=True)
        path = self.home / "claude/projects/preview" / f"{self.session}.jsonl"
        path.parent.mkdir(parents=True)
        token = "unique_fixture_credential"
        path.write_text("\n".join(json.dumps(row) for row in [
            {
                "type": "user", "sessionId": self.session, "cwd": str(workspace), "message": {
                    "content": f"Fix {workspace / 'src' / 'api.py'} with Bearer {token} and /api/v1/users",
                },
            },
            {
                "type": "assistant", "sessionId": self.session, "message": {"content": [
                    {"type": "thinking", "thinking": "hidden internal record"},
                    {"type": "text", "text": "已完成修复。"},
                    {"type": "image", "source": {"type": "base64", "data": "private image data"}},
                    {"type": "tool_use", "name": "Read", "input": {"password": "raw fixture password"}},
                ]},
            },
            {"type": "user", "sessionId": str(uuid4()), "message": {"content": "unrelated private session"}},
        ]), encoding="utf-8")
        command = ["share-session", "--runtime", "claude-code", "--session", self.session, "--project", project]
        with (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_transcript, "call", return_value={"id": "archive", "revision": 1}) as transport,
        ):
            preview = as_object(knowledge.execute(self.parser.parse_args(command)))
            transport.assert_not_called()
            snapshot = str(preview["turns"])
            for hidden in (
                token, "raw fixture password", str(workspace), "hidden internal", "unrelated private",
                "private image data",
            ):
                self.assertNotIn(hidden, snapshot)
            self.assertIn("<repo>", snapshot)
            self.assertIn("<image>", snapshot)
            self.assertIn("/api/v1/users", snapshot)
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n" + json.dumps({
                    "type": "user", "sessionId": self.session, "message": {"content": "added after preview"},
                }))
            confirmation = ["--confirm", str(preview["confirm"])]
            wrong_scope = command[:-1] + [str(uuid4())]
            with self.assertRaises(CollabError) as denied:
                knowledge.execute(self.parser.parse_args([*wrong_scope, *confirmation]))
            self.assertEqual(denied.exception.code, "KNOWLEDGE_TRANSCRIPT_CONFIRM")
            transport.assert_not_called()
            written = knowledge.execute(self.parser.parse_args([*command, *confirmation]))
            self.assertEqual(written, {"id": "archive", "revision": 1})
            sent = transport.call_args.kwargs["payload"]
            self.assertEqual(sent["turns"], preview["turns"])
            self.assertEqual(sent["title"], preview["title"])
            self.assertNotIn("added after preview", str(sent))

    def test_codex_transcript_discovery_rejects_another_root_and_omits_reasoning(self) -> None:
        path = self.home / "codex/sessions" / f"rollout-run-{self.session}.jsonl"
        path.parent.mkdir(parents=True)
        metadata = {"id": str(uuid4()), "source": "cli", "cwd": str(self.home)}
        rows = [
            {"type": "session_meta", "payload": metadata},
            {"type": "response_item", "payload": {"type": "reasoning", "content": "private reasoning"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Inspect C:\\unrelated\\private.txt"},
            ]}},
        ]
        command = ["share-session", "--runtime", "codex", "--session", self.session]
        with patch.object(knowledge, "paired_client", return_value=self.client):
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            with self.assertRaises(CollabError) as wrong:
                knowledge.execute(self.parser.parse_args(command))
            self.assertEqual(wrong.exception.code, "KNOWLEDGE_TRANSCRIPT_SCOPE")
            metadata["id"] = self.session
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            preview = as_object(knowledge.execute(self.parser.parse_args(command)))
            self.assertEqual(preview["turns"], [{"role": "user", "text": "Inspect <path>"}])

    def test_confirmation_rejects_changed_content_and_publishes_only_selected_drafts(self) -> None:
        first, second = self.home / "第一份.md", self.home / "第二份.md"
        first.write_text("经验第一份", encoding="utf-8")
        second.write_text("经验第二份", encoding="utf-8")
        project = str(uuid4())
        argv = [
            "share-learnings",
            "--runtime",
            "codex",
            "--session",
            self.session,
            "--file",
            str(first),
            "--file",
            str(second),
            "--project",
            project,
        ]
        with (
            patch.object(knowledge, "paired_client", return_value=self.client) as paired,
            patch.object(knowledge_session_share, "call", return_value={"items": []}) as publish,
        ):
            preview = as_object(knowledge.execute(self.parser.parse_args(argv)))
            paired.assert_called_once()
            self.assertEqual(preview["workspace_id"], self.client.workspace)
            publish.assert_not_called()
            approve = self.parser.parse_args([*argv, "--confirm", str(preview["confirm"])])
            first.write_text("已修改", encoding="utf-8")
            with self.assertRaises(CollabError) as caught:
                knowledge.execute(approve)
            self.assertEqual(caught.exception.code, "KNOWLEDGE_SHARE_CHANGED")
            publish.assert_not_called()
            first.write_text("经验第一份", encoding="utf-8")
            knowledge.execute(approve)
            self.assertEqual(publish.call_args.args, (self.client, project, "/imports"))
            self.assertEqual(publish.call_args.kwargs["payload"], {"items": preview["items"]})
