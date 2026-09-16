"""Configuration and secret handling."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from app.config import Settings
from app.logging_setup import RedactFilter


def build(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_paper_is_the_default_mode():
    assert build().trading_mode == "PAPER"
    assert build().is_live is False


def test_live_must_be_asked_for_explicitly():
    settings = build(trading_mode="live")
    assert settings.trading_mode == "LIVE"
    assert settings.is_live


def test_channels_and_halal_coins_are_split():
    settings = build(
        telegram_channels="@one, @two ;@three", halal_coins="BTC,eth/usdt, SOL-USDT"
    )
    assert settings.channels == ["@one", "@two", "@three"]
    assert settings.halal_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_tp_splits_become_fractions():
    assert build(tp_splits="30,30,40").tp_split_ratios == [
        Decimal("0.3"), Decimal("0.3"), Decimal("0.4")
    ]
    assert build(tp_splits="100").tp_split_ratios == [Decimal("1")]


def test_tp_splits_must_sum_to_100():
    with pytest.raises(ValueError):
        build(tp_splits="30,30,30")


def test_impossible_risk_settings_are_rejected():
    with pytest.raises(ValueError):
        build(trade_amount_usdt="0")
    with pytest.raises(ValueError):
        build(max_open_trades=0)


def test_secrets_do_not_leak_through_repr():
    settings = build(mexc_api_secret="super-secret-value", telegram_session="session-string")
    assert "super-secret-value" not in repr(settings)
    assert "session-string" not in str(settings)
    assert settings.mexc_api_secret.get_secret_value() == "super-secret-value"


def test_secrets_are_redacted_from_log_records():
    settings = build(mexc_api_secret="super-secret-value")
    log_filter = RedactFilter(settings.secret_values())
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1,
        "calling with key=super-secret-value", None, None,
    )
    log_filter.filter(record)
    assert "super-secret-value" not in record.getMessage()
    assert "***" in record.getMessage()


def test_notifications_need_both_token_and_chat():
    assert build().notifications_enabled is False
    assert build(telegram_admin_bot_token="t").notifications_enabled is False
    assert build(telegram_admin_bot_token="t", telegram_admin_chat_id="1").notifications_enabled


def test_breakers_are_off_by_default_except_the_spread_gate():
    settings = build()
    assert settings.max_daily_loss_usdt == 0
    assert settings.max_consecutive_losses == 0
    assert settings.max_entry_spread_percent == Decimal("1")


def test_breaker_values_are_parsed_and_checked():
    settings = build(max_daily_loss_usdt="25.5", max_consecutive_losses="3",
                     max_entry_spread_percent="0")
    assert settings.max_daily_loss_usdt == Decimal("25.5")
    assert settings.max_consecutive_losses == 3
    assert settings.max_entry_spread_percent == 0
    with pytest.raises(ValueError):
        build(max_daily_loss_usdt="-1")
    with pytest.raises(ValueError):
        build(max_consecutive_losses=-1)
