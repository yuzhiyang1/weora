"""创建 Agent 资产、草稿、不可变版本与 SkillVersion 绑定表。

Revision ID: 20260918_0003
Revises: 20260822_0002
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0003"
down_revision: str | None = "20260822_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _binding_columns() -> list[sa.Column[object]]:
    """返回草稿和版本绑定表共享的 SkillVersion 快照列。"""
    return [
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_slug", sa.String(length=64), nullable=False),
        sa.Column("skill_display_name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("package_size", sa.Integer(), nullable=False),
        sa.Column("package_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
    ]


def upgrade() -> None:
    """创建 Agent 资产、草稿、版本绑定和审计表。"""
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latest_published_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agents_workspace_updated_at",
        "agents",
        ["workspace_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "agent_drafts",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "agent_draft_skill_bindings",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        *_binding_columns(),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_drafts.agent_id"]),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"]),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"]),
        sa.PrimaryKeyConstraint("agent_id", "position"),
        sa.UniqueConstraint(
            "agent_id",
            "skill_version_id",
            name="uq_agent_draft_skill_version",
        ),
    )
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("changelog", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
    )
    op.create_index(
        "ix_agent_versions_workspace_published_at",
        "agent_versions",
        ["workspace_id", "published_at"],
        unique=False,
    )
    op.create_table(
        "agent_version_skill_bindings",
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        *_binding_columns(),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"]),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"]),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"]),
        sa.PrimaryKeyConstraint("agent_version_id", "position"),
        sa.UniqueConstraint(
            "agent_version_id",
            "skill_version_id",
            name="uq_agent_version_skill_version",
        ),
    )
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("event_data", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_events_agent_occurred_at",
        "agent_events",
        ["agent_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    """按引用依赖的反向顺序移除 Agent Registry 表。"""
    op.drop_index("ix_agent_events_agent_occurred_at", table_name="agent_events")
    op.drop_table("agent_events")
    op.drop_table("agent_version_skill_bindings")
    op.drop_index("ix_agent_versions_workspace_published_at", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_table("agent_draft_skill_bindings")
    op.drop_table("agent_drafts")
    op.drop_index("ix_agents_workspace_updated_at", table_name="agents")
    op.drop_table("agents")
