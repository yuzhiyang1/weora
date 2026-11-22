"""切换知识检索的分词版本，原始正文、评审和反馈无需迁移。"""

import sqlalchemy as sa
from alembic import op

from weora.modules.knowledge.icu_words import require_icu
from weora.modules.knowledge.search_backfill import IndexDocument, backfill
from weora.modules.knowledge.search_tokens_v1 import document_index as bigram_index
from weora.modules.knowledge.search_tokens_v2 import document_index as word_index

revision = "20261119_0044"
down_revision = "20261119_0043"
branch_labels = None
depends_on = None


def _rebuild(index_document: IndexDocument) -> None:
    db = op.get_bind()
    # 回填期间等待版本写入结束，避免跨分词版本混合发布。
    op.execute("LOCK TABLE knowledge_revisions IN SHARE ROW EXCLUSIVE MODE")
    postings = sa.Table("knowledge_search_terms", sa.MetaData(), autoload_with=db)
    db.execute(postings.delete())
    backfill(db, postings, index_document)


def upgrade() -> None:
    require_icu()
    _rebuild(word_index)


def downgrade() -> None:
    _rebuild(bigram_index)
