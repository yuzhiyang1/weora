"""只查询预设厂商的官方模型接口，不生成内容，也不接受自定义 URL。"""

import json
from urllib.parse import quote

import aiohttp

from weora.modules.console_settings.domain import SettingsError


async def probe_model(provider: str, model_id: str, api_key: str) -> None:
    """限制主机、超时、重定向和响应大小；错误不包含请求头或供应商响应。"""
    model = quote(model_id, safe="")
    endpoints = {
        "deepseek": "https://api.deepseek.com/models",
        "openai": f"https://api.openai.com/v1/models/{model}",
        "anthropic": f"https://api.anthropic.com/v1/models/{model}",
        "google": f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
        "moonshotai-cn": "https://api.moonshot.cn/v1/models",
    }
    if provider not in endpoints:
        raise SettingsError("MODEL_NOT_SUPPORTED", "当前提供方尚未接入", 422)
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif provider == "google":
        headers = {"x-goog-api-key": api_key}
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as client,
            client.get(endpoints[provider], allow_redirects=False, headers=headers) as response,
        ):
            if response.status != 200:
                raise SettingsError("MODEL_CHECK_FAILED", "认证或模型服务检测未通过，请核对密钥与服务状态", 502)
            # 网络可能分段到达；不能把一次读取的半段 JSON 当成完整响应。
            content = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                content.extend(chunk)
                if len(content) > 65536:
                    raise SettingsError("MODEL_CHECK_FAILED", "模型服务返回内容异常", 502)
            body = json.loads(content)
        if not isinstance(body, dict):
            raise ValueError
        if provider in {"deepseek", "moonshotai-cn"}:
            if not isinstance(body.get("data"), list):
                raise ValueError
            available = {str(item["id"]) for item in body["data"] if isinstance(item, dict) and "id" in item}
            if model_id not in available:
                raise SettingsError("MODEL_NOT_AVAILABLE", "认证通过，但当前账号不可使用所选模型", 422)
        else:
            # 单模型接口可能将别名解析为版本 ID；200 与有效元数据证明请求的别名可见。
            identifier = body.get("name" if provider == "google" else "id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError
    except (TimeoutError, aiohttp.ClientError, ValueError, AttributeError):
        raise SettingsError("MODEL_CHECK_FAILED", "模型服务暂不可用，请稍后重试", 502) from None
