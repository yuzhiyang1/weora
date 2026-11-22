"""保存知识工作流启动、步骤状态及审批审计。"""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0102"
down_revision = "20261121_0101"
branch_labels = None
depends_on = None

RUN = "knowledge_workflow_runs"
DECISION = "knowledge_workflow_decisions"


def upgrade() -> None:
    op.create_table(
        RUN,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id")),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("collab_agent_devices.id")),
        sa.Column("device_name", sa.String(80)),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_name", sa.String(64), nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("session_workflow_revision", sa.Integer()),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("outcomes", sa.JSON(), nullable=False),
        sa.Column("drafts", sa.JSON(none_as_null=True)),
        sa.Column("published", sa.JSON(none_as_null=True)),
        sa.Column("session_result", sa.JSON(none_as_null=True)),
        sa.Column("launch", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "created_by", "operation_id", name="uq_knowledge_workflow_run_operation"),
    )
    op.create_table(
        DECISION,
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey(f"{RUN}.id"), primary_key=True),
        sa.Column("step", sa.String(40), primary_key=True),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("collab_agent_devices.id")),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM knowledge_workflow_runs LIMIT 1")):
        raise RuntimeError("存在知识工作流运行或审批记录，无法安全移除。")
    op.drop_table(DECISION)
    op.drop_table(RUN)
