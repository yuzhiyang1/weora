"""汇合测试机既有的知识库迁移与 Rules 迁移。"""

revision = "20261125_0105"
down_revision = ("20261124_0104", "20261125_0036")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """两个父版本已应用全部数据库结构，合并点只登记版本。"""


def downgrade() -> None:
    """回退合并点时继续保留两个父版本的数据结构。"""
