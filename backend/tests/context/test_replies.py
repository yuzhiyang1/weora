"""验证短回复、原生图文结构与真实 multipart HTTP；不连接生产飞书。"""

import base64
import json
from uuid import uuid4

import pytest
from aiohttp import BodyPartReader, web
from cryptography.fernet import Fernet

from weora.bootstrap.execution_services import ManagedFeishuReader
from weora.modules.context.domain import ContextError, Json
from weora.modules.context.feishu import FernetSecrets
from weora.modules.context.replies import brief_reply

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7uoAAAAASUVORK5CYII="
)


def test_reply_is_brief_and_does_not_show_raw_markdown() -> None:
    assert brief_reply("## 结论\n**未复现**，状态为 `200`。") == "结论\n未复现，状态为 200。"
    original = "四科页面正常，明细没有变化。\n" * 100
    result = brief_reply(original)
    assert len(result) <= 280 and result.endswith("详细过程见 Weora 运行记录。")
    assert len(original) > len(result)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["normal", "expired_token", "forbidden"])
async def test_reply_uploads_screenshots_then_sends_one_native_post(mode: str) -> None:
    uploads: list[bytes] = []
    sent: list[Json] = []
    tokens: list[str] = []
    auth_count = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal auth_count
        if request.path == "/auth/v3/tenant_access_token/internal":
            auth_count += 1
            return web.json_response({"code": 0, "tenant_access_token": f"token-{auth_count}", "expire": 7200})
        if request.path == "/im/v1/images":
            if mode == "forbidden":
                return web.json_response({"code": 230002}, status=403)
            tokens.append(request.headers["Authorization"])
            form = await request.multipart()
            kind = await form.next()
            assert isinstance(kind, BodyPartReader) and kind.name == "image_type" and await kind.text() == "message"
            image = await form.next()
            assert isinstance(image, BodyPartReader) and image.name == "image"
            assert image.headers["Content-Type"] == "image/png"
            data = bytes(await image.read())
            assert data == PNG
            if mode == "expired_token" and request.headers["Authorization"] == "Bearer token-1":
                return web.json_response({"code": 99991663})
            uploads.append(data)
            return web.json_response({"code": 0, "data": {"image_key": f"img_test_{len(uploads)}"}})
        payload = await request.json()
        sent.append(payload)
        return web.json_response({"code": 0, "data": {"message_id": "om_post"}})

    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        cipher = FernetSecrets(Fernet.generate_key().decode())
        connection: Json = {"id": str(uuid4()), "version": 1, "app_id": "cli_test", "secret": cipher.encrypt("secret")}
        # 使用生产包装层，避免只验证底层 HTTP 而遗漏接口转发。
        reader = ManagedFeishuReader(cipher)
        reader._reader().BASE = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            if mode == "forbidden":
                with pytest.raises(ContextError, match="权限"):
                    await reader.upload_image(connection, PNG)
                assert sent == []
                return
            first = await reader.upload_image(connection, PNG)
            second = await reader.upload_image(connection, PNG)
            conclusion = "## 核对结论\n\n" + "这里保留证据和文件位置。\n" * 28 + "\n**尚未核实生产版本。**"
            result = await reader.reply(connection, "om_original", conclusion, "stable-id", image_keys=(first, second))
        finally:
            await reader.aclose()
        assert result == "om_post" and len(sent) == 1 and len(uploads) == 2
        assert sent[0]["msg_type"] == "post" and sent[0]["uuid"] == "stable-id"
        assert sent[0]["reply_in_thread"] is True
        content = json.loads(str(sent[0]["content"]))["zh_cn"]["content"]
        assert content == [
            [{"tag": "md", "text": conclusion}],
            [{"tag": "img", "image_key": "img_test_1"}],
            [{"tag": "img", "image_key": "img_test_2"}],
        ]
        assert auth_count == (2 if mode == "expired_token" else 1)
    finally:
        await runner.cleanup()
