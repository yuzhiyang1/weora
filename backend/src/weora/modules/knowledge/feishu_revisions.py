"""飞书显式知识更新请求只提交项目修订，沿用条目版本与审核。"""

import re
from functools import partial
from typing import cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid5

from weora.modules.collaboration.application.tasks import TaskRecord
from weora.modules.context.domain import ContextActor, ContextError, ContextRunScope, Json
from weora.modules.context.service import ContextService
from weora.modules.identity.infrastructure.unit_of_work import get_active_workspace_principal_in_session
from weora.modules.knowledge.domain import KnowledgeError
from weora.modules.knowledge.namespace_policy import resolve_namespaces
from weora.modules.knowledge.store import KnowledgeStore, Scope
from weora.modules.projects.domain import ProjectActor, ProjectError
from weora.modules.projects.service import ProjectService
from weora.modules.runs.application.service import RunService
from weora.modules.runs.infrastructure.module_access import context_scope
from weora.modules.tools.application.gateway import ToolGateway, ToolScope, ToolSpec, failure
from weora.shared.infrastructure.database import SessionFactory

_REQUEST = re.compile(
    r"^(?:(?:请你?|麻烦你?|帮我)\s*)*(?:"
    r"(?:更新|修改|修订)(?:当前|本)?\s*(?:项目)?知识库"
    r"|提交(?:项目)?知识修订"
    r"|(?:把|将)[^\n。！？]{1,120}?(?:更新|写入|沉淀|同步)(?:到|进|至)?(?:当前|本)?\s*(?:项目)?知识库"
    r")(?=$|[\s，。,:：；;！!]|里|中)"
)
_UUID = {"type": "string", "pattern": "^[0-9a-fA-F-]{36}$"}
_LINK = re.compile(r"https://[^\s<>\x22\x27，。；！\]）]+")

_TARGETS = ToolSpec(
    "knowledge.find_revision_target",
    "用 query 在本项目搜索可修订条目。目标明确后用 id 读取发布正文和基准版本，长正文使用 next_offset 继续读取。",
    {
        "type": "object", "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 512},
            "id": _UUID,
            "offset": {"type": "integer", "minimum": 0, "maximum": 64000},
        },
    },
)
_SUBMIT = ToolSpec(
    "knowledge.submit_revision",
    "以群消息发起人的权限提交既有条目的一处修订待审。原片段必须逐字匹配且唯一。"
    "如有请求或被引用消息中的 PRD 链接，可填写 prd_url。",
    {
        "type": "object", "additionalProperties": False,
        "required": ["id", "base_revision", "expected_text", "replacement"],
        "properties": {
            "id": _UUID,
            "base_revision": {"type": "integer", "minimum": 1, "maximum": 2147483647},
            "expected_text": {"type": "string", "minLength": 1, "maxLength": 8000},
            "replacement": {"type": "string", "minLength": 1, "maxLength": 8000},
            "prd_url": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
    },
    effect="write",
)
FEISHU_REVISION_TOOLS = frozenset((_TARGETS.name, _SUBMIT.name))


def requests_knowledge_revision(instruction: str) -> bool:
    """在可信的飞书请求正文中识别显式修订指令。"""
    return _REQUEST.match(instruction) is not None


def is_feishu_revision_request(task: TaskRecord) -> bool:
    """以本次已认证的消息为准，群历史、跟帖及协调任务都不能继承写入意图。"""
    origin = task.origin_metadata or {}
    return (
        task.audience == "group" and task.origin == "feishu" and bool(task.project_context)
        and origin.get("mentioned") is True and not origin.get("coordination_scope_id")
        and requests_knowledge_revision(task.instruction)
    )


def register_feishu_revision_tools(
    gateway: ToolGateway, sessions: SessionFactory, runs: RunService, projects: ProjectService, context: ContextService,
) -> None:
    async def execute(scope: ToolScope, arguments: dict[str, object], *, submit: bool) -> dict[str, object]:
        if not submit and ("query" in arguments) == ("id" in arguments):
            return failure("TOOL_ARGUMENTS_INVALID")
        detail = await runs.access_metadata(scope.actor, scope.run_id)
        if detail is None or not is_feishu_revision_request(detail.task):
            return failure("KNOWLEDGE_AUDIENCE_NOT_AUTHORIZED")
        origin = detail.task.origin_metadata or {}
        workspace, user = scope.actor.workspace_id, scope.actor.user_id
        try:
            current = context_scope(ContextActor(user, workspace), detail.run, detail.task)
            if current.source_id is None or not isinstance(origin.get("message_id"), str):
                return failure("KNOWLEDGE_FEISHU_SOURCE_REQUIRED")
            entry_id = UUID(str(arguments["id"])) if "id" in arguments else None
            evidence: dict[str, str] = {}
            if submit:
                evidence = await _evidence(context, current, origin, detail.task.instruction, arguments)
            async with sessions() as db, db.begin():
                principal = await get_active_workspace_principal_in_session(db, user_id=user, workspace_id=workspace)
                if principal is None:
                    return failure("ACCESS_REVOKED")
                project = UUID(str((detail.task.project_context or {})["project_id"]))
                await projects.require_active_project(ProjectActor(user, workspace), project, db)
                folders = await resolve_namespaces(db, workspace, user, project)
                store = KnowledgeStore(db, Scope(workspace, project, user, principal.role, namespaces=folders,
                                                 include_shared=False))
                if not submit:
                    return await store.channel_revision_targets(
                        query=str(arguments.get("query", "")).strip(), entry_id=entry_id,
                        offset=int(str(arguments.get("offset", 0))),
                    )
                assert entry_id is not None  # Gateway 校验了修订必填的条目标识。
                operation = uuid5(current.source_id, f"feishu-knowledge-revision/{origin['message_id']}")
                return await store.propose_channel_revision(
                    entry_id, int(str(arguments["base_revision"])), operation,
                    str(arguments["expected_text"]), str(arguments["replacement"]), evidence,
                )
        except ValueError:
            return failure("TOOL_ARGUMENTS_INVALID")
        except (ContextError, ProjectError, KnowledgeError) as exc:
            return failure(exc.code)

    gateway.register(_TARGETS, partial(execute, submit=False))
    gateway.register(_SUBMIT, partial(execute, submit=True))


async def _evidence(
    context: ContextService, scope: ContextRunScope, origin: Json, instruction: str, arguments: dict[str, object],
) -> dict[str, str]:
    """引用从本次已授权快照回读，审核记录只保存出处而不复制群聊原文。"""
    evidence = {
        "feishu_source_id": str(scope.source_id),
        "feishu_message_id": str(origin["message_id"]),
    }
    sources = [instruction]
    if quote := origin.get("quote_id"):
        snapshots = await context.snapshots(scope)
        quoted = next((
            item for snapshot in snapshots if snapshot.get("source_id") == str(scope.source_id)
            for item in cast(list[Json], snapshot.get("messages", []))
            if item["message_id"] == quote and item["parse_status"] == "complete"
        ), None)
        if quoted is None:
            raise ContextError("KNOWLEDGE_FEISHU_QUOTE_UNAVAILABLE", "本次运行没有读到被引用的群消息。")
        evidence["feishu_quote_id"] = str(quote)
        sources.append(str(quoted["text"]))
    if url := arguments.get("prd_url"):
        supplied = str(url)
        if not any(supplied == link.rstrip(").,;!?") for source in sources for link in _LINK.findall(source)):
            raise ContextError("KNOWLEDGE_FEISHU_PRD_UNVERIFIED", "PRD 链接须来自请求或引用消息。")
        link = urlsplit(supplied)
        if link.scheme != "https" or not link.hostname or link.username is not None or link.password is not None:
            raise ValueError
        # 只保留文档定位，查询参数可能带有临时访问凭据。
        evidence["feishu_prd_ref"] = urlunsplit((link.scheme, link.netloc, link.path, "", link.fragment))
    return evidence
