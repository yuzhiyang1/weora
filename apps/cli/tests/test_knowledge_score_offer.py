"""验证评分建议、预览确认和本机重试的副作用边界。"""

import argparse
import hashlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import knowledge, knowledge_score_offer, knowledge_score_work
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_local_scoring import evidence_digest


class ScoringOfferTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.client = Client("https://example.invalid", str(uuid4()), "fixture")
        self.project, self.native = str(uuid4()), str(uuid4())
        parser = argparse.ArgumentParser()
        knowledge.arguments(parser)
        self.parser = parser
        self.rubric: dict[str, JsonValue] = {
            "id": str(uuid4()), "name": "verification", "revision": 1, "sampling_rate": 100,
        }
        self.turns: list[JsonValue] = [{"role": "assistant", "text": "Tested using the original evidence."}]
        key = "c" * 64
        self.metadata = {"runtime": "codex", "subject_key": key, "digest": evidence_digest(key, self.turns)}
        for mock in (
            patch.dict(os.environ, {
                "WEORA_COLLAB_CONFIG": str(self.root / "device.json"), "WEORA_SCORING_DISABLED": "",
                "WEORA_SCORING_ISOLATION": "",
            }),
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.object(knowledge_score_offer, "local_evidence", return_value=(self.metadata, self.turns)),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def _run(self, *args: str, project: str | None = None) -> dict[str, JsonValue]:
        return as_object(knowledge.execute(self.parser.parse_args([
            "score-offer", "--project", project or self.project, *args,
        ])))

    def test_confirmation_reuses_the_original_verdict_after_a_failed_submission(self) -> None:
        plan = {"status": "ask", "items": [self.rubric]}
        receipt = {"status": "labeled", "label": "pass"}
        with (
            patch.object(knowledge_score_offer, "call", return_value=plan) as api,
            patch.object(knowledge_score_offer, "grade_rubric", return_value={"label": "pass"}) as model,
            patch.object(knowledge_score_offer, "register_evidence", return_value=(str(uuid4()), 3)) as register,
            patch.object(
                knowledge_score_offer, "submit_label", side_effect=[CollabError("offline", "故障"), receipt],
            ) as send,
        ):
            code = str(self._run("--session", self.native)["confirm"])
            model.assert_not_called()
            register.assert_not_called()
            with self.assertRaisesRegex(CollabError, "范围不一致"):
                self._run("--confirm", code, project=str(uuid4()))
            self.assertEqual(self._run("--confirm", code)["status"], "pending")
            result = self._run("--confirm", code)
            self.assertEqual(result, {"status": "completed", "items": [receipt]})
            self.assertEqual(self._run("--confirm", code), result)
            self.assertEqual(send.call_args_list[0], send.call_args_list[1])
            model.assert_called_once_with(self.rubric, self.turns)
            self.assertEqual(api.call_count, 3)

    def test_skips_and_stale_scorers_never_invoke_a_model(self) -> None:
        good = {"status": "ask", "items": [self.rubric]}
        newer = {"status": "ask", "items": [self.rubric | {"revision": 2}]}
        with (
            patch.object(knowledge_score_offer, "call", side_effect=[good, good, newer]) as api,
            patch.object(knowledge_score_offer, "grade_rubric") as model,
            patch.object(knowledge_score_offer, "register_evidence") as register,
        ):
            code = str(self._run("--session", self.native)["confirm"])
            skipped = self._run("--skip", code)
            self.assertEqual(skipped, {"status": "skipped", "items": []})
            self.assertEqual(self._run("--confirm", code), skipped)
            stale = str(self._run("--session", self.native)["confirm"])
            with self.assertRaisesRegex(CollabError, "评分标准或策略已变化"):
                self._run("--confirm", stale)
            self.assertEqual(api.call_count, 3)
            model.assert_not_called()
            register.assert_not_called()

    def test_changed_isolation_invalidates_unconfirmed_offer(self) -> None:
        original = {"status": "ask", "items": [self.rubric], "isolation": "best-effort"}
        changed = original | {"isolation": "verified"}
        with (
            patch.object(knowledge_score_offer, "call", side_effect=[original, changed]),
            patch.object(knowledge_score_offer, "grade_rubric") as model,
            patch.object(knowledge_score_offer, "register_evidence") as register,
        ):
            preview = self._run("--session", self.native)
            self.assertEqual(preview["isolation"], "best-effort")
            with self.assertRaisesRegex(CollabError, "评分标准或策略已变化"):
                self._run("--confirm", str(preview["confirm"]))
            model.assert_not_called()
            register.assert_not_called()

    def test_named_mixed_offer_confirms_pinned_group_and_reuses_the_original_verdict(self) -> None:
        entry = str(uuid4())
        name = "teamai:adoption:" + hashlib.sha256(entry.encode()).hexdigest()[:12]
        question = {"name": name, "source_hash": "f" * 64,
                    "adoption": {"id": entry, "revision": 2, "excerpt": "Use the mirror."}}
        plan = {"status": "ask", "items": [self.rubric, question]}
        local = self.metadata | {"recall_session_key": "a" * 64, "captured_at": "2026-11-19T01:00:00Z"}
        labels = {"verification": {"label": "pass", "driver": "codex"}, name: {"label": "adopted", "driver": "codex"}}
        receipt = {"verification": {"label": "pass"}, name: {"label": "adopted", "applied": True}}
        with (
            patch.object(knowledge_score_offer, "call", return_value=plan) as api,
            patch.object(knowledge_score_offer, "local_evidence", return_value=(local, self.turns)) as snapshot,
            patch.object(knowledge_score_offer, "grade_batch", return_value=labels) as judge,
            patch.object(knowledge_score_offer, "register_evidence") as register,
            patch.object(knowledge_score_offer, "submit_named_group",
                         side_effect=[CollabError("offline", "中断"), (str(uuid4()), 1, receipt)]) as submit,
        ):
            preview = self._run("--session", self.native, "--scorer", "verification", "--scorer", name, "--batch")
            code = str(preview["confirm"])
            self.assertEqual(as_object(preview["items"][1])["entry_revision"], 2)
            self.assertEqual(api.call_args.kwargs["payload"]["subject"], {"kind": "local", **local})
            snapshot.assert_called_once_with(None, self.native, None, with_recall=True)
            judge.assert_not_called()
            submit.assert_not_called()
            self.assertEqual(self._run("--confirm", code)["status"], "pending")
            result = self._run("--confirm", code)
            self.assertEqual(result, {"status": "completed", "items": list(receipt.values())})
            self.assertEqual(self._run("--confirm", code), result)
            self.assertEqual(submit.call_args_list[0], submit.call_args_list[1])
            self.assertEqual(submit.call_args.kwargs["offer_mode"], "attended")
            self.assertEqual(submit.call_args.args[4], [self.rubric, question])
            judge.assert_called_once()
            register.assert_not_called()

    def test_named_offer_rejects_changed_template_and_honors_skip(self) -> None:
        entry = str(uuid4())
        name = "teamai:adoption:" + hashlib.sha256(entry.encode()).hexdigest()[:12]
        question = {"name": name, "source_hash": "a" * 64,
                    "adoption": {"id": entry, "revision": 1, "excerpt": "Pinned."}}
        ask = {"status": "ask", "items": [question]}
        updated = {"status": "ask", "items": [question | {"source_hash": "b" * 64}]}
        with (
            patch.object(knowledge_score_offer, "call", side_effect=[ask, ask, updated]),
            patch.object(knowledge_score_offer, "grade_rubric") as judge,
            patch.object(knowledge_score_offer, "submit_named_group") as submit,
        ):
            skipped = self._run("--session", self.native, "--scorer", name)
            self.assertEqual(self._run("--skip", str(skipped["confirm"])), {"status": "skipped", "items": []})
            stale = self._run("--session", self.native, "--scorer", name)
            with self.assertRaisesRegex(CollabError, "评分标准或策略已变化"):
                self._run("--confirm", str(stale["confirm"]))
            judge.assert_not_called()
            submit.assert_not_called()

    def test_directory_settings_import_and_project_status_use_distinct_scopes(self) -> None:
        manifest = self.root / "scoring.yaml"
        manifest.write_text(
            "sessions: {attended: ask}\nbatch: false\ndaily_limit: 2\nrecall_adoption: false\n", encoding="utf-8",
        )
        configured = {"namespace": "Delivery", "revision": 2}
        payload = {
            "base_revision": 0, "sessions": {"attended": "ask"}, "batch": False, "daily_limit": 2,
            "recall_adoption": False,
        }
        suffix = "/settings/session-scoring"
        def run_settings(*args: str) -> dict[str, JsonValue]:
            return knowledge_score_work.configure(
                self.client, self.root, self.parser.parse_args(["scoring-settings", *args]),
            )

        with patch.object(knowledge_score_work, "call", return_value=configured) as request:
            result = run_settings("apply", "--namespace", "Delivery", "--file", str(manifest), "--base-revision", "0")
            self.assertEqual(result, configured)
            request.assert_called_once_with(self.client, None, f"{suffix}/namespaces/Delivery", method="PUT",
                                            payload=payload)
            request.reset_mock()
            run_settings("status", "--namespace", "Delivery")
            request.assert_called_once_with(self.client, None, f"{suffix}/namespaces/Delivery")
            request.reset_mock()
            manifest.write_text("skill_routing: maybe\n", encoding="utf-8")
            with self.assertRaisesRegex(CollabError, "skill_routing 应为布尔值"):
                run_settings("apply", "--namespace", "Delivery", "--file", str(manifest), "--base-revision", "2")
            request.assert_not_called()
            policy = {"action": "ask", "daily_limit": 2, "source": "namespace"}
            request.return_value = {"attended": policy}
            self.assertEqual(run_settings("status", "--project", self.project), policy)
            request.assert_called_once_with(self.client, self.project, suffix)
            request.reset_mock()
            run_settings("status", "--workspace")
            request.assert_called_once_with(self.client, None, suffix, workspace_defaults=1)

    def test_ask_binding_exposes_a_preview_hint_and_stops_background_dispatch(self) -> None:
        args = self.parser.parse_args(["scoring-settings", "ask", "--project", self.project])
        policy = {"attended": {"action": "auto", "daily_limit": 10}}
        with (
            patch.object(knowledge_score_work, "call", return_value=policy),
            patch("subprocess.Popen") as start,
        ):
            configured = knowledge_score_work.configure(self.client, self.root, args)
            self.assertEqual(configured, {"action": "ask", "project_id": self.project})
            knowledge_score_work.start_if_bound(self.client, self.root, "attended")
            start.assert_not_called()
            hint = knowledge_score_work.score_offer_hint(self.client, self.root, "codex", self.native)
            self.assertIn(f"score-offer --project {self.project}", hint)
            self.assertIn("收到同意后再确认", hint)
