"""在私有任务中复用同一套项目知识查询，作用域来自冻结任务。"""

from functools import partial
from uuid import UUID

from weora.modules.identity.infrastructure.unit_of_work import get_active_workspace_principal_in_session
from weora.modules.knowledge.codebase import CodebaseIndex
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.namespace_policy import hidden_wiki_ids, resolve_namespaces
from weora.modules.knowledge.store import KnowledgeStore, Scope
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.service import RunService
from weora.modules.tools.application.gateway import ToolGateway, ToolScope, ToolSpec, failure
from weora.shared.infrastructure.database import SessionFactory
from weora.shared.object_storage import ObjectStoragePort

_SEARCH = ToolSpec(
    "knowledge.search", "按当前项目召回范围检索已发布团队知识。用 knowledge.read 查看正文及出处。",
    {
        "type": "object", "additionalProperties": False, "required": ["query"],
        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 512}},
    },
)
_READ = ToolSpec(
    "knowledge.read", "读取此前检索到的团队知识正文；引用结论时一并说明其出处。",
    {
        "type": "object", "additionalProperties": False, "required": ["id"],
        "properties": {"id": {"type": "string", "pattern": "^[0-9a-fA-F-]{36}$"}},
    },
)


def register_knowledge_tools(
    gateway: ToolGateway, sessions: SessionFactory, runs: RunService, projects: ProjectService,
    storage: ObjectStoragePort | None = None,
) -> None:
    async def execute(
        scope: ToolScope, arguments: dict[str, object], *, search: bool,
    ) -> dict[str, object]:
        run = await runs.access_metadata(scope.actor, scope.run_id)
        # 群聊交付需要独立的来源授权，发起人的个人知识权限不能授予群成员。
        if not run or run.task.audience != "private":
            return failure("KNOWLEDGE_AUDIENCE_NOT_AUTHORIZED")
        frozen = run.task.project_context or {}
        workspace, user = scope.actor.workspace_id, scope.actor.user_id
        async with sessions() as db, db.begin():
            principal = await get_active_workspace_principal_in_session(db, user_id=user, workspace_id=workspace)
            if principal is None:
                return failure("ACCESS_REVOKED")
            try:
                project = UUID(str(frozen["project_id"])) if frozen else None
                connections: tuple[tuple[UUID, int], ...] = ()
                if project is not None:
                    actor = ProjectActor(user, workspace)
                    await projects.require_active_project(actor, project, db)
                    current = await projects.active_repository_connections(actor, project, db)
                    frozen_repositories = frozen.get("repositories")
                    allowed = {
                        (str(item.get("id")), item.get("connection_version"))
                        for item in frozen_repositories if isinstance(item, dict)
                    } if isinstance(frozen_repositories, list) else set()
                    connections = tuple(
                        (repo, version) for repo, version in current.items() if (str(repo), version) in allowed
                    )
                folders = await resolve_namespaces(db, workspace, user, project)
                hidden_wikis = await hidden_wiki_ids(db, workspace, project, folders)
                store = KnowledgeStore(
                    db, Scope(workspace, project, user, principal.role, connections, folders, hidden_wikis), storage,
                )
                code = CodebaseIndex(store, projects)
                # 同一个任务的续接运行共享采纳归因；工具网关负责每次调用去重。
                policy = (await store.session_policies())["recall"]
                session = str(run.task.id) if policy["enabled"] is True else None
                if search:
                    query = str(arguments["query"]).strip()
                    result = await store.search(
                        query, 10, session, recall=True, request_id=str(scope.run_id),
                    ) if query else {"items": []}
                    return await code.annotate(result)
                return await code.annotate(await store.detail(UUID(str(arguments["id"])), session, recall=True))
            except ValueError:
                return failure("TOOL_ARGUMENTS_INVALID")
            except (ProjectError, KnowledgeError) as exc:
                return failure(exc.code)

    gateway.register(_SEARCH, partial(execute, search=True))
    gateway.register(_READ, partial(execute, search=False))
