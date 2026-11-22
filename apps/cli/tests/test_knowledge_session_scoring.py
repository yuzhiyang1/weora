"""验证团队评分试跑与固定版本的人工标签。"""

import argparse
import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import (
    knowledge,
    knowledge_local_scoring,
    knowledge_named_scoring,
    knowledge_score_work,
    knowledge_session_scoring,
)
from weora_cli.collab_client import Client, CollabError, as_object, items
from weora_cli.commands import JsonValue

_RUBRIC = """---
labels:
  - value: confirmed
    description: Evidence is present.
    score: 1
  - value: missing
    description: No evidence.
    score: 0
passingScore: 1
tool: claude-code
model: test-model
---
Check for evidence of verification.
"""


class SessionScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client("https://example.invalid", str(uuid4()), "fixture")
        self.archive = str(uuid4())
        self.project = str(uuid4())
        self.parser = argparse.ArgumentParser()
        knowledge.arguments(self.parser)
        pairing = patch.object(knowledge, "paired_client", return_value=self.client)
        pairing.start()
        self.addCleanup(pairing.stop)
        self.scope = ["--archive", self.archive, "--revision", "3", "--project", self.project]

    def test_local_dry_run_validates_then_scores_without_label_write(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "team-check.md"
        path.write_text(_RUBRIC, encoding="utf-8")
        args = self.parser.parse_args(["score", *self.scope, "--file", str(path)])
        evidence: dict[str, JsonValue] = {
            "id": self.archive, "revision": 3, "digest": "abc", "turns": [{"role": "assistant", "text": "Tested."}],
        }
        with (
            patch.object(knowledge_session_scoring, "call", return_value=evidence) as request,
            patch.object(knowledge_session_scoring, "_answer", return_value='{"label": "confirmed"}') as judge,
        ):
            result = as_object(knowledge.execute(args))
            self.assertTrue(result["preview"])
            self.assertEqual(result["items"], [{"scorer": "team-check", "scorer_revision": None,
                                               "label": "confirmed", "score": 1.0, "passed": True,
                                               "driver": "claude", "requested_model": "test-model"}])
            self.assertEqual(request.call_count, 2)
            request.assert_any_call(self.client, self.project, "/settings/session-scoring")
            self.assertEqual(request.call_args.kwargs, {"revision": 3})
            self.assertEqual(judge.call_args.args[0], "claude")
            self.assertEqual(judge.call_args.kwargs, {"model": "test-model"})
            data = json.loads(judge.call_args.args[1].split("\n", 1)[1])
            self.assertEqual(data["transcript"], evidence["turns"])
            path.write_text(_RUBRIC.replace("passingScore: 1", "passingScore: 100"), encoding="utf-8")
            with self.assertRaisesRegex(CollabError, "通过标签"):
                knowledge.execute(args)
            self.assertEqual(request.call_count, 2)

    def test_folder_trial_resolves_attachment_paths_and_excludes_hidden_files(self) -> None:
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "team-check"
            guide = folder / "examples" / "guide.md"
            guide.parent.mkdir(parents=True)
            guide.write_bytes(b"Verified.\r\n")
            (folder / ".private").write_bytes(b"synthetic private marker")
            marker = folder / "SCORER.md"
            marker.write_text(_RUBRIC + "Read ${TEAMAI_SCORER_DIR}/examples/guide.md.", encoding="utf-8")
            self.assertEqual(
                knowledge_session_scoring._definition(folder), knowledge_session_scoring._definition(marker),
            )
            args = self.parser.parse_args(["score", *self.scope, "--file", str(folder)])
            evidence = {"id": self.archive, "revision": 3, "digest": "abc", "turns": [{"text": "Tested."}]}
            with (
                patch.object(knowledge_session_scoring, "call", return_value=evidence),
                patch.object(knowledge_session_scoring, "_answer", return_value='{"label":"confirmed"}') as judge,
            ):
                self.assertTrue(as_object(knowledge.execute(args))["preview"])
                data = json.loads(judge.call_args.args[1].split("\n", 1)[1])
                # 模型使用可移植的路径，输入里没有隐藏文件或本机目录。
                self.assertIn("Read scorers/team-check/examples/guide.md", data["rubric"])
                self.assertEqual(data["files"], [{"path": "scorers/team-check/examples/guide.md",
                                                  "content": "Verified.\n"}])
            guide.write_bytes(b"\xff\x00")
            with self.assertRaises(CollabError):
                knowledge_session_scoring._definition(folder)

    def test_manual_label_uses_pinned_scorer_without_reading_transcript(self) -> None:
        rubric = str(uuid4())
        operation = str(uuid4())
        args = self.parser.parse_args([
            "score", *self.scope, "--scorer", "team-check", "--label", "confirmed", "--operation-id", operation,
        ])
        plan = {"archive": {"id": self.archive, "revision": 3, "digest": "abc"}, "items": [{
            "id": rubric, "name": "team-check", "revision": 4, "passing_score": 1,
            "labels": [{"value": "confirmed", "score": 1}, {"value": "missing", "score": 0}],
        }]}
        with (
            patch.object(knowledge_session_scoring, "call", side_effect=[plan, {"id": 17}]) as request,
            patch.object(knowledge_session_scoring, "_answer") as judge,
        ):
            answer = as_object(knowledge.execute(args))
            self.assertEqual(answer["items"], [{"id": 17}])
            self.assertEqual(answer["operation_id"], operation)
            self.assertFalse(answer["preview"])
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args.args[1:], (self.project, f"/shared-sessions/{self.archive}/labels"))
            payload = request.call_args.kwargs["payload"]
            self.assertEqual(
                (payload["driver"], payload["scorer_id"], payload["scorer_revision"]), ("human", rubric, 4),
            )
            judge.assert_not_called()

    def test_background_retries_same_claim_and_settlement_without_rescoring(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        args = self.parser.parse_args(["score-drain", "--project", self.project, "--limit", "1"])
        operation = uuid4()
        work = {
            "status": "open", "attempt_id": str(operation),
            "archive": {"id": self.archive, "revision": 3, "digest": "abc"},
            "scorer": {"tool": "claude", "labels": [{"value": "confirmed", "score": 1}]},
        }
        unreachable = CollabError("WEORA_UNREACHABLE", "离线")
        receipt = {"status": "completed", "label": {"label": "confirmed"}}
        with (
            patch.dict(os.environ, {
                "WEORA_COLLAB_CONFIG": str(Path(temp.name) / "device.json"), "WEORA_SCORING_DISABLED": "",
            }),
            patch.object(knowledge_score_work, "uuid4", return_value=operation),
            patch.object(knowledge_score_work, "call", side_effect=[unreachable, work, unreachable, receipt]) as call,
            patch.object(knowledge_score_work, "archived_turns", return_value=[{"text": "Tested."}]) as evidence,
            patch.object(knowledge_score_work, "judge_session", return_value="confirmed") as judge,
        ):
            answer = knowledge.execute(args)
            self.assertEqual(answer, {"status": "processed", "items": [{"attempt_id": str(operation), **receipt}]})
            self.assertEqual(call.call_args_list[0], call.call_args_list[1])
            self.assertEqual(call.call_args_list[2], call.call_args_list[3])
            self.assertEqual(evidence.call_args.args[1:], (self.project, self.archive, 3, "abc"))
            judge.assert_called_once()

    def test_native_session_posts_only_fingerprint_and_label(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        native_id = str(uuid4())
        transcript = root / "codex" / "sessions" / f"rollout-{native_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("\n".join(json.dumps(row) for row in [
            {"type": "session_meta", "payload": {"id": native_id, "source": "cli", "cwd": str(root)}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Private report. token=synthetic_local_password"},
            ]}},
        ]), encoding="utf-8")
        args = self.parser.parse_args([
            "score", "--session", native_id, "--project", self.project, "--scorer", "team-check",
        ])
        plan = {"items": [{"id": str(uuid4()), "name": "team-check", "revision": 1, "prompt": "Verify the report.",
                           "passing_score": 1, "labels": [{"value": "confirmed", "score": 1}]}]}

        def receipt(
            _client: Client, _project: str, _url: str, *, payload: dict[str, JsonValue], method: str,
        ) -> dict[str, JsonValue]:
            self.assertEqual(set(payload), {"runtime", "subject_key", "digest"})
            self.assertEqual(method, "POST")
            self.assertNotIn(native_id, str(payload))
            return {"id": self.archive, "revision": 2, "evidence": "local", "digest": payload["digest"]}

        with (
            patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"), "CLAUDE_CONFIG_DIR": str(root / "claude")}),
            patch.object(knowledge_session_scoring, "call", side_effect=[plan, {"id": 17}]) as api,
            patch.object(knowledge_local_scoring, "call", side_effect=receipt) as register,
            patch.object(knowledge_session_scoring, "_answer", return_value='{"label": "confirmed"}') as judge,
        ):
            args.dry_run = True
            preview = as_object(knowledge.execute(args))
            self.assertIsNone(preview["subject_id"])
            register.assert_not_called()
            api.side_effect = [plan, {"id": 17}]
            args.dry_run = False
            args.session = None
            with patch.dict(os.environ, {
                "CODEX_SESSION_ID": native_id, "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_ID": "",
            }):
                answer = as_object(knowledge.execute(args))
            self.assertEqual((answer["subject_id"], answer["subject_revision"]), (self.archive, 2))
            self.assertIn("Private report.", judge.call_args.args[1])
            self.assertNotIn("synthetic_local_password", judge.call_args.args[1])
            self.assertNotIn("Private report.", str(api.call_args_list))
            self.assertEqual(api.call_args.kwargs["payload"]["label"], "confirmed")

    def test_named_local_mixed_preview_and_settle_use_only_evidence_fingerprints(self) -> None:
        native, entry = str(uuid4()), str(uuid4())
        name = "teamai:adoption:" + hashlib.sha256(entry.encode()).hexdigest()[:12]
        command = ["score", "--session", native, "--scorer", "quality", "--scorer", name, "--batch"]
        normal = {"name": "quality", "id": str(uuid4()), "revision": 1, "prompt": "Review evidence.",
                  "passing_score": 1, "labels": [{"value": "pass", "score": 1}, {"value": "fail", "score": 0}]}
        plan = {"items": [normal, {"name": name, "source_hash": "f" * 64,
                                  "adoption": {"id": entry, "revision": 1, "excerpt": "Use the mirror."}}]}
        evidence = {"runtime": "codex", "subject_key": "a" * 64, "digest": "b" * 64,
                    "recall_session_key": "c" * 64, "captured_at": "2026-11-19T00:00:00Z"}
        turns = [{"role": "assistant", "text": "Only sent to the judge."}]
        labels = {"quality": {"label": "pass"}, name: {"label": "adopted"}}
        receipt = {"subject_id": self.archive, "subject_revision": 1, "items": [
            {"scorer": "quality", "label": "pass"}, {"scorer": name, "label": "adopted"},
        ]}
        with (
            patch.object(knowledge_named_scoring, "local_evidence", return_value=(evidence, turns)),
            patch.object(knowledge_named_scoring, "call", side_effect=[plan, plan, receipt]) as api,
            patch.object(knowledge_session_scoring, "_answer", return_value=json.dumps(labels)) as judge,
        ):
            preview = as_object(knowledge.execute(self.parser.parse_args([*command, "--dry-run"])))
            self.assertTrue(preview["preview"])
            self.assertEqual(api.call_count, 1)
            done = as_object(knowledge.execute(self.parser.parse_args(command)))
            self.assertEqual(done["items"], receipt["items"])
            self.assertEqual(judge.call_count, 2)
            request = api.call_args.kwargs["payload"]
            self.assertEqual(request["subject"], {"kind": "local", **evidence})
            self.assertNotIn(native, json.dumps(request))
            self.assertNotIn(turns[0]["text"], json.dumps(request))
            self.assertEqual(len(request["items"]), 2)

    def test_background_mixed_batch_uses_one_judge_and_one_settlement(self) -> None:
        args = self.parser.parse_args(["score-drain", "--project", self.project, "--limit", "1"])
        operation, wanted, entry = uuid4(), str(uuid4()), str(uuid4())
        regular = {"name": "quality", "prompt": "Verify notes.", "passing_score": 1,
                   "labels": [{"value": "ok", "score": 1}, {"value": "missing", "score": 0}]}
        work = {
            "status": "open", "attempt_id": str(operation),
            "archive": {"id": self.archive, "revision": 1, "digest": "fixed"},
            "mixed_batch": [
                {"attempt_id": str(operation), "scorer": regular},
                {"attempt_id": wanted, "adoption": {"id": entry, "revision": 2, "excerpt": "Original advice."}},
            ],
        }
        with (
            TemporaryDirectory() as scratch,
            patch.dict(os.environ, {"WEORA_COLLAB_CONFIG": str(Path(scratch) / "device.json"),
                                    "WEORA_SCORING_DISABLED": ""}),
            patch.object(knowledge_score_work, "uuid4", return_value=operation),
            patch.object(knowledge_score_work, "call", side_effect=[work, {"status": "completed"}]) as api,
            patch.object(knowledge_score_work, "archived_turns", return_value=[{"text": "Used original advice."}]),
            patch.object(knowledge_session_scoring, "_answer") as judge,
        ):
            def answer(_driver: str, prompt: str, _schema: str, **_options: JsonValue) -> str:
                payload = json.loads(prompt.split("\n", 1)[1])
                normal, adopted = payload["rubrics"]
                self.assertEqual(normal["name"], "quality")
                self.assertEqual(adopted["evidence_files"][0]["content"], "Original advice.")
                return json.dumps({normal["name"]: {"label": "ok"}, adopted["name"]: {"label": "adopted"}})

            judge.side_effect = answer
            result = as_object(knowledge.execute(args))
            self.assertEqual(result["status"], "processed")
            judge.assert_called_once()
            self.assertTrue(api.call_args_list[0].kwargs["payload"]["accept_mixed_batch"])
            self.assertEqual(api.call_args.args[-1], f"/session-scoring/batches/{operation}/settle")
            self.assertEqual(api.call_args.kwargs["payload"], {"labels": {str(operation): "ok", wanted: "adopted"}})

    def test_batch_shares_evidence_once_and_rejects_an_incomplete_answer(self) -> None:
        args = self.parser.parse_args([
            "score", *self.scope, "--batch", "--scorer", "accuracy", "--scorer", "evidence",
        ])
        rubric: dict[str, JsonValue] = {
            "prompt": "Inspect the result.", "tool": "claude", "model": "test-model", "revision": 2,
            "passing_score": 1, "labels": [{"value": "pass", "score": 1}, {"value": "fail", "score": 0}],
        }
        archive = {"id": self.archive, "revision": 3, "digest": "abc"}
        plan = {"archive": archive, "items": [
            rubric | {"name": name, "id": str(uuid4())} for name in ("accuracy", "evidence")
        ]}
        subject = archive | {"turns": [{"role": "assistant", "text": "Tested."}]}
        with (
            patch.object(knowledge_session_scoring, "call", side_effect=[plan, subject]) as request,
            patch.object(knowledge_session_scoring, "_answer", return_value='{"accuracy":{"label":"pass"}}') as model,
        ):
            result = as_object(knowledge.execute(args))
            self.assertEqual(request.call_count, 2)
            self.assertEqual([row["error"] for row in items(result)], ["KNOWLEDGE_SCORER_LABEL"] * 2)
            model.assert_called_once()
            self.assertEqual(json.loads(model.call_args.args[1].split("\n", 1)[1])["transcript"], subject["turns"])
            self.assertEqual(model.call_args.kwargs["model"], "test-model")
        with (
            patch.object(knowledge_session_scoring, "call", side_effect=[plan, subject, {"id": 4}, {"id": 5}]) as api,
            patch.object(knowledge_session_scoring, "_answer", return_value=(
                '{"accuracy":{"label":"pass"},"evidence":{"label":"fail"}}'
            )) as model,
        ):
            self.assertEqual(as_object(knowledge.execute(args))["items"], [{"id": 4}, {"id": 5}])
            model.assert_called_once()
            first, second = (row.kwargs["payload"] for row in api.call_args_list[-2:])
            self.assertEqual(first["judge_call_id"], second["judge_call_id"])
            self.assertNotEqual(first["operation_id"], second["operation_id"])
