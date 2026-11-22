"""使用已有会话策略审计保存计划后召回提醒。"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0093"
down_revision = "20261121_0092"
branch_labels = None
depends_on = None

_TABLE = "knowledge_session_policies"
_KIND = "ck_knowledge_session_policy_kind"
_OPTIONS = "ck_knowledge_session_plan_reminder_options"
_PRIOR = "'recall', 'share-reminder', 'recall-adoption', 'usage-report'"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("plan_language", sa.String(2), nullable=True))
    op.add_column(_TABLE, sa.Column("plan_message", sa.String(4000), nullable=True))
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_PRIOR}, 'plan-reminder')")
    op.create_check_constraint(
        _OPTIONS, _TABLE,
        "(kind = 'plan-reminder' OR (plan_language IS NULL AND plan_message IS NULL)) "
        "AND (plan_language IS NULL OR plan_language IN ('zh', 'en'))",
    )


def downgrade() -> None:
    # 已使用新策略时保留审计，不允许降级静默清除团队设置。
    saved = sa.text("SELECT 1 FROM knowledge_session_policies WHERE kind = 'plan-reminder' LIMIT 1")
    if op.get_bind().scalar(saved):
        raise RuntimeError("已有召回计划提醒策略历史，无法无损回退。")
    op.drop_constraint(_OPTIONS, _TABLE, type_="check")
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_PRIOR})")
    op.drop_column(_TABLE, "plan_message")
    op.drop_column(_TABLE, "plan_language")
