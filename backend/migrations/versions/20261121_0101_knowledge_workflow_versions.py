"""保存团队知识工作流覆盖、默认恢复和变更审计。"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0101"
down_revision = "20261121_0100"
branch_labels = None
depends_on = None

TABLE = "knowledge_workflow_versions"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("definition", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_workflow_versions LIMIT 1")):
        raise RuntimeError("存在团队知识工作流配置记录，无法安全移除。")
    op.drop_table(TABLE)
