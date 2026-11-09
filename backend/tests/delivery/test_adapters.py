"""真实适配器的结构化调用与安全边界测试；不启动真实 Docker/网络。"""

import base64
import hashlib
import io
import json
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.delivery.test_service import scope
from weora.modules.delivery.application.ports import SandboxResult
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.browser import SandboxBrowserVerifier
from weora.modules.delivery.infrastructure.docker import (
    DockerConfig,
    DockerResources,
    DockerSandbox,
    RepositorySnapshot,
)
from weora.modules.delivery.infrastructure.github import GitHubRemote, TokenLease
from weora.modules.delivery.infrastructure.github_tokens import GitHubAppTokens


def snapshot(tmp_path: Path, *, name: str = "src/a.py", link: bool = False) -> RepositorySnapshot:
    archive_path = tmp_path / "input.tar"
    with tarfile.open(archive_path, "w") as archive:
        info = tarfile.TarInfo(name)
        if link:
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        else:
            info.size = 4
            archive.addfile(info, io.BytesIO(b"pass"))
    return RepositorySnapshot(archive_path, "a" * 40, hashlib.sha256(archive_path.read_bytes()).hexdigest())


class CaptureDocker(DockerSandbox):
    def __init__(self, snapshot_record: RepositorySnapshot) -> None:
        super().__init__(DockerConfig("registry/weora@sha256:" + "f" * 64, {"test-repo": snapshot_record}))
        self.calls: list[tuple[str, ...]] = []
        self.stdin: list[bytes] = []
        self.exec_output = SandboxResult("PASSED", "", 0)

    async def _cli(self, *args: str, stdin: bytes | None = None, limit: int | None = None) -> tuple[int, bytes]:
        self.calls.append(args)
        if stdin:
            self.stdin.append(stdin)
        return (1, b"") if args[0] == "inspect" else (0, b"")


@pytest.mark.anyio
async def test_docker_create_has_no_host_mount_network_or_privilege(tmp_path: Path) -> None:
    docker = CaptureDocker(snapshot(tmp_path))
    run_id = uuid4()
    await docker.prepare(run_id, scope())
    create = next(call for call in docker.calls if call[0] == "create")
    assert "--read-only" in create and "--cap-drop" in create and "ALL" in create
    assert create[create.index("--network") + 1] == "none"
    assert "no-new-privileges=true" in create and "--pids-limit" in create and "--cpus" in create
    assert "--memory" in create and "--memory-swap" in create and "10001:10001" in create
    assert not ({"--mount", "--volume", "-v", "--privileged", "--publish", "-p"} & set(create))
    assert docker.config is not None
    assert len(docker.stdin) == 1
    assert docker.stdin[0] == docker.config.repositories["test-repo"].archive_path.read_bytes()


@pytest.mark.parametrize(
    "name,link", [("../escape", False), ("/etc/passwd", False), (".git/config", False), ("src/link", True)]
)
@pytest.mark.anyio
async def test_archive_rejects_escape_and_symlinks(tmp_path: Path, name: str, link: bool) -> None:
    docker = CaptureDocker(snapshot(tmp_path, name=name, link=link))
    with pytest.raises(DeliveryError):
        await docker.prepare(uuid4(), scope())
    assert not any(call[0] == "create" for call in docker.calls)


@pytest.mark.anyio
async def test_changed_archive_or_unconfigured_docker_is_rejected(tmp_path: Path) -> None:
    record = snapshot(tmp_path)
    record.archive_path.write_bytes(b"replaced")
    docker = CaptureDocker(record)
    with pytest.raises(DeliveryError) as error:
        await docker.prepare(uuid4(), scope())
    assert error.value.code == "SNAPSHOT_CHANGED"
    with pytest.raises(DeliveryError) as error:
        await DockerSandbox().prepare(uuid4(), scope())
    assert error.value.code == "SANDBOX_NOT_CONFIGURED"


@pytest.mark.anyio
async def test_cli_real_child_does_not_inherit_host_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实启动本地 Python，只验证 CLI 子进程边界，不声称启动了容器。"""
    monkeypatch.setenv("WEORA_TEST_HOST_SECRET", "local-fixture-only")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted-fixture:2375")
    docker = DockerSandbox(DockerConfig("registry/weora@sha256:" + "f" * 64, {}, executable=sys.executable))
    code, output = await docker._cli("-c", "import os,json;print(json.dumps(sorted(os.environ)))")
    assert code == 0
    names = json.loads(output)
    assert "WEORA_TEST_HOST_SECRET" not in names and "DOCKER_HOST" not in names


@pytest.mark.anyio
async def test_cli_real_child_output_and_time_are_bounded() -> None:
    docker = DockerSandbox(
        DockerConfig("registry/weora@sha256:" + "f" * 64, {}, executable=sys.executable, timeout_seconds=1)
    )
    with pytest.raises(DeliveryError) as error:
        await docker._cli("-c", "print('x'*2048)", limit=1024)
    assert error.value.code == "OUTPUT_LIMIT"
    with pytest.raises(TimeoutError):
        await docker._cli("-c", "import time;time.sleep(30)")


@pytest.mark.anyio
async def test_cli_fixed_prefix_keeps_model_arguments_structured() -> None:
    docker = DockerSandbox(
        DockerConfig(
            "registry/weora@sha256:" + "f" * 64,
            {},
            executable=sys.executable,
            executable_args=("-c", "import sys,json;print(json.dumps(sys.argv[1:]))"),
        )
    )
    code, output = await docker._cli("exec", "value with spaces", "$(no-shell)")
    assert code == 0 and json.loads(output) == ["exec", "value with spaces", "$(no-shell)"]


@pytest.mark.anyio
async def test_cleanup_cli_has_independent_grace_for_short_task_timeout() -> None:
    docker = DockerSandbox(
        DockerConfig(
            "sha256:" + "a" * 64,
            {},
            executable=sys.executable,
            timeout_seconds=1,
            executable_args=("-c", "import time;time.sleep(1.2)"),
        )
    )
    code, output = await docker._cli("rm", "--force", "fixture-container-id")
    assert code == 0 and output == b""


@pytest.mark.parametrize("args", [("",), ("bad\x00arg",), ("x",) * 33])
def test_cli_invalid_fixed_prefix_is_rejected(args: tuple[str, ...]) -> None:
    with pytest.raises(DeliveryError) as error:
        DockerConfig("registry/weora@sha256:" + "f" * 64, {}, executable_args=args).validate()
    assert error.value.code == "SANDBOX_NOT_CONFIGURED"


@pytest.mark.anyio
async def test_command_uses_frozen_image_timeout_and_output_budget(tmp_path: Path) -> None:
    """真实子进程验证预算选择；这里不把 Python 子进程冒充 Docker 验收。"""
    image = "sha256:" + "c" * 64
    record = snapshot(tmp_path)
    docker = DockerSandbox(
        DockerConfig(
            "sha256:" + "a" * 64,
            {},
            executable=sys.executable,
            executable_args=("-c", "import time; time.sleep(1.2); print('x'*1200)"),
            timeout_seconds=1,
            output_bytes=1024,
            image_resources={image: DockerResources(timeout_seconds=3, output_bytes=2048)},
        )
    )
    run = uuid4()
    docker.bind_snapshot(run, scope(), record, image=image)
    result = await docker.command(run, ("mvn", "-o", "verify"))
    assert result.exit_code == 0 and len(result.output) > 1024


@pytest.mark.parametrize("image", ["sha256:" + "a" * 64, "registry/weora@sha256:" + "f" * 64])
def test_sandbox_accepts_only_immutable_image_references(image: str) -> None:
    DockerConfig(image, {}).validate()


@pytest.mark.parametrize("image", ["latest", "weora:test", "sha256:abcd", "prefix-sha256:" + "a" * 64])
def test_sandbox_rejects_mutable_or_partial_image_references(image: str) -> None:
    with pytest.raises(DeliveryError):
        DockerConfig(image, {}).validate()


@pytest.mark.parametrize("args", [("-d", "Ubuntu", "--", "docker"), ("docker",), ("--", "--exec", "docker")])
def test_wsl_prefix_rejects_implicit_shell_execution(args: tuple[str, ...]) -> None:
    with pytest.raises(DeliveryError) as error:
        DockerConfig("sha256:" + "a" * 64, {}, executable="wsl.exe", executable_args=args).validate()
    assert error.value.code == "SANDBOX_NOT_CONFIGURED"


@pytest.mark.parametrize("args", [("-d", "Ubuntu", "--exec", "docker"), ("-e", "docker")])
def test_wsl_prefix_requires_direct_exec(args: tuple[str, ...]) -> None:
    DockerConfig("sha256:" + "a" * 64, {}, executable="C:/Windows/System32/wsl.exe", executable_args=args).validate()


class BrowserDocker(DockerSandbox):
    def __init__(self, result: SandboxResult) -> None:
        super().__init__()
        self.result = result
        self.script = ""
        self.input: dict[str, str] = {}

    async def _exec(
        self, run_id: UUID, *argv: str, stdin: bytes | None = None, output_limit: int | None = None
    ) -> SandboxResult:
        self.script = argv[-1]
        self.input = json.loads(stdin or b"{}")
        return self.result


@pytest.mark.parametrize("path", ["https://external.example", "//127.0.0.1:8000", "/\\evil"])
@pytest.mark.anyio
async def test_browser_cannot_accept_host_or_arbitrary_urls(path: str) -> None:
    verifier = SandboxBrowserVerifier(BrowserDocker(SandboxResult("PASSED", "")), service_port=3000)
    with pytest.raises(DeliveryError) as error:
        await verifier.verify(uuid4(), path, "Hello")
    assert error.value.code == "BROWSER_URL_DENIED"


@pytest.mark.parametrize(
    "status", ["PASSED", "ASSERTION_FAILED", "URL_DENIED", "SERVICE_NOT_READY", "SCREENSHOT_FAILED"]
)
@pytest.mark.anyio
async def test_browser_preserves_truthful_status_and_restricted_driver(status: str) -> None:
    evidence = {"status": status, "report": "真实验证状态"}
    if status == "PASSED":
        evidence["screenshot_base64"] = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
    docker = BrowserDocker(SandboxResult("PASSED", json.dumps(evidence), 0))
    result = await SandboxBrowserVerifier(docker, service_port=3000).verify(uuid4(), "/login", "Hello")
    assert result.status == status
    assert docker.input["origin"] == "http://127.0.0.1:3000"
    assert "url.origin!==input.origin" in docker.script and "routeWebSocket" in docker.script
    assert "require('/opt/weora-browser/node_modules/playwright')" in docker.script


@pytest.mark.anyio
async def test_browser_invalid_screenshot_and_timeout_are_not_passed() -> None:
    bad = BrowserDocker(
        SandboxResult(
            "PASSED",
            json.dumps(
                {
                    "status": "PASSED",
                    "report": "通过",
                    "screenshot_base64": base64.b64encode(b"<svg/>").decode(),
                }
            ),
            0,
        )
    )
    result = await SandboxBrowserVerifier(bad, service_port=3000).verify(uuid4(), "/", "hello")
    assert result.status == "SCREENSHOT_FAILED"
    timeout = BrowserDocker(SandboxResult("TIMEOUT", "超时"))
    result = await SandboxBrowserVerifier(timeout, service_port=3000).verify(uuid4(), "/", "hello")
    assert result.status == "TIMEOUT"
    result = await SandboxBrowserVerifier(timeout).verify(uuid4(), "/", "hello")
    assert result.status == "NOT_CONFIGURED"


@pytest.mark.parametrize("status,code", [("FAILED", 1), ("PASSED", 1), ("PASSED", None)])
@pytest.mark.anyio
async def test_browser_stdout_cannot_override_failed_process(status: str, code: int | None) -> None:
    docker = BrowserDocker(SandboxResult(status, '{"status":"PASSED","report":"通过"}', code))
    result = await SandboxBrowserVerifier(docker, service_port=3000).verify(uuid4(), "/", "hello")
    assert result.status == "SERVICE_NOT_READY" and result.screenshot_base64 is None


@pytest.mark.parametrize("shot", [None, ""])
@pytest.mark.anyio
async def test_browser_pass_requires_screenshot_evidence(shot: str | None) -> None:
    data = {"status": "PASSED", "report": "通过", "screenshot_base64": shot}
    docker = BrowserDocker(SandboxResult("PASSED", json.dumps(data), 0))
    result = await SandboxBrowserVerifier(docker, service_port=3000).verify(uuid4(), "/", "hello")
    assert result.status == "SCREENSHOT_FAILED"


class FailedDiffDocker(DockerSandbox):
    def __init__(self, result: SandboxResult) -> None:
        super().__init__()
        self.result = result
        self.executed = 0

    def owns(self, run_id: UUID) -> bool:
        return True

    async def _inspect(self, run_id: UUID, plan: PlanScope) -> bool:
        return True

    async def _exec(
        self, run_id: UUID, *argv: str, stdin: bytes | None = None, output_limit: int | None = None
    ) -> SandboxResult:
        self.executed += 1
        assert self.executed == 1  # 核验失败后不能继续读取文件或输出 Diff。
        return self.result


@pytest.mark.parametrize("status,code", [("FAILED", 1), ("TIMEOUT", None)])
@pytest.mark.anyio
async def test_failed_diff_stops_export_before_reading_files(status: str, code: int | None) -> None:
    docker = FailedDiffDocker(SandboxResult(status, "未完成", code))
    with pytest.raises(DeliveryError) as error:
        await docker.export_changes(uuid4(), scope())
    assert error.value.code == "EXPORT_FAILED" and docker.executed == 1


@pytest.mark.parametrize("labels,running", [(None, True), ({}, "false")])
@pytest.mark.anyio
async def test_inspect_malformed_state_maps_to_protocol_error(
    tmp_path: Path, labels: dict[str, str] | None, running: object
) -> None:
    class InspectDocker(CaptureDocker):
        async def _cli(self, *args: str, stdin: bytes | None = None, limit: int | None = None) -> tuple[int, bytes]:
            actual_labels = labels
            if actual_labels is not None:
                actual_labels = {
                    "weora.run": str(run_id),
                    "weora.scope": scope().digest,
                    "weora.deployment": "local",
                    "weora.instance": self._instance_id,
                }
            return 0, json.dumps({"Config": {"Labels": actual_labels}, "State": {"Running": running}}).encode()

    run_id = uuid4()
    docker = InspectDocker(snapshot(tmp_path))
    with pytest.raises(DeliveryError) as error:
        await docker._inspect(run_id, scope())
    assert error.value.code == "SANDBOX_PROTOCOL"


def test_github_jwt_uses_rsa_signature_and_bounded_claims() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    tokens = GitHubAppTokens("123", 456, pem, {"test-remote": GitHubRemote("example/test", ("main",))})
    encoded = tokens._jwt()
    header, payload, signature = encoded.split(".")

    def decode(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    claims = json.loads(decode(payload))
    assert json.loads(decode(header)) == {"alg": "RS256", "typ": "JWT"}
    assert claims["iss"] == "123" and claims["exp"] - claims["iat"] == 600
    key.public_key().verify(decode(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert "PRIVATE KEY" not in encoded


@pytest.mark.parametrize("app_id,installation_id,pem", [("invalid", 1, "x"), ("1", -1, "x"), ("1", 1, "x")])
def test_invalid_github_app_configuration_is_safe(app_id: str, installation_id: int, pem: str) -> None:
    with pytest.raises(DeliveryError):
        GitHubAppTokens(app_id, installation_id, pem, {})


def test_token_repr_never_exposes_secret() -> None:
    lease = TokenLease("very-secret-token", datetime.now(UTC) + timedelta(minutes=1))
    assert "very-secret-token" not in repr(lease)


def test_scope_requires_fixed_revision_and_separate_remote_approval() -> None:
    invalid = PlanScope("test-repo", "main", ("src",), (), ("file.write",))
    with pytest.raises(DeliveryError):
        invalid.validate()


class ReaperDocker(DockerSandbox):
    def __init__(self) -> None:
        super().__init__(DockerConfig("registry/weora@sha256:" + "f" * 64, {}))
        self.containers: dict[str, dict[str, object]] = {}
        self.deleted: list[str] = []
        self.fail_remove = False

    def register(self, run_id: UUID, container_id: str, *, valid: bool = True) -> None:
        self.containers[container_id] = {
            "Id": container_id,
            "Name": "/" + self.name(run_id),
            "Config": {
                "Labels": {
                    "weora.run": str(run_id),
                    "weora.managed": "v1",
                    "weora.deployment": "local",
                    "weora.instance": self._instance_id if valid else "invalid",
                    "weora.scope": "a" * 64,
                }
            },
        }

    async def _cli(self, *args: str, stdin: bytes | None = None, limit: int | None = None) -> tuple[int, bytes]:
        if args[0] == "ps":
            assert "label=weora.managed=v1" in args and "--no-trunc" in args
            return 0, "\n".join(self.containers).encode()
        if args[0] == "inspect":
            record = self.containers.get(args[-1])
            if record is None:
                record = next((value for value in self.containers.values() if value["Name"] == "/" + args[-1]), None)
            return (0, json.dumps(record).encode()) if record else (1, b"")
        assert args[0] == "rm" and len(args[-1]) == 64  # 永不按可被替换的 name 删除。
        self.deleted.append(args[-1])
        return (1 if self.fail_remove else 0), b""


@pytest.mark.anyio
async def test_reaper_never_removes_active_or_unverified_container_and_uses_id() -> None:
    docker = ReaperDocker()
    live, expired, malformed = uuid4(), uuid4(), uuid4()
    docker.register(live, "a" * 64)
    docker.register(expired, "b" * 64)
    docker.register(malformed, "c" * 64, valid=False)

    async def is_live(run_id: UUID) -> bool:
        return run_id == live

    assert await docker.reap_orphans(is_live) == []
    assert docker.deleted == ["b" * 64]
    docker.fail_remove = True
    assert await docker.reap_orphans(is_live) == [expired]


@pytest.mark.anyio
async def test_late_cleanup_does_not_remove_new_worker_container() -> None:
    docker = ReaperDocker()
    run_id = uuid4()
    docker.register(run_id, "d" * 64)
    docker._owned_runs.add(run_id)
    docker._instance_id = "e" * 32  # 模拟现存容器已属于另一个 Worker。
    await docker.cleanup(run_id)
    assert docker.deleted == []
    invalid = PlanScope("test-repo", "a" * 40, ("src",), (), ("git.push",))
    with pytest.raises(DeliveryError):
        invalid.validate()
