"""同步已归档 Registry 资产关联的晋升知识状态。"""

from alembic import op

revision = "20261121_0098"
down_revision = "20261121_0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 使用原 Registry 归档事件的操作者。只修复可从发布事件确定且仍活跃的晋升条目。
    op.execute("""
        WITH archived_promotions AS (
            SELECT DISTINCT ON (entry.id)
                entry.id, COALESCE(archive.actor_id, skill.created_by) AS actor_id
            FROM knowledge_entries AS entry
            JOIN knowledge_events AS published
              ON published.entry_id = entry.id AND published.action = 'skill_published'
             AND published.revision = entry.published_revision
            JOIN skills AS skill
              ON CAST(skill.id AS text) = published.reason AND skill.workspace_id = entry.workspace_id
             AND skill.project_id IS NOT DISTINCT FROM entry.project_id
            LEFT JOIN LATERAL (
                SELECT event.actor_id
                FROM skill_events AS event
                WHERE event.skill_id = skill.id AND event.workspace_id = skill.workspace_id
                  AND event.event_type = 'SKILL_ARCHIVED'
                ORDER BY event.occurred_at DESC, event.id DESC
                LIMIT 1
            ) AS archive ON TRUE
            WHERE entry.kind = 'skill' AND entry.promoted_from IS NOT NULL AND entry.status = 'active'
              AND skill.status = 'archived' AND skill.latest_published_version_id IS NOT NULL
            ORDER BY entry.id, published.occurred_at DESC, published.id DESC
        ), updated AS (
            UPDATE knowledge_entries AS entry
            SET status = 'archived'
            FROM archived_promotions AS linked
            WHERE entry.id = linked.id AND entry.status = 'active'
            RETURNING entry.id, entry.published_revision, linked.actor_id
        )
        INSERT INTO knowledge_events (id, entry_id, actor_id, action, revision, reason, occurred_at)
        SELECT gen_random_uuid(), id, actor_id, 'archived', published_revision,
               '迁移同步既有 Skill 归档状态', CURRENT_TIMESTAMP
        FROM updated
    """)


def downgrade() -> None:
    """回退迁移版本时保留真实归档和已登记的审计记录。"""
