from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPriceKrw:
    input_per_million_tokens: float
    output_per_million_tokens: float


DEFAULT_PRICING_KRW: dict[str, ModelPriceKrw] = {
    "fake-json-v1": ModelPriceKrw(0.0, 0.0),
}

# OpenAI 공식 API 가격(USD / 1M tokens). 원화 표시는 운영 환경의
# QNA_GPT_USD_KRW_RATE를 곱한 예상값이며 실제 청구액과 다를 수 있습니다.
OPENAI_PRICING_USD: dict[str, ModelPriceKrw] = {
    "gpt-5.6-sol": ModelPriceKrw(5.0, 30.0),
    "gpt-5.6": ModelPriceKrw(5.0, 30.0),
    "gpt-5.6-terra": ModelPriceKrw(2.5, 15.0),
    "gpt-5.6-luna": ModelPriceKrw(1.0, 6.0),
}


def estimate_cost_krw(
    model: str,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    pricing: dict[str, ModelPriceKrw] | None = None,
) -> float | None:
    table = pricing or DEFAULT_PRICING_KRW
    price = table.get(str(model))
    if price is None and pricing is None:
        usd_price = OPENAI_PRICING_USD.get(str(model))
        if usd_price is not None:
            try:
                exchange_rate = max(
                    0.0, float(os.getenv("QNA_GPT_USD_KRW_RATE", "1450"))
                )
            except ValueError:
                exchange_rate = 1450.0
            price = ModelPriceKrw(
                usd_price.input_per_million_tokens * exchange_rate,
                usd_price.output_per_million_tokens * exchange_rate,
            )
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return round(
        input_tokens / 1_000_000 * price.input_per_million_tokens
        + output_tokens / 1_000_000 * price.output_per_million_tokens,
        6,
    )
