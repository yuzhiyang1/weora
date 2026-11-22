"""按项目或工作空间发布团队评分标准，保存会话标签历史。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0051"
down_revision = "20261119_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_session_scorers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("passing_score", sa.Float(), nullable=False),
        sa.Column("sampling_rate", sa.Float(), nullable=False),
        sa.Column("sessions", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(160)),
        sa.Column("tool", sa.String(12)),
        sa.Column("published_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_knowledge_session_scorer_scope", "knowledge_session_scorers",
        ["workspace_id", "project_id", "name", "revision"],
    )
    op.create_table(
        "knowledge_session_labels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("archive_id", sa.Uuid(), nullable=False),
        sa.Column("archive_revision", sa.Integer(), nullable=False),
        sa.Column("scorer_id", sa.Uuid(), nullable=False),
        sa.Column("scorer_revision", sa.Integer(), nullable=False),
        sa.Column("judge_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("driver", sa.String(12), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("request_id", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["archive_id", "archive_revision"], [
            "knowledge_shared_session_revisions.archive_id", "knowledge_shared_session_revisions.revision",
        ]),
        sa.ForeignKeyConstraint(["scorer_id", "scorer_revision"], [
            "knowledge_session_scorers.id", "knowledge_session_scorers.revision",
        ]),
        sa.UniqueConstraint("judge_id", "operation_id", name="uq_knowledge_session_label_operation"),
        sa.CheckConstraint("driver IN ('human', 'codex', 'claude')", name="ck_knowledge_session_label_driver"),
    )
    op.create_index(
        "ix_knowledge_session_label_archive", "knowledge_session_labels", ["archive_id", "archive_revision", "id"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_session_scorers LIMIT 1")):
        raise RuntimeError("存在已发布的团队评分标准，回退会丢失评分或审计记录。")
    op.drop_table("knowledge_session_labels")
    op.drop_table("knowledge_session_scorers")
