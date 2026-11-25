"""为产品调和的图谱映射保存两个已发布版本的引用。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0076"
down_revision = "20261119_0075"
branch_labels = None
depends_on = None

_TABLE = "knowledge_product_mappings"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("product_id", sa.Uuid(), primary_key=True),
        sa.Column("code_id", sa.Uuid(), primary_key=True),
        sa.Column("product_revision", sa.Integer(), nullable=False),
        sa.Column("code_revision", sa.Integer(), nullable=False),
        sa.Column("terms", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id", "product_revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["code_id", "code_revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("product_id <> code_id", name="ck_knowledge_product_mapping_distinct"),
    )
    op.create_index("ix_knowledge_product_mapping_code", _TABLE, ["project_id", "code_id"])


def downgrade() -> None:
    op.drop_table(_TABLE)
