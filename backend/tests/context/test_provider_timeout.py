"""提供方超时属于上下文不可用，不能泄漏成 Pi 整轮运行超时。"""

from contextlib import AbstractAsyncContextManager
from typing import cast

import aiohttp
import pytest
from cryptography.fernet import Fernet

from weora.modules.context.domain import ContextError
from weora.modules.context.feishu import FeishuHttpReader, FernetSecrets


@pytest.mark.anyio
async def test_provider_timeout_is_safe_context_failure() -> None:
    class TimedOut(AbstractAsyncContextManager[object]):
        async def __aenter__(self) -> object:
            raise TimeoutError("提供方内部网络详情不得返回模型")

        async def __aexit__(self, *args: object) -> None:
            return None

    class Session:
        def request(self, *args: object, **kwargs: object) -> TimedOut:
            return TimedOut()

    reader = FeishuHttpReader(cast(aiohttp.ClientSession, Session()), FernetSecrets(Fernet.generate_key().decode()))
    with pytest.raises(ContextError) as caught:
        await reader._json("GET", "/im/v1/messages")
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert "网络详情" not in caught.value.message
