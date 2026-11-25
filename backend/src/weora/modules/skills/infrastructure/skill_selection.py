"""在知识召回和设备安装之间共享项目 Skill 的选择规则。"""

from uuid import UUID

from sqlalchemy import and_, case, func, literal, or_, select, true
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

from weora.modules.skills.infrastructure.models import SkillModel


def visible_skill_scope(
    workspace: UUID, project: UUID | None, *, include_shared: bool = True,
) -> ColumnElement[bool]:
    """共享资产对工作空间可见，项目资产只在已授权项目可见。"""
    shared = SkillModel.project_id.is_(None)
    current = SkillModel.project_id == project
    return and_(SkillModel.workspace_id == workspace, or_(shared, current) if include_shared else current)


def skill_tree_path() -> ColumnElement[str]:
    root = literal("skills/")
    prefix = case((SkillModel.namespace == "", root), else_=root.concat(SkillModel.namespace).concat("/"))
    return prefix.concat(SkillModel.slug)


def visible_skill_directories(directories: tuple[str, ...] | None) -> ColumnElement[bool]:
    if directories is None:
        return true()
    location = SkillModel.namespace
    prefixes = (f"{directory}/" for directory in directories)
    return or_(
        location == "", location.in_(directories),
        *(func.substr(location, 1, len(prefix)) == prefix for prefix in prefixes),
    )


def ranked_skill_sources(
    workspace: UUID, project: UUID | None, directories: tuple[str, ...] | None = None,
    *, include_shared: bool = True,
) -> CTE:
    """同一个安装名只交付一份，同权限层的多个激活目录报告冲突。"""
    scope = visible_skill_scope(workspace, project, include_shared=include_shared)
    if directories is not None:
        return _selected_skill_sources(scope, directories)
    skill = SkillModel
    project_copy = skill.project_id.is_not(None)
    namespaced_project_copy = and_(project_copy, skill.namespace != "")
    # 项目覆盖共享来源；一个项目内的有效命名空间覆盖项目根目录。
    order = (
        case((project_copy, 0), else_=1),
        case((namespaced_project_copy, 0), else_=1),
        skill.namespace,
        skill.id,
    )
    return select(
        skill.id, skill.slug,
        func.row_number().over(partition_by=skill.slug, order_by=order).label("source_rank"),
        func.count().filter(namespaced_project_copy).over(partition_by=skill.slug).label("namespace_copies"),
    ).where(
        scope,
        skill.status == "active", skill.latest_published_version_id.is_not(None),
    ).cte("ranked_skill_sources")


def _selected_skill_sources(scope: ColumnElement[bool], directories: tuple[str, ...]) -> CTE:
    skill = SkillModel
    ownership = func.row_number().over(
        partition_by=(skill.namespace, skill.slug), order_by=(skill.project_id.is_(None), skill.id),
    )
    # 同一个完整路径可以由当前项目覆盖共享副本；不同目录有独立的来源身份。
    visible = select(skill.id, skill.slug, skill.namespace, ownership.label("ownership")).where(
        scope, visible_skill_directories(directories),
        skill.status == "active", skill.latest_published_version_id.is_not(None),
    ).subquery()
    named = visible.c.namespace != ""
    return select(
        visible.c.id, visible.c.slug,
        func.row_number().over(
            partition_by=visible.c.slug, order_by=(case((named, 0), else_=1), visible.c.id),
        ).label("source_rank"),
        func.count().filter(named).over(partition_by=visible.c.slug).label("namespace_copies"),
    ).where(visible.c.ownership == 1).cte("ranked_skill_sources")
