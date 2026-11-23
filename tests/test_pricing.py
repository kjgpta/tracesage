"""Dollar pricing. genai-prices is optional; overrides and failures never raise."""
from __future__ import annotations

from tracesage.config import ModelPrice, TraceSageConfig
from tracesage.pricing import format_cost, price_usd


def test_known_model_prices() -> None:
    cost = price_usd("gpt-4o-mini", "openai", 1000, 200, 0, 0, TraceSageConfig())
    assert cost is not None
    assert cost > 0


def test_unknown_model_is_none() -> None:
    assert price_usd("not-a-real-model-xyz", None, 10, 10, 0, 0, TraceSageConfig()) is None


def test_override_beats_library() -> None:
    cfg = TraceSageConfig(
        price_overrides={
            "openai:gpt-4o-mini": ModelPrice(input_per_mtok=1, output_per_mtok=2)
        }
    )
    cost = price_usd("gpt-4o-mini", "openai", 1_000_000, 1_000_000, 0, 0, cfg)
    assert cost == 3.0


def test_cache_tokens_use_their_rate() -> None:
    cfg = TraceSageConfig(
        price_overrides={
            "m": ModelPrice(
                input_per_mtok=10,
                output_per_mtok=0,
                cache_read_per_mtok=1,
                cache_write_per_mtok=2,
            )
        }
    )
    # 100 input includes 40 cache-read and 10 cache-write, so 50 fresh.
    cost = price_usd("m", None, 100, 0, 40, 10, cfg)
    assert cost is not None
    assert abs(cost - ((50 * 10 + 40 * 1 + 10 * 2) / 1_000_000)) < 1e-12


def test_disabled_and_missing_tokens_are_none() -> None:
    cfg = TraceSageConfig(pricing_enabled=False)
    assert price_usd("gpt-4o-mini", "openai", 10, 10, 0, 0, cfg) is None
    assert price_usd("gpt-4o-mini", "openai", 0, 0, 0, 0, TraceSageConfig()) is None


def test_never_raises_on_bad_input() -> None:
    assert price_usd(None, None, "nope", None, None, None, TraceSageConfig()) is None  # type: ignore[arg-type]


def test_format_cost() -> None:
    assert format_cost(None) == "n/a"
    assert format_cost(0.00026).startswith("$")
    assert format_cost(1.2) == "$1.20"


def test_overrides_from_json_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "TRACESAGE_PRICE_OVERRIDES",
        '{"local": {"input_per_mtok": 3, "output_per_mtok": 4}}',
    )
    cfg = TraceSageConfig()
    assert cfg.price_overrides["local"].output_per_mtok == 4
