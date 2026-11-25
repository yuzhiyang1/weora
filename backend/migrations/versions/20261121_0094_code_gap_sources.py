"""索引不可变缺口页对应的实际源码位置。"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0094"
down_revision = "20261121_0093"
branch_labels = None
depends_on = None

_TABLE = "knowledge_code_gap_sources"
_KEYS = ("name_key", "alias_key", "path_key", "basename_key")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("entry_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("finding", sa.String(48), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("line", sa.Integer(), nullable=True),
        *(sa.Column(key, sa.String(64), nullable=True) for key in _KEYS),
        sa.ForeignKeyConstraint(
            ["entry_id", "revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("line IS NULL OR line > 0", name="ck_knowledge_gap_source_line"),
    )
    for key in _KEYS:
        op.create_index("ix_gap_source_" + key, _TABLE, [key])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {_TABLE} LIMIT 1")):
        raise RuntimeError("已保存缺口的文件和行号证据，无法无损回退。")
    op.drop_table(_TABLE)
