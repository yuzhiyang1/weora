"""使用已锁定官方 SDK 的真实分发/ACK 编码路径，不建立飞书连接。"""

import asyncio
import importlib
import json
from typing import Protocol, cast

import pytest

from tests.context.test_context import inbound
from weora.modules.context.domain import ContextError, Json
from weora.modules.context.receiver import InboxAckBridge, SafeTransportLogs, run_long_connection


@pytest.mark.parametrize("storage_fails", [False, True])
def test_sdk_sends_success_ack_only_after_durable_callback(storage_fails: bool) -> None:
    async def scenario() -> None:
        sdk = importlib.import_module("lark_oapi")
        protobuf = importlib.import_module("lark_oapi.ws.pb.pbbp2_pb2")
        committed: list[str] = []

        async def persist(payload: Json) -> Json:
            await asyncio.sleep(0)
            if storage_fails:
                raise RuntimeError("sensitive database values")
            committed.append(str(cast(Json, payload["header"])["event_id"]))
            return {"status": "persisted"}

        bridge = InboxAckBridge(persist)

        def callback(event: object) -> None:
            bridge.accept(cast(Json, json.loads(sdk.JSON.marshal(event))))

        handler = sdk.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(callback).build()
        client = sdk.ws.Client("cli_test", "fake-secret", event_handler=handler, log_level=sdk.LogLevel.ERROR)
        acknowledged: list[Json] = []

        async def write_frame(raw: bytes) -> None:
            frame = protobuf.Frame()
            frame.ParseFromString(raw)
            acknowledged.append(cast(Json, json.loads(frame.payload)))
            if not storage_fails:
                assert committed == ["event1"]

        client._write_message = write_frame
        frame = protobuf.Frame()
        frame.SeqID, frame.LogID, frame.service, frame.method = 1, 1, 1, 1
        headers = {"type": "event", "message_id": "frame1", "sum": "1", "seq": "0", "trace_id": "trace"}
        for name, value in headers.items():
            header = frame.headers.add()
            header.key, header.value = name, value
        payload = inbound()
        payload["schema"] = "2.0"
        frame.payload = json.dumps(payload).encode()
        try:
            await client._handle_data_frame(frame)
            assert len(acknowledged) == 1
            assert acknowledged[0]["code"] == (500 if storage_fails else 200)
            assert committed == ([] if storage_fails else ["event1"])
        finally:
            bridge.close()

    asyncio.run(scenario())


def test_transport_logs_do_not_print_vendor_urls_or_secrets() -> None:
    import logging

    record = logging.LogRecord(
        "Lark",
        logging.ERROR,
        "transport",
        1,
        "connection failed %s",
        ("wss://vendor.example/private?secret=never-print",),
        None,
    )
    assert SafeTransportLogs().filter(record)
    assert record.getMessage() == "FEISHU_TRANSPORT_ERROR"
    assert "never-print" not in record.getMessage()


@pytest.mark.parametrize(
    "stage,code",
    [
        ("connect failed", "FEISHU_CONNECT_ERROR"),
        ("receive message loop exit", "FEISHU_RECEIVE_LOOP_ERROR"),
        ("handle message failed", "FEISHU_EVENT_HANDLER_ERROR"),
        ("ping failed", "FEISHU_PING_ERROR"),
        ("connected to", "FEISHU_CHANNEL_CONNECTED"),
        ("disconnected to", "FEISHU_CHANNEL_DISCONNECTED"),
        ("trying to reconnect", "FEISHU_CHANNEL_RECONNECTING"),
        ("weora event received", "FEISHU_EVENT_RECEIVED"),
        ("weora inbox committed", "FEISHU_INBOX_COMMITTED"),
        ("weora event ignored", "FEISHU_EVENT_IGNORED"),
        ("weora inbox failed", "FEISHU_INBOX_FAILED"),
    ],
)
def test_transport_logs_keep_safe_failure_stage(stage: str, code: str) -> None:
    """保留故障发生在哪一层，但绝不回显握手URL、事件正文或异常栈。"""
    import logging

    record = logging.LogRecord(
        "Lark", logging.ERROR, "transport", 1, stage + ", err: wss://host/path?secret=never-print", (), None
    )
    assert SafeTransportLogs().filter(record)
    assert record.getMessage() == code


@pytest.mark.parametrize("outcome", ["persisted", "ignored", "failed"])
def test_adapter_reports_actual_callback_and_commit_stage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    """真实 SDK 解码与生产回调，仅替换建连；根消息不需要 thread_id 才能进入 Inbox。"""
    import logging

    from cryptography.fernet import Fernet

    from weora.modules.context.feishu import FernetSecrets

    class Handler(Protocol):
        def _do_without_validation(self, payload: bytes) -> object: ...

    received: list[str] = []

    async def persist(payload: Json) -> Json:
        received.append(str(cast(Json, cast(Json, payload["event"])["message"])["message_id"]))
        if outcome == "failed":
            raise RuntimeError("sensitive database value")
        return {"status": outcome}

    def client_factory(*args: object, **kwargs: object) -> object:
        handler = cast(Handler, kwargs["event_handler"])

        class Client:
            def start(self) -> None:
                payload = inbound()
                payload["schema"] = "2.0"
                message = cast(Json, cast(Json, payload["event"])["message"])
                message["thread_id"] = None
                message["parent_id"] = None
                handler._do_without_validation(json.dumps(payload).encode())

        return Client()

    sdk = importlib.import_module("lark_oapi")
    monkeypatch.setattr(sdk.ws, "Client", client_factory)
    caplog.set_level(logging.INFO, logger="Lark")
    cipher = FernetSecrets(Fernet.generate_key().decode())
    connection: Json = {
        "enabled": True,
        "app_id": "cli_test",
        "bot_open_id": "ou_bot",
        "secret": cipher.encrypt("not-real"),
    }
    if outcome == "failed":
        with pytest.raises(ContextError, match="入站持久化失败"):
            run_long_connection(connection, cipher, persist)
    else:
        run_long_connection(connection, cipher, persist)
    assert received == ["om_trigger"]
    assert "FEISHU_EVENT_RECEIVED" in caplog.messages
    expected = {
        "persisted": "FEISHU_INBOX_COMMITTED",
        "ignored": "FEISHU_EVENT_IGNORED",
        "failed": "FEISHU_INBOX_FAILED",
    }[outcome]
    assert expected in caplog.messages
    assert "sensitive database value" not in " ".join(caplog.messages)


@pytest.mark.parametrize("event_type", [
    "im.message.reaction.created_v1", "im.message.reaction.deleted_v1", "im.message.message_read_v1",
    "im.chat.member.bot.added_v1", "im.chat.member.bot.deleted_v1", "drive.notice.comment_add_v1",
    "vc.meeting.participant_meeting_ended_v1", "vc.note.generated_v1", "minutes.minute.generated_v1",
])
@pytest.mark.parametrize("storage_fails", [False, True])
def test_non_trigger_events_ack_only_after_audit_without_starting_work(
    monkeypatch: pytest.MonkeyPatch, event_type: str, storage_fails: bool,
) -> None:
    """复现真实 reaction 帧：共享应用已订阅的非任务事件不能因缺 handler 永久返回 500。"""
    from cryptography.fernet import Fernet

    from weora.modules.context.feishu import FernetSecrets

    sdk = importlib.import_module("lark_oapi")
    protobuf = importlib.import_module("lark_oapi.ws.pb.pbbp2_pb2")
    original_client = sdk.ws.Client
    audited: list[str] = []
    codes: list[int] = []

    async def persist(payload: Json) -> Json:
        assert cast(Json, payload["header"])["event_type"] == event_type
        if storage_fails:
            raise RuntimeError("never-log-secret")
        audited.append(event_type)
        return {"status": "ignored", "reason": "not_a_new_message"}

    def client_factory(*args: object, **kwargs: object) -> object:
        async def acknowledge(raw: bytes) -> None:
            frame = protobuf.Frame()
            frame.ParseFromString(raw)
            codes.append(int(json.loads(frame.payload)["code"]))

        async def receive_frame() -> None:
            # 缓存清理任务也应属于本测试运行的循环，避免测试退出遗留 SDK 协程。
            client = original_client(*args, **kwargs)
            frame = protobuf.Frame()
            frame.SeqID, frame.LogID, frame.service, frame.method = 1, 1, 1, 1
            headers = {"type": "event", "message_id": "frame1", "sum": "1", "seq": "0", "trace_id": "trace"}
            for name, value in headers.items():
                header = frame.headers.add()
                header.key, header.value = name, value
            frame.payload = json.dumps({"schema": "2.0", "header": {"event_id": "event_aux", "event_type": event_type},
                "event": {"message_id": "om_trigger"}}).encode()
            client._write_message = acknowledge
            await client._handle_data_frame(frame)

        class Client:
            def start(self) -> None:
                asyncio.run(receive_frame())

        return Client()

    monkeypatch.setattr(sdk.ws, "Client", client_factory)
    cipher = FernetSecrets(Fernet.generate_key().decode())
    run_long_connection({"enabled": True, "app_id": "cli_test", "bot_open_id": "ou_bot",
                         "secret": cipher.encrypt("fake")}, cipher, persist)
    assert codes == ([500] if storage_fails else [200])
    assert audited == ([] if storage_fails else [event_type])
