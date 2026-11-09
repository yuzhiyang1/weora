"""接收进程的非阻塞唤醒；通知可丢，权威 Inbox 和两秒兜底不会丢。"""

from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weora-coordination-notify")
_pending = BoundedSemaphore(1)


def notify() -> None:
    # 有界合并，不能因 Redis 故障堆积线程或占用飞书 ACK 的三秒窗口。
    if not _pending.acquire(blocking=False):
        return

    def send() -> None:
        try:
            from weora.bootstrap.worker import celery_app

            celery_app.send_task("weora.coordination.dispatch", queue="weora.coordination.control")
        except Exception:
            pass
        finally:
            _pending.release()

    _pool.submit(send)
