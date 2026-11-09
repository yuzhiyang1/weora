"""显式只读恢复，禁止崩溃后隐式重放写工具。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0010"
down_revision: str | None = "20261017_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("recovery_read_only", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("recovery_read_only")
