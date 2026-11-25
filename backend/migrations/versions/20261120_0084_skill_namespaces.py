"""用团队目录和所属项目区分同名 Skill，保留现有资产的原路径。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0084"
down_revision = "20261120_0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("namespace", sa.String(512), nullable=False, server_default=""))
    op.drop_constraint("uq_skills_workspace_slug", "skills", type_="unique")
    op.create_index(
        "uq_skills_shared_namespace_slug", "skills", ["workspace_id", "namespace", "slug"],
        unique=True, postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_skills_project_namespace_slug", "skills", ["workspace_id", "project_id", "namespace", "slug"],
        unique=True, postgresql_where=sa.text("project_id IS NOT NULL"),
    )


def downgrade() -> None:
    # 旧约束没有目录或项目维度，回退不得丢掉路径或使资产合并。
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM skills WHERE namespace <> '' LIMIT 1")).first() or connection.execute(
        sa.text("SELECT 1 FROM skills GROUP BY workspace_id, slug HAVING count(*) > 1 LIMIT 1")
    ).first():
        raise RuntimeError("已有 Skill 目录或跨项目同名资产，无法无损回退")
    op.drop_index("uq_skills_project_namespace_slug", table_name="skills")
    op.drop_index("uq_skills_shared_namespace_slug", table_name="skills")
    op.create_unique_constraint("uq_skills_workspace_slug", "skills", ["workspace_id", "slug"])
    op.drop_column("skills", "namespace")
