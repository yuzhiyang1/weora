"""为会话归档和本机证据登记增加可冻结的 Skill 分发快照。"""

import sqlalchemy as sa
from alembic import op

revision = "20261120_0090"
down_revision = "20261120_0089"
branch_labels = None
depends_on = None

_TABLE = "knowledge_shared_session_revisions"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("skill_evidence", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM knowledge_shared_session_revisions WHERE skill_evidence IS NOT NULL LIMIT 1",
    )).first():
        raise RuntimeError("已有会话 Skill 证据，回退将丢失已固定的快照")
    op.drop_column(_TABLE, "skill_evidence")
