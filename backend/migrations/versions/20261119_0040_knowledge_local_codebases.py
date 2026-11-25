"""登记项目内本地代码快照及所有者。"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0040"
down_revision = "20261118_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_codebases", sa.Column("local_owner_id", sa.Uuid(), nullable=True))
    op.add_column("knowledge_codebases", sa.Column("local_key", sa.String(64), nullable=True))
    op.add_column("knowledge_codebases", sa.Column("local_name", sa.String(160), nullable=True))
    op.create_foreign_key(
        "fk_knowledge_codebase_local_owner", "knowledge_codebases", "users", ["local_owner_id"], ["id"]
    )
    op.alter_column("knowledge_codebases", "repository_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column("knowledge_codebases", "connection_version", existing_type=sa.Integer(), nullable=True)
    op.create_unique_constraint(
        "uq_knowledge_codebase_local",
        "knowledge_codebases",
        ["project_id", "local_owner_id", "local_key"],
    )
    op.create_check_constraint(
        "ck_knowledge_codebase_source",
        "knowledge_codebases",
        "(repository_id IS NOT NULL AND connection_version IS NOT NULL "
        "AND local_owner_id IS NULL AND local_key IS NULL AND local_name IS NULL) OR "
        "(repository_id IS NULL AND connection_version IS NULL "
        "AND local_owner_id IS NOT NULL AND local_key IS NOT NULL AND local_name IS NOT NULL)",
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_codebases WHERE local_key IS NOT NULL LIMIT 1")):
        raise RuntimeError("请保留本地代码快照和发布历史后处理迁移回退。")
    op.drop_constraint("ck_knowledge_codebase_source", "knowledge_codebases", type_="check")
    op.drop_constraint("uq_knowledge_codebase_local", "knowledge_codebases", type_="unique")
    op.drop_constraint("fk_knowledge_codebase_local_owner", "knowledge_codebases", type_="foreignkey")
    op.alter_column("knowledge_codebases", "connection_version", existing_type=sa.Integer(), nullable=False)
    op.alter_column("knowledge_codebases", "repository_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("knowledge_codebases", "local_name")
    op.drop_column("knowledge_codebases", "local_key")
    op.drop_column("knowledge_codebases", "local_owner_id")
