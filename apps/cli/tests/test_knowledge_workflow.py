"""覆盖公开 CLI 入口和团队工作流的名称、发起参数。"""

from unittest.mock import patch
from uuid import uuid4

from weora_cli import cli, knowledge, knowledge_workflow
from weora_cli.collab_client import Client


def test_start_preserves_top_level_command_and_pins_operation() -> None:
    operation, session, workspace, project, run = (str(uuid4()) for _ in range(5))
    command = cli.build_parser().parse_args([
        "context", "workflow", "start", "share-notes", "--runtime", "codex", "--session", session,
        "--project", project, "--operation", operation, "--hitl", "person",
    ])
    assert command.operation == "context"
    client = Client("https://example.test", workspace, "local-fixture")
    with (
        patch.object(knowledge, "paired_client", return_value=client),
        patch.object(knowledge_workflow, "configured_mode", return_value="attended"),
        patch.object(knowledge_workflow, "call", side_effect=[
            {"definition": {"name": knowledge_workflow.SHARE_LEARNINGS, "alias": "share-notes"}},
            {"id": run, "launch": "pending"},
        ]) as transport,
    ):
        result = cli.execute(command)
    args, kwargs = transport.call_args
    assert args == (client, project, "/workflows/teamai-share-learnings/runs")
    assert kwargs["payload"] == {
        "operation_id": operation, "runtime": "codex", "session": session, "hitl": "person",
        "unattended": False, "publish": False, "scorers": [],
    }
    assert result == {
        "id": run, "launch": "pending",
        "approval_url": f"https://example.test/knowledge/workflows/{run}?workspace={workspace}&project={project}",
    }
