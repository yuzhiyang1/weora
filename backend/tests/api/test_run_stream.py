"""终态 SSE 可有界重放；不能用 URL 令牌绕过当前会话。"""

import pytest

from tests.api.test_runs import RunsHarness

pytest_plugins = ["tests.api.test_runs"]


@pytest.mark.anyio
async def test_terminal_stream_replays_persisted_events_and_header_cursor(runs: RunsHarness) -> None:
    created = await runs.client.post(runs.path, json=runs.payload(), headers=runs.headers())
    run_id = created.json()["id"]
    await runs.client.post(f"{runs.path}/{run_id}/cancel", headers=runs.headers())
    response = await runs.client.get(f"{runs.path}/{run_id}/events/stream", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 2" in response.text
    assert "run.cancelled" in response.text
    assert "run.queued" not in response.text
    assert "event: stream_end" in response.text
    invalid = await runs.client.get(f"{runs.path}/{run_id}/events/stream", headers={"Last-Event-ID": "../bad"})
    assert invalid.status_code == 422


@pytest.mark.anyio
async def test_stream_checks_private_run_before_sending_headers(runs: RunsHarness) -> None:
    from uuid import uuid4

    response = await runs.client.get(f"{runs.path}/{uuid4()}/events/stream")
    assert response.status_code == 404
