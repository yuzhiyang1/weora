"""固定已结束根会话的产出及结算口径，保留隐私范围。"""

import hashlib
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision = "20261119_0073"
down_revision = "20261119_0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_revisions", sa.Column("source_session_key", sa.String(64)))
    op.create_index(
        "ix_knowledge_revision_session_source", "knowledge_revisions",
        ["workspace_id", "author_id", "source_session_key"],
    )
    # 已有经验可以在新的会话结算中被识别。迁移只读取来源标记，不读取正文。
    db = op.get_bind()
    source_rows = db.execute(sa.text(
        "SELECT entry_id, revision, source ->> 'ref' AS ref FROM knowledge_revisions "
        "WHERE source ->> 'type' = 'session'",
    )).mappings()
    for row in source_rows:
        runtime, _, value = (row["ref"] or "").partition(":")
        if runtime not in ("codex", "claude-code"):
            continue
        try:
            native = UUID(value)
        except ValueError:
            continue
        digest = hashlib.sha256(f"weora-score-subject:{runtime}:{native}".encode()).hexdigest()
        db.execute(sa.text(
            "UPDATE knowledge_revisions SET source_session_key = :digest WHERE entry_id = :entry AND revision = :rev",
        ), {"digest": digest, "entry": row["entry_id"], "rev": row["revision"]})
    op.create_table(
        "knowledge_session_outcomes",
        sa.Column("activity_id", sa.Uuid(), sa.ForeignKey("knowledge_session_activity.id"), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("run_mode", sa.String(16), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("has_commit", sa.Boolean(), nullable=False),
        sa.Column("has_pr", sa.Boolean(), nullable=False),
        sa.Column("has_learning", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("interventions", sa.Integer(), nullable=False),
        sa.CheckConstraint("NOT has_pr OR has_commit", name="ck_knowledge_outcome_pr_has_commit"),
    )
    op.create_index("ix_knowledge_outcome_scope", "knowledge_session_outcomes", ["workspace_id", "project_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_session_outcomes LIMIT 1")):
        raise RuntimeError("已记录会话产出，回退会丢失结算快照。")
    op.drop_table("knowledge_session_outcomes")
    op.drop_index("ix_knowledge_revision_session_source", table_name="knowledge_revisions")
    op.drop_column("knowledge_revisions", "source_session_key")
