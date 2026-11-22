"""让已发布的知识路径索引也能保存显式声明及审核结果。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0080"
down_revision = "20261120_0079"
branch_labels = None
depends_on = None

_PATHS = "knowledge_markdown_paths"
_VERSIONS = "knowledge_revisions"
_PRIMARY = "knowledge_markdown_paths_pkey"


def upgrade() -> None:
    op.add_column(_VERSIONS, sa.Column("relation_findings", sa.JSON()))
    op.add_column(_PATHS, sa.Column("kind", sa.String(16), nullable=False, server_default="REFERENCES"))
    op.drop_constraint(_PRIMARY, _PATHS, type_="primary")
    op.create_primary_key(_PRIMARY, _PATHS, ["entry_id", "revision", "key", "kind"])


def downgrade() -> None:
    db = op.get_bind()
    if db.scalar(sa.text(f"SELECT 1 FROM {_PATHS} WHERE position < 0 LIMIT 1")) or db.scalar(
        sa.text(f"SELECT 1 FROM {_VERSIONS} WHERE json_array_length(relation_findings) > 0 LIMIT 1"),
    ):
        raise RuntimeError("已有团队路径声明或审核结果，回退会丢失贡献数据。")
    op.drop_constraint(_PRIMARY, _PATHS, type_="primary")
    op.create_primary_key(_PRIMARY, _PATHS, ["entry_id", "revision", "key"])
    op.drop_column(_PATHS, "kind")
    op.drop_column(_VERSIONS, "relation_findings")
