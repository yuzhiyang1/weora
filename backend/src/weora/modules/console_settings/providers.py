"""与锁定 Pi SDK 对齐的厂商目录；不接受浏览器指定请求主机。"""

import json
from pathlib import Path
from typing import TypedDict

from weora.modules.console_settings.domain import SettingsError


class ModelOption(TypedDict):
    id: str
    name: str


class ProviderOption(TypedDict):
    id: str
    label: str
    models: list[ModelOption]


PROVIDERS: list[ProviderOption] = json.loads(Path(__file__).with_name("providers.json").read_text(encoding="utf-8"))[
    "providers"
]
PROVIDER_IDS = frozenset(item["id"] for item in PROVIDERS)


def validate_model(provider: str, model_id: str) -> None:
    """只保存当前 SDK 能实际解析的预设厂商及模型，避免可保存却不可执行。"""
    selected = next((item for item in PROVIDERS if item["id"] == provider), None)
    if selected is None or not any(model["id"] == model_id for model in selected["models"]):
        raise SettingsError("MODEL_NOT_SUPPORTED", "请选择预设厂商及当前运行时支持的模型", 422)
