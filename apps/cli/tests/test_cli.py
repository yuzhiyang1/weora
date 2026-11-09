"""验证真实进程的发现协议，避免只测内部函数而漏掉入口和退出码。"""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from importlib.metadata import version
from unittest.mock import patch

from weora_cli.cli import main


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "weora_cli", *args],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=10,
        )

    def test_help_is_readable_without_backend(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("schema", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_version_matches_installed_package(self) -> None:
        result = self.run_cli("version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "ok": True,
                "data": {"version": version("weora-cli"), "protocol_version": 1},
            },
        )

    def test_discovery_does_not_advertise_unavailable_business_tools(self) -> None:
        result = self.run_cli("commands")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)["data"]
        self.assertEqual([command["name"] for command in data["commands"]], ["version", "commands", "schema"])
        self.assertEqual(data["business_capabilities"], [])
        self.assertTrue(all(not command["side_effects"] for command in data["commands"]))

    def test_schema_and_discovery_use_the_same_contract(self) -> None:
        discovered = json.loads(self.run_cli("commands").stdout)["data"]["commands"]
        schemas = json.loads(self.run_cli("schema").stdout)["data"]["commands"]
        self.assertEqual(discovered, schemas)

    def test_schema_for_specific_command(self) -> None:
        result = self.run_cli("schema", "schema")
        self.assertEqual(result.returncode, 0)
        commands = json.loads(result.stdout)["data"]["commands"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["input_schema"]["properties"]["command"]["type"], "array")

    def test_invalid_invocations_are_json_errors_on_stderr(self) -> None:
        for args in [(), ("memory", "create"), ("schema", "memory", "create"), ("version", "--unknown")]:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                error = json.loads(result.stderr)
                self.assertFalse(error["ok"])
                self.assertEqual(error["error"]["code"], "INVALID_ARGUMENT")

    def test_internal_error_does_not_leak_exception(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("weora_cli.cli.execute", side_effect=RuntimeError("secret-value")),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["version"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("secret-value", stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "INTERNAL_ERROR")


if __name__ == "__main__":
    unittest.main()
