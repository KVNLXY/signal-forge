"""Parser tests on signal samples collected by other projects.

The files live in tests/fixtures/external_signals/ (see the README there for
the sources).  They are real channel posts written by people we have never
read, which is exactly what makes them worth more than shapes we invent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.telegram.parser import BUY, SHORT, parse_signal

FIXTURES = Path(__file__).parent / "fixtures" / "external_signals"


def _read(name: str) -> str:
    return (FIXTURES / "joostmbakker" / name).read_text(encoding="utf-8")


def _dec(values) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


# --------------------------------------------------------------------------- #
# joostmbakker/telegram-crypto-signal-parser - 8 posts, expectations are ours
# --------------------------------------------------------------------------- #

LONGS = {
    # "Get in : 725 - 735" is the entry; targets are dash-separated on one line.
    "signal_1.txt": ("ETHUSDT", "725", "735", "700", ["740", "750", "770", "790"]),
    # "LONG : 1.38" carries the entry; an empty "Target :" line precedes the
    # "TARGET 1 : 1.43$" list, whose indexes must not become prices.
    "signal_2.txt": ("CTKUSDT", "1.38", "1.38", "1.28", ["1.43", "1.49", "1.53", "1.58"]),
    # "Buy around 666-652" (reversed range), "Selling Targets - ...", "Stop at – 600".
    "signal_7.txt": ("BRDUSDT", "652", "666", "600", ["677", "690", "738", "850"]),
}


@pytest.mark.parametrize("name", sorted(LONGS))
def test_joostmbakker_long_is_fully_read(name):
    symbol, low, high, sl, tps = LONGS[name]
    result = parse_signal(_read(name))
    assert result.ok, result.reason
    signal = result.signal
    assert signal.symbol == symbol
    assert signal.direction == BUY
    assert (signal.entry_low, signal.entry_high) == (Decimal(low), Decimal(high))
    assert signal.stop_loss == Decimal(sl)
    assert signal.take_profits == _dec(tps)


SHORTS = {
    "signal_3.txt": "BTCUSDT",   # "#SIGNAL (BTCUSD)" - USD is our USDT market
    "signal_4.txt": "BTCUSDT",   # "#BTC #BTCUSD (Midterm SHORT)"
    "signal_5.txt": "BTCUSDT",   # "#BTC/USDT ... Trade Type: Regular (SHORT)"
    "signal_8.txt": "XBTUSDT",   # "Short #xbt at 6640-6700"
}


@pytest.mark.parametrize("name", sorted(SHORTS))
def test_joostmbakker_short_is_reported_and_never_traded(name):
    result = parse_signal(_read(name))
    assert not result.ok
    assert result.looks_like_signal
    assert result.signal.direction == SHORT
    assert result.signal.symbol == SHORTS[name]


def test_joostmbakker_btc_quoted_pair_is_rejected():
    # "ETH / BTC ... LONG : 0.027" is priced in BTC - not a market the bot
    # can buy with USDT, and 0.027 must never be read as an ETHUSDT entry.
    result = parse_signal(_read("signal_6.txt"))
    assert not result.ok
    assert result.signal is None
    assert result.looks_like_signal
    assert "ETH/BTC" in result.reason


# --------------------------------------------------------------------------- #
# vooi-app/vooi-signals-bot-example - 16 posts with the authors' expectations
# --------------------------------------------------------------------------- #

VOOI = json.loads((FIXTURES / "vooi" / "appendix_b_signals.json").read_text(encoding="utf-8"))
VOOI_BY_ID = {item["id"]: item for item in VOOI}

# Their bot trades futures, so a post without an SL is still a signal there.
# Here it is the NO SL -> NO TRADE rule; the post is recognised, then refused.
NO_SL = {6}


@pytest.mark.parametrize("item", VOOI, ids=[f"vooi-{i['id']}" for i in VOOI])
def test_vooi_expectations_mapped_onto_spot_rules(item):
    expected = item["expected"]
    result = parse_signal(item["raw_text"])

    if not expected["is_signal"]:
        assert not result.ok
        return

    signal = result.signal
    assert signal is not None, result.reason
    assert signal.symbol == expected["symbol"] + "USDT"

    if expected["side"] == "sell":
        assert not result.ok
        assert signal.direction == SHORT
        return

    assert signal.direction == BUY
    if item["id"] in NO_SL:
        assert not result.ok
        assert "SL" in result.reason
        return
    assert result.ok, result.reason
    entries = expected["entry_prices"]
    assert signal.entry_low == Decimal(str(min(entries)))
    assert signal.entry_high == Decimal(str(max(entries)))
    assert signal.stop_loss == Decimal(str(expected["stop_loss"]))
    assert signal.take_profits == _dec(expected["take_profits"])


def test_vooi_field_labels_are_not_tickers():
    # "Pair: AVAX-PERP" / "Direction: LONG" once produced DIRECTIONUSDT.
    assert parse_signal(VOOI_BY_ID[4]["raw_text"]).signal.symbol == "AVAXUSDT"
    # "NEAR PROTOCOL LONG" once produced PROTOCOLUSDT.
    assert parse_signal(VOOI_BY_ID[16]["raw_text"]).signal.symbol == "NEARUSDT"
    # "URGENT SHORT SIGNAL" once produced URGENTUSDT.
    assert parse_signal(VOOI_BY_ID[15]["raw_text"]).signal.symbol == "XRPUSDT"


# --------------------------------------------------------------------------- #
# Shapes the samples exposed, reduced to the smallest post that shows each
# --------------------------------------------------------------------------- #

def test_two_digit_decimal_after_tp_is_a_price_not_an_index():
    result = parse_signal("SOL LONG\nEntry 12.5\nTP 14.5\nSL 11.5")
    assert result.ok, result.reason
    assert result.signal.take_profits == [Decimal("14.5")]


def test_numbered_entry_legs_form_one_zone():
    # A DCA ladder is bought as a zone; the index digit is never the price.
    result = parse_signal("TON LONG\nEntry 1: 5.2\nEntry 2: 5.0\nEntry 3: 4.8\nTP: 6\nSL: 4.5")
    assert result.ok, result.reason
    assert (result.signal.entry_low, result.signal.entry_high) == (Decimal("4.8"), Decimal("5.2"))

    # vooi #6 shape: a plain "Entry:" followed by "Entry 2:".
    result = parse_signal("DOGE LONG\nEntry: 0.1420\nEntry 2: 0.1380\nTP1: 0.16\nSL: 0.13")
    assert result.ok, result.reason
    assert (result.signal.entry_low, result.signal.entry_high) == (Decimal("0.1380"), Decimal("0.1420"))


def test_only_numbered_legs_widen_the_zone():
    # A later, unnumbered entry-like label never stretches the zone.
    result = parse_signal("BTC LONG\nEntry: 100000\nOpen: 12:00\nTP: 105000\nSL: 98000")
    assert result.ok, result.reason
    assert (result.signal.entry_low, result.signal.entry_high) == (Decimal("100000"), Decimal("100000"))


def test_target_list_on_the_following_lines_is_read_in_full():
    # Cornix layout: numbered lines with the share sold at each target.
    result = parse_signal(
        "BTC LONG\nEntry Zone:\n100000 - 101000\nTake-Profit Targets:\n"
        "1) 102000 - 20%\n2) 104000 - 30%\n3) 106000 - 50%\nStop Targets:\n1) 98000 - 100%"
    )
    assert result.ok, result.reason
    assert result.signal.take_profits == [Decimal("102000"), Decimal("104000"), Decimal("106000")]
    assert result.signal.stop_loss == Decimal("98000")

    # Keycaps and bullets count as list markers too.
    result = parse_signal("#DOGE LONG\nEntry: 0.14\nTargets:\n1️⃣ 0.16\n2️⃣ 0.18\n3️⃣ $0.20\nSL: 0.13")
    assert result.ok, result.reason
    assert result.signal.take_profits == [Decimal("0.16"), Decimal("0.18"), Decimal("0.20")]

    result = parse_signal("SOL LONG\nEntry: 150\nTP:\n- 160\n- 170\n• 180\nSL: 140")
    assert result.ok, result.reason
    assert result.signal.take_profits == [Decimal("160"), Decimal("170"), Decimal("180")]


def test_target_list_stops_at_the_first_line_with_words():
    result = parse_signal("SOL LONG\nEntry: 150\nTP: 160\n2. 10x leverage max\n3) 170\nSL: 140")
    assert result.ok, result.reason
    assert result.signal.take_profits == [Decimal("160")]

    result = parse_signal("ETH LONG\nEntry: 3000\nTargets: 3100 / 3200\n1) do not use leverage\nSL: 2900")
    assert result.ok, result.reason
    assert result.signal.take_profits == [Decimal("3100"), Decimal("3200")]


def test_blank_line_ends_an_empty_label():
    # The "1" of "TARGET 1" on the line after the blank line is not a target.
    result = parse_signal("CTK/USDT\nLONG : 1.38\nTarget :\n\nTARGET 1 : 1.43$\nSTOP : 1.28$")
    assert result.ok, result.reason
    assert result.signal.take_profits == [Decimal("1.43")]


def test_value_on_the_next_line_still_belongs_to_the_label():
    result = parse_signal("BTC LONG\nEntry Zone:\n100000 - 101000\nTP:\n104000\nSL:\n98000")
    assert result.ok, result.reason
    assert (result.signal.entry_low, result.signal.entry_high) == (Decimal("100000"), Decimal("101000"))
    assert result.signal.take_profits == [Decimal("104000")]
    assert result.signal.stop_loss == Decimal("98000")


def test_long_with_leverage_is_not_an_entry():
    result = parse_signal("BTC LONG 10x\nEntry: 100000\nTP: 105000\nSL: 98000")
    assert result.ok, result.reason
    assert result.signal.entry_low == Decimal("100000")


def test_bare_zone_is_an_entry_but_tp_zone_is_not():
    result = parse_signal("ARB LONG\nZone: 1.050 - 1.080\nTP: 1.250\nSL: 0.980")
    assert result.ok, result.reason
    assert (result.signal.entry_low, result.signal.entry_high) == (Decimal("1.050"), Decimal("1.080"))

    result = parse_signal("ARB LONG\nEntry: 1.05\nTP Zone: 1.25\nSL: 0.98")
    assert result.ok, result.reason
    assert result.signal.entry_low == Decimal("1.05")
    assert result.signal.take_profits == [Decimal("1.25")]


def test_dollar_stable_quotes_trade_on_the_bots_pair():
    for pair in ("BTC/USD", "BTC/USDC", "BTCUSD", "BTC-BUSD"):
        result = parse_signal(f"{pair} LONG\nEntry: 100000\nTP: 105000\nSL: 98000")
        assert result.ok, (pair, result.reason)
        assert result.signal.symbol == "BTCUSDT", pair


def test_cross_pair_mentioned_in_passing_does_not_hide_the_hashtag():
    result = parse_signal("#SOL LONG\nEntry: 150\nTP: 170\nSL: 140\nETH/BTC ratio looks weak")
    assert result.ok, result.reason
    assert result.signal.symbol == "SOLUSDT"
