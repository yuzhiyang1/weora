"""为本机后台证据和团队归档记录相同的评分来源。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0054"
down_revision = "20261119_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_shared_sessions", sa.Column("score_source_key", sa.String(64)))
    op.add_column("knowledge_shared_session_revisions", sa.Column("local_run_mode", sa.String(12)))
    op.add_column("knowledge_shared_session_revisions", sa.Column("score_fingerprint", sa.String(64)))
    op.create_index(
        "ix_knowledge_scoring_source", "knowledge_shared_sessions", ["workspace_id", "author_id", "score_source_key"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(
        "SELECT 1 FROM knowledge_shared_session_revisions WHERE local_run_mode IS NOT NULL LIMIT 1",
    )):
        raise RuntimeError("存在本机后台评分证据，回退会丢失运行模式及证据关联。")
    op.drop_column("knowledge_shared_session_revisions", "score_fingerprint")
    op.drop_column("knowledge_shared_session_revisions", "local_run_mode")
    op.drop_index("ix_knowledge_scoring_source", table_name="knowledge_shared_sessions")
    op.drop_column("knowledge_shared_sessions", "score_source_key")
