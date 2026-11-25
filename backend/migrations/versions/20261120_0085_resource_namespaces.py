"""保存职能与项目目录清单的不可变版本。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0085"
down_revision = "20261120_0084"
branch_labels = None
depends_on = None

_TABLE = "knowledge_namespace_manifests"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_knowledge_namespace_revision"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM knowledge_namespace_manifests LIMIT 1")).first():
        raise RuntimeError("已有资源目录清单审计，无法无损回退")
    op.drop_table(_TABLE)
