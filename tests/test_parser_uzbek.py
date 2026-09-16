"""Parser tests built from real posts in the configured channels.

The channels write in Uzbek, wrap everything in Telegram markdown, and often
never name the side - these are the exact shapes that were being missed.
"""

from __future__ import annotations

from decimal import Decimal

from app.telegram.parser import BUY, SHORT, parse_signal

AVAX = """🔺#AVAX

4 soatlikda juda go'zal holatda. Agar BTC pastga yurib ketmasa, bu tepaga yurish qilishi kerak.

**Kirish:**     $12.6
**TP 1: **      $13.75
**TP 2: **      $14.82
**Stop:**      $11.94

**__Faqat stop bilan kiring__**"""

SOL_SETUP = """**__🧬__****__ __****__#SOL__****__ Setup __**

Kirish: 120
Stop: 116
Profit: 144

O'zim ham limit qo'ydim
**__Stopsiz kirish yo'q__**"""


def test_uzbek_labels_with_markdown_and_dollar_prices():
    result = parse_signal(AVAX)
    assert result.ok
    signal = result.signal
    assert signal.symbol == "AVAXUSDT"
    assert signal.entry_low == Decimal("12.6")
    assert signal.stop_loss == Decimal("11.94")
    assert signal.take_profits == [Decimal("13.75"), Decimal("14.82")]


def test_direction_is_read_from_the_numbers_when_no_word_is_written():
    result = parse_signal(AVAX)
    assert result.signal.direction == BUY
    assert result.signal.direction_inferred


def test_profit_label_and_underscored_hashtag():
    result = parse_signal(SOL_SETUP)
    assert result.ok
    assert result.signal.symbol == "SOLUSDT"
    assert result.signal.entry_low == Decimal("120")
    assert result.signal.stop_loss == Decimal("116")
    assert result.signal.take_profits == [Decimal("144")]


def test_mirror_geometry_is_a_short_and_stays_untraded():
    result = parse_signal("#ETH\n\nKirish: 3000\nTP 1: 2800\nStop: 3150")
    assert not result.ok
    assert result.signal.direction == SHORT
    assert "SHORT" in result.reason


def test_inference_can_be_turned_off():
    result = parse_signal(AVAX, infer_direction=False)
    assert not result.ok
    assert result.reason == "no BUY/LONG/SHORT direction found"


def test_ambiguous_numbers_are_never_inferred():
    # SL and both TPs on the same side of the entry: not a readable setup.
    result = parse_signal("#BTC\nKirish: 100\nTP: 110\nStop: 105")
    assert not result.ok
    assert result.signal is None


def test_russian_labels():
    result = parse_signal("#BTC\nВход: 100000\nЦель 1: 105000\nСтоп: 98000")
    assert result.ok
    assert result.signal.entry_low == Decimal("100000")
    assert result.signal.take_profits == [Decimal("105000")]
    assert result.signal.stop_loss == Decimal("98000")


def test_commentary_about_a_finished_trade_is_not_a_signal():
    result = parse_signal(
        "Bundan birinchi TPni oldik.\n\nYarmini sotib, stop 13.03 qo'yildi."
    )
    assert not result.ok
    assert result.signal is None


def test_coin_list_without_prices_is_not_tradable():
    result = parse_signal("Buy uchun\nBtc\nZec\nSahara\nTnsr\nShular yaxshi")
    assert not result.ok
    assert "missing" in result.reason
