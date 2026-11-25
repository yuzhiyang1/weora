"""记录首次 Stop 所属日及按实际请求日累计的成本。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0074"
down_revision = "20261119_0073"
branch_labels = None
depends_on = None

_ACTIVITY = "knowledge_session_activity"
_REQUESTS = "knowledge_session_request_days"


def upgrade() -> None:
    op.add_column(_ACTIVITY, sa.Column("first_stop_day", sa.Date()))
    op.add_column(_ACTIVITY, sa.Column("active_ms", sa.BigInteger()))
    op.add_column(_ACTIVITY, sa.Column("trend_error", sa.Boolean()))
    op.create_index("ix_knowledge_activity_stop_day", _ACTIVITY, ["workspace_id", "project_id", "first_stop_day"])
    op.create_table(
        _REQUESTS,
        sa.Column("activity_id", sa.Uuid(), sa.ForeignKey(f"{_ACTIVITY}.id"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("priced_requests", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("priced_requests > 0 AND cost_micros >= 0", name="ck_knowledge_request_day_values"),
    )
    op.create_index("ix_knowledge_request_day", _REQUESTS, ["day"])


def downgrade() -> None:
    db = op.get_bind()
    if db.scalar(sa.text(f"SELECT 1 FROM {_ACTIVITY} WHERE first_stop_day IS NOT NULL LIMIT 1")) or db.scalar(
        sa.text(f"SELECT 1 FROM {_REQUESTS} LIMIT 1"),
    ):
        raise RuntimeError("已有会话趋势记录，回退会丢失日度统计。")
    op.drop_table(_REQUESTS)
    op.drop_index("ix_knowledge_activity_stop_day", table_name=_ACTIVITY)
    for field in ("trend_error", "active_ms", "first_stop_day"):
        op.drop_column(_ACTIVITY, field)
