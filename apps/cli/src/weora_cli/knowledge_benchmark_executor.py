"""用固定 Git 对象和两个隔离容器完成一次基准试验。"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from weora_cli.collab_client import Client, CollabError, as_object, items, lock_file, write_json
from weora_cli.commands import JsonValue
from weora_cli.knowledge_api import call
from weora_cli.knowledge_repo_import import repository_plan
from weora_cli.knowledge_session_origin import checkout_remote, commits_in_checkout
from weora_cli.knowledge_settings import session_state_root

_IMAGE = re.compile(r"(?:[a-z0-9][a-z0-9_./:-]*@)?sha256:[a-f0-9]{64}")
_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
_OBJECT = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,159}")
_ENTRY = Path(__file__).with_name("benchmark_native.py").resolve()


def _env(*, git: bool = False) -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "HOME"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if git:
        result.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0",
        })
    return result


def _clone(source: Path, commit: str, destination: Path) -> None:
    if not _OBJECT.fullmatch(commit):
        raise CollabError("KNOWLEDGE_BENCHMARK_START", "基准计划的提交标识无效。")
    destination.mkdir()
    git = shutil.which("git")
    if git is None:
        raise CollabError("KNOWLEDGE_BENCHMARK_GIT", "当前机器没有 Git。")
    fmt = "sha1" if len(commit) == 40 else "sha256"
    commands = (
        ["init", "-q", "--template=", f"--object-format={fmt}"],
        ["fetch", "-q", "--depth=1", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
         str(source.resolve()), commit],
        ["-c", "core.hooksPath=/dev/null", "checkout", "-q", "--detach", commit],
    )
    try:
        for args in commands:
            subprocess.run([git, "-C", str(destination), *args], env=_env(git=True), check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise CollabError("KNOWLEDGE_BENCHMARK_CHECKOUT", "无法从本地对象建立临时检出。") from error


def _assets(checkout: Path, target: Path) -> None:
    managed = checkout / ".teamai"
    if managed.is_symlink() or managed.is_junction():
        raise CollabError("KNOWLEDGE_BENCHMARK_RESOURCES", "基准资源根目录不能使用链接。")
    root = managed if managed.is_dir() else checkout
    target.mkdir()
    total = 0
    paths = [root / name for name in ("skills", "rules", "agents", "AGENTS.md", "CLAUDE.md")]
    while paths:
        path = paths.pop()
        if path.is_symlink() or path.is_junction():
            raise CollabError("KNOWLEDGE_BENCHMARK_RESOURCES", "基准资源不能通过链接读取。")
        if path.is_dir():
            paths.extend(path.iterdir())
        elif path.is_file():
            total += path.stat().st_size
            if total > 32_000_000:
                raise CollabError("KNOWLEDGE_BENCHMARK_RESOURCES", "组合资源超出沙箱输入大小。")
            placed = target / path.relative_to(root)
            placed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, placed)


def _gateway(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme == "http" and url.hostname and _NAME.fullmatch(url.hostname)
            and url.username is None and url.password is None and url.port is not None
            and url.path in ("", "/") and not url.query and not url.fragment
        ):
            return f"http://{url.hostname}:{url.port}"
    except ValueError:
        pass
    raise CollabError("KNOWLEDGE_BENCHMARK_GATEWAY", "网关须为 Docker 内部网络的 HTTP 主机及端口。")


@dataclass(frozen=True, slots=True)
class Sandbox:
    docker: str
    image: str
    network: str
    gateway: str | None

    def _base(self) -> list[str]:
        return [self.docker, "--context", "default"]

    @classmethod
    def prepare(cls, image: str, network: str | None, gateway: str | None) -> "Sandbox":
        docker = shutil.which("docker")
        if not docker or not _IMAGE.fullmatch(image) or (os.name == "nt" and Path(docker).suffix.lower() != ".exe"):
            raise CollabError("KNOWLEDGE_BENCHMARK_SANDBOX", "需要本机 Docker 和已准备的 SHA-256 固定镜像。")
        if (network is None) != (gateway is None) or (network and not _NAME.fullmatch(network)):
            raise CollabError("KNOWLEDGE_BENCHMARK_GATEWAY", "内部网络及网关须同时指定。")
        runner = cls(docker, image, network or "none", _gateway(gateway) if gateway else None)
        try:
            info = runner._inspect(["info", "--format", "{{.OSType}}"])
            if info != "linux":
                raise ValueError("Linux containers required")
            resolved = runner._inspect(["image", "inspect", "--format", "{{.Id}}", image])
            if not _IMAGE_ID.fullmatch(resolved) or (_IMAGE_ID.fullmatch(image) and image != resolved):
                raise ValueError("unexpected OCI image ID")
            if network and runner._inspect(["network", "inspect", "--format", "{{.Internal}}", network]) != "true":
                raise ValueError("external network")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise CollabError("KNOWLEDGE_BENCHMARK_SANDBOX", "无法确认 Linux 镜像及内部网络。") from error
        # 仓库摘要可能是多架构清单。执行和跨机续跑都用真正选中的镜像 ID。
        return replace(runner, image=resolved)

    def _inspect(self, args: list[str]) -> str:
        return subprocess.check_output([*self._base(), *args], env=_env(), stderr=subprocess.DEVNULL,
                                       timeout=15, encoding="utf-8").strip()

    def invoke(
        self, phase: str, request: dict[str, JsonValue], timeout: int,
        *, source: Path | None = None, assets: Path | None = None,
    ) -> dict[str, JsonValue]:
        name = f"weora-benchmark-{uuid4().hex}"
        command = [
            *self._base(), "run", "--rm", "-i", "--init", "--name", name, "--pull", "never",
            "--network", self.network, "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--read-only", "--pids-limit", "192", "--memory", "2g", "--cpus", "2", "--user", "1000:1000",
            "--workdir", "/work",
            "--tmpfs", "/work:rw,nosuid,nodev,size=1g,uid=1000,gid=1000",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        ]
        for path, mount in ((_ENTRY, "/weora-native.py"), (source, "/source"), (assets, "/resources")):
            if path:
                resolved = str(path.resolve())
                if any(char in resolved for char in ',\r\n"'):
                    raise ValueError("unsupported mount path")
                command.extend(["--mount", f"type=bind,source={resolved},target={mount},readonly"])
        if self.gateway:
            command.extend(["--env", f"WEORA_BENCHMARK_GATEWAY={self.gateway}"])
        command.extend(["--entrypoint", "/usr/bin/python3", self.image, "-I", "/weora-native.py", phase])
        try:
            output = _bounded(command, json.dumps(request, ensure_ascii=False).encode(), timeout)
        except (subprocess.TimeoutExpired, ValueError, KeyboardInterrupt):
            # Docker 客户端退出后容器可能仍在运行。只移除此调用创建的容器。
            with suppress(OSError, subprocess.SubprocessError):
                subprocess.run([*self._base(), "rm", "-f", name], env=_env(), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15, check=False)
            raise
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError("unexpected sandbox response")
        return as_object(value)


def _bounded(command: list[str], request: bytes, timeout: int) -> bytes:
    """主进程边读边限制输出，容器意外产生大量日志时立即结束。"""
    started = monotonic()
    with TemporaryFile() as stdin:
        stdin.write(request)
        stdin.seek(0)
        with (
            subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=_env()) as process,
            ThreadPoolExecutor(max_workers=1) as reader,
        ):
            assert process.stdout is not None
            output = reader.submit(process.stdout.read, 1_000_001)
            try:
                data = output.result(timeout=timeout)
                if len(data) > 1_000_000:
                    raise ValueError("sandbox response too large")
                status = process.wait(timeout=max(0, timeout - (monotonic() - started)))
                if status:
                    raise subprocess.CalledProcessError(status, command)
                return data
            except TimeoutError as error:
                raise subprocess.TimeoutExpired(command[0], timeout) from error
            finally:
                if process.poll() is None:
                    process.kill()


def _time_left(deadline: datetime) -> int:
    seconds = int((deadline - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        raise CollabError("KNOWLEDGE_BENCHMARK_RUN_EXPIRED", "基准运行已超时。")
    return seconds


def _trial(
    sandbox: Sandbox, slot: dict[str, JsonValue], task: dict[str, JsonValue], source: Path, assets: Path,
    deadline: datetime, judge_tool: str, judge_model: str,
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"trial_id": slot["id"], "outcome": "error"}
    if not isinstance(task.get("prompt"), str) or not isinstance(task.get("success_criteria"), str):
        raise CollabError("KNOWLEDGE_BENCHMARK_INPUT", "缺少已固定的基准任务输入。")
    try:
        evidence = sandbox.invoke("trial", {"tool": slot["tool"], "prompt": task["prompt"]},
                                  _time_left(deadline), source=source, assets=assets)
        if set(evidence) != {"answer", "diff"} or not all(isinstance(text, str) for text in evidence.values()):
            return result
    except (OSError, ValueError, subprocess.SubprocessError):
        return result
    try:
        judged = sandbox.invoke("judge", {"tool": judge_tool, "model": judge_model, "prompt": task["prompt"],
                                         "criteria": task["success_criteria"], "evidence": evidence},
                                _time_left(deadline))
    except (OSError, ValueError, subprocess.SubprocessError):
        return result | {"outcome": "unscored"}
    if set(judged) != {"passed"} or type(judged["passed"]) is not bool:
        return result | {"outcome": "unscored"}
    return result | {"outcome": "passed" if judged["passed"] else "failed",
                     "judge_tool": judge_tool, "judge_model": judge_model}


def _checkouts(
    client: Client, project: str, args: argparse.Namespace,
    run: dict[str, JsonValue], pending: list[dict[str, JsonValue]],
) -> tuple[set[str], dict[str, str]]:
    work, resources = checkout_remote(args.checkout), checkout_remote(args.resources)
    if not work or not resources:
        raise CollabError("KNOWLEDGE_BENCHMARK_REPOSITORY", "两个本地检出均需配置已登记的 origin。")
    bindings = repository_plan(client, project, [work, resources])
    for binding, repo, version in zip(
        bindings, ("repository_id", "resource_repository_id"), ("connection_version", "resource_connection_version"),
        strict=True,
    ):
        if (
            binding.get("status") != "ready" or binding.get("repository_id") != run.get(repo)
            or binding.get("connection_version") != run.get(version)
        ):
            raise CollabError("KNOWLEDGE_BENCHMARK_REPOSITORY", "本地检出与运行固定的仓库连接不一致。")
    starts = {str(slot.get("starting_commit")) for slot in pending}
    arms = {"main": str(run.get("base_resources")), "pulled": str(run.get("proposal_resources"))}
    if starts != commits_in_checkout(args.checkout, starts) or set(arms.values()) != commits_in_checkout(
        args.resources, set(arms.values()),
    ):
        raise CollabError("KNOWLEDGE_BENCHMARK_START", "检出缺少运行固定的项目提交或资源提交。")
    return starts, arms


def _execution_id(
    client: Client, project: str, api: str, data: dict[str, JsonValue], run: dict[str, JsonValue],
    setup: dict[str, JsonValue], config: Path, requested_image: str, attempt: dict[str, JsonValue],
) -> str | None:
    prior_results = any(slot.get("outcome") != "not_run" for slot in items({"items": run.get("trials")}))
    if "execution" in data and (data["execution"] is not None or not prior_results):
        registered = call(client, project, f"{api}/execution", method="PUT", payload=setup | attempt)
        if registered.get("profile") != setup:
            raise CollabError("KNOWLEDGE_BENCHMARK_CONFIG", "平台返回的执行条件与当前配置不一致。")
        try:
            return str(UUID(str(registered["id"])))
        except (KeyError, ValueError) as error:
            raise CollabError("KNOWLEDGE_BENCHMARK_CONFIG", "平台返回的执行登记标识无效。") from error

    # 历史结算延续原契约。旧 CLI 使用镜像引用作为本机配置键。
    legacy = {key: value for key, value in setup.items() if key != "runner_sha256"} | {"image": requested_image}
    if config.is_file():
        if as_object(json.loads(config.read_text(encoding="utf-8"))) != legacy:
            raise CollabError("KNOWLEDGE_BENCHMARK_CONFIG", "恢复运行时须使用原镜像、模型及网络配置。")
    else:
        write_json(config, legacy)
    return None


def execute(
    client: Client, project: str, args: argparse.Namespace, *, sandbox: Sandbox | None = None,
) -> dict[str, JsonValue]:
    if not _MODEL.fullmatch(args.judge_model):
        raise CollabError("KNOWLEDGE_BENCHMARK_JUDGE", "请指定明确的评判模型名称。")
    run_id = str(UUID(args.run))
    pending_dir = session_state_root(client) / "benchmarks" / run_id
    try:
        with lock_file(pending_dir / "execute.json"):
            return _execute_locked(client, project, run_id, pending_dir, args, sandbox)
    except OSError as error:
        raise CollabError("KNOWLEDGE_BENCHMARK_LOCAL", "无法取得本机运行锁或保存试验结果。") from error


def _execute_locked(
    client: Client, project: str, run_id: str, pending_dir: Path, args: argparse.Namespace, sandbox: Sandbox | None,
) -> dict[str, JsonValue]:
    api = f"/benchmark/runs/{run_id}"
    data = call(client, project, f"{api}/inputs")
    run = as_object(data.get("run"))
    if run.get("id") != run_id:
        raise CollabError("KNOWLEDGE_BENCHMARK_INPUT", "平台返回的运行标识与请求不符。")
    pending = [slot for slot in items({"items": run.get("trials")}) if slot.get("outcome") == "not_run"]
    if not pending or run.get("status") != "pending":
        return run
    deadline = datetime.fromisoformat(str(run["deadline"]))
    tasks = {
        identifier: task for task in items({"items": data.get("tasks")})
        if isinstance((identifier := task.get("id")), str)
    }
    operation = getattr(args, "check_operation_id", None)
    attempt: dict[str, JsonValue] = {"check_operation_id": operation} if operation is not None else {}

    def settle(result: dict[str, JsonValue], path: Path) -> None:
        nonlocal run
        # 接管者可以用当前尝试身份重放先前保存的同一基准收据。
        run = call(client, project, f"{api}/results", method="POST", payload={"items": [result]} | attempt)
        path.unlink(missing_ok=True)

    # 平台写入失败时保留判定。重试先补登记，避免重复运行已经完成的试验。
    remaining = []
    for slot in pending:
        receipt = pending_dir / f"{UUID(str(slot['id']))}.json"
        if receipt.is_file():
            result = as_object(json.loads(receipt.read_text(encoding="utf-8")))
            if result.get("trial_id") != slot["id"]:
                raise CollabError("KNOWLEDGE_BENCHMARK_LOCAL", "试验收据与平台编号不一致。")
            settle(result, receipt)
        else:
            remaining.append(slot)
    if not remaining:
        return run

    starts, arms = _checkouts(client, project, args, run, remaining)
    sandbox = sandbox or Sandbox.prepare(args.image, args.network, args.gateway)
    # 入口脚本由宿主挂载。冻结镜像之外还要固定这份脚本，防止不同版本的 CLI 混跑。
    with _ENTRY.open("rb") as runner:
        runner_sha256 = hashlib.file_digest(runner, "sha256").hexdigest()
    setup: dict[str, JsonValue] = {
        "image": sandbox.image, "runner_sha256": runner_sha256, "network": sandbox.network, "gateway": sandbox.gateway,
        "judge_tool": args.judge_tool, "judge_model": args.judge_model,
    }
    execution_id = _execution_id(
        client, project, api, data, run, setup, pending_dir / "execution-config.json", args.image, attempt,
    )
    with TemporaryDirectory(prefix="weora-benchmark-") as temporary:
        scratch = Path(temporary)
        code = {commit: scratch / f"code-{commit}" for commit in starts}
        assets = {arm: scratch / f"assets-{arm}" for arm in arms}
        for commit, directory in code.items():
            _time_left(deadline)
            _clone(args.checkout, commit, directory)
        for arm, commit in arms.items():
            _time_left(deadline)
            checkout = scratch / f"resource-{arm}"
            _clone(args.resources, commit, checkout)
            _assets(checkout, assets[arm])
        for slot in remaining:
            task = tasks.get(str(slot.get("task_id")))
            trial_arm = slot.get("arm")
            if (
                task is None or slot.get("tool") not in ("claude", "codex")
                or not isinstance(trial_arm, str) or trial_arm not in assets
            ):
                raise CollabError("KNOWLEDGE_BENCHMARK_INPUT", "基准计划的试验输入不完整。")
            result = _trial(sandbox, slot, task, code[str(slot["starting_commit"])], assets[trial_arm],
                            deadline, args.judge_tool, args.judge_model)
            if execution_id is not None:
                result["execution_id"] = execution_id
            receipt = pending_dir / f"{UUID(str(slot['id']))}.json"
            write_json(receipt, result)
            settle(result, receipt)
    return run
