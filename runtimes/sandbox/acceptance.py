"""显式运行真实 Docker 适配器验收；只操作本脚本创建并标记的随机 Run 容器。"""

import argparse
import asyncio
import hashlib
import io
import json
import tarfile
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.browser import SandboxBrowserVerifier
from weora.modules.delivery.infrastructure.docker import DockerConfig, DockerSandbox, RepositorySnapshot


async def accept(image: str, *, wsl: bool, output: Path, browser_enabled: bool) -> None:
    """不读取应用配置或凭据；生成最小 Bug 仓库并保存可复核的事实记录。"""
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    facts: dict[str, object] = {"image": image, "wsl": wsl, "checks": {}}
    checks: dict[str, object] = {}
    facts["checks"] = checks
    with tempfile.TemporaryDirectory(prefix="weora-sandbox-input-") as temporary:
        archive_path = Path(temporary) / "input.tar"
        files = {
            "src/calc.py": "def add(a, b):\n    return a - b\n",
            "tests/test_calc.py": "from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            "README.md": "Weora isolated sandbox acceptance fixture. No credentials.\n",
            "index.html": "<!doctype html><meta charset='utf-8'><h1>Weora sandbox accepted</h1>",
        }
        with tarfile.open(archive_path, "w") as archive:
            for name, text in files.items():
                data = text.encode()
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(data), 0o644
                archive.addfile(item, io.BytesIO(data))
        snapshot = RepositorySnapshot(archive_path, "a" * 40, hashlib.sha256(archive_path.read_bytes()).hexdigest())
        # 保存隔离夹具用于后续人工复验，绝不归档实际项目或后端配置目录。
        (output / "input.tar").write_bytes(archive_path.read_bytes())
        config = DockerConfig(
            image,
            {"acceptance-fixture": snapshot},
            executable="wsl.exe" if wsl else "docker",
            executable_args=(
                "-d",
                "Ubuntu",
                "-u",
                "root",
                "--exec",
                "docker",
                "--host",
                "unix:///var/run/docker.sock",
                "--config",
                "/var/lib/weora/docker-cli",
            )
            if wsl
            else (),
            deployment_id="weora-acceptance-" + uuid4().hex,
            timeout_seconds=30,
            memory_mb=1024,
        )
        sandbox = DockerSandbox(config)
        scope = PlanScope(
            "acceptance-fixture", "a" * 40, ("src", "tests", "README.md", "index.html"), (), ("file.write", "git.diff")
        )
        run_id = uuid4()
        facts["run_id"] = str(run_id)
        facts["deployment_id"] = config.deployment_id
        facts["snapshot_sha256"] = snapshot.archive_sha256
        facts["repository_id"] = "acceptance-fixture"
        # 纯测试仓库没有远端提交；这个固定 revision 仅用于管理员快照登记，不用于真实 PR。
        facts["base_revision"] = snapshot.revision

        def save() -> None:
            # 这是运行生成的验收产物，不包含宿主环境、凭据或真实项目源码。
            (output / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")

        async def gone(identifier: UUID) -> bool:
            code, _ = await sandbox._cli("inspect", sandbox.name(identifier))
            return code != 0

        try:
            await sandbox.prepare(run_id, scope)
            _, raw = await sandbox._cli("inspect", "--format", "{{json .}}", sandbox.name(run_id))
            record = json.loads(raw)
            host, runtime = record["HostConfig"], record["Config"]
            assert runtime["User"] == "10001:10001"
            assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
            assert host["Memory"] == 1024 * 1024 * 1024 and host["NanoCpus"] == 1_000_000_000
            assert host["PidsLimit"] == 128 and "ALL" in host["CapDrop"]
            assert "no-new-privileges=true" in host["SecurityOpt"]
            assert not host["Binds"] and not host["PortBindings"]
            checks["container_boundaries"] = "PASSED"
            literal = "$(echo SHOULD_NOT_EXECUTE); value with spaces"
            argv = await sandbox.command(
                run_id, ("python3", "-c", "import json,sys;print(json.dumps(sys.argv[1:]))", literal)
            )
            assert argv.status == "PASSED" and json.loads(argv.output) == [literal], argv.output
            checks["structured_argv_no_shell"] = "PASSED"
            test = ("python3", "-m", "pytest", "-q", "-p", "no:cacheprovider")
            before = await sandbox.command(run_id, test)
            checks["bug_test"] = {"status": before.status, "exit_code": before.exit_code}
            assert before.status == "FAILED" and before.exit_code == 1 and "1 failed" in before.output, before.output
            written = await sandbox.write(run_id, "src/calc.py", "def add(a, b):\n    return a + b\n")
            assert written.status == "PASSED", written.output
            after = await sandbox.command(run_id, test)
            checks["fixed_test"] = {"status": after.status, "exit_code": after.exit_code}
            assert after.status == "PASSED" and after.exit_code == 0, after.output
            diff = await sandbox.diff(run_id, scope)
            assert diff.status == "PASSED" and "return a + b" in diff.output
            (output / "changes.diff").write_text(diff.output, encoding="utf-8")
            changes = await sandbox.export_changes(run_id, scope)
            assert len(changes) == 1 and changes[0]["path"] == "src/calc.py"
            checks["diff_and_export"] = "PASSED"
            network = await sandbox.command(
                run_id,
                (
                    "python3",
                    "-c",
                    "import socket,sys\ntry: socket.create_connection(('1.1.1.1',443),timeout=1)\n"
                    "except OSError: print('NETWORK_BLOCKED')\nelse: sys.exit(2)",
                ),
            )
            assert network.status == "PASSED" and "NETWORK_BLOCKED" in network.output, network.output
            checks["network_blocked"] = "PASSED"
            rootfs = await sandbox.command(
                run_id,
                (
                    "python3",
                    "-c",
                    "import errno,sys\n"
                    "try: open('/etc/weora-test','w').write('x')\n"
                    "except OSError as e: assert e.errno in (errno.EROFS,errno.EACCES);print('ROOTFS_BLOCKED')\n"
                    "else: sys.exit(2)",
                ),
            )
            assert rootfs.status == "PASSED" and "ROOTFS_BLOCKED" in rootfs.output, rootfs.output
            checks["rootfs_write_blocked"] = "PASSED"
            link = await sandbox.command(run_id, ("python3", "-c", "import os;os.symlink('/etc/passwd','src/link')"))
            assert link.status == "PASSED", link.output
            blocked = await sandbox.write(run_id, "src/link", "must not overwrite outside workspace")
            assert blocked.status == "FAILED", blocked.output
            checks["symlink_write_blocked"] = "PASSED"
            await sandbox.command(run_id, ("python3", "-c", "import os;os.unlink('src/link')"))
            if browser_enabled:
                server = await sandbox.command(
                    run_id,
                    (
                        "python3",
                        "-c",
                        "import subprocess;subprocess.Popen(['python3','-m','http.server','3000','--bind','127.0.0.1'],"
                        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)",
                    ),
                )
                assert server.status == "PASSED", server.output
                browser = SandboxBrowserVerifier(sandbox, service_port=3000)
                passed = await browser.verify(run_id, "/index.html", "Weora sandbox accepted")
                failed = await browser.verify(run_id, "/index.html", "text that does not exist")
                checks["browser_success"] = {"status": passed.status, "report": passed.report}
                checks["browser_failure"] = {"status": failed.status, "report": failed.report}
                if passed.screenshot_base64:
                    import base64

                    (output / "browser.png").write_bytes(base64.b64decode(passed.screenshot_base64))
                save()
                # 浏览器未就绪必须保留失败状态，不能为了截图放宽网络/权限边界。
                assert passed.status == "PASSED" and failed.status == "ASSERTION_FAILED"
            else:
                checks["browser"] = "NOT_RUN"
            sandbox.config = replace(config, timeout_seconds=2)
            timed = await sandbox.command(run_id, ("python3", "-c", "import time;time.sleep(30)"))
            assert timed.status == "TIMEOUT" and await gone(run_id)
            checks["timeout_reclaims_container"] = "PASSED"
            sandbox.config = config
            await sandbox.prepare(run_id, scope)
            task = asyncio.create_task(sandbox.command(run_id, ("python3", "-c", "import time;time.sleep(30)")))
            await asyncio.sleep(1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Cancellation must remain observable")
            assert await gone(run_id)
            checks["cancellation_reclaims_container"] = "PASSED"
            await sandbox.prepare(run_id, scope)
            try:
                await sandbox.command(run_id, ("python3", "-c", "print('x'*70000)"))
            except DeliveryError as error:
                assert error.code == "OUTPUT_LIMIT"
            else:
                raise AssertionError("Oversized output must fail closed")
            assert await gone(run_id)
            checks["output_limit_reclaims_container"] = "PASSED"
            await sandbox.prepare(run_id, scope)

            async def no_lease(_: UUID) -> bool:
                return False

            reaper = DockerSandbox(config)
            assert await reaper.reap_orphans(no_lease) == [] and await gone(run_id)
            sandbox._owned_runs.discard(run_id)
            checks["orphan_reaper"] = "PASSED"
        finally:
            # 仅回收这个适配器确认拥有的容器，不执行全局 prune 或宿主删除。
            if run_id in sandbox._owned_runs:
                sandbox.config = config
                await sandbox.cleanup(run_id)
            save()
        print(json.dumps(facts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="已本地准备的不可变镜像 digest")
    parser.add_argument("--wsl", action="store_true", help="通过本机 WSL Ubuntu 的 Unix socket 调用 Docker")
    parser.add_argument("--browser", action="store_true", help="同时验证预装 Playwright 的浏览器镜像")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(accept(arguments.image, wsl=arguments.wsl, output=arguments.output, browser_enabled=arguments.browser))
