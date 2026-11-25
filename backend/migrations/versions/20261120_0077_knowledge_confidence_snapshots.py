"""为学习条目的已发布版本保存维护时写回的置信度。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0077"
down_revision = "20261120_0076"
branch_labels = None
depends_on = None

_TABLE = "knowledge_confidence_snapshots"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("entry_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.ForeignKeyConstraint(
            ["entry_id", "revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name="ck_knowledge_confidence_score"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {_TABLE} LIMIT 1")) is not None:
        raise RuntimeError("存在已发布的知识置信度快照，不能丢失维护记录。")
    op.drop_table(_TABLE)
