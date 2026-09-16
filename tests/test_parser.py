"""Parser tests - the shapes from the spec plus the traps around them."""

from __future__ import annotations

from decimal import Decimal

from app.telegram.parser import BUY, SHORT, parse_signal


def test_btc_long_with_entry_range_is_parsed():
    result = parse_signal(
        "BTC LONG\n\nEntry: 100000-101000\nTP1: 102000\nTP2: 104000\nSL: 98000"
    )
    assert result.ok
    signal = result.signal
    assert signal.symbol == "BTCUSDT"
    assert signal.direction == BUY
    assert signal.entry_low == Decimal("100000")
    assert signal.entry_high == Decimal("101000")
    assert signal.stop_loss == Decimal("98000")
    assert signal.take_profits == [Decimal("102000"), Decimal("104000")]


def test_single_entry_with_emoji_and_symbol_suffix():
    result = parse_signal("\U0001F680 BTCUSDT LONG\nEntry 100500\nTP 103000\nSL 99000")
    assert result.ok
    assert result.signal.symbol == "BTCUSDT"
    assert result.signal.entry_low == result.signal.entry_high == Decimal("100500")
    assert result.signal.take_profits == [Decimal("103000")]
    assert result.signal.stop_loss == Decimal("99000")


def test_hashtag_and_buy_keyword_format():
    result = parse_signal("#BTC LONG\nBUY 100500\nTP1 102000\nTP2 104000\nSTOP 99000")
    assert result.ok
    assert result.signal.symbol == "BTCUSDT"
    assert result.signal.entry_low == Decimal("100500")
    assert result.signal.take_profits == [Decimal("102000"), Decimal("104000")]
    assert result.signal.stop_loss == Decimal("99000")


def test_short_signal_is_reported_as_short():
    result = parse_signal("BTC SHORT\n\nEntry: 100000\nTP: 98000\nSL: 102000")
    assert not result.ok
    assert result.signal.direction == SHORT
    assert "SHORT" in result.reason


def test_sell_keyword_is_also_a_short():
    result = parse_signal("SELL ETHUSDT\nEntry 3500\nTP 3400\nSL 3600")
    assert result.signal.direction == SHORT


def test_long_wins_over_the_word_sell_in_the_body():
    result = parse_signal(
        "ETH/USDT LONG\nEntry: 3500\nTP1: 3700 (sell here)\nSL: 3400"
    )
    assert result.ok
    assert result.signal.direction == BUY
    assert result.signal.symbol == "ETHUSDT"


def test_missing_sl_is_rejected():
    result = parse_signal("BTC LONG\nEntry: 100000-101000\nTP1: 102000")
    assert not result.ok
    assert "SL" in result.reason
    assert result.looks_like_signal


def test_missing_entry_is_rejected():
    result = parse_signal("BTC LONG\nTP1: 102000\nSL: 98000")
    assert not result.ok
    assert "entry" in result.reason


def test_missing_tp_is_rejected():
    result = parse_signal("BTC LONG\nEntry: 100000\nSL: 98000")
    assert not result.ok
    assert "TP" in result.reason


def test_sl_above_entry_is_rejected():
    result = parse_signal("BTC LONG\nEntry: 100000\nTP1: 102000\nSL: 101000")
    assert not result.ok
    assert "SL" in result.reason


def test_tp_below_entry_is_rejected():
    result = parse_signal("BTC LONG\nEntry: 100000\nTP1: 99000\nSL: 98000")
    assert not result.ok
    assert "TP" in result.reason


def test_plain_chat_is_not_signal_shaped():
    result = parse_signal("gm everyone, the market looks strong today")
    assert result.signal is None
    assert not result.looks_like_signal


def test_thousand_separators_and_percentages():
    result = parse_signal(
        "ETH/USDT LONG\nEntry Zone: 3,500 - 3,550\n"
        "Take Profit 1: 3,700\nTake Profit 2: 3,900 (+10%)\nStop Loss: 3,400"
    )
    assert result.ok
    assert result.signal.entry_low == Decimal("3500")
    assert result.signal.entry_high == Decimal("3550")
    assert result.signal.take_profits == [Decimal("3700"), Decimal("3900")]
    assert result.signal.stop_loss == Decimal("3400")


def test_lowercase_and_dash_variants():
    result = parse_signal("sol long\nentry: 150 – 152\ntargets: 160 / 170\nstop-loss: 145")
    assert result.ok
    assert result.signal.symbol == "SOLUSDT"
    assert result.signal.entry_high == Decimal("152")
    assert result.signal.take_profits == [Decimal("160"), Decimal("170")]


def test_reversed_entry_range_is_normalised():
    result = parse_signal("BTC LONG\nEntry: 101000-100000\nTP: 102000\nSL: 98000")
    assert result.ok
    assert result.signal.entry_low == Decimal("100000")
    assert result.signal.entry_high == Decimal("101000")


def test_unknown_ticker_still_parses_so_the_whitelist_can_reject_it():
    result = parse_signal("ABC LONG\nEntry: 1.0-1.1\nTP1: 1.5\nSL: 0.9")
    assert result.ok
    assert result.signal.symbol == "ABCUSDT"


def test_empty_message():
    assert parse_signal("   ").signal is None
