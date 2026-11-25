"""为已发布和待审核的知识版本建立可重建的词项索引。"""

import sqlalchemy as sa
from alembic import op

from weora.modules.knowledge.search_backfill import backfill
from weora.modules.knowledge.search_tokens_v1 import document_index

revision = "20261119_0042"
down_revision = "20261119_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, column_type in (
        ("search_domain", sa.String(16)), ("search_title_length", sa.Integer()), ("search_length", sa.Integer()),
    ):
        op.add_column("knowledge_revisions", sa.Column(name, column_type))
    postings = op.create_table(
        "knowledge_search_terms",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("term", sa.String(160), primary_key=True),
        sa.Column("entry_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("fields", sa.Integer(), nullable=False),
        sa.Column("frequency", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["entry_id", "revision"], ["knowledge_revisions.entry_id", "knowledge_revisions.revision"],
            ondelete="CASCADE",
        ),
    )
    backfill(op.get_bind(), postings, document_index)
    op.create_index("ix_knowledge_search_terms_revision", "knowledge_search_terms", ["entry_id", "revision"])
    for name in ("search_domain", "search_title_length", "search_length"):
        op.alter_column("knowledge_revisions", name, nullable=False)


def downgrade() -> None:
    op.drop_table("knowledge_search_terms")
    for name in ("search_domain", "search_title_length", "search_length"):
        op.drop_column("knowledge_revisions", name)
