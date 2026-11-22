"""验证 Swift 的模块范围、词法遮蔽及调用关系的发布。"""

from uuid import UUID, uuid5

import pytest

from tests.api.test_projects import project
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.knowledge.code_preview import relation_delta
from weora_code_graph.extractor import extract, resolved_calls

pytestmark = pytest.mark.anyio


def test_swift_calls_resolve_module_declarations_and_skip_shadowing() -> None:
    source = "Packages/A/Sources/Billing/Start.swift"
    syntax = """func free() {
    schedule(); Runner.make(); Runner.load(); Runner.secret()
    ActorWorker(); hidden(); Unrelated(); Duplicate(); Tools.deep.schedule()
}
func parameters(_ schedule: () -> Void, _ Runner: Local) {
    schedule(); Runner.make()
}
func patterns() {
    if let schedule = choose() { schedule() } else { schedule() }
    for (schedule, other) in jobs { schedule() }
    switch value { case .success(let schedule): schedule(); default: schedule() }
    do { attempt() } catch let schedule { schedule() }
    let tuple = { [schedule = schedule()] in schedule() }
    let parameter = { (schedule: () -> Void) in schedule() }
    guard let schedule = choose() else { schedule(); return }
    schedule()
}
struct Local { func run() { schedule(); Runner.make() } }
func another() { let (schedule, other) = localPair(); schedule() }
func ordinary() { schedule() }
func typedParameter(_ value: Runner) { Runner.make() }
"""
    module = "Packages/A/Sources/Billing"
    function_file, runner_file, extension = f"{module}/Work.swift", f"{module}/Runner.swift", f"{module}/Overlay.swift"
    files = {
        source: syntax,
        function_file: "func schedule() {}\nprivate func hidden() {}\nfunc Duplicate() {}\n",
        runner_file: """
public final class Runner {
    static func make() {}
    private static func secret() {}
}
actor ActorWorker {}
""",
        extension: """extension Runner { static func load() {} }
struct ContainsMembers { let schedule: () -> Void }
func Duplicate() {}
""",
        "Packages/B/Sources/Billing/Other.swift": "func Unrelated() {}\nfunc schedule() {}",
        "Packages/A/Tests/Billing/Test.swift": "func schedule() {}\nfunc Unrelated() {}",
        "Playground/Free.swift": "func go() { schedule() }",
        f"{module}/Selective.swift": (
            "import func Vendor.schedule\nimport struct Vendor.Runner\nfunc use() { schedule(); Runner.make() }"
        ),
    }
    facts = {path: extract(path, "fixed", body) for path, body in files.items()}
    calls = resolved_calls(source, facts, set(files))
    assert [(call["line"], call["callee"], call["target"]) for call in calls] == [
        (2, "schedule", function_file),
        (2, "Runner.make", runner_file),
        (2, "Runner.load", extension),
        (3, "ActorWorker", runner_file),
        (9, "schedule", function_file),
        (11, "schedule", function_file),
        (13, "schedule", function_file),
        (15, "schedule", function_file),
        (18, "Runner.make", runner_file),
        (20, "schedule", function_file),
        (21, "Runner.make", runner_file),
    ]
    assert {call["confidence"] for call in calls} == {"INFERRED"}
    assert resolved_calls("Playground/Free.swift", facts, set(facts)) == []
    assert resolved_calls(f"{module}/Selective.swift", facts, set(facts)) == []
    # 删除文件足以让未修改调用者的关系出现在评审差异里。
    after = {path: evidence for path, evidence in facts.items() if path != function_file}
    delta = relation_delta(facts, after)["relations"]
    assert isinstance(delta, dict)
    assert {"kind": "REFERENCES", "source": source, "target": function_file} in delta["removed"]


async def test_swift_call_evidence_publishes_and_invalidates_when_target_changes(runs: RunsHarness) -> None:
    scope = {"project_id": str((await project(runs, "Swift 调用索引"))["id"])}
    root = f"/api/v1/workspaces/{runs.workspace_id}/knowledge"
    caller, peer = "Sources/App/Workflow.swift", "Sources/App/Executor.swift"

    async def index(body: str) -> tuple[UUID, int]:
        files = {
            caller: "func planLedgerWorkflow() { submitLedger(); Executor.make() }\n",
            peer: body,
            "Tests/App/Executor.swift": "func submitLedger() {}\n",
            "Packages/Other/Sources/App/Executor.swift": "func submitLedger() {}\n",
        }
        response = await runs.client.post(
            f"{root}/codebases/local", params=scope, headers=runs.headers(), json={
                "source_key": "f" * 64, "name": "Swift 构建模块", "incremental": True, "candidates": len(files),
                "files": [{"path": path, "text": value} for path, value in files.items()],
            },
        )
        assert response.status_code == 200, response.text
        return UUID(response.json()["codebase_id"]), int(response.json()["reused"])

    async def get(entry: str) -> dict[str, object]:
        response = await runs.client.get(f"{root}/{entry}", params=scope)
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    repository, _ = await index("func submitLedger() {}\nstruct Executor { static func make() {} }\n")
    identity, target = str(uuid5(repository, caller)), str(uuid5(repository, peer))
    original = await get(identity)
    relations = original["relations"]
    assert isinstance(relations, list)
    assert ("REFERENCES", target) in {(item["kind"], item["target"]) for item in relations}
    assert all(call in str(original["body"]) for call in ("submitLedger()", "Executor.make()", "INFERRED"))
    recall = await runs.client.get(root, params=scope | {"query": "planLedgerWorkflow", "recall_mode": True})
    found = next(row for row in recall.json()["items"] if row["id"] == identity)
    assert any(item["kind"] == "REFERENCES" and item["target"] == target for item in found["relations"])

    _, reused = await index("private func submitLedger() {}\nprivate struct Executor {}\n")
    assert reused == 3
    current = await get(identity)
    links = current["relations"]
    assert isinstance(links, list)
    assert all(item["kind"] != "REFERENCES" for item in links)
    assert "Executor.make()" not in str(current["body"])
