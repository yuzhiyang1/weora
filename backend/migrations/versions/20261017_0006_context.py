"""受管连接、授权、快照、预算与渠道可靠记录。

Revision ID: 20261017_0006
Revises: 20261017_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0006"
down_revision: str | None = "20261017_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("resource_key", sa.String(256), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "kind", "resource_key", name="uq_context_document_key"),
    )
    op.create_index("ix_context_documents_workspace_id", "context_documents", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_context_documents_workspace_id", table_name="context_documents")
    op.drop_table("context_documents")
