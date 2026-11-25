"""受限评分先检查版本，未通过前不传入证据。"""

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from weora_cli import knowledge_classifier, knowledge_judge_isolation, knowledge_session_scoring
from weora_cli.collab_client import CollabError
from weora_cli.commands import JsonValue


class JudgeIsolationTests(unittest.TestCase):
    def test_unsupported_version_never_receives_model_prompt(self) -> None:
        version = subprocess.CompletedProcess(["codex", "--version"], 0, "codex-cli 0.144.2\n", "")
        with (
            patch.object(knowledge_classifier.shutil, "which", return_value="codex"),
            patch.object(knowledge_judge_isolation.subprocess, "run", return_value=version) as run,
            self.assertRaisesRegex(CollabError, "verified"),
        ):
            knowledge_classifier._answer("codex", "synthetic evidence", "{}", verified=True)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-1], "--version")
        self.assertNotIn("input", run.call_args.kwargs)

    def test_verified_profiles_remove_tools_and_filter_environment(self) -> None:
        for driver, version in (("codex", "codex-cli 0.163.0"), ("claude", "2.1.308 (Claude Code)")):
            with self.subTest(driver=driver):
                def run_client(args, *, chosen_driver=driver, chosen_version=version, **kwargs):
                    self.assertNotIn("WEORA_COLLAB_TOKEN", kwargs["env"])
                    self.assertNotIn("FIXTURE_SECRET", kwargs["env"])
                    if "--version" in args:
                        return subprocess.CompletedProcess(args, 0, chosen_version + "\n", "")
                    self.assertEqual(kwargs["input"], "synthetic evidence")
                    if chosen_driver == "codex":
                        self.assertIn("--ignore-user-config", args)
                        self.assertIn("read-only", args)
                        self.assertIn("shell_tool", args)
                        self.assertIn("browser_use", args)
                        (Path(kwargs["cwd"]) / "answer.json").write_text('{"label":"pass"}', encoding="utf-8")
                        return subprocess.CompletedProcess(args, 0, "", "")
                    self.assertIn("--restricted", args)
                    self.assertIn("--tools=", args)
                    self.assertIn("--strict-mcp-config", args)
                    return subprocess.CompletedProcess(args, 0, '{"structured_output":{"label":"pass"}}', "")

                with (
                    patch.dict(os.environ, {"WEORA_COLLAB_TOKEN": "test-token", "FIXTURE_SECRET": "synthetic"}),
                    patch.object(knowledge_classifier.shutil, "which", return_value=driver),
                    patch.object(knowledge_classifier.subprocess, "run", side_effect=run_client) as launch,
                ):
                    result = knowledge_classifier._answer(driver, "synthetic evidence", "{}", verified=True)
                self.assertEqual(json.loads(result), {"label": "pass"})
                self.assertEqual(launch.call_count, 2)

    def test_one_verified_rubric_requires_verified_for_the_whole_batch(self) -> None:
        rubric: dict[str, JsonValue] = {
            "prompt": "Check", "passing_score": 1, "labels": [{"value": "pass", "score": 1}],
        }
        pair = [rubric | {"name": "first", "isolation": "best-effort"},
                rubric | {"name": "second", "isolation": "verified"}]
        with patch.object(knowledge_session_scoring, "_answer",
                          return_value='{"first":{"label":"pass"},"second":{"label":"pass"}}') as answer:
            outcomes = knowledge_session_scoring.grade_batch(pair, [])
        self.assertTrue(answer.call_args.kwargs["verified"])
        self.assertEqual([row["isolation"] for row in outcomes.values()], ["verified", "verified"])
