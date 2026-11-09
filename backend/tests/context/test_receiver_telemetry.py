"""诊断状态与真实入站互不替代；观测通道不能进入 ACK 的可靠性边界。"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from tests.context.test_context import setup
from weora.modules.context.channels import FeishuChannels
from weora.modules.context.domain import Json
from weora.modules.context.receiver import SafeTransportLogs
from weora.modules.context.telemetry import ReceiverTelemetry


def test_transport_stage_and_process_heartbeat_are_separate() -> None:
    async def persist(_row: Json) -> None:
        return None

    telemetry = ReceiverTelemetry(persist)
    assert telemetry.snapshot()["transport_state"] == "starting"
    telemetry.observe("FEISHU_CHANNEL_CONNECTED")
    assert telemetry.snapshot()["transport_state"] == "connected"
    assert "last_callback_at" not in telemetry.snapshot()
    telemetry.observe("FEISHU_CONTROL_PONG")
    assert "last_pong_at" in telemetry.snapshot() and telemetry.snapshot()["data_frames"] == 0
    telemetry.observe("FEISHU_DATA_FRAME")
    assert telemetry.snapshot()["data_frames"] == 1
    telemetry.observe("FEISHU_EVENT_RECEIVED")
    assert "last_callback_at" in telemetry.snapshot() and "last_inbox_commit_at" not in telemetry.snapshot()
    telemetry.observe("FEISHU_INBOX_COMMITTED")
    assert "last_inbox_commit_at" in telemetry.snapshot()
    telemetry.observe("FEISHU_CHANNEL_RECONNECTING")
    assert telemetry.snapshot()["reconnects"] == 1


def test_diagnostic_write_failure_does_not_raise_or_claim_ingress() -> None:
    async def persist(_row: Json) -> None:
        raise RuntimeError("not exposed")

    telemetry = ReceiverTelemetry(persist)
    telemetry._flush()
    assert "last_inbox_commit_at" not in telemetry.snapshot()


def test_debug_frame_is_redacted_before_observation_or_any_handler() -> None:
    observed = []
    record = logging.LogRecord(
        "Lark",
        logging.DEBUG,
        "transport",
        1,
        "receive message, message_type: event, payload: DO-NOT-LOG-THIS-SECRET",
        (),
        None,
    )
    assert SafeTransportLogs(observed.append).filter(record)
    assert record.getMessage() == "FEISHU_DATA_FRAME"
    assert observed == ["FEISHU_DATA_FRAME"]


def test_stale_or_old_configuration_receiver_is_not_shown_connected() -> None:
    async def scenario() -> None:
        f = await setup()
        connection = await f.store.get(f.actor.workspace_id, "connection", str(f.connection))
        assert connection
        row: Json = {
            "instance_id": "fixture",
            "connection_id": str(f.connection),
            "connection_version": connection["version"],
            "transport_state": "connected",
            "heartbeat_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        }
        await f.store.change(f.actor.workspace_id, "receiver", "fixture", lambda _old: row)
        result = await FeishuChannels(f.service).status(f.actor)
        assert result["receivers"][0]["transport_state"] == "stale"
        assert not result["inbox"] and not result["events"]
        row["connection_version"] = 0
        await f.store.change(f.actor.workspace_id, "receiver", "fixture", lambda _old: row)
        assert not (await FeishuChannels(f.service).status(f.actor))["receivers"]

    asyncio.run(scenario())
