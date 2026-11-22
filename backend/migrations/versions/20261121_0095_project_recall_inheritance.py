"""保存项目召回继承设置及其历史。"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0095"
down_revision = "20261121_0094"
branch_labels = None
depends_on = None

_TABLE = "knowledge_project_recall_policies"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("inherit_shared", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_project_recall_version", _TABLE, ["workspace_id", "project_id", "id"])


def downgrade() -> None:
    # 保留已经启用或停用过的继承历史。
    if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {_TABLE} LIMIT 1")):
        raise RuntimeError("已有项目召回继承设置历史，无法无损回退。")
    op.drop_table(_TABLE)
