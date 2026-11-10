"""项目固定提交与沙箱衔接；Fake Docker 不冒充真实容器验收。"""

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from tests.api.test_project_repositories import Reader
from tests.api.test_projects import project, projects_path, repository, repository_input, task
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.delivery.test_adapters import CaptureDocker, snapshot
from weora.bootstrap.project_execution import ProjectExecution
from weora.modules.delivery.domain.models import DeliveryError, PlanScope
from weora.modules.delivery.infrastructure.docker import DockerResources
from weora.modules.projects.domain import ProjectActor, RepositoryArchive
from weora.modules.runs.application.ports import RunActor


@pytest.mark.anyio
async def test_selected_revision_export_has_no_default_fallback_and_rechecks_access(
    runs: RunsHarness, tmp_path: Path
) -> None:
    archive = snapshot(tmp_path)

    class Exporter(Reader):
        async def archive(self, cache_id: UUID, revision: str) -> RepositoryArchive:
            return RepositoryArchive(archive.archive_path, revision, archive.archive_sha256)

    projects = runs.app.state.project_service
    projects.reader = Exporter()
    p = await project(runs)
    a = await repository(runs, p["id"], "selected")
    b = await repository(runs, p["id"], "unselected")
    created = (await task(runs, p["id"], [a["id"]])).json()
    actor = RunActor(UUID(runs.user_id), UUID(runs.workspace_id))
    run_id = UUID(created["id"])
    docker = CaptureDocker(archive)
    bridge = ProjectExecution(projects, runs.app.state.run_service, docker, enabled=True)
    scope = PlanScope(str(a["id"]), "a" * 40, ("src",), (), ("file.write", "git.diff"))
    with pytest.raises(DeliveryError, match="尚未准备固定提交"):
        await bridge.validate(actor, run_id, scope)
    await projects.read_file(
        ProjectActor(actor.user_id, actor.workspace_id),
        run_id,
        created["project_context"],
        UUID(str(a["id"])),
        "README.md",
    )
    await bridge.validate(actor, run_id, scope)
    for bad in [replace(scope, repository_id=str(b["id"])), replace(scope, base_revision="b" * 40)]:
        with pytest.raises(DeliveryError):
            await bridge.validate(actor, run_id, bad)
    await bridge.prepare(actor, run_id, scope)
    assert docker.has_bound_snapshot(run_id, scope)
    assert not archive.archive_path.exists()
    assert any(call[0] == "create" for call in docker.calls)
    # 相同仓库也不能借用另一 Run 的私有快照或原 test-repo 默认副本。
    with pytest.raises(DeliveryError, match="固定基准快照"):
        docker._archive(scope, uuid4())
    disabled = await runs.client.put(
        f"{projects_path(runs)}/{p['id']}/repositories/{a['id']}",
        json=repository_input(a, status="disabled"),
        headers=runs.headers(),
    )
    assert disabled.status_code == 200
    with pytest.raises(DeliveryError):
        await bridge.validate(actor, run_id, scope)


def test_bound_snapshot_cannot_be_replaced_by_another_scope(tmp_path: Path) -> None:
    archive = snapshot(tmp_path)
    docker = CaptureDocker(archive)
    run_id = uuid4()
    scope = PlanScope(str(uuid4()), "a" * 40, ("src",), (), ("file.write",))
    docker.bind_snapshot(run_id, scope, archive)
    with pytest.raises(DeliveryError, match="已有的代码副本"):
        docker.bind_snapshot(run_id, replace(scope, paths=("tests",)), archive)


@pytest.mark.anyio
async def test_project_image_is_pinned_without_changing_legacy_default(tmp_path: Path) -> None:
    archive = snapshot(tmp_path)
    docker = CaptureDocker(archive)
    assert docker.config is not None
    default_image = docker.config.image
    project_image = "sha256:" + "c" * 64
    run_id = uuid4()
    selected = PlanScope(str(uuid4()), "a" * 40, ("src",), (), ("file.write",))
    docker.bind_snapshot(run_id, selected, archive, image=project_image)
    await docker.prepare(run_id, selected)
    create = next(call for call in docker.calls if call[0] == "create")
    assert project_image in create and default_image not in create
    assert docker.config.image == default_image
    with pytest.raises(DeliveryError, match="不能被替换"):
        docker.bind_snapshot(run_id, selected, archive, image="sha256:" + "d" * 64)


@pytest.mark.anyio
async def test_java_resources_are_frozen_per_run_and_preserve_offline_boundary(tmp_path: Path) -> None:
    """Java 提高构建预算，但不改变旧镜像、网络隔离和宿主挂载规则。"""
    archive = snapshot(tmp_path)
    docker = CaptureDocker(archive)
    assert docker.config is not None
    image = "sha256:" + "c" * 64
    resources = DockerResources(memory_mb=3072, cpus=2, timeout_seconds=600, workspace_mb=1024, temporary_mb=256)
    docker.config = replace(docker.config, image_resources={image: resources})
    run = uuid4()
    scope = PlanScope(str(uuid4()), "a" * 40, ("src",), (), ("command.run",))
    docker.bind_snapshot(run, scope, archive, image=image)
    await docker.prepare(run, scope)
    create = next(call for call in docker.calls if call[0] == "create")
    assert create[create.index("--memory") + 1] == "3072m"
    assert create[create.index("--network") + 1] == "none"
    assert "/workspace:rw,nosuid,nodev,size=1073741824,uid=10001,gid=10001,mode=0700" in create
    assert not ({"--mount", "--volume", "--privileged", "-v"} & set(create))
    assert docker.config.memory_mb == 512 and docker.config.timeout_seconds == 60
    # 配置更改不能让同一 Run 在未重建时静默提高预算。
    docker.config = replace(docker.config, image_resources={image: replace(resources, memory_mb=4096)})
    with pytest.raises(DeliveryError, match="预算不能被替换"):
        docker.bind_snapshot(run, scope, archive, image=image)


@pytest.mark.parametrize(
    "resources",
    [DockerResources(memory_mb=4097), DockerResources(workspace_mb=0), DockerResources(timeout_seconds=601)],
)
def test_invalid_image_resources_fail_before_container_creation(tmp_path: Path, resources: DockerResources) -> None:
    docker = CaptureDocker(snapshot(tmp_path))
    assert docker.config is not None
    docker.config = replace(docker.config, image_resources={"sha256:" + "c" * 64: resources})
    with pytest.raises(DeliveryError):
        docker.config.validate()
