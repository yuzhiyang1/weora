"""将当前职能与项目目录选择应用到已发布的团队路径。"""

from collections.abc import Sequence

from sqlalchemy import SQLColumnExpression, and_, false, func, or_, select, true
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from weora.modules.knowledge.models import KnowledgeEntry, KnowledgeMarkdownPath
from weora.modules.knowledge.namespace_policy import NamespaceSelection


def selected_folders(
    path: SQLColumnExpression[str], root: str, directories: Sequence[str],
) -> ColumnElement[bool]:
    """按完整目录前缀匹配，规则名中的下划线不参与通配。"""
    prefixes = (f"{root}/{name}/" for name in directories)
    return or_(false(), *(func.substr(path, 1, len(prefix)) == prefix for prefix in prefixes))


def namespace_visible(selection: NamespaceSelection | None) -> ColumnElement[bool]:
    if selection is None:
        return true()
    paths = aliased(KnowledgeMarkdownPath)
    disallowed = or_(
        and_(
            KnowledgeEntry.kind == "learning", paths.path.like("learnings/%/%"),
            ~selected_folders(paths.path, "learnings", selection.learnings),
        ),
        and_(
            KnowledgeEntry.kind == "rule", paths.path.like("rules/%/%"),
            ~selected_folders(paths.path, "rules", selection.knowledge),
        ),
        and_(
            KnowledgeEntry.kind == "doc", selected_folders(paths.path, "docs", selection.inactive_docs),
        ),
        and_(
            KnowledgeEntry.kind == "skill", paths.path.like("skills/%/%"),
            ~selected_folders(paths.path, "skills", selection.skills),
        ),
    )
    # 只有正式路径参与筛选，正文中的链接不会改变条目自身的可见范围。
    return ~select(paths.entry_id).where(
        paths.entry_id == KnowledgeEntry.id, paths.revision == KnowledgeEntry.published_revision,
        paths.position == 0, disallowed,
    ).correlate(KnowledgeEntry).exists()
