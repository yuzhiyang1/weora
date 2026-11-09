"""工具调用绑定执行尝试，用于强制退出后的精准状态补偿。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261019_0012"
down_revision: str | None = "20261017_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增量可空字段保留所有旧调用；不能凭空回填旧执行者。"""
    op.add_column("tool_invocations", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column("tool_invocations", sa.Column("fencing_token", sa.Integer(), nullable=True))


def downgrade() -> None:
    """只移除执行标识，不删除工具调用或幂等事实。"""
    op.drop_column("tool_invocations", "fencing_token")
    op.drop_column("tool_invocations", "lease_owner")
