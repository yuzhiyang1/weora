"""两套真实 CLI 回归。模型提供固定 SSE，读取、脚本、写入、测试和 Diff 在容器中发生。"""

import asyncio
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from aiohttp import web

from tests.api.test_projects import project, repository
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from weora.modules.agents.application.ports import RuntimeSkillPackage
from weora.modules.projects.domain import RepositoryArchive
from weora.modules.runs.application.execution import RunExecutor, RunLease, RuntimeInput, RuntimeSession
from weora.modules.runs.application.harness import OMP_RPC_RUNTIME, PI_RPC_RUNTIME
from weora.modules.runs.application.ports import ExecutionProfile, RunActor, RunRecord
from weora.modules.runs.infrastructure.native_rpc import NativeCodingRuntime, NativeSandbox, skill_tar
from weora.modules.tools.application.gateway import ToolGateway, ToolScope, ToolSpec

ROOT = Path(__file__).resolve().parents[3]


def published_skill() -> RuntimeSkillPackage:
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr(
            "rpc-proof/SKILL.md", "---\nname: rpc-proof\ndescription: Checks RPC proof.\n---\nRun scripts/check.sh.\n"
        )
        archive.writestr("rpc-proof/scripts/check.sh", '#!/bin/sh\ncat "$(dirname "$0")/../references/proof.txt"\n')
        archive.writestr("rpc-proof/references/proof.txt", "native-skill-proof\n")
    return RuntimeSkillPackage("rpc-proof", data.getvalue())


def test_skill_package_keeps_script_and_reference() -> None:
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(skill_tar((published_skill(),)))) as archive:
        assert archive.getmember("rpc-proof/scripts/check.sh").mode == 0o555
        reference = archive.extractfile("rpc-proof/references/proof.txt")
        assert reference is not None and reference.read() == b"native-skill-proof\n"


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize("engine", [PI_RPC_RUNTIME, OMP_RPC_RUNTIME])
async def test_native_rpc_with_real_weora_checkout(
    engine: str,
    tmp_path: Path,
    runs: RunsHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = os.getenv("WEORA_TEST_NATIVE_RPC_IMAGE")
    if not image:
        pytest.skip("配置 WEORA_TEST_NATIVE_RPC_IMAGE 后运行两套原生引擎")
    command = tuple(shlex.split(os.getenv("WEORA_TEST_NATIVE_RPC_DOCKER", "docker")))
    profile = ExecutionProfile(engine, "deepseek", "deepseek-chat", 2048, 40, 120)

    async def select_profile(_: RunActor) -> ExecutionProfile:
        return profile

    service = runs.app.state.run_service
    monkeypatch.setattr(service, "_private_project_profile", select_profile)
    actual_project = await project(runs, "Weora RPC 验收")
    selected = await repository(runs, actual_project["id"], "weora-oss")
    runs.app.state.project_service.reader = object()  # 归档由测试固定到实际本地 Git HEAD。
    source = runs.payload("原始任务") | {"project_id": actual_project["id"], "repository_ids": [selected["id"]]}
    submitted = await runs.client.post(runs.path, json=source, headers=runs.headers("rpc-coding"))
    assert submitted.status_code == 201, submitted.text
    run_id = UUID(submitted.json()["id"])
    workspace_id = UUID(runs.workspace_id)
    sandbox = NativeSandbox(command, image, workspace_id, UUID(submitted.json()["task_id"]))
    checkout = Path(os.getenv("WEORA_TEST_NATIVE_RPC_REPOSITORY", str(ROOT)))
    revision = (
        await asyncio.to_thread(subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=checkout, text=True)
    ).strip()
    repo_id = UUID(str(selected["id"]))
    sequence = 0
    last_input: tuple[tuple[object, ...], ...] | None = None
    tools_called: list[str] = []
    diffs: list[str] = []
    events: list[str] = []
    received: list[str] = []
    model_inputs: list[str] = []
    request_shapes: list[tuple[object, object, object]] = []
    failures: list[BaseException] = []
    stop_wait = asyncio.Event()
    phase = "coding"
    readonly_step = 0

    async def provider(request: web.Request) -> web.StreamResponse:
        nonlocal sequence, last_input, readonly_step
        try:
            data = await request.json()
            auxiliary = not data.get("tools")
            request_shapes.append((data.get("stream"), data.get("thinking"), data.get("stream_options")))
            if not auxiliary:
                model_inputs.append(json.dumps(data, ensure_ascii=False))
                # 断言真实引擎送往模型的 system，而非只检查桥接代码里存在这个字符串。
                system = "\n".join(
                    str(item.get("content", "")) for item in data.get("messages", []) if item.get("role") == "system"
                )
                assert revision in system and str(repo_id) in system, system
                assert "git HEAD 是隔离基线提交" in system, system
                manifest_match = re.search(r"(/tmp/weora-engine-[^\s]+/source-revisions\.json)", system)
                assert manifest_match is not None, system
                source_manifest = manifest_match.group(1)
            messages = tuple(
                (
                    item.get("role"),
                    item.get("tool_call_id"),
                    str(item.get("content")) if item.get("role") == "tool" else "",
                )
                for item in data.get("messages", [])
                if item.get("role") != "system"
            )
            if not auxiliary and messages != last_input:
                sequence += 1
                last_input = messages
                if phase == "readonly":
                    readonly_step += 1
            if phase == "stop" and not auxiliary:
                stop_wait.set()
                await asyncio.sleep(20)
            names = {item["function"]["name"] for item in data.get("tools", [])}
            tool: tuple[str, dict[str, str]] | None = None
            if auxiliary:
                # OMP uses its model to summarize discovered skill routing descriptions.
                pass
            elif phase == "readonly":
                # 从真实引擎发出的 schema 验证只读能力，不用 Python mock 代替 CLI 参数生效。
                assert not {"edit", "write", "bash"} & names, sorted(names)
                assert "read" in names, sorted(names)
                assert "原始任务" in model_inputs[-1] and "续接" in model_inputs[-1]
                host = next((name for name in names if name.endswith("proof")), None)
                assert host is not None, sorted(names)
                if readonly_step == 1:
                    tool = ("read", {"path": "backend/src/weora/rpc_proof.py"})
                elif readonly_step == 2:
                    assert "PROOF = 42" in model_inputs[-1]
                    tool = (host, {"message": "tool-proof"})
                else:
                    assert "tool-proof" in model_inputs[-1]
            elif phase == "after-readonly":
                # 只读分支可恢复旧历史，但其新消息不能污染主会话的后续执行。
                assert "read" in names and "bash" in names and "write" in names, sorted(names)
                assert "只读探针" not in model_inputs[-1]
                assert "原始任务" in model_inputs[-1] and "主会话恢复" in model_inputs[-1]
            elif sequence == 1:
                assert "rpc-proof" in model_inputs[-1]
                skill = "$(find \"$PI_CODING_AGENT_DIR/skills\" -path '*/scripts/check.sh' -print -quit)"
                tool = ("bash", {"command": f'sh "{skill}" && cat {shlex.quote(source_manifest)}'})
            elif sequence == 2:
                # 原生工具实际读回清单后，其持久消息必须独立带有源版本，不能只靠提示词旁白。
                tool_results = "\n".join(
                    str(item.get("content", "")) for item in data.get("messages", []) if item.get("role") == "tool"
                )
                assert revision in tool_results and str(repo_id) in tool_results, tool_results
                assert "source_revision" in tool_results, tool_results
                tool = ("read", {"path": "backend/src/weora/bootstrap/run_profile.py"})
            elif phase == "coding" and sequence == 3:
                results = [
                    (item["role"], item.get("tool_calls"), str(item.get("content"))[:160])
                    for item in data["messages"]
                    if item["role"] != "system"
                ]
                assert "native-skill-proof" in model_inputs[-1], (events, results, request_shapes)
                tool = ("bash", {"command": "sleep 2"})
            elif sequence == 4:
                assert "验收插话" in "".join(model_inputs)
                name = next((name for name in names if name.endswith("proof")), None)
                assert name is not None, sorted(names)
                tool = (name, {"message": "tool-proof"})
            elif sequence == 5:
                assert "tool-proof" in model_inputs[-1]
                tool = ("write", {"path": "backend/src/weora/rpc_proof.py", "content": "PROOF = 42\n"})
            elif sequence == 6:
                tool = (
                    "bash",
                    {"command": "python3 -m py_compile backend/src/weora/rpc_proof.py && git status --short"},
                )
            elif phase == "resume":
                assert "原始任务" in model_inputs[-1] and "续接" in model_inputs[-1]
            finish = "tool_calls" if tool else "stop"
            delta: dict[str, object] = {"role": "assistant", "content": "当前轮验证完成"}
            if tool:
                arguments = tool[1] | ({"i": "verify native coding flow"} if engine == OMP_RPC_RUNTIME else {})
                delta = {
                    "role": "assistant",
                    "reasoning_content": "Use the requested coding tool.",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{sequence}",
                            "type": "function",
                            "function": {"name": tool[0], "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            if data.get("stream") is not True:
                return web.json_response(
                    {
                        "id": f"message-{sequence}",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": profile.model_id,
                        "choices": [{"index": 0, "message": delta, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 15, "total_tokens": 115},
                    }
                )
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            segments = [(delta, finish)] if engine == OMP_RPC_RUNTIME else [(delta, None), ({}, finish)]
            for frame_delta, reason in segments:
                item = {
                    "id": f"message-{sequence}",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": profile.model_id,
                    "choices": [{"index": 0, "delta": frame_delta, "finish_reason": reason}],
                }
                await response.write(f"data: {json.dumps(item)}\n\n".encode())
                await asyncio.sleep(0.01)
            if engine == OMP_RPC_RUNTIME:
                # OMP's transport accounts for trailing usage before finalizing a turn.
                usage = {
                    "id": f"message-{sequence}",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": profile.model_id,
                    "choices": [],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 15, "total_tokens": 115},
                }
                await response.write(f"data: {json.dumps(usage)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response
        except AssertionError as error:
            failures.append(error)
            raise web.HTTPBadRequest() from None

    async def archives() -> dict[UUID, RepositoryArchive]:
        path = tmp_path / f"{uuid4()}.tar"
        await asyncio.to_thread(
            subprocess.run,
            ["git", "archive", "--format=tar", "-o", str(path), revision],
            cwd=checkout,
            check=True,
        )
        return {repo_id: RepositoryArchive(path, revision, hashlib.sha256(path.read_bytes()).hexdigest())}

    async def check_access() -> bool:
        return True

    async def diff(repository: UUID, actual_revision: str, text: str, truncated: bool) -> None:
        assert repository == repo_id and actual_revision == revision and not truncated
        diffs.append(text)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", provider)
    runner = web.AppRunner(app, shutdown_timeout=0.2)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    model_origin = f"http://127.0.0.1:{runner.addresses[0][1]}"

    actor = RunActor(UUID(runs.user_id), workspace_id)
    store = runs.app.state.execution_store
    gateway: ToolGateway = runs.app.state.tool_gateway

    def runtime(lease: RunLease) -> NativeCodingRuntime:
        session: RuntimeSession = service.console_session(lease)

        async def call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
            scope = ToolScope(lease.run.id, actor, lease.owner, lease.fencing_token, runtime_id=engine)
            return await gateway.invoke(scope, call_id, name, arguments)

        async def progress(event: str) -> None:
            events.append(event)
            if phase == "coding" and event == "native.tool.read.finished":
                reply = await runs.client.post(
                    f"{runs.path}/{run_id}/continue",
                    headers=runs.headers("rpc-steer"),
                    json={"mode": "steer", "instruction": "验收插话"},
                )
                assert reply.status_code == 202, reply.text
                received.append(reply.json()["input_id"])

        async def publish(repo: UUID, rev: str, text: str, truncated: bool) -> None:
            await diff(repo, rev, text, truncated)
            if text:
                await runs.app.state.delivery_service.record_native_diff(
                    actor,
                    lease.run.id,
                    repo,
                    rev,
                    text,
                    truncated,
                )

        return NativeCodingRuntime(
            replace(sandbox, read_only=phase == "readonly"),
            key="rpc-test-key",
            session=session,
            call=call,
            load_repositories=archives,
            check_access=check_access,
            progress=progress,
            diff=publish,
            test_model_origin=model_origin,
        )

    skill_packages = (published_skill(),)
    spec = ToolSpec(
        "internal.proof",
        "Verify the Weora tool bridge.",
        {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    )

    async def proof(_: ToolScope, arguments: dict[str, object]) -> dict[str, object]:
        assert arguments == {"message": "tool-proof"}
        tools_called.append(spec.name)
        return {"ok": True, "echo": "tool-proof"}

    gateway.register(spec, proof)

    class AcceptanceInput:
        async def is_active(self, actor: RunActor) -> bool:
            return True

        async def load_input(self, run: RunRecord) -> RuntimeInput:
            detail = await service.access_metadata(actor, run.id)
            return RuntimeInput(
                "Follow the test protocol.",
                detail.task.instruction,
                (spec,),
                skill_packages=skill_packages,
            )

    async def execute(identity: UUID) -> bool:
        # 每轮使用新的执行者与容器，从 Task 卷恢复原生运行。
        executor = RunExecutor(
            store=store,
            access=AcceptanceInput(),
            runtime=runtime_placeholder,
            profile=profile,
            runtime_factory=runtime,
            heartbeat_seconds=0.5,
        )
        return await executor.execute(workspace_id=workspace_id, run_id=identity, owner=str(uuid4()))

    class UnexpectedRuntime:
        async def run(self, payload: RuntimeInput, profile: ExecutionProfile) -> str:
            raise AssertionError("冻结的任务配置必须选择原生 RPC")

    runtime_placeholder = UnexpectedRuntime()

    async def continue_with(anchor: UUID, instruction: str) -> UUID:
        response = await runs.client.post(
            f"{runs.path}/{anchor}/continue",
            headers=runs.headers("rpc-" + str(uuid4())),
            json={"instruction": instruction},
        )
        assert response.status_code == 202, response.text
        return UUID(response.json()["run_id"])

    try:
        async with asyncio.timeout(180):
            try:
                assert await execute(run_id)
            except Exception:
                if failures:
                    raise failures[0] from None
                raise
            if failures:
                raise failures[0]
            completed = (await runs.client.get(f"{runs.path}/{run_id}")).json()
            assert completed["status"] == "SUCCEEDED", completed.get("failure_code")
            assert tools_called == ["internal.proof"]
            calls = (await runs.client.get(f"{runs.path}/{run_id}/tools")).json()["items"]
            assert any(call["name"] == spec.name and call["result"]["echo"] == "tool-proof" for call in calls)
            inputs = (await runs.client.get(f"{runs.path}/{run_id}/inputs")).json()["items"]
            assert any(item["input_id"] in received and item["status"] == "READ" for item in inputs), inputs
            assert "native.tool.write.finished" in events and "native.tool.bash.finished" in events
            assert "+PROOF = 42" in diffs[-1]
            artifacts = (await runs.client.get(f"{runs.path}/{run_id}/artifacts")).json()["items"]
            assert any(item["kind"] == "diff" and "+PROOF = 42" in item["body"] for item in artifacts)
            phase = "stop"
            stop_id = await continue_with(run_id, "停止探针")
            stopped = asyncio.create_task(execute(stop_id))
            try:
                await asyncio.wait_for(stop_wait.wait(), 45)
                response = await runs.client.post(f"{runs.path}/{stop_id}/cancel", headers=runs.headers())
                assert response.status_code == 200, response.text
                assert await asyncio.wait_for(stopped, 18)
            finally:
                if not stopped.done():
                    stopped.cancel()
                    await asyncio.gather(stopped, return_exceptions=True)
            assert (await runs.client.get(f"{runs.path}/{stop_id}")).json()["status"] == "CANCELLED"
            phase = "resume"
            resumed = await continue_with(stop_id, "续接")
            assert await execute(resumed)
            after = (await runs.client.get(f"{runs.path}/{resumed}")).json()
            assert after["status"] == "SUCCEEDED", after.get("failure_code")
            assert events.count("native.resumed") == 2
            assert "+PROOF = 42" in diffs[-1]
            # 原工作卷及 JSONL 均已存在，再使用只读挂载，避免仅覆盖空卷启动的假阳性。
            phase = "readonly"
            prior_diffs = len(diffs)
            observation = await continue_with(resumed, "只读探针")
            assert await execute(observation)
            if failures:
                raise failures[0]
            observed = (await runs.client.get(f"{runs.path}/{observation}")).json()
            assert observed["status"] == "SUCCEEDED", observed.get("failure_code")
            assert readonly_step >= 3 and tools_called == ["internal.proof", "internal.proof"]
            assert len(diffs) == prior_diffs, "只读轮不得发布修改证据"
            readonly_calls = (await runs.client.get(f"{runs.path}/{observation}/tools")).json()["items"]
            assert any(call["name"] == spec.name and call["result"]["echo"] == "tool-proof" for call in readonly_calls)
            phase = "after-readonly"
            continued = await continue_with(observation, "主会话恢复")
            assert await execute(continued)
            if failures:
                raise failures[0]
            final = (await runs.client.get(f"{runs.path}/{continued}")).json()
            assert final["status"] == "SUCCEEDED", final.get("failure_code")
            assert events.count("native.resumed") == 4
            assert "+PROOF = 42" in diffs[-1]
            # 从独立只读挂载读取 JSONL，证明源版本在原生工具结果内持久保存，不只存在于 system。
            inspect_history = """
const fs = require('node:fs');
const [revision, repository] = process.argv.slice(1);
const records = fs.readdirSync('/proof/sessions').filter(name => name.endsWith('.jsonl'))
  .flatMap(name => fs.readFileSync('/proof/sessions/' + name, 'utf8').trim().split('\\n'))
  .map(line => JSON.parse(line));
const proof = records.some(record => {
  const message = record.message;
  if (!message || message.role !== 'toolResult') return false;
  const body = JSON.stringify(message.content);
  return body.includes('source_revision') && body.includes(revision) && body.includes(repository);
});
if (!proof) throw new Error('SOURCE_REVISION_TOOL_RESULT_NOT_PERSISTED');
console.log('source-revision-persisted');
"""
            inspection = await asyncio.create_subprocess_exec(
                *command, "run", "--rm", "--network", "none", "--user", "0:0",
                "--mount", f"type=volume,source={sandbox.volume},target=/proof,readonly",
                "--entrypoint", "node", image, "-e", inspect_history, revision, str(repo_id),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            output, error = await inspection.communicate()
            assert inspection.returncode == 0, error.decode(errors="replace")
            assert output.strip() == b"source-revision-persisted"
    finally:
        await runner.cleanup()
        clean = await asyncio.create_subprocess_exec(
            *command, "volume", "rm", sandbox.volume, sandbox.volume + "-readonly",
        )
        await clean.wait()
