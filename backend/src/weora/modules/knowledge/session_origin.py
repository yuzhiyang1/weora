"""将设备上报的 Git 启动证据绑定到项目已登记的仓库版本。"""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from weora.modules.knowledge.models import KnowledgeSessionOrigin
from weora.modules.knowledge.review_source import registered_repo
from weora.modules.knowledge.store import KnowledgeStore
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.service import ProjectService


class GitStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(min_length=1, max_length=512)
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    tracked_changes: StrictBool
    observed_at: AwareDatetime


async def remember_start(
    store: KnowledgeStore, projects: ProjectService, key: str, evidence: GitStartInput,
    captured_at: datetime | None,
) -> None:
    """只保存可绑定的首次证据。仓库地址用于比对，持久化时只保留仓库 ID。"""
    scope, db = store.scope, store.db
    now = datetime.now(UTC)
    if scope.project is None or evidence.observed_at > min(captured_at or now, now):
        return
    identity = registered_repo(evidence.repository)
    if identity is None:
        return
    actor = ProjectActor(scope.user, scope.workspace)
    addresses = await projects.active_repository_addresses(actor, scope.project, db)
    matches = [(repo, version) for repo, version in scope.code_connections
               if repo in addresses and registered_repo(addresses[repo]) == identity]
    if len(matches) != 1:
        return
    repo, version = matches[0]
    try:
        await projects.lock_active_repository(actor, scope.project, repo, version, db)
    except ProjectError:
        return
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(insert(KnowledgeSessionOrigin).values(
        workspace_id=scope.workspace, project_id=scope.project, author_id=scope.user, subject_key=key,
        repository_id=repo, connection_version=version, commit=evidence.commit,
        tracked_changes=evidence.tracked_changes, observed_at=evidence.observed_at,
    ).on_conflict_do_nothing(index_elements=["workspace_id", "project_id", "author_id", "subject_key"]))


def start_state(
    evidence: KnowledgeSessionOrigin | None, connections: dict[UUID, int], captured_at: datetime,
) -> dict[str, object]:
    if evidence is None or _utc(evidence.observed_at) > _utc(captured_at):
        return {"status": "not_recorded"}
    if connections.get(evidence.repository_id) != evidence.connection_version:
        return {"status": "repository_unavailable"}
    return {
        "status": "tracked_changes" if evidence.tracked_changes else "recorded",
        "repository_id": evidence.repository_id, "commit": evidence.commit,
        "observed_at": _utc(evidence.observed_at).isoformat(),
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
