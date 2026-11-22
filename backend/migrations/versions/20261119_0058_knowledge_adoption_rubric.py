"""固定自动采纳所用的团队标准以及模型返回的原标签。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0058"
down_revision = "20261119_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_session_scorers", sa.Column(
        "is_adoption_template", sa.Boolean(), server_default=sa.false(), nullable=False,
    ))
    op.add_column("knowledge_session_scorers", sa.Column(
        "adoption_template_archived", sa.Boolean(), server_default=sa.false(), nullable=False,
    ))
    op.add_column("knowledge_adoption_attempts", sa.Column("rubric", sa.JSON(none_as_null=True)))
    op.add_column("knowledge_adoption_attempts", sa.Column("scorer_label", sa.String(80)))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_adoption_attempts WHERE rubric IS NOT NULL LIMIT 1")):
        raise RuntimeError("存在团队采纳标准的评分历史，回退会丢失固定标准及原标签。")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_session_scorers WHERE is_adoption_template LIMIT 1")):
        raise RuntimeError("已发布团队采纳标准，回退会改变该标准的用途。")
    op.drop_column("knowledge_adoption_attempts", "scorer_label")
    op.drop_column("knowledge_adoption_attempts", "rubric")
    op.drop_column("knowledge_session_scorers", "adoption_template_archived")
    op.drop_column("knowledge_session_scorers", "is_adoption_template")
