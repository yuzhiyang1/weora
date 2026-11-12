"""由 PostgreSQL 通知唤醒运行 SSE。"""

from weora.shared.infrastructure.postgres_watch import PostgresWatch


class RunWatch(PostgresWatch):
    """沿用运行事件的 PostgreSQL 频道。"""

    def __init__(self) -> None:
        super().__init__("weora_run_changed")
