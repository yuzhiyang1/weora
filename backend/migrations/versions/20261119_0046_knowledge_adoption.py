"""为会话召回固定正文版本，区分人工、阅读和评分采纳。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0046"
down_revision = "20261119_0045"
branch_labels = None
depends_on = None


def _policy_kinds(include_adoption: bool) -> None:
    op.drop_constraint("ck_knowledge_session_policy_kind", "knowledge_session_policies", type_="check")
    kinds = "'recall', 'share-reminder', 'recall-adoption'" if include_adoption else "'recall', 'share-reminder'"
    op.create_check_constraint("ck_knowledge_session_policy_kind", "knowledge_session_policies", f"kind IN ({kinds})")


def upgrade() -> None:
    op.add_column("knowledge_recalls", sa.Column("recalled_revision", sa.Integer()))
    op.add_column("knowledge_recalls", sa.Column("credit_source", sa.String(12)))
    op.create_index("ix_knowledge_recalls_session", "knowledge_recalls", ["user_id", "session_key"])
    op.create_foreign_key(
        "fk_knowledge_recall_revision", "knowledge_recalls", "knowledge_revisions",
        ["entry_id", "recalled_revision"], ["entry_id", "revision"],
    )
    op.create_check_constraint(
        "ck_knowledge_recall_credit_source", "knowledge_recalls",
        "credit_source IN ('opened', 'manual', 'judge')",
    )
    # 旧召回没有正文版本记录，不能用当前发布版本补成评分证据。
    _policy_kinds(include_adoption=True)


def downgrade() -> None:
    db = op.get_bind()
    if db.scalar(sa.text("SELECT 1 FROM knowledge_events WHERE action IN ('judge_adopted', 'judge_ignored') LIMIT 1")):
        raise RuntimeError("存在采纳评分审计，回退会丢失评分计数口径。")
    if db.scalar(sa.text("SELECT 1 FROM knowledge_session_policies WHERE kind = 'recall-adoption' LIMIT 1")):
        raise RuntimeError("存在采纳评分策略历史，回退会丢失设置。")
    _policy_kinds(include_adoption=False)
    op.drop_constraint("ck_knowledge_recall_credit_source", "knowledge_recalls", type_="check")
    op.drop_constraint("fk_knowledge_recall_revision", "knowledge_recalls", type_="foreignkey")
    op.drop_index("ix_knowledge_recalls_session", table_name="knowledge_recalls")
    op.drop_column("knowledge_recalls", "credit_source")
    op.drop_column("knowledge_recalls", "recalled_revision")
