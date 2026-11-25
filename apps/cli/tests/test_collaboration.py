"""验证通用会话寻址、hook 合并和原生交接协议。"""

import json
import os
import re
import tempfile
import time
import tomllib
import unittest
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli.cli_auth import _finish
from weora_cli.collab_client import (
    SERVICE,
    Client,
    CollabError,
    config_path,
    pair,
    paired_client,
    read_config,
    write_json,
)
from weora_cli.collab_kimi import KimiAcp
from weora_cli.collab_native import adapter, install_hook, profile
from weora_cli.collaboration import _cursor_hook, _resolve, hook_output, wake_pending
from weora_cli.commands import JsonValue


class CollaborationTests(unittest.TestCase):
    def test_switch_server_clears_previous_credential_without_contacting_old_server(self) -> None:
        previous_base, new_base = "https://old.example.com", "https://new.example.com"
        workspace, old_device, new_device = (str(uuid4()) for _ in range(3))
        login_id = uuid4()
        old_account = f"{previous_base}/{workspace}/{old_device}"
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(Path(folder) / "collaboration.json")}),
            patch("weora_cli.cli_auth.request_json", return_value={
                "status": "authorized", "id": new_device, "workspace_id": workspace, "token": "new-token",
            }),
            patch.object(Client, "request") as device_request,
            patch("weora_cli.cli_auth.keyring.get_password", return_value="old-token"),
            patch("weora_cli.cli_auth.keyring.delete_password") as forget,
            patch("weora_cli.cli_auth.keyring.set_password") as save,
        ):
            write_json(config_path(), {"base": previous_base, "workspace": workspace, "device_id": old_device})
            _finish(login_id, {
                "base": new_base, "verifier": "proof", "interval": 1, "deadline": time.time() + 30,
            })
            device_request.assert_not_called()
            forget.assert_any_call(SERVICE, old_account)
            save.assert_called_once_with(SERVICE, f"{new_base}/{workspace}/{new_device}", "new-token")
            self.assertEqual(read_config(), {"base": new_base, "workspace": workspace, "device_id": new_device})

    def test_pairing_two_devices_in_one_workspace_keeps_both_credentials(self) -> None:
        workspace, first, second = (str(uuid4()) for _ in range(3))
        base = "http://127.0.0.1:5173"
        credentials: dict[tuple[str, str], str] = {}
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                Client, "request", side_effect=[{"id": first, "token": "one"}, {"id": second, "token": "two"}]
            ),
            patch(
                "weora_cli.collab_client.keyring.set_password",
                side_effect=lambda service, account, token: credentials.__setitem__((service, account), token),
            ),
            patch(
                "weora_cli.collab_client.keyring.get_password",
                side_effect=lambda service, account: credentials.get((service, account)),
            ),
        ):
            for side in ("desktop", "terminal"):
                os.environ["WEORA_COLLAB_CONFIG"] = str(Path(folder) / f"{side}.json")
                pair(base, workspace, "one-time-code")
            for side, expected in (("desktop", "one"), ("terminal", "two")):
                os.environ["WEORA_COLLAB_CONFIG"] = str(Path(folder) / f"{side}.json")
                self.assertEqual(paired_client().token, expected)

    def test_selector_does_not_pick_an_arbitrary_runtime_or_duplicate_name(self) -> None:
        client = Mock(spec=Client)
        wanted, other = str(uuid4()), str(uuid4())
        client.request.return_value = {
            "items": [
                {"id": wanted, "address": "dev/session@host-a", "runtime": "custom-agent"},
                {"id": other, "address": "dev/session@host-b", "runtime": "pi"},
            ]
        }
        with self.assertRaises(CollabError) as caught:
            _resolve(client, "dev/session", None)
        self.assertEqual(caught.exception.code, "AMBIGUOUS_SESSION")
        self.assertEqual(_resolve(client, "dev/session@host-a", None), wanted)

    def test_install_hook_keeps_existing_handlers_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"CODEX_HOME": home}):
            path = Path(home) / "hooks.json"
            original = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "another-hook"}]}]}}
            path.write_text(json.dumps(original), encoding="utf-8")
            install_hook("codex", auto=False)
            install_hook("codex", auto=True)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["hooks"]["SessionStart"][0], original["hooks"]["SessionStart"][0])
            for event in ("SessionStart", "UserPromptSubmit", "Stop", "SubagentStart", "PostToolUse"):
                group = document["hooks"][event][-1]
                self.assertEqual(group["hooks"][0]["command"], "weora collab hook --runtime codex --auto")
            self.assertEqual(document["hooks"]["PostToolUse"][0]["matcher"], "TodoWrite")
            self.assertEqual(document["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"], 3)
            self.assertEqual(len(document["hooks"]["SessionStart"]), 2)
            agent = Path(home) / "agents/weora-recall.toml"
            self.assertEqual(tomllib.loads(agent.read_text(encoding="utf-8"))["name"], "weora-recall")
            agent.write_text('name = "weora-recall"\n', encoding="utf-8")
            self.assertEqual(install_hook("codex", auto=True)["recall_agent"], str(agent))
            self.assertEqual(agent.read_text(encoding="utf-8"), 'name = "weora-recall"\n')

    def test_subagent_start_binds_native_actor_to_both_knowledge_commands(self) -> None:
        client = Mock(spec=Client)
        root, project = str(uuid4()), str(uuid4())
        native = "native-agent-id-without-platform-exposure"
        event = {"session_id": root, "hook_event_name": "SubagentStart", "agent_id": native,
                 "agent_type": "weora-recall", "cwd": "/work/project"}
        actors = []
        for runtime in ("codex", "claude-code"):
            with (
                self.subTest(runtime=runtime),
                patch("sys.stdin", StringIO(json.dumps(event))),
                patch("weora_cli.collaboration.paired_client", return_value=client),
                patch("weora_cli.collaboration.SessionSettings.effective",
                      return_value={"enabled": True, "project": project}),
                patch("weora_cli.collaboration._register") as register,
            ):
                value = hook_output(Namespace(runtime=runtime, auto=True))
                output = value["hookSpecificOutput"]
                assert isinstance(output, dict)
                hint = output["additionalContext"]
                assert isinstance(hint, str)
                mark = re.search(r"--recall-actor ([a-f0-9-]{36})", hint)
                assert mark is not None
                actors.append(mark[1])
                self.assertIn(f"--actor {mark[1]}", hint)
                self.assertIn(f"--session {root} --project {project}", hint)
                self.assertIn(f"--check --project {project}", hint)
                self.assertIn("--depth context --limit 1", hint)
                self.assertIn(f"weora context graph 主题 --session {root} --project {project}", hint)
                self.assertNotIn("先单独调用", hint)
                self.assertNotIn(native, hint)
                register.assert_not_called()
        self.assertNotEqual(actors[0], actors[1])

        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}),
        ):
            receipt = install_hook("claude-code", auto=False)
            profile_path = Path(str(receipt["recall_agent"]))
            self.assertIn("name: weora-recall", profile_path.read_text(encoding="utf-8"))

    def test_plan_tool_hook_checks_policy_before_claiming_the_session_hint(self) -> None:
        client = Mock(spec=Client, account="plan-fixture")
        client.token = "fixture-device"
        session = str(uuid4())
        event = {"session_id": session, "hook_event_name": "PostToolUse", "tool_name": "TodoWrite", "cwd": "/test"}
        policy = {"enabled": False, "language": "en", "message": "workspace plan reminder"}
        args = Namespace(runtime="claude-code", auto=True)

        def run(**overrides: object) -> dict[str, JsonValue]:
            with patch("sys.stdin", StringIO(json.dumps(event | overrides))):
                return hook_output(args)

        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(Path(home) / "collab.json")}),
            patch("weora_cli.collaboration.paired_client", return_value=client) as pair_client,
            patch("weora_cli.collaboration.SessionSettings.effective", return_value={"enabled": False}) as recall,
            patch("weora_cli.knowledge_plan_reminder.call", return_value=policy) as read_policy,
            patch("weora_cli.knowledge_plan_reminder.time.time", return_value=200_000) as clock,
            patch("weora_cli.collaboration._register") as register,
        ):
            self.assertEqual(run(tool_name="Bash"), {})
            self.assertEqual(run(session_id="../bad-path"), {})
            pair_client.assert_not_called()
            self.assertEqual(run(), {})
            read_policy.assert_not_called()
            recall.return_value = {"enabled": True}
            self.assertEqual(run(), {})
            policy["enabled"] = True
            expected = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": policy["message"]}}
            self.assertEqual(run(), expected)
            self.assertEqual(run(), {})
            clock.return_value += 24 * 60 * 60 + 1
            policy["message"] = None
            value = run()["hookSpecificOutput"]
            assert isinstance(value, dict)
            self.assertIn("Task plan detected.", str(value["additionalContext"]))
            self.assertNotIn("任务已规划", str(value["additionalContext"]))
            register.assert_not_called()

    def test_connector_only_dispatches_reserved_wake_and_sends_no_message_body(self) -> None:
        client = Mock(spec=Client)
        session, message, native = (str(uuid4()) for _ in range(3))
        item = {
            "session_id": session,
            "message_id": message,
            "native_id": native,
            "runtime": "codex",
            "profile": "same-profile",
            "delivery": "queue",
            "wake": True,
            "body": "消息正文不进入原生输入",
        }
        client.request.side_effect = [
            {"items": [item]},
            {"reserved": False},
            {"items": [item]},
            {"reserved": True},
            {},
        ]
        handoff = Mock(return_value="accepted")
        with (
            patch("weora_cli.collaboration.adapter", return_value=("queue", handoff)),
            patch("builtins.print"),
        ):
            seen: set[str] = set()
            wake_pending(client, resume=False, seen=seen)
            handoff.assert_not_called()
            wake_pending(client, resume=False, seen=seen)
        handoff.assert_called_once()
        hint = handoff.call_args.args[0]
        self.assertIn(session, hint)
        self.assertIn(message, hint)
        self.assertNotIn(item["body"], hint)

    def test_session_bridge_cannot_reserve_a_neighbor_session(self) -> None:
        client = Mock(spec=Client)
        ours, neighbor, message, other_message = (str(uuid4()) for _ in range(4))
        rows = [
            {"session_id": neighbor, "message_id": other_message, "delivery": "channel", "wake": True},
            {"session_id": ours, "message_id": message, "delivery": "channel", "wake": True},
        ]
        client.request.side_effect = [{"items": rows}, {"reserved": True}, {}]
        deliver = Mock(return_value="accepted")
        with patch("builtins.print"):
            wake_pending(client, resume=False, seen=set(), session=ours, local_wake=deliver)
        reserve = client.request.call_args_list[1]
        self.assertEqual(reserve.args, ("POST", f"/sessions/{ours}/messages/{message}/wake"))
        self.assertNotIn(other_message, deliver.call_args.args[0])

    def test_cursor_stop_uses_only_current_conversation_and_does_not_repeat(self) -> None:
        client = Mock(spec=Client)
        ours, neighbor, message, other_message = (str(uuid4()) for _ in range(4))
        client.request.side_effect = [
            {"id": ours},
            {
                "items": [
                    {"session_id": neighbor, "message_id": other_message},
                    {"session_id": ours, "message_id": message},
                ]
            },
        ]
        event: dict[str, JsonValue] = {
            "conversation_id": "cursor-original",
            "hook_event_name": "stop",
            "status": "completed",
            "loop_count": 0,
        }
        with patch("weora_cli.collaboration.paired_client", return_value=client):
            result = _cursor_hook(event)
            self.assertEqual(_cursor_hook(event | {"loop_count": 1}), {})
        self.assertIn(message, str(result.get("followup_message")))
        self.assertNotIn(other_message, str(result))
        self.assertEqual(client.request.call_count, 2)

    def test_kimi_permission_applies_to_the_bound_inbox_tools_only(self) -> None:
        # ACP 的权限判断不应对字符串相似的工具或持久批准放行。
        acp = KimiAcp.__new__(KimiAcp)
        acp.inbox_tools = {"weora_kimi_test_claim"}
        options: list[JsonValue] = [
            {"kind": "allow_always", "optionId": "always"},
            {"kind": "allow_once", "optionId": "once"},
        ]
        allowed = acp._permission({"toolCall": {"title": "weora_kimi_test_claim"}, "options": options})
        self.assertEqual(allowed, {"outcome": "selected", "optionId": "once"})
        for name in ("Shell", "weora_kimi_other_claim", "weora_kimi_test_send"):
            self.assertEqual(
                acp._permission({"toolCall": {"title": name}, "options": options}), {"outcome": "cancelled"}
            )

    @unittest.skipUnless(os.name == "nt", "Windows 原生 inbox 使用 named pipe")
    def test_claude_windows_inbox_requires_exact_session_and_peer_token(self) -> None:
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
            native = str(uuid4())
            registry = Path(home) / "sessions"
            registry.mkdir()
            pipe = r"\\.\pipe\weora-test-inbox"
            (registry / "824.json").write_text(
                json.dumps({"sessionId": native, "messagingSocketPath": pipe}),
                encoding="utf-8",
            )
            self.assertEqual(adapter("claude-code", profile("claude-code"), native), ("manual", None))
            (registry / "824.test.key").write_text(json.dumps({"peerToken": "local-token"}), encoding="utf-8")
            self.assertEqual(adapter("claude-code", "wrong-profile", native), ("manual", None))
            kind, handoff = adapter("claude-code", profile("claude-code"), native)
            self.assertEqual(kind, "channel")
            assert handoff is not None
            with patch("builtins.open") as opening:
                inbox = opening.return_value.__enter__.return_value
                inbox.write.side_effect = len
                self.assertEqual(handoff("read Weora"), "accepted")
                opening.assert_called_once_with(pipe, "r+b", buffering=0)
                frames = [json.loads(line) for line in inbox.write.call_args.args[0].splitlines()]
            self.assertEqual(frames[0], {"type": "auth", "token": "local-token"})
            self.assertEqual(frames[1], {"type": "user", "message": {"role": "user", "content": "read Weora"}})
