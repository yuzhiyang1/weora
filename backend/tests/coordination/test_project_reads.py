"""主会话只读配置仓库：不写 Run 表、不继承任意 URL，也不留下评估克隆。"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from tests.api.test_project_repositories import Reader
from tests.api.test_projects import project, repository
from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.projects.test_repository_reader import REVISION, Runner
from weora.modules.projects.domain import ProjectActor, ProjectError, RepositorySource
from weora.modules.projects.git_reader import GitRepositoryReader
from weora.modules.projects.service import ProjectService


@pytest.mark.anyio
async def test_assessment_pins_configured_repository_and_always_discards(runs: RunsHarness) -> None:
    service: ProjectService = runs.app.state.project_service
    reader = Reader()
    service.reader = reader
    p = await project(runs)
    selected = await repository(runs, p["id"], "selected")
    other = await repository(runs, p["id"], "unselected")
    actor = ProjectActor(UUID(runs.user_id), UUID(runs.workspace_id))
    value = await service.assessment_read(
        actor, UUID(str(p["id"])), (UUID(str(selected["id"])),), UUID(str(selected["id"])), "README.md"
    )
    reader.revision = "b" * 40
    repeated = await service.assessment_read(
        actor,
        UUID(str(p["id"])),
        (UUID(str(selected["id"])),),
        UUID(str(selected["id"])),
        "README.md",
        revision=str(value["revision"]),
    )
    assert repeated["revision"] == "a" * 40
    assert len(reader.checked) == 1 and len(reader.discarded) == 2
    with pytest.raises(ProjectError, match="当前选中"):
        await service.assessment_read(
            actor, UUID(str(p["id"])), (UUID(str(selected["id"])),), UUID(str(other["id"])), "README.md"
        )
    with pytest.raises(ProjectError, match="不支持目录"):
        await service.assessment_read(
            actor, UUID(str(p["id"])), (UUID(str(selected["id"])),), UUID(str(selected["id"])), "", directory=True
        )
    assert len(reader.discarded) == 3


@pytest.mark.anyio
async def test_git_object_directory_is_bounded_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    runner = Runner()
    reader = GitRepositoryReader(tmp_path, runner=runner)
    source = RepositorySource(uuid4(), uuid4(), "https://93.184.216.34/team/configured.git", "main")
    cache = await reader.clone(source, REVISION)
    directory = await reader.directory(cache, REVISION, "")
    assert directory["entries"] == [{"name": "README.md", "kind": "blob"}]
    runner.mode = b"120000"
    assert (await reader.directory(cache, REVISION, ""))["entries"] == []
    for path in ("../private", "/private", "a\\b", ".git/config", "x:y"):
        with pytest.raises(ProjectError):
            await reader.directory(cache, REVISION, path)
    await reader.discard(cache)
