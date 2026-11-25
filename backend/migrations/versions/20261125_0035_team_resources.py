"""团队资源的修订、默认引用和变更请求。"""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0035"
down_revision = "20261113_0034"
branch_labels = None
depends_on = None


def revision_fk(column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id", column],
        ["team_resource_revisions.workspace_id", "team_resource_revisions.id"],
    )


def upgrade() -> None:
    op.create_table(
        "team_resource_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("parent_id", sa.Uuid()),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.UniqueConstraint("workspace_id", "id"),
        revision_fk("parent_id"),
    )
    op.create_index("ix_team_resource_revisions_workspace_id", "team_resource_revisions", ["workspace_id"])
    op.create_table(
        "team_resource_heads",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("revision_id", sa.Uuid()),
        revision_fk("revision_id"),
    )
    op.create_table(
        "team_resource_changes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("branch", sa.String(120), nullable=False),
        sa.Column("author_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("base_id", sa.Uuid()),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("merged_id", sa.Uuid()),
        sa.UniqueConstraint("workspace_id", "branch"),
        revision_fk("base_id"),
        revision_fk("revision_id"),
        revision_fk("merged_id"),
    )
    op.create_index("ix_team_resource_changes_workspace_id", "team_resource_changes", ["workspace_id"])
    op.create_table(
        "team_resource_tags",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("name", sa.String(120), primary_key=True),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        revision_fk("revision_id"),
    )


def downgrade() -> None:
    op.drop_table("team_resource_tags")
    op.drop_table("team_resource_changes")
    op.drop_table("team_resource_heads")
    op.drop_table("team_resource_revisions")
