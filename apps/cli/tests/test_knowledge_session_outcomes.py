"""使用真实 Git 对象验证会话归因，托管接口仅用本地替身。"""

import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from weora_cli import knowledge_activity as activity
from weora_cli import knowledge_session_outcomes as outcomes
from weora_cli.collab_client import Client, CollabError
from weora_cli.commands import JsonValue


@unittest.skipUnless(shutil.which("git"), "需要真实 Git")
class SessionOutcomeTests(unittest.TestCase):
    def test_idle_session_reports_own_commits_and_cached_pr_once_after_network_retry(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            work = root / "checkout"
            work.mkdir()
            stamp = int(time.time()) - 10
            clock = Mock()
            clock.time.return_value = stamp
            native = str(uuid4())
            transcript = root / "codex/sessions" / f"rollout-{native}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({
                "type": "session_meta", "payload": {"id": native, "source": "cli"},
            }) + "\n", encoding="utf-8")

            def git(*args: str, when: int = stamp, author: str = "member@example.invalid") -> str:
                return subprocess.check_output(["git", "-C", str(work), *args], text=True, env=os.environ | {
                    "GIT_AUTHOR_NAME": "Member", "GIT_AUTHOR_EMAIL": author, "GIT_COMMITTER_NAME": "Member",
                    "GIT_COMMITTER_EMAIL": "member@example.invalid", "GIT_AUTHOR_DATE": f"{when} +0000",
                    "GIT_COMMITTER_DATE": f"{when} +0000",
                }).strip()

            git("init", "-q", "--initial-branch=main")
            git("config", "user.email", "member@example.invalid")
            git("commit", "--allow-empty", "-qm", "base", when=stamp - 30)
            base = git("rev-parse", "HEAD")
            git("remote", "add", "origin", "https://github.com/example/learning.git")
            git("update-ref", "refs/remotes/origin/main", base)
            git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            git("switch", "-q", "-c", "session-topic")
            client = Client("https://example.invalid", str(uuid4()), "test")
            project = str(uuid4())
            pushed: list[dict[str, JsonValue]] = []

            def request(
                _client: Client, _project: str | None, resource: str = "", *, method: str = "GET",
                payload: dict[str, JsonValue] | None = None, **_query: object,
            ) -> dict[str, JsonValue]:
                if resource == "/settings/usage-report":
                    return {"enabled": True}
                if resource == "/activity/session-outcomes":
                    assert payload is not None
                    pushed.append(payload)
                    if len(pushed) == 1:
                        raise CollabError("WEORA_UNREACHABLE", "模拟回执丢失")
                return {"status": "recorded"}

            stop: dict[str, JsonValue] = {
                "hook_event_name": "Stop", "session_id": native, "transcript_path": str(transcript),
            }
            with (
                patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(root / "device.json"),
                                        "WEORA_USAGE_REPORT_DISABLED": "", "CODEX_HOME": str(root / "codex")}),
                patch.object(activity, "time", clock),
                patch.object(activity, "_SETTLE_SECONDS", 0),
                patch.object(activity, "call", side_effect=request),
                patch.object(outcomes, "_authorization", return_value={}),
                patch.object(outcomes, "_request") as provider,
            ):
                activity.configure(client, work, "enable", workspace=False, project=project, shared=False)
                activity.observe(client, work, "codex", stop | {"hook_event_name": "SessionStart", "source": "startup"})
                git("commit", "--allow-empty", "-qm", "other member", when=stamp + 1, author="peer@example.invalid")
                git("commit", "--allow-empty", "-qm", "owned", when=stamp + 2)
                head = git("rev-parse", "HEAD")
                git("update-ref", "refs/remotes/origin/session-topic", head)
                remote = {"full_name": "example/learning"}
                provider.return_value = ([{"state": "open",
                                           "head": {"ref": "session-topic", "repo": remote, "sha": head},
                                           "base": {"ref": "main", "repo": remote}}], {})
                clock.time.return_value = stamp + 3
                activity.observe(client, work, "codex", stop)
                git("commit", "--allow-empty", "-qm", "after stop", when=stamp + 4)
                origin = activity._read(activity._queue(client, "codex") / f"{native}.json")
                frozen = origin["outcome_start"]
                assert isinstance(frozen, dict)
                self.assertEqual(outcomes._commits(work, frozen, head, stamp + 3), {head})
                early = activity.publish(client, work)
                assert early is not None
                self.assertEqual(early["reported"], 1)
                self.assertEqual(pushed, [])
                provider.assert_called_once()

                # 恢复会话后旧的 Stop 不因等待时间过去而被误结算。
                activity.observe(
                    client, work, "codex", stop | {"hook_event_name": "UserPromptSubmit", "prompt": "next"},
                )
                clock.time.return_value = stamp + 6 * 3600 + 5
                activity.publish(client, work)
                self.assertEqual(pushed, [])
                git("switch", "--detach", "-q")
                activity.observe(client, work, "codex", stop)
                clock.time.return_value += 6 * 3600 + 5
                failed = activity.publish(client, work)
                assert failed is not None
                self.assertEqual(len(pushed), 1)
                retried = activity.publish(client, work)
                assert retried is not None
                self.assertEqual(retried["outcomes_reported"], 1)
                self.assertEqual(pushed[0], pushed[1])
                self.assertTrue(pushed[1]["has_commit"])
                self.assertTrue(pushed[1]["has_pr"])
                activity.publish(client, work)
                self.assertEqual(len(pushed), 2)
                provider.assert_called_once()
                self.assertNotIn(head, str(pushed))
                self.assertNotIn(native, str(pushed))
                self.assertNotIn("session-topic", str(pushed))

    def test_unattended_drain_settles_without_waiting_for_idle(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            native = str(uuid4())
            transcript = root / "codex/sessions" / f"rollout-{native}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({
                "type": "session_meta", "payload": {"id": native, "source": "exec"},
            }) + "\n", encoding="utf-8")
            client = Client("https://example.invalid", str(uuid4()), "test")
            with (
                patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(root / "device.json"),
                                        "WEORA_USAGE_REPORT_DISABLED": "", "CODEX_HOME": str(root / "codex")}),
                patch.object(activity, "call", return_value={"enabled": True}) as platform,
            ):
                activity.configure(client, root, "enable", workspace=False, project=None, shared=True)
                activity.observe(client, root, "codex", {
                    "hook_event_name": "SessionStart", "source": "startup", "session_id": native, "is_headless": True,
                })
                result = activity.drain(client, root, "codex", native, transcript)
                assert result is not None
                self.assertEqual((result["reported"], result["outcomes_reported"]), (1, 1))
                self.assertEqual(platform.call_args.args[2], "/activity/session-outcomes")
                self.assertEqual(platform.call_args.kwargs["payload"]["run_mode"], "unattended")
