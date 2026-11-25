"""保留知识晋升 Skill 的项目范围。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0061"
down_revision = "20261119_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 已安装的共享 Skill 保持原有范围。
    op.add_column("skills", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_skills_project_id", "skills", "projects", ["project_id"], ["id"])
    op.create_index("ix_skills_workspace_project_id", "skills", ["workspace_id", "project_id"])


def downgrade() -> None:
    # 移除范围字段会使项目资产进入共享清单，存在项目资产时禁止降级。
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT 1 FROM skills WHERE project_id IS NOT NULL LIMIT 1")):
        raise RuntimeError("存在项目 Skill，无法移除项目范围字段。")
    op.drop_index("ix_skills_workspace_project_id", table_name="skills")
    op.drop_constraint("fk_skills_project_id", "skills", type_="foreignkey")
    op.drop_column("skills", "project_id")
