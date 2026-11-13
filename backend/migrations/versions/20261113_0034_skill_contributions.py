"""保存 Skill 展示快照与成员候选包。"""

from dataclasses import asdict

import sqlalchemy as sa
from alembic import context, op

from weora.modules.skills.domain.package import read_skill_interface

revision = "20261113_0034"
down_revision = "20261112_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立候选队列，并从旧账号、审计和已入库包补齐展示信息。"""
    op.add_column("skills", sa.Column("uploader_name", sa.String(120)))
    op.add_column("skills", sa.Column("metadata_edited", sa.Boolean(), server_default=sa.false(), nullable=False))
    for name in ("skill_drafts", "skill_versions"):
        op.add_column(name, sa.Column("interface", sa.JSON(none_as_null=True)))
        op.add_column(name, sa.Column("uploader_name", sa.String(120)))
    op.create_table(
        "skill_contributions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("skill_id", sa.Uuid(), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("uploader_name", sa.String(120)),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("package_name", sa.String(255), nullable=False),
        sa.Column("base_revision", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "package_sha256"],
            ["skill_packages.workspace_id", "skill_packages.package_sha256"],
        ),
    )
    op.create_index("ix_skill_contributions_workspace_skill", "skill_contributions", ["workspace_id", "skill_id"])
    op.execute(
        "UPDATE skills SET uploader_name = (SELECT display_name FROM users WHERE users.id = skills.created_by), "
        "metadata_edited = EXISTS (SELECT 1 FROM skill_events WHERE skill_events.skill_id = skills.id "
        "AND skill_events.event_type = 'SKILL_METADATA_UPDATED')"
    )
    op.execute(
        "UPDATE skill_drafts SET uploader_name = "
        "(SELECT display_name FROM users WHERE users.id = skill_drafts.updated_by)"
    )
    if not context.is_offline_mode():
        _backfill_packages(op.get_bind())


def _backfill_packages(db: sa.Connection) -> None:
    """只读取本库包体。旧 OSS 包的元数据可随原包回填。"""
    metadata = sa.MetaData()
    skills, drafts, versions, packages, events, users = (
        sa.Table(name, metadata, autoload_with=db)
        for name in ("skills", "skill_drafts", "skill_versions", "skill_packages", "skill_events", "users")
    )
    uploader = (
        sa.select(users.c.display_name)
        .join(events, events.c.actor_id == users.c.id)
        .where(
            events.c.workspace_id == versions.c.workspace_id,
            events.c.skill_id == versions.c.skill_id,
            events.c.occurred_at <= versions.c.published_at,
            events.c.event_type.in_(("SKILL_CREATED", "SKILL_DRAFT_REPLACED")),
            events.c.metadata["content_sha256"].as_string() == versions.c.content_sha256,
        )
        .order_by(events.c.occurred_at.desc(), events.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    db.execute(versions.update().values(uploader_name=uploader))
    for table, key, source, workspace_id in (
        (drafts, drafts.c.skill_id, drafts.join(skills, drafts.c.skill_id == skills.c.id), skills.c.workspace_id),
        (versions, versions.c.id, versions, versions.c.workspace_id),
    ):
        query = sa.select(key, workspace_id, table.c.package_sha256).select_from(source).order_by(key)
        cursor = None
        while rows := db.execute((query if cursor is None else query.where(key > cursor)).limit(100)).all():
            for record_id, workspace, digest in rows:
                archive = db.scalar(
                    sa.select(packages.c.archive_bytes).where(
                        packages.c.workspace_id == workspace,
                        packages.c.package_sha256 == digest,
                    )
                )
                interface = read_skill_interface(archive) if archive else None
                if interface:
                    db.execute(table.update().where(key == record_id).values(interface=asdict(interface)))
            cursor = rows[-1][0]


def downgrade() -> None:
    """移除候选队列和展示快照，保留原 Skill 资产及发布版本。"""
    op.drop_table("skill_contributions")
    for name in ("skill_versions", "skill_drafts"):
        op.drop_column(name, "uploader_name")
        op.drop_column(name, "interface")
    op.drop_column("skills", "metadata_edited")
    op.drop_column("skills", "uploader_name")
