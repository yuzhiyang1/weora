"""官方长连接运输适配。仅收到 Inbox 持久提交结果后，SDK 才发送成功 ACK。"""

import asyncio
import importlib
import json
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import cast

from weora.modules.context.domain import ContextError, Json, SecretCipher
from weora.modules.context.telemetry import ReceiverTelemetry

PersistEvent = Callable[[Json], Awaitable[Json]]

# 应用可能同时订阅这些辅助事件。仅持久化事件元数据后 ACK，不读取历史、
# 不生成 Inbox/Run，更不把表情、已读或成员变化当成用户的新任务请求。
NON_TRIGGER_EVENTS = (
    "im.message.message_read_v1",
    "im.message.reaction.created_v1",
    "im.message.reaction.deleted_v1",
    "im.chat.member.bot.added_v1",
    "im.chat.member.bot.deleted_v1",
    "drive.notice.comment_add_v1",
    "vc.meeting.participant_meeting_ended_v1",
    "vc.note.generated_v1",
    "minutes.minute.generated_v1",
)


class SafeTransportLogs(logging.Filter):
    """供应商连接异常可能含握手 URL；只保留固定告警，不输出 SDK 原始文本。"""

    def __init__(self, observe: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self.observe = observe

    def filter(self, record: logging.LogRecord) -> bool:
        # 只匹配 SDK 自身的固定前缀；不输出已经插值的 URL、事件正文或供应商异常。
        stages = {
            "connect failed": "FEISHU_CONNECT_ERROR",
            "receive message loop exit": "FEISHU_RECEIVE_LOOP_ERROR",
            "handle message failed": "FEISHU_EVENT_HANDLER_ERROR",
            "ping failed": "FEISHU_PING_ERROR",
            "connected to": "FEISHU_CHANNEL_CONNECTED",
            "disconnected to": "FEISHU_CHANNEL_DISCONNECTED",
            "trying to reconnect": "FEISHU_CHANNEL_RECONNECTING",
            "weora event received": "FEISHU_EVENT_RECEIVED",
            "weora inbox committed": "FEISHU_INBOX_COMMITTED",
            "weora event ignored": "FEISHU_EVENT_IGNORED",
            "weora inbox failed": "FEISHU_INBOX_FAILED",
            "send ping": "FEISHU_CONTROL_PING",
            "receive pong": "FEISHU_CONTROL_PONG",
            "receive message, message_type:": "FEISHU_DATA_FRAME",
        }
        message = record.msg if isinstance(record.msg, str) else ""
        # 多个安全过滤器或重入时保留已经脱敏的自有代码，不再次压成泛化错误。
        record.msg = (
            message
            if message in stages.values()
            else next((code for prefix, code in stages.items() if message.startswith(prefix)), "FEISHU_TRANSPORT_ERROR")
        )
        record.args = ()
        record.exc_info = None
        record.stack_info = None
        if self.observe:
            self.observe(str(record.msg))
        return True


class InboxAckBridge:
    """SDK 同步回调运行在其事件循环；DB 工作在独立线程，不能嵌套 asyncio.run。"""

    def __init__(self, persist: PersistEvent, *, timeout_seconds: float = 2.5) -> None:
        self.persist, self.timeout_seconds = persist, timeout_seconds
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weora-inbox")

    def accept(self, payload: Json) -> Json:
        """超时仍可能完成数据库提交；重投靠业务键去重，不能先 ACK 再等待。"""

        async def save() -> Json:
            return await self.persist(payload)

        future = self.executor.submit(lambda: asyncio.run(save()))
        try:
            return future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError:
            raise ContextError("INBOX_UNAVAILABLE", "入站持久化未确认") from None
        except Exception:
            # SDK 会打印 handler 异常；转换固定消息，不能把 SQL 参数或正文写入日志。
            raise ContextError("INBOX_UNAVAILABLE", "入站持久化失败") from None

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)


def run_long_connection(
    connection: Json, cipher: SecretCipher, persist: PersistEvent, *, telemetry: ReceiverTelemetry | None = None
) -> None:
    """在独立 receiver 进程调用。persist 每次自行创建并释放 DB 引擎，不共享跨循环 Session。"""
    if connection.get("enabled") is not True or not connection.get("bot_open_id"):
        raise ContextError("CONNECTION_NOT_READY", "连接须启用并先完成机器人身份探测")
    # SDK 没有 py.typed；动态边界严格停留在运输层，不泄漏其类型到应用层。
    sdk = importlib.import_module("lark_oapi")
    transport_logger = logging.getLogger("Lark")
    log_filter = SafeTransportLogs(telemetry.observe if telemetry else None)
    transport_logger.addFilter(log_filter)
    bridge = InboxAckBridge(persist)
    if telemetry:
        telemetry.start()

    def on_message(event: object) -> None:
        # 阶段回执与供应商消息正文分离；连接成功不能代替实际事件入站成功。
        logger = logging.getLogger("Lark")
        logger.info("weora event received")
        payload = cast(Json, json.loads(sdk.JSON.marshal(event)))
        if not isinstance(payload, dict):
            raise ContextError("INVALID_EVENT", "飞书事件结构无效")
        try:
            receipt = bridge.accept(payload)
        except ContextError:
            logger.error("weora inbox failed")
            raise
        logger.info("weora inbox committed" if receipt.get("status") == "persisted" else "weora event ignored")

    try:
        builder = sdk.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message)
        for event_type in NON_TRIGGER_EVENTS:
            # 只用官方自定义事件解码器保留通用信封；不引入这些领域的业务能力。
            builder.register_p2_customized_event(event_type, on_message)
        handler = builder.build()
        client = sdk.ws.Client(
            str(connection["app_id"]),
            cipher.decrypt(str(connection["secret"])),
            event_handler=handler,
            # 连接生命周期也只输出自有固定代码，便于区分“进程存活”和“渠道已连接”。
            # DEBUG 的协议帧日志先经 logger 级过滤器，仅保留固定代码；正文和握手 URL 不落盘。
            # 这让网络 PONG 与进程心跳分开，而不改写 SDK 分发、ACK 或重连逻辑。
            log_level=sdk.LogLevel.DEBUG if telemetry else sdk.LogLevel.INFO,
        )
        client.start()
    except ContextError:
        raise
    except Exception:
        raise ContextError("CHANNEL_UNAVAILABLE", "飞书长连接不可用，请检查连接状态与网络") from None
    finally:
        try:
            bridge.close()
        finally:
            transport_logger.removeFilter(log_filter)
            if telemetry:
                telemetry.close()
