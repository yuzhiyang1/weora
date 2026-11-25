"""保存 Registry Skill 召回用量和手工反馈及其固定版本。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0083"
down_revision = "20261120_0082"
branch_labels = None
depends_on = None

_RECALLS = "knowledge_skill_recalls"
_EVENTS = "knowledge_skill_feedback_events"


def upgrade() -> None:
    op.create_table(
        _RECALLS,
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("skills.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("session_key", sa.String(64), primary_key=True),
        sa.Column("recall_count", sa.Integer(), nullable=False),
        sa.Column("last_recalled_at", sa.DateTime(timezone=True)),
        sa.Column("positive", sa.Boolean()),
        sa.Column("voted_at", sa.DateTime(timezone=True)),
        sa.Column("credit_source", sa.String(12)),
        sa.Column("manual_vote_at", sa.DateTime(timezone=True)),
        sa.Column("manual_vote_id", sa.Uuid()),
        sa.Column("recalled_version_id", sa.Uuid(), sa.ForeignKey("skill_versions.id")),
        sa.Column("credited_version_id", sa.Uuid(), sa.ForeignKey("skill_versions.id")),
        sa.CheckConstraint("credit_source IN ('opened', 'manual')", name="ck_knowledge_skill_recall_credit"),
    )
    op.create_table(
        _EVENTS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("skill_versions.id"), nullable=False),
        sa.Column("session_key", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.Uuid()),
        sa.Column("positive", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "entry_id", "user_id", "session_key", "operation_id", name="uq_knowledge_skill_feedback_op",
        ),
    )
    op.create_index("ix_knowledge_skill_feedback_entry", _EVENTS, ["entry_id", "received_at"])
    # 旧账本保留真实命中和打开时间。按会话累计次数，并保留首次打开所采纳的版本。
    op.execute(sa.text(f"""
        WITH existing AS (
            SELECT hit.entry_id, run.user_id, run.session_key,
                   hit.skill_version_id, hit.skill_opened_at, run.created_at,
                   count(*) OVER scope AS recall_count,
                   max(run.created_at) OVER scope AS last_recalled_at,
                   min(hit.skill_opened_at) OVER scope AS first_opened_at,
                   first_value(hit.skill_version_id) OVER opening AS first_version,
                   row_number() OVER recency AS position
            FROM knowledge_recall_run_hits hit
            JOIN knowledge_recall_runs run ON run.id = hit.run_id
            JOIN skill_versions release ON release.id = hit.skill_version_id
                AND release.skill_id = hit.entry_id AND release.workspace_id = run.workspace_id
            WHERE hit.eligible
            WINDOW scope AS (PARTITION BY hit.entry_id, run.user_id, run.session_key),
                   opening AS (scope ORDER BY hit.skill_opened_at ASC NULLS LAST, run.id),
                   recency AS (scope ORDER BY run.created_at DESC, run.id DESC)
        )
        INSERT INTO {_RECALLS} (
            entry_id, user_id, session_key, recall_count, last_recalled_at,
            recalled_version_id, credited_version_id, positive, voted_at, credit_source
        )
        SELECT entry_id, user_id, session_key, recall_count, last_recalled_at,
               skill_version_id, CASE WHEN first_opened_at IS NOT NULL THEN first_version END,
               CASE WHEN first_opened_at IS NOT NULL THEN TRUE END,
               first_opened_at, CASE WHEN first_opened_at IS NOT NULL THEN 'opened' END
        FROM existing WHERE position = 1
    """))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {_RECALLS} LIMIT 1")):
        raise RuntimeError("已有 Registry Skill 反馈账本，回退会丢失长期用量或手工反馈。")
    op.drop_table(_EVENTS)
    op.drop_table(_RECALLS)
