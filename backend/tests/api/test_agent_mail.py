"""从公开入口验证授权、跨设备寻址、领取和答复。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests.api.test_runs import RunsHarness
from tests.api.test_runs import runs as runs
from tests.api.test_task_collaboration import headers, member
from weora.modules.collaboration.infrastructure import agent_delivery


async def device(client: AsyncClient, path: str, name: str) -> tuple[str, dict[str, str]]:
    assert (await client.post(path + "/devices", json={"name": name})).status_code == 403
    issued = await client.post(path + "/devices", headers=headers(client), json={"name": name})
    assert issued.status_code == 201, issued.text
    code = {"code": issued.json()["pairing_code"]}
    paired = await client.post(path + "/devices/pair", json=code)
    assert paired.status_code == 200, paired.text
    assert (await client.post(path + "/devices/pair", json=code)).status_code == 401
    return paired.json()["id"], {"Authorization": "Bearer " + paired.json()["token"]}


async def register(client: AsyncClient, path: str, credential: dict[str, str], runtime: str, name: str) -> str:
    payload = {
        "native_id": str(uuid4()),
        "runtime": runtime,
        "name": name,
        "repo": "orders-ui",
        "branch": "feature/pagination",
        "delivery": "queue",
    }
    session = await client.put(path + "/sessions", headers=credential, json=payload)
    assert session.status_code == 200, session.text
    repeat = await client.put(path + "/sessions", headers=credential, json=payload | {"branch": "main"})
    assert repeat.json()["id"] == session.json()["id"]
    return str(session.json()["id"])


async def exercise_direct(runs: RunsHarness, *, concurrent: bool = False) -> None:
    browser = runs.client
    path = f"/api/v1/workspaces/{runs.workspace_id}/agent-collaboration"
    async with member(runs, "林晨") as (peer, _), member(runs, "同事") as (other, _):
        _, sender_auth = await device(browser, path, "笔记本")
        device_id, target_auth = await device(peer, path, "开发台")
        _, other_auth = await device(other, path, "办公机")
        sender = await register(browser, path, sender_auth, "pi", "接口调查")
        target = await register(peer, path, target_auth, "custom-agent", "分页联调")
        second = await register(peer, path, target_auth, "codex", "第二个窗口")
        assert (await browser.get(path + "/sessions", headers=sender_auth)).json() == {"items": []}
        grant_path = f"{path}/my-sessions/{target}/contacts"
        assert (await browser.put(grant_path, headers=headers(browser), json={"member_ids": []})).status_code == 404
        grant = await peer.put(grant_path, headers=headers(peer), json={"member_ids": [runs.user_id]})
        assert grant.status_code == 204, grant.text
        directory = await browser.get(path + "/sessions", headers=sender_auth, params={"q": "林晨 orders-ui"})
        assert [item["id"] for item in directory.json()["items"]] == [target]
        assert "native_id" not in directory.json()["items"][0]
        assert (await other.get(path + "/sessions", headers=other_auth)).json() == {"items": []}
        invalid_workspace = path.replace(runs.workspace_id, str(uuid4()))
        assert (await browser.get(invalid_workspace + "/sessions", headers=sender_auth)).status_code == 401
        payload = {"sender_id": sender, "recipient_id": target, "body": "cursor 是省略还是传空串？"}
        send_headers = sender_auth | {"Idempotency-Key": "ask-cursor"}
        if concurrent:
            sent, retry = await asyncio.gather(
                *(browser.post(path + "/messages", json=payload, headers=send_headers) for _ in range(2))
            )
        else:
            sent = await browser.post(path + "/messages", json=payload, headers=send_headers)
            retry = await browser.post(path + "/messages", json=payload, headers=send_headers)
        assert sent.status_code == retry.status_code == 201, (sent.text, retry.text)
        message = sent.json()["id"]
        assert retry.json()["id"] == message
        assert (
            await browser.post(
                path + "/messages",
                json=payload | {"body": "不同内容"},
                headers=send_headers,
            )
        ).status_code == 409
        denied = await browser.post(path + "/messages", json=payload | {"recipient_id": second}, headers=send_headers)
        assert denied.status_code == 404
        pending = await peer.get(path + "/pending", headers=target_auth)
        assert [(item["session_id"], item["message_id"]) for item in pending.json()["items"]] == [(target, message)]
        assert "cursor" not in pending.text
        wake = f"{path}/sessions/{target}/messages/{message}/wake"
        assert (await peer.post(wake, headers=target_auth)).json() == {"reserved": True}
        assert (await peer.post(wake, headers=target_auth)).json() == {"reserved": False}
        assert (await peer.put(wake, headers=target_auth, json={"state": "accepted"})).status_code == 204
        status = (await browser.get(f"{path}/messages/{message}", headers=sender_auth)).json()
        assert (status["state"], status["wake_state"]) == ("pending", "accepted")
        claim_headers = target_auth | {"Idempotency-Key": "stable-claim"}
        wrong = await peer.post(f"{path}/sessions/{second}/claims", headers=claim_headers, json={"message_id": message})
        assert wrong.json() == {"item": None}
        claim_path = f"{path}/sessions/{target}/claims"
        claim = await peer.post(claim_path, headers=claim_headers, json={"message_id": message})
        assert claim.json()["item"]["body"] == payload["body"]
        assert (await peer.post(claim_path, headers=claim_headers, json={"message_id": message})).json() == claim.json()
        following = await browser.post(
            path + "/messages", headers=sender_auth | {"Idempotency-Key": "following"}, json=payload
        )
        assert following.status_code == 201, following.text
        following_id = following.json()["id"]
        following_wake = f"{path}/sessions/{target}/messages/{following_id}/wake"
        # 让已领取消息进入待确认，不等待真实超时，也不直接改表。
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(agent_delivery, "CLAIM_TTL", timedelta(0))
            assert (await peer.post(following_wake, headers=target_auth)).json() == {"reserved": False}
            blocked = await peer.post(
                claim_path, headers=target_auth | {"Idempotency-Key": "next-claim"}, json={"message_id": following_id}
            )
            assert blocked.status_code == 409, blocked.text
            recovered = await peer.post(claim_path, headers=claim_headers, json={"message_id": message})
            assert recovered.json()["item"]["state"] == "needs_confirmation"
            assert recovered.json()["item"]["body"] == payload["body"]
        reply = await peer.post(
            path + "/messages",
            headers=target_auth | {"Idempotency-Key": "reply"},
            json={
                "sender_id": target,
                "recipient_id": sender,
                "reply_to": message,
                "body": "首屏省略。",
            },
        )
        assert reply.status_code == 201, reply.text
        assert (await browser.get(f"{path}/messages/{message}", headers=sender_auth)).json()["state"] == "replied"
        assert (await peer.post(following_wake, headers=target_auth)).json() == {"reserved": True}
        result = await browser.post(
            f"{path}/sessions/{sender}/claims",
            headers=sender_auth | {"Idempotency-Key": "receive-reply"},
            json={"message_id": reply.json()["id"]},
        )
        assert result.json()["item"]["body"] == "首屏省略。"
        ack = await browser.post(
            f"{path}/sessions/{sender}/messages/{reply.json()['id']}/acknowledge", headers=sender_auth
        )
        assert ack.json()["state"] == "acknowledged"

        # 撤权和重新授权之间的旧问题不能再次成为未读消息。
        queued = await browser.post(path + "/messages", json=payload, headers=sender_auth | {"Idempotency-Key": "late"})
        assert queued.status_code == 201
        for ids in ([], [runs.user_id]):
            assert (await peer.put(grant_path, headers=headers(peer), json={"member_ids": ids})).status_code == 204
        assert (await peer.get(path + "/pending", headers=target_auth)).json() == {"items": []}
        assert (await peer.delete(path + "/devices/" + device_id, headers=headers(peer))).status_code == 204
        assert (await peer.get(path + "/pending", headers=target_auth)).status_code == 401
        assert (await browser.get(path + "/sessions", headers=sender_auth)).json() == {"items": []}


@pytest.mark.anyio
async def test_direct_agent_mail(runs: RunsHarness) -> None:
    await exercise_direct(runs)


@pytest.mark.anyio
async def test_task_scope_and_published_evidence(runs: RunsHarness) -> None:
    browser = runs.client
    created = await browser.post(runs.path, headers=runs.headers(), json=runs.payload("只有所有者能看"))
    task_id = created.json()["task_id"]
    path = f"/api/v1/workspaces/{runs.workspace_id}/agent-collaboration"
    async with member(runs, "任务参与人") as (peer, peer_id), member(runs, "未参与人") as (other, _):
        _, sender_auth = await device(browser, path, "台式机")
        _, target_auth = await device(peer, path, "笔记本")
        _, other_auth = await device(other, path, "测试机")
        sender = await register(browser, path, sender_auth, "claude-code", "我的任务会话")
        target = await register(peer, path, target_auth, "codex", "同事任务会话")
        outsider = await register(other, path, other_auth, "opencode", "未参与的会话")
        bind = f"/tasks/{task_id}"
        assert (await browser.put(f"{path}/sessions/{sender}{bind}", headers=sender_auth)).status_code == 404
        share = await browser.put(
            runs.path.replace("/runs", bind + "/sharing"),
            headers=runs.headers(),
            json={
                "revision": 0,
                "title": "分页联调",
                "summary": "约定首屏游标",
                "participant_ids": [peer_id],
            },
        )
        assert share.status_code == 204, share.text
        for client, key, session in [(browser, sender_auth, sender), (peer, target_auth, target)]:
            assert (await client.put(f"{path}/sessions/{session}{bind}", headers=key)).status_code == 204
        assert (await other.put(f"{path}/sessions/{outsider}{bind}", headers=other_auth)).status_code == 404
        assert (await other.get(f"{path}/task/{task_id}")).status_code == 404
        listing = await peer.get(f"{path}/task/{task_id}")
        assert {item["id"] for item in listing.json()["sessions"]} == {sender, target}
        assert "只有所有者能看" not in listing.text
        send = await browser.post(
            path + "/messages",
            headers=sender_auth | {"Idempotency-Key": "task-send"},
            json={
                "sender_id": sender,
                "recipient_id": target,
                "task_id": task_id,
                "body": "联调提问",
            },
        )
        assert send.status_code == 201, send.text
        evidence = await peer.post(
            f"{path}/sessions/{target}{bind}/evidence",
            headers=target_auth | {"Idempotency-Key": "evidence"},
            json={"title": "复测记录", "content": "仅包含主动发布的结果"},
        )
        assert evidence.status_code == 201, evidence.text
        assert (await browser.get(f"{path}/task/{task_id}")).json()["evidence"][0]["title"] == "复测记录"
        assert (await peer.delete(f"{path}/sessions/{target}{bind}", headers=target_auth)).status_code == 204
        assert (await peer.get(path + "/pending", headers=target_auth)).json() == {"items": []}
