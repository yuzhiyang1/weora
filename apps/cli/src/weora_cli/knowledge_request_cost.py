"""用本机固定费率或宿主记录的金额计算 USD 微元。"""

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path

from weora_cli.commands import JsonValue

_RATE_FILE = Path(__file__).with_name("knowledge_usage_prices.json")
_MAX_MICROS = 10**15
_DATE_SUFFIX = re.compile(r"-(?:[0-9]{8}|20[0-9]{2}-[0-9]{2}-[0-9]{2})$")


@cache
def _prices() -> dict[str, dict[str, JsonValue]]:
    rates: dict[str, dict[str, JsonValue]] = json.loads(_RATE_FILE.read_text(encoding="utf-8"))["models"]
    return rates


def reported_micros(value: JsonValue) -> int | None:
    if type(value) not in (int, float):
        return None
    amount = Decimal(str(value))
    if not amount.is_finite() or not 0 <= amount <= _MAX_MICROS / 1_000_000:
        return None
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def request_cost(
    model: JsonValue, tokens: dict[str, int], *, tier: JsonValue, hour_write: int = 0, reported: JsonValue = None,
) -> int | None:
    actual = reported_micros(reported)
    if actual is not None:
        return actual
    if not isinstance(model, str) or not any(tokens.values()):
        return None
    name = _DATE_SUFFIX.sub("", model.strip().lower()).removeprefix("openai/").removeprefix("anthropic/")
    rates = _prices().get(name)
    if rates is None:
        return None
    speed = "fast" if tier in ("fast", "priority") else "flex" if tier == "flex" else "standard"
    selected = rates.get(speed)
    if isinstance(selected, dict):
        rates = selected
    prompt = tokens["input"] + tokens["cache_read"] + tokens["cache_creation"]
    bands = rates.get("above")
    if isinstance(bands, list):
        for band in bands:
            if isinstance(band, dict) and type(limit := band.get("tokens")) is int and prompt > limit:
                rates = band
    hour = min(hour_write, tokens["cache_creation"])
    units = {
        "input": tokens["input"], "output": tokens["output"], "cacheRead": tokens["cache_read"],
        "cacheWrite": tokens["cache_creation"] - hour, "cacheWrite1h": hour,
    }
    price = sum(
        (Decimal(str(rates.get(key, rates.get("cacheWrite", 0)))) * value for key, value in units.items()),
        Decimal(0),
    )
    return int(price.to_integral_value(rounding=ROUND_HALF_UP)) if 0 <= price <= _MAX_MICROS else None
