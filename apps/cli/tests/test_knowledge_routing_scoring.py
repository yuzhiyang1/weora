"""点名 Skill 问题复用模型调用、预览确认及收据流程。"""

import argparse
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from weora_cli import (
    knowledge,
    knowledge_named_scoring,
    knowledge_score_offer,
    knowledge_score_work,
    knowledge_session_scoring,
)
from weora_cli.collab_client import Client, CollabError, as_object
from weora_cli.commands import JsonValue
from weora_cli.knowledge_local_scoring import evidence_digest


class RoutingScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.client = Client("https://example.invalid", str(uuid4()), "fixture")
        self.project, self.archive = str(uuid4()), str(uuid4())
        self.name = "teamai:routing:ops:release-check"
        self.version = str(uuid4())
        self.question: dict[str, JsonValue] = {
            "name": self.name, "source_hash": "a" * 64,
            "routing": {
                "skill": {"name": "release-check", "namespace": "ops", "version_id": self.version,
                          "description": "Check artifacts."},
                "used": ["release-check"], "evidence_hash": "b" * 64,
                "rubric": {"id": None, "revision": None, "prompt": "Judge release-check.", "passing_score": 1,
                           "labels": [{"value": "fit", "score": 1}, {"value": "missed", "score": 0}]},
            },
        }
        self.turns: list[JsonValue] = [{"role": "assistant", "text": "Checked the release."}]
        self.parser = argparse.ArgumentParser()
        knowledge.arguments(self.parser)
        for mock in (
            patch.object(knowledge, "paired_client", return_value=self.client),
            patch.dict(os.environ, {
                "WEORA_COLLAB_CONFIG": str(self.root / "device.json"), "WEORA_SCORING_ISOLATION": "",
            }),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def _run(self, *args: str) -> dict[str, JsonValue]:
        return as_object(knowledge.execute(self.parser.parse_args([*args, "--project", self.project])))

    def test_archive_named_call_models_pinned_description_and_saves_only_label(self) -> None:
        plan = {
            "archive": {"id": self.archive, "revision": 1, "digest": "d" * 64}, "items": [self.question],
        }
        label = {"scorer": self.name, "label": "fit"}
        receipt = {"subject_id": self.archive, "subject_revision": 1, "items": [label]}
        with (
            patch.object(knowledge_named_scoring, "call", side_effect=[plan, receipt]) as server,
            patch.object(knowledge_named_scoring, "archived_turns", return_value=self.turns),
            patch.object(knowledge_session_scoring, "_answer", return_value='{"label":"fit"}') as judge,
        ):
            output = self._run("score", "--archive", self.archive, "--revision", "1", "--scorer", self.name)
        self.assertEqual(output["items"], [label])
        model_data = json.loads(judge.call_args.args[1].split("\n", 1)[1])
        evidence = json.loads(model_data["evidence_files"][0]["content"])
        self.assertEqual(evidence["skills"]["available"], [{
            "name": "release-check", "namespace": "ops", "description": "Check artifacts.",
        }])
        self.assertEqual(evidence["skills"]["used"], ["release-check"])
        sent = server.call_args.kwargs["payload"]
        self.assertEqual(sent["items"][0]["evidence_hash"], "b" * 64)
        self.assertEqual(sent["items"][0]["source_hash"], "a" * 64)
        self.assertNotIn("Check artifacts.", json.dumps(sent))

    def test_local_offer_rejects_changed_evidence_before_invoking_model(self) -> None:
        key = "e" * 64
        metadata = {
            "runtime": "codex", "subject_key": key, "digest": evidence_digest(key, self.turns),
            "recall_session_key": "f" * 64, "captured_at": "2026-11-20T00:00:00+00:00",
        }
        changed = self.question | {"routing": as_object(self.question["routing"]) | {"evidence_hash": "c" * 64}}
        capture = {"skill_capture": {"installations": []}}
        with (
            patch.object(knowledge_score_offer, "local_evidence", return_value=(metadata, self.turns)),
            patch.object(knowledge_score_offer, "local_skill_metadata", return_value=capture),
            patch.object(knowledge_score_offer, "call", side_effect=[
                {"status": "ask", "items": [self.question]}, {"status": "ask", "items": [changed]},
            ]) as server,
            patch.object(knowledge_score_offer, "grade_rubric") as judge,
        ):
            preview = self._run("score-offer", "--session", str(uuid4()), "--scorer", self.name)
            self.assertEqual(preview["items"][0]["skill"]["version_id"], self.version)
            with self.assertRaisesRegex(CollabError, "评分标准或策略已变化"):
                self._run("score-offer", "--confirm", str(preview["confirm"]))
        self.assertIn("skill_capture", server.call_args.kwargs["payload"]["subject"])
        judge.assert_not_called()

    def test_default_offer_rechecks_routing_switch_before_model_invocation(self) -> None:
        with (
            patch.object(knowledge_score_offer, "call", side_effect=[
                {"id": self.archive, "revision": 1, "digest": "d" * 64},
                {"status": "ask", "items": [self.question]}, {"status": "off", "items": []},
            ]) as server,
            patch.object(knowledge_score_offer, "grade_rubric") as judge,
        ):
            preview = self._run("score-offer", "--archive", self.archive, "--revision", "1")
            self.assertEqual(preview["items"][0]["name"], self.name)
            with self.assertRaisesRegex(CollabError, "评分标准或策略已变化"):
                self._run("score-offer", "--confirm", str(preview["confirm"]))
        self.assertTrue(server.call_args_list[1].kwargs["payload"]["accept_routing"])
        self.assertTrue(server.call_args.kwargs["payload"]["recommended_routing"])
        judge.assert_not_called()

    def test_drainer_returns_routing_labels_keyed_by_generated_name(self) -> None:
        work: dict[str, JsonValue] = {
            "archive": {"id": self.archive, "revision": 1, "digest": "d" * 64},
            "routing_batch": [self.question],
        }
        with (
            patch.object(knowledge_score_work, "archived_turns", return_value=self.turns),
            patch.object(knowledge_session_scoring, "_answer", return_value='{"label":"fit"}'),
        ):
            outcome = knowledge_score_work._attempt_outcome(
                self.client, self.project, work, lambda: True, {}, lambda _: self.project,
            )
        self.assertEqual(outcome, {"labels": {self.name: "fit"}})

    def test_mixed_drainer_invokes_model_once_and_submits_all_labels(self) -> None:
        operation, check, adoption = uuid4(), str(uuid4()), str(uuid4())
        adoption_id = str(uuid4())
        work: dict[str, JsonValue] = {
            "status": "open", "attempt_id": str(operation),
            "archive": {"id": self.archive, "revision": 1, "digest": "d" * 64},
            "mixed_batch": [
                {"attempt_id": check, "scorer": {
                    "name": "quality", "prompt": "Check quality.", "passing_score": 1,
                    "labels": [{"value": "pass", "score": 1}, {"value": "fail", "score": 0}],
                }},
                {"attempt_id": adoption, "adoption": {
                    "id": adoption_id, "revision": 1, "excerpt": "Use the release checklist.",
                }},
                self.question | {"attempt_id": self.name},
            ],
        }
        answer: dict[str, dict[str, str]] = {}

        def judge_group(driver: str, prompt: str, schema: str, **options: object) -> str:
            group = json.loads(prompt.split("\n", 1)[1])
            self.assertEqual(driver, "codex")
            self.assertEqual(group["transcript"], self.turns)
            rubrics = {row["name"]: row for row in group["rubrics"]}
            recall_name = next(name for name in rubrics if name.startswith("teamai:adoption:"))
            self.assertEqual(rubrics[recall_name]["evidence_files"][0]["content"], "Use the release checklist.")
            self.assertEqual(rubrics[self.name]["evidence_files"][0]["path"], "evidence.json")
            answer.update({
                "quality": {"label": "pass"}, recall_name: {"label": "adopted"}, self.name: {"label": "fit"},
            })
            self.assertEqual(set(json.loads(schema)["properties"]), set(answer))
            return json.dumps(answer)

        with (
            patch.object(knowledge_score_work, "uuid4", return_value=operation),
            patch.object(knowledge_score_work, "call", side_effect=[work, {"status": "completed"}]) as server,
            patch.object(knowledge_score_work, "archived_turns", return_value=self.turns),
            patch.object(knowledge_session_scoring, "_answer", side_effect=judge_group) as judge,
        ):
            result = self._run("score-drain", "--limit", "1")
        self.assertEqual(result["status"], "processed")
        judge.assert_called_once()
        self.assertTrue(server.call_args_list[0].kwargs["payload"]["accept_routing_mix"])
        self.assertEqual(server.call_args.args[2], f"/session-scoring/routing/{operation}/settle")
        self.assertEqual(server.call_args.kwargs["payload"], {"labels": {
            check: "pass", adoption: "adopted", self.name: "fit",
        }})
