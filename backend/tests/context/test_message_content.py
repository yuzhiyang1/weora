"""以飞书官方接收结构为夹具，不调用网络、不下载或解析资源。"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from tests.context.test_context import setup
from weora.modules.context.domain import Json, Message, MessagePage
from weora.modules.context.feishu import FeishuHttpReader


def message(kind: str, content: object, *, deleted: bool = False) -> Message:
    raw: Json = {
        "message_id": "om_content",
        "chat_id": "oc_project",
        "create_time": "1609296809000",
        "sender": {"id": "ou_author"},
        "msg_type": kind,
        "deleted": deleted,
        "body": {"content": json.dumps(content, ensure_ascii=False)},
    }
    return FeishuHttpReader._message(raw)


def test_text_and_official_flat_post_preserve_body_mentions_and_links() -> None:
    plain = message("text", {"text": "原有文本 @_user_1"})
    assert plain.text == "原有文本 @_user_1" and plain.parse_status == "complete"
    post = message(
        "post",
        {
            "title": "登录方案",
            "content": [
                [
                    {"tag": "text", "text": "请联系"},
                    {"tag": "at", "user_id": "@_user_1", "user_name": "小明"},
                    {"tag": "a", "text": "方案", "href": "https://example.com/design"},
                ],
                [{"tag": "code_block", "language": "python", "text": "print('ok')"}],
            ],
        },
    )
    assert post.content_type == "post" and post.parse_status == "complete"
    assert "登录方案" in post.text and "小明" in post.text and "@_user_1" in post.text
    assert "https://example.com/design" in post.text and "print('ok')" in post.text


def test_content_v2_is_preferred_and_markdown_images_are_explicit_metadata_only() -> None:
    post = message(
        "post",
        {
            "title": "方案",
            "content": [[{"tag": "text", "text": "旧转换文本"}]],
            "content_v2": [[{"tag": "md", "text": "**原始内容** ![截图](img_demo)"}]],
        },
    )
    assert "原始内容" in post.text and "旧转换文本" not in post.text
    assert post.parse_status == "partial" and "resource_not_downloaded" in post.parse_notes
    assert post.resources == ({"type": "image", "image_key": "img_demo", "downloaded": False, "parsed": False},)


@pytest.mark.parametrize("markdown", ["![图][ref]\n\n[ref]: img_demo", '<img src="https://untrusted.invalid/image">'])
def test_unresolved_markdown_media_cannot_claim_complete(markdown: str) -> None:
    post = message("post", {"content_v2": [[{"tag": "md", "text": markdown}]]})
    assert post.parse_status == "partial" and "resource_not_downloaded" in post.parse_notes
    assert post.resources == ()


@pytest.mark.parametrize(
    "node",
    [
        {"tag": "future_node", "secret": "不得回显"},
        None,
        {"tag": "text", "text": []},
        {"tag": "at", "user_id": ""},
        {"tag": "a", "text": "空地址", "href": ""},
    ],
)
def test_unknown_or_malformed_post_nodes_are_not_silently_dropped(node: object) -> None:
    post = message("post", {"title": "可读标题", "content": [[{"tag": "text", "text": "可读片段"}, node]]})
    assert post.parse_status == "partial" and post.parse_notes
    assert "可读片段" in post.text and "未解析" in post.text and "不得回显" not in post.text


@pytest.mark.parametrize(
    "kind,key",
    [
        ("image", "image_key"),
        ("file", "file_key"),
        ("folder", "file_key"),
        ("audio", "file_key"),
        ("media", "file_key"),
        ("sticker", "file_key"),
    ],
)
def test_resource_metadata_is_whitelisted_not_fetched(kind: str, key: str) -> None:
    value = message(
        kind,
        {
            key: "resource_demo",
            "file_name": "测试.txt",
            "duration": 2000,
            "download_url": "https://untrusted.invalid/private",
            "arbitrary": "不可回显",
        },
    )
    assert value.parse_status == "partial" and value.resources
    resource = value.resources[0]
    assert resource[key] == "resource_demo" and resource["downloaded"] is False and resource["parsed"] is False
    assert "download_url" not in resource and "arbitrary" not in resource
    assert "未下载" in value.text and "未解析" in value.text


def test_official_system_template_resolves_only_explicit_values() -> None:
    value = message(
        "system",
        {
            "template": "{from_user} invited {to_chatters} to this chat.",
            "from_user": ["机器人"],
            "to_chatters": ["小明", "小王"],
        },
    )
    assert value.parse_status == "complete" and value.text == "机器人 invited 小明、 小王 to this chat."
    divider = message(
        "system",
        {
            "template": "{divider_text}",
            "from_user": [],
            "to_chatters": [],
            "divider_text": {"text": "New Session", "i18n_text": {"zh_cn": "新话题"}},
        },
    )
    assert divider.parse_status == "complete" and divider.text == "新话题"
    unknown = message("system", {"template": "{unknown} updated settings", "unknown": ["不应猜测"]})
    assert unknown.parse_status in {"partial", "unsupported"} and "不应猜测" not in unknown.text
    assert "未下载附件" not in unknown.text


def test_official_received_card_extracts_text_but_never_executes_buttons() -> None:
    card = message(
        "interactive",
        {
            "title": "发布通知",
            "elements": [[{"tag": "text", "text": "已发布"}], [{"tag": "button", "text": "查看", "type": "primary"}]],
        },
    )
    assert "发布通知" in card.text and "已发布" in card.text and "查看" in card.text
    assert card.parse_status == "partial" and "card_interaction_not_executed" in card.parse_notes
    assert "未下载附件" not in card.text


@pytest.mark.parametrize("kind", ["text", "post", "image", "system"])
def test_recalled_message_never_retains_body_or_resources(kind: str) -> None:
    value = message(kind, {"text": "删除前秘密", "image_key": "secret_key"}, deleted=True)
    assert value.parse_status == "recalled" and value.text == "[消息已撤回]"
    assert value.resources == () and "删除前秘密" not in str(value)


@pytest.mark.parametrize("content", [None, [], {"text": None}])
def test_malformed_text_is_not_stringified_into_facts(content: object) -> None:
    value = message("text", content)
    assert value.parse_status == "unsupported" and "malformed_content" in value.parse_notes


def test_oversize_text_and_resources_cannot_claim_full_parse() -> None:
    value = message("text", {"text": "中" * 25000})
    assert len(value.text) <= 24000 and value.parse_status == "partial"
    assert "content_truncated" in value.parse_notes
    post = message("post", {"content": [[{"tag": "img", "image_key": f"img_{index}"} for index in range(25)]]})
    assert len(post.resources) <= 20 and "content_truncated" in post.parse_notes


def test_unknown_message_kind_does_not_dump_provider_payload() -> None:
    value = message("future_type", cast(Json, {"private": "不得回显"}))
    assert value.parse_status == "unsupported" and "不得回显" not in value.text
    assert "未下载附件" not in value.text


@pytest.mark.parametrize("automatic", [False, True])
def test_parsed_post_is_complete_and_keeps_citable_snapshot_contract(automatic: bool) -> None:
    async def scenario() -> None:
        f = await setup()
        post = replace(
            message("post", {"title": "登录方案", "content": [[{"tag": "text", "text": "采用验证码"}]]}),
            created_at=f.message(1).created_at,
        )
        f.reader.pages[None] = MessagePage((post,))
        result = (
            await f.service.automatic(f.scope, f.source, trigger_message_id="om_trigger", include_nearby=True)
            if automatic
            else await f.service.read(f.scope, f.source)
        )
        assert result["status"] == "complete"
        actual = cast(list[Json], result["messages"])[0]
        assert actual["content_type"] == "post" and actual["parse_status"] == "complete"
        await f.service.validate_refs(f.scope, [str(actual["ref"])])

    asyncio.run(scenario())


def test_resource_metadata_consumes_budget_and_purges_with_snapshot_messages() -> None:
    async def scenario() -> None:
        f = await setup()
        resource = replace(
            message("file", {"file_key": "file_private", "file_name": "未公开文件.txt"}),
            created_at=f.message(1).created_at,
        )
        f.reader.pages[None] = MessagePage((resource,))
        await f.store.change(
            f.actor.workspace_id, "budget", str(f.scope.run_id), lambda _: {"calls": 0, "messages": [], "tokens": 11900}
        )
        limited = await f.service.read(f.scope, f.source)
        assert limited["status"] == "partial" and limited["messages"] == []
        assert cast(Json, limited["coverage"])["reason"] == "budget_exceeded"
        await f.store.change(
            f.actor.workspace_id, "budget", str(f.scope.run_id), lambda _: {"calls": 0, "messages": [], "tokens": 0}
        )
        actual = await f.service.read(f.scope, f.source)
        assert cast(list[Json], actual["messages"])[0]["resources"]
        key = str(actual["snapshot_id"])
        await f.store.change(
            f.actor.workspace_id,
            "snapshot",
            key,
            lambda old: dict(
                old or {},
                expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            ),
        )
        await f.service.purge_expired(f.actor.workspace_id)
        retained = await f.store.get(f.actor.workspace_id, "snapshot", key)
        assert retained and retained["messages"] == [] and retained["sha256"] == actual["sha256"]
        assert "file_private" not in str(retained) and "未公开文件" not in str(retained)

    asyncio.run(scenario())


def test_old_persisted_snapshot_is_not_reinterpreted_or_rewritten() -> None:
    async def scenario() -> None:
        f = await setup()
        f.reader.pages[None] = MessagePage((replace(f.message(1), content_type="post", text="旧占位正文"),))
        old = await f.service.read(f.scope, f.source)
        for item in cast(list[Json], old["messages"]):
            for field in ("parse_status", "parse_notes", "resources"):
                item.pop(field)
        key = str(old["snapshot_id"])
        await f.store.change(f.actor.workspace_id, "snapshot", key, lambda _: old)
        preview = await f.service.preview(f.scope, UUID(key))
        assert preview == old and preview["status"] == "partial"
        assert await f.store.get(f.actor.workspace_id, "snapshot", key) == old

    asyncio.run(scenario())
