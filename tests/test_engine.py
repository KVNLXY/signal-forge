"""End-to-end engine tests: every gate from the spec, plus paper TP/SL."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal


from app.database import repository as repo
from app.database.database import Database
from app.database.models import SignalStatus, TradeStatus, utcnow
from app.halal.whitelist import HalalWhitelist
from app.trading.engine import TradingEngine
from app.trading.paper import PaperExecutor
from tests.conftest import (
    BTC_LONG,
    BTC_NO_SL,
    BTC_SHORT,
    UNKNOWN_COIN,
    FakeMarket,
    make_settings,
)

DOGE_LONG = """DOGE LONG

Entry: 0.10-0.11
TP1: 0.13
SL: 0.09"""

SOL_LONG = """SOL LONG

Entry: 150-152
TP1: 160
SL: 145"""


async def open_trades(database: Database):
    async with database.session() as session:
        return await repo.open_trades(session)


async def all_trades(database: Database):
    async with database.session() as session:
        return await repo.all_trades(session)


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
async def test_valid_signal_is_accepted_and_waits(engine, market, notifier, database):
    market.set("BTCUSDT", 99000)                     # below the entry zone
    signal = await engine.handle_message(BTC_LONG, channel_identifier="@test", tg_message_id=1)

    assert signal is not None
    assert signal.status == SignalStatus.WAITING.value
    assert signal.symbol == "BTCUSDT"
    assert notifier.find("📡 SIGNAL")
    assert "Status: WAITING" in notifier.last
    assert await open_trades(database) == []


async def test_short_signal_is_ignored_without_noise(engine, notifier):
    signal = await engine.handle_message(BTC_SHORT, tg_message_id=2)
    assert signal.status == SignalStatus.SKIPPED.value
    assert "SHORT" in signal.reject_reason
    assert notifier.messages == []


async def test_non_halal_coin_is_rejected(engine, notifier):
    signal = await engine.handle_message(DOGE_LONG, tg_message_id=3)
    assert signal.status == SignalStatus.SKIPPED.value
    assert signal.reject_reason == "Coin is not in halal whitelist."
    assert notifier.find("SIGNAL SKIPPED")
    assert "halal whitelist" in notifier.last


async def test_unknown_coin_is_rejected(engine, notifier):
    signal = await engine.handle_message(UNKNOWN_COIN, tg_message_id=4)
    assert signal.symbol == "ABCUSDT"
    assert signal.status == SignalStatus.SKIPPED.value
    assert "halal whitelist" in signal.reject_reason


async def test_signal_without_sl_is_rejected(engine, notifier):
    signal = await engine.handle_message(BTC_NO_SL, tg_message_id=5)
    assert signal.status == SignalStatus.SKIPPED.value
    assert "SL" in signal.reject_reason
    assert notifier.find("SIGNAL SKIPPED")


async def test_expired_signal_is_rejected(engine, market, notifier):
    market.set("BTCUSDT", 99000)
    old = utcnow() - timedelta(minutes=30)
    signal = await engine.handle_message(BTC_LONG, tg_message_id=6, sent_at=old)
    assert signal.status == SignalStatus.SKIPPED.value
    assert "SIGNAL_EXPIRY_MINUTES" in signal.reject_reason


async def test_waiting_signal_expires_in_the_price_loop(engine, market, database, notifier):
    market.set("BTCUSDT", 99000)
    signal = await engine.handle_message(BTC_LONG, tg_message_id=7)
    assert signal.status == SignalStatus.WAITING.value

    async with database.session() as session:
        stored = (await repo.waiting_signals(session))[0]
        stored.expires_at = utcnow() - timedelta(seconds=1)

    await engine.tick()

    async with database.session() as session:
        assert await repo.waiting_signals(session) == []
    assert notifier.find("SIGNAL SKIPPED")


async def test_price_above_the_entry_zone_waits_for_the_pullback(engine, market, notifier, database):
    """"Kelsa": the entry sits below the live price - a resting buy order."""
    market.set("BTCUSDT", 105000)
    signal = await engine.handle_message(BTC_LONG, tg_message_id=8)

    assert signal.status == SignalStatus.WAITING.value
    assert signal.entry_mode == "limit"
    hours_left = (signal.expires_at - utcnow()).total_seconds() / 3600
    assert 23 < hours_left <= 24                       # LIMIT_ENTRY_EXPIRY_HOURS
    assert "pullback" in notifier.last
    assert await open_trades(database) == []

    market.set("BTCUSDT", 103000)                      # still above - keep waiting
    await engine.tick()
    assert await open_trades(database) == []

    market.set("BTCUSDT", 100800)                      # touched the zone from above
    await engine.tick()
    trades = await open_trades(database)
    assert len(trades) == 1
    assert trades[0].entry_price == Decimal("100800")


async def test_a_pullback_that_crashes_through_the_stop_cancels_the_order(engine, market, notifier, database):
    market.set("BTCUSDT", 105000)
    signal = await engine.handle_message(BTC_LONG, tg_message_id=80)
    assert signal.entry_mode == "limit"

    market.set("BTCUSDT", 97000)                       # below SL 98000 without ever filling
    await engine.tick()

    async with database.session() as session:
        assert await repo.waiting_signals(session) == []
    assert await open_trades(database) == []
    assert notifier.find("fell through the stop")


async def test_entry_above_zone_skip_restores_the_original_rule(tmp_path, notifier):
    settings = make_settings(
        tmp_path,
        entry_above_zone="skip",
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/skiprule.db",
    )
    database = Database(settings.database_url)
    await database.create_all()
    market = FakeMarket({"BTCUSDT": Decimal("105000")})
    whitelist = HalalWhitelist(quote_asset=settings.quote_asset)
    async with database.session() as session:
        await whitelist.sync_from_config(session, settings.halal_symbols)
    engine = TradingEngine(
        settings=settings, database=database, market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier, whitelist=whitelist,
    )
    try:
        signal = await engine.handle_message(BTC_LONG, tg_message_id=81)
        assert signal.status == SignalStatus.SKIPPED.value
        assert "above the entry zone" in signal.reject_reason
        async with database.session() as session:
            assert await repo.open_trades(session) == []
    finally:
        await database.close()


async def test_symbol_not_listed_on_mexc_is_skipped(engine, market, database, notifier):
    # SOL is halal-whitelisted but the exchange has no price for it here.
    signal = await engine.handle_message(SOL_LONG, tg_message_id=9)
    assert signal.status == SignalStatus.WAITING.value

    await engine.tick()
    async with database.session() as session:
        assert await repo.waiting_signals(session) == []
    assert notifier.find("not listed on MEXC")


# --------------------------------------------------------------------------- #
# entry / paper buy
# --------------------------------------------------------------------------- #
async def test_entry_inside_the_zone_opens_a_paper_trade(engine, market, notifier, database):
    market.set("BTCUSDT", 100500)
    signal = await engine.handle_message(BTC_LONG, tg_message_id=10)

    assert signal.status == SignalStatus.TRIGGERED.value
    trades = await open_trades(database)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.symbol == "BTCUSDT"
    assert trade.mode == "PAPER"
    assert trade.entry_price == Decimal("100500")
    assert trade.quote_spent == Decimal("100")
    assert trade.quantity == Decimal("100") / Decimal("100500")
    assert trade.remaining_qty == trade.quantity
    assert trade.sl_price == Decimal("98000")
    assert trade.take_profits == [Decimal("102000"), Decimal("104000")]
    assert trade.splits == [Decimal("0.3"), Decimal("0.7")]   # 30/30/40 with two TPs
    assert notifier.find("🟢 BUY")
    assert "Mode: PAPER" in notifier.last


async def test_entry_reached_later_in_the_price_loop(engine, market, database):
    market.set("BTCUSDT", 99000)
    await engine.handle_message(BTC_LONG, tg_message_id=11)
    assert await open_trades(database) == []

    market.set("BTCUSDT", 100200)
    await engine.tick()

    trades = await open_trades(database)
    assert len(trades) == 1
    assert trades[0].entry_price == Decimal("100200")


async def test_duplicate_trade_for_the_same_coin_is_skipped(engine, market, notifier, database):
    market.set("BTCUSDT", 100500)
    await engine.handle_message(BTC_LONG, tg_message_id=12)
    assert len(await open_trades(database)) == 1

    second = await engine.handle_message(BTC_LONG, tg_message_id=13)
    assert second.status == SignalStatus.SKIPPED.value
    assert "already open" in second.reject_reason
    assert len(await open_trades(database)) == 1


async def test_second_waiting_signal_for_the_same_coin_is_skipped(engine, market):
    market.set("BTCUSDT", 99000)
    await engine.handle_message(BTC_LONG, tg_message_id=14)
    second = await engine.handle_message(BTC_LONG, tg_message_id=15)
    assert second.status == SignalStatus.SKIPPED.value
    assert "already waiting" in second.reject_reason


async def test_same_telegram_message_is_only_handled_once(engine, market, database):
    market.set("BTCUSDT", 99000)
    first = await engine.handle_message(BTC_LONG, channel_identifier="@test", tg_message_id=16)
    again = await engine.handle_message(BTC_LONG, channel_identifier="@test", tg_message_id=16)
    assert first is not None
    assert again is None


# --------------------------------------------------------------------------- #
# TP / SL
# --------------------------------------------------------------------------- #
async def test_take_profits_close_the_position_in_two_steps(engine, market, notifier, database):
    market.set("BTCUSDT", 100500)
    await engine.handle_message(BTC_LONG, tg_message_id=17)
    quantity = (await open_trades(database))[0].quantity

    market.set("BTCUSDT", 102000)          # TP1
    await engine.tick()
    trade = (await open_trades(database))[0]
    assert trade.tp_hit == 1
    assert trade.remaining_qty == quantity * Decimal("0.7")
    assert notifier.find("PARTIAL SELL")
    assert notifier.find("Reason: TP1")
    assert trade.sl_price == trade.entry_price          # stop moved to the entry
    assert notifier.find("stop moved to the entry")

    market.set("BTCUSDT", 104000)          # TP2 -> fully closed
    await engine.tick()
    assert await open_trades(database) == []

    trade = (await all_trades(database))[0]
    assert trade.status == TradeStatus.CLOSED.value
    assert trade.close_reason == "TP2"
    assert trade.remaining_qty == 0
    assert trade.pnl_usdt > 0
    assert trade.pnl_usdt == quantity * Decimal("103400") - Decimal("100")
    assert notifier.find("🔴 SELL")
    assert "Reason: TP2" in notifier.last


async def test_stop_loss_closes_the_whole_position(engine, market, notifier, database):
    market.set("BTCUSDT", 100500)
    await engine.handle_message(BTC_LONG, tg_message_id=18)
    quantity = (await open_trades(database))[0].quantity

    market.set("BTCUSDT", 97000)
    await engine.tick()

    assert await open_trades(database) == []
    trade = (await all_trades(database))[0]
    assert trade.close_reason == "SL"
    assert trade.status == TradeStatus.CLOSED.value
    assert trade.remaining_qty == 0
    assert trade.pnl_usdt == quantity * Decimal("97000") - Decimal("100")
    assert trade.pnl_usdt < 0
    assert notifier.find("Reason: SL")


async def test_stop_loss_after_a_partial_take_profit(engine, market, database):
    market.set("BTCUSDT", 100500)
    await engine.handle_message(BTC_LONG, tg_message_id=19)
    quantity = (await open_trades(database))[0].quantity

    market.set("BTCUSDT", 102000)
    await engine.tick()
    market.set("BTCUSDT", 97900)
    await engine.tick()

    trade = (await all_trades(database))[0]
    assert trade.status == TradeStatus.CLOSED.value
    assert trade.close_reason == "SL"
    expected = (
        quantity * Decimal("0.3") * Decimal("102000")
        + quantity * Decimal("0.7") * Decimal("97900")
        - Decimal("100")
    )
    assert trade.pnl_usdt == expected


async def test_a_jump_past_both_targets_closes_in_one_tick(engine, market, database):
    market.set("BTCUSDT", 100500)
    await engine.handle_message(BTC_LONG, tg_message_id=20)

    market.set("BTCUSDT", 110000)
    await engine.tick()

    trade = (await all_trades(database))[0]
    assert trade.status == TradeStatus.CLOSED.value
    assert trade.close_reason == "TP2"
    assert trade.tp_hit == 2


# --------------------------------------------------------------------------- #
# risk
# --------------------------------------------------------------------------- #
async def test_max_open_trades_holds_the_next_signal(tmp_path, notifier):
    settings = make_settings(
        tmp_path,
        max_open_trades=1,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/capacity.db",
        halal_coins="BTCUSDT,ETHUSDT",
    )
    database = Database(settings.database_url)
    await database.create_all()
    market = FakeMarket({"BTCUSDT": Decimal("100500"), "ETHUSDT": Decimal("3500")})
    whitelist = HalalWhitelist(quote_asset=settings.quote_asset)
    async with database.session() as session:
        await whitelist.sync_from_config(session, settings.halal_symbols)
    engine = TradingEngine(
        settings=settings,
        database=database,
        market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier,
        whitelist=whitelist,
    )

    try:
        await engine.handle_message(BTC_LONG, tg_message_id=21)
        eth = await engine.handle_message(
            "ETH LONG\nEntry: 3400-3600\nTP1: 3800\nSL: 3300", tg_message_id=22
        )
        assert eth.status == SignalStatus.WAITING.value      # not traded, still waiting
        async with database.session() as session:
            assert len(await repo.open_trades(session)) == 1
        assert notifier.find("MAX_OPEN_TRADES")
    finally:
        await database.close()


async def test_statistics_after_a_winning_trade(engine, market, database):
    market.set("BTCUSDT", 100500)
    await engine.handle_message(BTC_LONG, tg_message_id=23)
    market.set("BTCUSDT", 110000)
    await engine.tick()

    text = await engine.stats_text()
    assert "Total trades: 1" in text
    assert "Winning trades: 1" in text
    assert "Losing trades: 0" in text
    assert "Win rate: 100.0%" in text
    assert "Mode: PAPER" in text


# --------------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------------- #
async def test_messages_buttons_and_ticks_never_deadlock_or_lock_the_db(engine, market, database):
    """The three writing paths share one lock; they must interleave cleanly."""
    import asyncio

    market.set("BTCUSDT", 99000)

    async def message(i: int):
        return await engine.handle_message(BTC_LONG, tg_message_id=100 + i)

    jobs = []
    for i in range(15):
        jobs.append(message(i))
        jobs.append(engine.tick())
        jobs.append(engine.handle_button("sig:999999:ok"))   # unknown signal, harmless
    results = await asyncio.wait_for(asyncio.gather(*jobs), timeout=30)

    accepted = [r for r in results if getattr(r, "status", None) == SignalStatus.WAITING.value]
    assert len(accepted) == 1                       # one waits, the rest are duplicates
    async with database.session() as session:
        assert len(await repo.waiting_signals(session)) == 1
