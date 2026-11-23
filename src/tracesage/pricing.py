"""Dollar cost for an LLM call.

genai-prices is an optional extra (``tracesage[pricing]`` or ``tracesage[apps]``).
When it is missing, or the model is unknown, this returns None — shown as
"n/a", never zero, and never raised into the tracer.
"""
from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracesage.config import ModelPrice, TraceSageConfig


def format_cost(value: float | None) -> str:
    """Dollar amount, or ``n/a`` when the call was not priced."""
    if value is None:
        return "n/a"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def pricing_available() -> bool:
    """True when the genai-prices extra is importable."""
    try:
        import genai_prices  # noqa: F401
    except ImportError:
        return False
    return True


def price_usd(
    model: str | None,
    provider: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_read: int | None,
    cache_write: int | None,
    config: TraceSageConfig | None = None,
) -> float | None:
    """Price one LLM call in USD. Never raises. None means unknown."""
    try:
        if config is not None and not config.pricing_enabled:
            return None
        if not model:
            return None
        ti = int(tokens_in or 0)
        to = int(tokens_out or 0)
        cr = int(cache_read or 0)
        cw = int(cache_write or 0)
        if ti == 0 and to == 0 and cr == 0 and cw == 0:
            return None
        override = _override(config, provider, model)
        if override is not None:
            return _from_rates(override, ti, to, cr, cw)
        if not pricing_available():
            return None
        return _library_price(provider or "", model, ti, to, cr, cw)
    except Exception:
        return None


def _override(
    config: TraceSageConfig | None, provider: str | None, model: str
) -> ModelPrice | None:
    if config is None or not config.price_overrides:
        return None
    table = config.price_overrides
    if provider:
        hit = table.get(f"{provider}:{model}")
        if hit is not None:
            return hit
    return table.get(model)


def _from_rates(rates: ModelPrice, ti: int, to: int, cr: int, cw: int) -> float:
    # input_tokens already includes cache tokens (genai-prices convention).
    fresh = max(0, ti - cr - cw)
    total = (
        fresh * rates.input_per_mtok
        + cr * rates.cache_read_per_mtok
        + cw * rates.cache_write_per_mtok
        + to * rates.output_per_mtok
    ) / 1_000_000.0
    return float(total)


@lru_cache(maxsize=1024)
def _library_price(
    provider: str, model: str, ti: int, to: int, cr: int, cw: int
) -> float | None:
    try:
        from genai_prices import Usage, calc_price
    except ImportError:
        return None
    try:
        usage: Any = Usage(
            input_tokens=ti,
            output_tokens=to,
            cache_read_tokens=cr or None,
            cache_write_tokens=cw or None,
        )
        kwargs: dict[str, Any] = {"model_ref": model}
        if provider:
            kwargs["provider_id"] = provider
        result = calc_price(usage, **kwargs)
        total = getattr(result, "total_price", None)
        if total is None:
            return None
        return float(total)
    except Exception:
        return None
