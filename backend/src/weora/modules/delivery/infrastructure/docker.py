"""真正的容器隔离：宿主只启动 Docker CLI，不执行模型给出的宿主命令。"""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import tarfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID, uuid4

from weora.modules.delivery.application.ports import SandboxResult
from weora.modules.delivery.domain.models import DeliveryError, PlanScope, safe_path


@dataclass(frozen=True)
class RepositorySnapshot:
    """管理员登记的不可变离线快照，不接受模型指定宿主路径或远端 URL。"""

    archive_path: Path
    revision: str
    archive_sha256: str


@dataclass(frozen=True)
class DockerResources:
    """部署管理员为固定镜像登记的预算；任务和模型不能提高这些上限。"""

    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    timeout_seconds: float = 60
    output_bytes: int = 65536
    workspace_mb: int = 512
    temporary_mb: int = 64


@dataclass(frozen=True)
class DockerConfig:
    image: str
    repositories: Mapping[str, RepositorySnapshot]
    executable: str = "docker"
    # 仅由管理员配置的固定启动前缀，例如 wsl.exe -d Ubuntu --exec docker。
    executable_args: tuple[str, ...] = ()
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    timeout_seconds: float = 60
    output_bytes: int = 65536
    deployment_id: str = "local"
    workspace_mb: int = 512
    temporary_mb: int = 64
    # 按不可变镜像绑定资源，不让 Java 的构建预算扩大旧浏览器环境的权限。
    image_resources: Mapping[str, DockerResources] = field(default_factory=dict)

    def for_image(self, image: str) -> "DockerConfig":
        resources = self.image_resources.get(image)
        values = {} if resources is None else vars(resources)
        resolved = replace(self, image=image, image_resources={}, **values)
        resolved.validate()
        return resolved

    def validate(self) -> None:
        if len(self.executable_args) > 32 or any(
            not item or len(item) > 1024 or "\x00" in item for item in self.executable_args
        ):
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "沙箱 CLI 固定参数配置无效")
        launcher = self.executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if launcher in {"wsl", "wsl.exe"}:
            # WSL 的 -- 只是结束选项，仍经默认 shell；必须 --exec 才能保持工具 argv 的边界。
            direct = next((i for i, value in enumerate(self.executable_args) if value in {"--exec", "-e"}), None)
            if direct is None or direct == len(self.executable_args) - 1 or "--" in self.executable_args[:direct]:
                raise DeliveryError("SANDBOX_NOT_CONFIGURED", "WSL 沙箱 CLI 必须使用 --exec，禁止隐式 shell 执行")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.deployment_id):
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "沙箱部署命名空间无效")
        if not re.fullmatch(r"(?:[A-Za-z0-9_./:-]+@)?sha256:[0-9a-f]{64}", self.image):
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "沙箱镜像必须固定到完整 sha256 digest 或本地镜像 ID")
        if not 128 <= self.memory_mb <= 4096 or not 0.1 <= self.cpus <= 4 or not 16 <= self.pids <= 512:
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "沙箱资源限制配置无效")
        if not 1 <= self.timeout_seconds <= 600 or not 1024 <= self.output_bytes <= 262144:
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "沙箱时长或输出限制配置无效")
        if not 64 <= self.workspace_mb <= 4096 or not 16 <= self.temporary_mb <= 512:
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "沙箱临时存储预算无效")
        for image in self.image_resources:
            self.for_image(image)


class DockerSandbox:
    """每 Run 一个无网络容器；进程退出不意味着测试通过，真实 exit code 单独保存。"""

    def __init__(self, config: DockerConfig | None = None) -> None:
        self.config = config
        self._instance_id = uuid4().hex
        self._owned_runs: set[UUID] = set()
        self._run_snapshots: dict[UUID, tuple[str, str, RepositorySnapshot]] = {}
        self._run_images: dict[UUID, str] = {}
        self._execution_configs: dict[str, DockerConfig] = {}

    def bind_snapshot(
        self, run_id: UUID, scope: PlanScope, snapshot: RepositorySnapshot, *, image: str | None = None
    ) -> None:
        """由已鉴权的后台桥接器登记，仅作用于该 Run，不修改部署默认仓库。"""
        if snapshot.revision != scope.base_revision:
            raise DeliveryError("REPOSITORY_DENIED", "沙箱归档与批准基准不一致")
        resolved = self._config().for_image(image or self._config().image)
        name = self.name(run_id)
        if name in self._execution_configs and self._execution_configs[name] != resolved:
            raise DeliveryError("SANDBOX_SCOPE_CHANGED", "本次执行镜像或资源预算不能被替换")
        previous = self._run_snapshots.get(run_id)
        value = (scope.repository_id, scope.digest, snapshot)
        if previous is not None and previous != value:
            raise DeliveryError("SANDBOX_SCOPE_CHANGED", "不能覆盖本次运行已有的代码副本")
        if image is not None:
            self._run_images[run_id] = image
        self._run_snapshots[run_id] = value
        self._execution_configs[name] = resolved

    def has_bound_snapshot(self, run_id: UUID, scope: PlanScope) -> bool:
        current = self._run_snapshots.get(run_id)
        return current is not None and current[:2] == (scope.repository_id, scope.digest)

    def _config(self) -> DockerConfig:
        if self.config is None:
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "尚未配置隔离沙箱与只读仓库快照")
        self.config.validate()
        return self.config

    @staticmethod
    def name(run_id: UUID) -> str:
        return "weora-run-" + run_id.hex

    async def _cli(self, *args: str, stdin: bytes | None = None, limit: int | None = None) -> tuple[int, bytes]:
        config = self._config()
        # 仅 exec 采用该 Run 创建时冻结的预算；inspect/rm 等控制面不借用任务参数。
        if len(args) >= 3 and args[:2] == ("exec", "-i"):
            config = self._execution_configs.get(args[2], config)
        # 不继承 DOCKER_HOST、代理或用户 HOME；管理员 CLI 只访问本机受控 daemon。
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
        }
        try:
            process = await asyncio.create_subprocess_exec(
                config.executable,
                *config.executable_args,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except (OSError, ValueError):
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "Docker 不可用或未正确配置") from None
        output = bytearray()
        bound = config.output_bytes if limit is None else limit
        # 控制面查询/回收不能复用极短的任务预算，否则任务超时后 rm 本身也被提前杀死。
        timeout = (
            max(config.timeout_seconds, 10) if args and args[0] in {"inspect", "rm", "ps"} else config.timeout_seconds
        )

        async def communicate() -> None:
            assert process.stdin and process.stdout
            if stdin:
                process.stdin.write(stdin)
                await process.stdin.drain()
            process.stdin.close()
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > bound:
                    raise DeliveryError("OUTPUT_LIMIT", "沙箱输出超过限制，已停止容器")
            await process.wait()

        try:
            async with asyncio.timeout(timeout):
                await communicate()
        except (TimeoutError, DeliveryError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        return process.returncode or 0, bytes(output)

    async def _inspect(self, run_id: UUID, scope: PlanScope) -> bool:
        code, output = await self._cli("inspect", "--format", "{{json .}}", self.name(run_id))
        if code:
            return False
        try:
            record = json.loads(output)
            labels = record["Config"]["Labels"]
            if labels.get("weora.run") != str(run_id) or labels.get("weora.scope") != scope.digest:
                raise DeliveryError("SANDBOX_SCOPE_CHANGED", "容器与当前批准范围不匹配，需要清理后重建")
            if labels.get("weora.deployment") != self._config().deployment_id:
                raise DeliveryError("SANDBOX_SCOPE_CHANGED", "容器不属于当前部署")
            if labels.get("weora.instance") != self._instance_id:
                raise DeliveryError("SANDBOX_RECOVERY_REQUIRED", "该沙箱属于先前 Worker，需要先核对和清理")
            running = record["State"]["Running"]
            if not isinstance(running, bool):
                raise ValueError
            if not running:
                raise DeliveryError("SANDBOX_STOPPED", "沙箱已停止，不能静默丢弃先前修改重建")
            return True
        except (ValueError, AttributeError, KeyError, TypeError):
            raise DeliveryError("SANDBOX_PROTOCOL", "Docker 返回了无效状态") from None

    def _archive(self, scope: PlanScope, run_id: UUID | None = None) -> bytes:
        config = self._config()
        bound = self._run_snapshots.get(run_id) if run_id else None
        if bound and bound[:2] != (scope.repository_id, scope.digest):
            raise DeliveryError("SANDBOX_SCOPE_CHANGED", "代码副本与批准计划不一致")
        snapshot = bound[2] if bound else config.repositories.get(scope.repository_id)
        if snapshot is None or snapshot.revision != scope.base_revision:
            raise DeliveryError("REPOSITORY_DENIED", "没有登记该仓库的固定基准快照")
        try:
            if not snapshot.archive_path.is_absolute() or snapshot.archive_path.is_symlink():
                raise DeliveryError("REPOSITORY_DENIED", "仓库快照必须为管理员登记的绝对文件路径")
            if snapshot.archive_path.stat().st_size > 64 * 1024 * 1024:
                raise DeliveryError("SNAPSHOT_LIMIT", "仓库快照超过 64 MiB")
            data = snapshot.archive_path.read_bytes()
        except OSError:
            raise DeliveryError("SANDBOX_NOT_CONFIGURED", "仓库快照不可读取") from None
        if hashlib.sha256(data).hexdigest() != snapshot.archive_sha256:
            raise DeliveryError("SNAPSHOT_CHANGED", "仓库快照与登记哈希不一致")
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                members = archive.getmembers()
                if len(members) > 20000 or sum(x.size for x in members) > 128 * 1024 * 1024:
                    raise DeliveryError("SNAPSHOT_LIMIT", "快照解包预算超限")
                for item in members:
                    safe_path(item.name.rstrip("/"))
                    if not (item.isfile() or item.isdir()):
                        raise DeliveryError("INVALID_SNAPSHOT", "仓库快照不能包含符号链接、设备或硬链接")
        except (tarfile.TarError, ValueError):
            raise DeliveryError("INVALID_SNAPSHOT", "仓库快照不是有效 tar 文件") from None
        return data

    async def prepare(self, run_id: UUID, scope: PlanScope) -> None:
        config = self._execution_configs.get(self.name(run_id), self._config())
        if await self._inspect(run_id, scope):
            return
        archive = self._archive(scope, run_id)
        name = self.name(run_id)
        # 在 create 之前登记本次所有权，覆盖 Docker 超时但可能已创建的未知结果。
        self._owned_runs.add(run_id)
        code, _ = await self._cli(
            "create",
            "--name",
            name,
            "--label",
            f"weora.run={run_id}",
            "--label",
            f"weora.scope={scope.digest}",
            "--label",
            f"weora.instance={self._instance_id}",
            "--label",
            "weora.managed=v1",
            "--label",
            f"weora.deployment={config.deployment_id}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(config.pids),
            "--memory",
            f"{config.memory_mb}m",
            "--memory-swap",
            f"{config.memory_mb}m",
            "--cpus",
            str(config.cpus),
            "--user",
            "10001:10001",
            "--tmpfs",
            f"/workspace:rw,nosuid,nodev,size={config.workspace_mb * 1048576},uid=10001,gid=10001,mode=0700",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={config.temporary_mb * 1048576},uid=10001,gid=10001,mode=0700",
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/tmp",
            "--env",
            "GIT_CONFIG_NOSYSTEM=1",
            self._run_images.get(run_id, config.image),
            "python3",
            "-c",
            "import time; time.sleep(86400)",
        )
        if code:
            if await self._inspect(run_id, scope):
                return
            raise DeliveryError("SANDBOX_START_FAILED", "无法创建受限容器")
        try:
            code, _ = await self._cli("start", name)
            if code:
                raise DeliveryError("SANDBOX_START_FAILED", "受限容器启动失败")
            # 所有归档成员已在控制面验证；容器内仍采用 data 过滤器，避免解析差异。
            script = (
                "import sys,tarfile,io,subprocess; "
                "a=tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())); "
                "a.extractall('/workspace',filter='data'); "
                "subprocess.run(['git','init','-q'],check=True); "
                "subprocess.run(['git','add','--all'],check=True); "
                "subprocess.run(['git','-c','user.name=Weora','-c','user.email=weora@localhost',"
                "'commit','--allow-empty','-qm','Immutable input snapshot'],check=True)"
            )
            code, _ = await self._cli("exec", "-i", name, "python3", "-c", script, stdin=archive)
            if code:
                raise DeliveryError("SANDBOX_PREPARE_FAILED", "沙箱基准快照准备失败")
        except BaseException:
            await self.cleanup(run_id)
            raise

    async def _exec(
        self, run_id: UUID, *argv: str, stdin: bytes | None = None, output_limit: int | None = None
    ) -> SandboxResult:
        try:
            code, data = await self._cli("exec", "-i", self.name(run_id), *argv, stdin=stdin, limit=output_limit)
            return SandboxResult("PASSED" if code == 0 else "FAILED", data.decode("utf-8", errors="replace"), code)
        except TimeoutError:
            await self.cleanup(run_id)
            return SandboxResult("TIMEOUT", "沙箱命令超时，整个容器已停止")
        except (DeliveryError, asyncio.CancelledError):
            await self.cleanup(run_id)
            raise

    async def command(self, run_id: UUID, argv: tuple[str, ...]) -> SandboxResult:
        """参数由应用层精确匹配审批后的 argv；永不拼接 shell。"""
        return await self._exec(run_id, *argv)

    def owns(self, run_id: UUID) -> bool:
        """迟到 Worker 不得导出或恢复其他执行者已经接管的容器。"""
        return run_id in self._owned_runs

    async def restore_changes(self, run_id: UUID, scope: PlanScope, changes: list[dict[str, str | None]]) -> None:
        """只恢复经过校验的普通文件或删除；逐级拒绝符号链接，不能触碰 Git 元数据。"""
        if not self.owns(run_id) or len(changes) > 500:
            raise DeliveryError("WORKSPACE_DENIED", "工作区不属于当前执行实例")
        if not await self._inspect(run_id, scope):
            raise DeliveryError("SANDBOX_STOPPED", "工作区容器不存在")
        total = 0
        for item in changes:
            path = str(item["path"])
            if not scope.allows_path(path):
                raise DeliveryError("PATH_DENIED", "前序修改不在本轮批准路径内")
            if item.get("mode") not in {None, "100644", "100755"}:
                raise DeliveryError("WORKSPACE_CORRUPT", "工作文件权限无效")
            if item["content"] is not None:
                try:
                    data = base64.b64decode(str(item["content"]), validate=True)
                except ValueError:
                    raise DeliveryError("WORKSPACE_CORRUPT", "工作文件编码无效") from None
                total += len(data)
                if len(data) > 262144 or total > 1048576:
                    raise DeliveryError("WORKSPACE_LIMIT", "工作文件超过恢复上限")
        script = "\n".join(
            [
                "import os,sys,json,base64",
                "items=json.load(sys.stdin)",
                "for item in items:",
                " parts=item['path'].split('/')",
                " fd=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY)",
                " try:",
                "  for part in parts[:-1]:",
                "   try: os.mkdir(part,dir_fd=fd)",
                "   except FileExistsError: pass",
                "   nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd); os.close(fd); fd=nxt",
                "  if item['content'] is None:",
                "   try: os.unlink(parts[-1],dir_fd=fd)",
                "   except FileNotFoundError: pass",
                "  else:",
                "   target=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600,dir_fd=fd)",
                "   with os.fdopen(target,'wb') as out:",
                "    out.write(base64.b64decode(item['content']))",
                "    if item.get('mode'): os.fchmod(out.fileno(),0o755 if item['mode']=='100755' else 0o644)",
                " finally: os.close(fd)",
                "print('Workspace restored')",
            ]
        )
        result = await self._exec(run_id, "python3", "-c", script, stdin=json.dumps(changes).encode())
        if result.status != "PASSED":
            raise DeliveryError("WORKSPACE_RESTORE_FAILED", "工作文件恢复失败，不能继续执行命令")

    async def write(self, run_id: UUID, path: str, content: str) -> SandboxResult:
        safe_path(path)
        # 使用 dir_fd + O_NOFOLLOW 逐级打开，避免父目录符号链接及检查/打开间竞态。
        script = "\n".join(
            [
                "import os,sys,json,base64",
                "data=json.load(sys.stdin)",
                "parts=data['path'].split('/')",
                "fd=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY)",
                "for part in parts[:-1]:",
                " try: os.mkdir(part,dir_fd=fd)",
                " except FileExistsError: pass",
                " next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)",
                " os.close(fd); fd=next_fd",
                "target=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600,dir_fd=fd)",
                "with os.fdopen(target,'wb') as out: out.write(base64.b64decode(data['content']))",
                "os.close(fd)",
                "print('File written inside approved sandbox')",
            ]
        )
        payload = json.dumps({"path": path, "content": base64.b64encode(content.encode()).decode()}).encode()
        return await self._exec(run_id, "python3", "-c", script, stdin=payload)

    async def diff(self, run_id: UUID, scope: PlanScope) -> SandboxResult:
        # 将未跟踪文件纳入 Diff；先检查每个改动路径，再输出正文。
        staged = await self._exec(run_id, "git", "add", "--intent-to-add", "--all")
        if staged.status != "PASSED" or staged.exit_code != 0:
            return staged
        names = await self._exec(
            run_id, "git", "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--"
        )
        if names.status != "PASSED" or names.exit_code != 0:
            return names
        if any(not scope.allows_path(item) for item in names.output.split("\x00") if item):
            raise DeliveryError("PATH_DENIED", "沙箱有超出批准范围的修改，禁止交付")
        return await self._exec(
            run_id, "git", "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "HEAD", "--"
        )

    async def cleanup(self, run_id: UUID) -> None:
        """删除指定 Run 容器即可停止全部容器进程；不触碰宿主文件树。"""
        if run_id not in self._owned_runs:
            return
        try:
            inspected, output = await self._cli("inspect", "--format", "{{json .}}", self.name(run_id))
        except TimeoutError:
            raise DeliveryError("CLEANUP_REQUIRED", "容器状态确认超时，需要后台补偿清理") from None
        if inspected:
            raise DeliveryError("CLEANUP_REQUIRED", "无法确认本 Worker 容器的状态，需要后台补偿清理")
        try:
            record = json.loads(output)
            labels = record["Config"]["Labels"]
            container_id = record["Id"]
            if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
                raise ValueError
            if (
                labels.get("weora.instance") != self._instance_id
                or labels.get("weora.run") != str(run_id)
                or labels.get("weora.deployment") != self._config().deployment_id
            ):
                # 迟到清理不能删除后来 Worker 的同 Run 容器。
                self._owned_runs.discard(run_id)
                return
        except (ValueError, AttributeError, KeyError, TypeError):
            raise DeliveryError("CLEANUP_REQUIRED", "无法确认容器所有权，需要后台补偿清理") from None
        try:
            code, _ = await self._cli("rm", "--force", container_id)
        except TimeoutError:
            raise DeliveryError("CLEANUP_REQUIRED", "沙箱清理超时，需要后台补偿核对") from None
        if code:
            raise DeliveryError("CLEANUP_REQUIRED", "沙箱清理未确认成功，需要后台补偿清理")
        self._owned_runs.discard(run_id)

    async def reap_orphans(self, is_live: Callable[[UUID], Awaitable[bool]]) -> list[UUID]:
        """仅回收已失去有效租约的受管容器，失败项交后台下次重试。

        必须按不可变完整 container ID 删除，不能在 inspect 后按可被替换的 name 删除。
        is_live 由 Runs 应用 API 判断；这里不直接读取其他模块的 ORM。
        """
        if self.config is None:
            return []
        code, output = await self._cli(
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "label=weora.managed=v1",
            "--filter",
            f"label=weora.deployment={self.config.deployment_id}",
            "--format",
            "{{.ID}}",
        )
        if code:
            raise DeliveryError("CLEANUP_REQUIRED", "无法查询受管容器，需要后台重试清理")
        failures: list[UUID] = []
        for raw_id in output.decode("ascii", errors="replace").splitlines():
            if not re.fullmatch(r"[0-9a-f]{64}", raw_id):
                continue
            inspected, raw = await self._cli("inspect", "--format", "{{json .}}", raw_id)
            if inspected:
                continue  # 容器可能已被同一后台任务的另一个实例清理。
            try:
                record = json.loads(raw)
                labels = record["Config"]["Labels"]
                run_id = UUID(labels["weora.run"])
                if (
                    record["Id"] != raw_id
                    or record["Name"] != "/" + self.name(run_id)
                    or labels.get("weora.managed") != "v1"
                    or labels.get("weora.deployment") != self.config.deployment_id
                    or not re.fullmatch(r"[0-9a-f]{32}", labels.get("weora.instance", ""))
                    or not re.fullmatch(r"[0-9a-f]{64}", labels.get("weora.scope", ""))
                ):
                    continue
            except (ValueError, KeyError, TypeError, AttributeError):
                continue  # 不完整标签不是可自动删除的目标。
            if await is_live(run_id):
                continue
            removed, _ = await self._cli("rm", "--force", raw_id)
            if removed:
                failures.append(run_id)
        return failures

    async def export_workspace(self, run_id: UUID, scope: PlanScope) -> list[dict[str, str | None]]:
        """工作恢复额外保留可执行位，不改变 Git 托管发布接口原有的文件契约。"""
        return await self.export_changes(run_id, scope, include_modes=True)

    async def export_changes(
        self,
        run_id: UUID,
        scope: PlanScope,
        *,
        include_modes: bool = False,
    ) -> list[dict[str, str | None]]:
        """只读导出批准路径的最终文件，提交凭据仅出现在控制面 HTTP 代理。"""
        if not self.owns(run_id) or not await self._inspect(run_id, scope):
            raise DeliveryError("SANDBOX_RECOVERY_REQUIRED", "工作区不属于当前执行者或已停止")
        # Diff 超时/失败意味着范围检查未完成，不能继续读取并交付文件。
        checked = await self.diff(run_id, scope)
        if checked.status != "PASSED" or checked.exit_code != 0:
            raise DeliveryError("EXPORT_FAILED", "沙箱改动范围尚未成功核验，禁止导出")
        script = "\n".join(
            [
                "import os,sys,json,subprocess,stat,base64",
                "names=subprocess.check_output(['git','diff','--no-ext-diff','--no-textconv',"
                "'--name-only','-z','HEAD','--']).split(b'\\x00')",
                "out=[]; total=0",
                "for raw in names:",
                " if not raw: continue",
                " path=raw.decode('utf8'); parts=path.split('/')",
                " fd=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY)",
                " try:",
                "  for part in parts[:-1]:",
                "   nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd); os.close(fd); fd=nxt",
                "  try: target=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)",
                "  except FileNotFoundError: out.append({'path':path,'content':None}); continue",
                "  with os.fdopen(target,'rb') as data:",
                "   mode=os.fstat(data.fileno()).st_mode; assert stat.S_ISREG(mode)",
                "   content=data.read(262145); assert len(content)<=262144",
                "  total+=len(content); assert total<=1048576",
                "  item={'path':path,'content':base64.b64encode(content).decode()}",
                "  if sys.argv[1]=='1': item['mode']='100755' if mode&0o111 else '100644'",
                "  out.append(item)",
                " finally: os.close(fd)",
                "print(json.dumps(out))",
            ]
        )
        result = await self._exec(run_id, "python3", "-c", script, "1" if include_modes else "0", output_limit=1500000)
        if result.status != "PASSED":
            raise DeliveryError("EXPORT_FAILED", "沙箱文件无法安全导出")
        try:
            data = json.loads(result.output)
            if not isinstance(data, list) or len(data) > 500:
                raise ValueError
            exports: list[dict[str, str | None]] = []
            for item in data:
                if not isinstance(item, dict) or set(item) not in ({"path", "content"}, {"path", "content", "mode"}):
                    raise ValueError
                if not isinstance(item["path"], str) or not scope.allows_path(item["path"]):
                    raise ValueError
                if item["content"] is not None and not isinstance(item["content"], str):
                    raise ValueError
                exported = {"path": item["path"], "content": item["content"]}
                if include_modes and item["content"] is not None:
                    if item.get("mode") not in {"100644", "100755"}:
                        raise ValueError
                    exported["mode"] = item["mode"]
                exports.append(exported)
            return exports
        except (ValueError, KeyError, TypeError):
            raise DeliveryError("EXPORT_FAILED", "沙箱返回了非法文件清单") from None
