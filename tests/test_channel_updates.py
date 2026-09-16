"""Follow-up posts about a running trade: read, tied to the position, and
acted on only after the admin's button (or at once with confirmation off)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.database import repository as repo
from app.database.database import Database
from app.database.models import SignalStatus, TradeStatus, UpdateStatus, utcnow
from app.halal.whitelist import HalalWhitelist
from app.telegram.updates import CANCEL, CLOSE, STOP, parse_update
from app.trading.engine import TradingEngine
from app.trading.paper import PaperExecutor
from tests.conftest import BTC_LONG, FakeMarket, RecordingNotifier, make_settings

ETH_LONG = "ETH LONG\nEntry: 3400-3600\nTP1: 3800\nSL: 3300"
SOL_LONG = "SOL LONG\nEntry: 150-152\nTP1: 160\nSL: 145"


# --------------------------------------------------------------------------- #
# the reader
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, action, detail", [
    ("TP1 urdi, 50% yopamiz", CLOSE, 50),
    ("Yopamiz ✅", CLOSE, 100),
    ("#SOL yopildi +8%", CLOSE, 100),               # +8% is the result, not a share
    ("Закрываем половину", CLOSE, 50),
    ("book profits 25%", CLOSE, 25),
    ("Stopni olgan joyga ko'taring", STOP, "entry"),
    ("12%✅ stop kotarib kutamiz", STOP, "entry"),
    ("SL to breakeven", STOP, "entry"),
    ("Стоп в БУ", STOP, "entry"),
    ("SL -> 0.118", STOP, Decimal("0.118")),
    ("Стоп переносим на 3400", STOP, Decimal("3400")),
    ("Bekor, kirmaymiz", CANCEL, None),
    ("Отмена сигнала", CANCEL, None),
])
def test_follow_up_posts_are_read(text, action, detail):
    update = parse_update(text)
    assert update is not None and update.action == action
    if action == CLOSE:
        assert update.percent == detail
    elif detail == "entry":
        assert update.to_entry
    elif action == STOP:
        assert update.price == detail


@pytest.mark.parametrize("text", ["good morning", "TP1 hit 12% ✅", "BTC looks strong", ""])
def test_chatter_is_not_an_update(text):
    assert parse_update(text) is None


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
async def build(tmp_path, name: str = "updates", **overrides):
    settings = make_settings(
        tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/{name}.db",
        halal_coins="BTCUSDT,ETHUSDT,SOLUSDT",
        max_open_trades=5,
        **overrides,
    )
    database = Database(settings.database_url)
    await database.create_all()
    market = FakeMarket({
        "BTCUSDT": Decimal("100500"), "ETHUSDT": Decimal("3500"), "SOLUSDT": Decimal("151"),
    })
    whitelist = HalalWhitelist(quote_asset=settings.quote_asset)
    async with database.session() as session:
        await whitelist.sync_from_config(session, settings.halal_symbols)
    notifier = RecordingNotifier()
    engine = TradingEngine(
        settings=settings, database=database, market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier, whitelist=whitelist,
    )
    return engine, market, notifier, database


async def open_trades(database: Database):
    async with database.session() as session:
        return await repo.open_trades(session)


async def pending(database: Database):
    async with database.session() as session:
        return await repo.pending_updates(session)


async def test_named_coin_close_waits_for_the_button_then_sells(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, channel_identifier="@alpha", tg_message_id=1)
        market.set("BTCUSDT", 101500)

        await engine.handle_message("#BTC TP1 yaqin, 50% yopamiz",
                                    channel_identifier="@alpha", tg_message_id=2)
        rows = await pending(database)
        assert len(rows) == 1 and rows[0].action == CLOSE and rows[0].percent == 50
        assert len(await open_trades(database)) == 1                # nothing sold yet
        assert notifier.find("CHANNEL UPDATE")
        assert "sell 50% of what is left" in notifier.last
        assert "Live price: 101500" in notifier.last
        assert notifier.callback_data[-2:] == [f"upd:{rows[0].id}:ok", f"upd:{rows[0].id}:no"]

        answer = await engine.handle_button(f"upd:{rows[0].id}:ok")
        assert "50% sold at 101500" in answer and "50% left" in answer
        trade = (await open_trades(database))[0]
        assert trade.remaining_qty == trade.quantity / 2
        assert trade.exits[-1]["reason"] == "CHANNEL"
        assert notifier.find("PARTIAL SELL")
        assert "already applied" in await engine.handle_button(f"upd:{rows[0].id}:ok")

        await engine.handle_message("Yopamiz ✅ #BTC", channel_identifier="@alpha", tg_message_id=3)
        row = (await pending(database))[0]
        answer = await engine.handle_button(f"upd:{row.id}:ok")
        assert "BTCUSDT closed at 101500" in answer
        assert await open_trades(database) == []
        async with database.session() as session:
            closed = (await repo.closed_trades(session))[0]
        assert closed.close_reason == "CHANNEL"
    finally:
        await database.close()


async def test_reply_and_only_position_resolve_the_coin(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, channel_identifier="@alpha", tg_message_id=10)
        market.set("ETHUSDT", 3500)
        await engine.handle_message(ETH_LONG, channel_identifier="@beta", tg_message_id=20)

        # @alpha has one position: an unnamed post is about it.
        await engine.handle_message("Stopni olgan joyga ko'taring",
                                    channel_identifier="@alpha", tg_message_id=11)
        row = (await pending(database))[0]
        assert row.symbol == "BTCUSDT" and row.action == STOP and row.to_entry

        # A reply to the ETH post is about ETH, whatever else is open.
        market.set("SOLUSDT", 151)
        await engine.handle_message(SOL_LONG, channel_identifier="@beta", tg_message_id=21)
        await engine.handle_message("SL -> 3450", channel_identifier="@beta",
                                    tg_message_id=22, reply_to_message_id=20)
        rows = await pending(database)
        assert [r.symbol for r in rows] == ["BTCUSDT", "ETHUSDT"]
        assert rows[1].price == Decimal("3450")

        # Two positions, no name, no reply: nobody is guessed.
        await engine.handle_message("Yopamiz", channel_identifier="@beta", tg_message_id=23)
        assert len(await pending(database)) == 2

        # Someone else's channel with nothing open: not ours.
        await engine.handle_message("Yopamiz", channel_identifier="@gamma", tg_message_id=30)
        assert len(await pending(database)) == 2

        # A coin we do not hold is named: never re-routed to the channel's
        # only position (@alpha holds BTC, the post is about DOGE).
        await engine.handle_message("#DOGE yopamiz", channel_identifier="@alpha", tg_message_id=31)
        assert len(await pending(database)) == 2
    finally:
        await database.close()


async def test_stop_moves_up_only_and_to_entry_means_breakeven(tmp_path):
    engine, market, notifier, database = await build(tmp_path, move_sl_to_entry_after_tp1=False)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, channel_identifier="@alpha", tg_message_id=1)

        await engine.handle_message("stop to BE", channel_identifier="@alpha", tg_message_id=2)
        row = (await pending(database))[0]
        assert "🛡 BTCUSDT SL 98000 -> 100500" in await engine.handle_button(f"upd:{row.id}:ok")
        assert (await open_trades(database))[0].sl_price == Decimal("100500")

        # Widening is refused - the warning is shown before, the refusal after.
        await engine.handle_message("SL -> 97000", channel_identifier="@alpha", tg_message_id=3)
        assert "never widened" in notifier.last
        row = (await pending(database))[0]
        answer = await engine.handle_button(f"upd:{row.id}:ok")
        assert "never widened" in answer
        assert (await open_trades(database))[0].sl_price == Decimal("100500")
        async with database.session() as session:
            assert (await repo.get_update(session, row.id)).status == UpdateStatus.FAILED.value

        # A stop above the live price sells on the next tick, and says so.
        market.set("BTCUSDT", 101000)
        await engine.handle_message("SL -> 101500", channel_identifier="@alpha", tg_message_id=4)
        assert "already at or under" in notifier.last
        row = (await pending(database))[0]
        assert "sells on the next tick" in await engine.handle_button(f"upd:{row.id}:ok")
        await engine.tick()
        assert await open_trades(database) == []
    finally:
        await database.close()


async def test_cancel_and_close_drop_a_waiting_signal(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        market.set("SOLUSDT", 140)                                   # below the zone
        sol = await engine.handle_message(SOL_LONG, channel_identifier="@alpha", tg_message_id=1)
        assert sol.status == SignalStatus.WAITING.value

        await engine.handle_message("#SOL bekor", channel_identifier="@alpha", tg_message_id=2)
        row = (await pending(database))[0]
        assert row.action == CANCEL
        assert "signal cancelled" in await engine.handle_button(f"upd:{row.id}:ok")
        async with database.session() as session:
            assert (await repo.get_signal(session, sol.id)).status == SignalStatus.SKIPPED.value

        # "close" for a signal that never entered is a cancel too.
        sol = await engine.handle_message(SOL_LONG, channel_identifier="@alpha", tg_message_id=3)
        await engine.handle_message("Yopamiz", channel_identifier="@alpha", tg_message_id=4)
        row = (await pending(database))[0]
        assert row.action == CANCEL and row.signal_id == sol.id

        # Ignore keeps everything as it was.
        assert "ignored" in await engine.handle_button(f"upd:{row.id}:no")
        async with database.session() as session:
            assert (await repo.get_signal(session, sol.id)).status == SignalStatus.WAITING.value
    finally:
        await database.close()


async def test_unconfirmed_update_expires_and_confirmation_can_be_switched_off(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, channel_identifier="@alpha", tg_message_id=1)
        await engine.handle_message("Yopamiz", channel_identifier="@alpha", tg_message_id=2)
        row = (await pending(database))[0]
        async with database.session() as session:
            (await repo.get_update(session, row.id)).expires_at = utcnow() - timedelta(minutes=1)
        await engine.tick()
        assert await pending(database) == []
        assert notifier.find("expired unconfirmed")
        assert len(await open_trades(database)) == 1
        assert "expired" in await engine.handle_button(f"upd:{row.id}:ok")
    finally:
        await database.close()

    engine, market, notifier, database = await build(tmp_path, "auto", update_confirmation=False)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, channel_identifier="@alpha", tg_message_id=1)
        market.set("BTCUSDT", 101000)
        await engine.handle_message("Yopamiz", channel_identifier="@alpha", tg_message_id=2)
        assert await open_trades(database) == []
        assert notifier.find("update applied")
        async with database.session() as session:
            trade = (await repo.closed_trades(session))[0]
            assert trade.status == TradeStatus.CLOSED.value
            assert (await repo.pending_updates(session)) == []
    finally:
        await database.close()


async def test_a_real_signal_is_never_mistaken_for_an_update(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, channel_identifier="@alpha", tg_message_id=1)
        # A second, broken signal with an SL line is a signal-shaped post,
        # not an instruction to move the stop.
        await engine.handle_message("BTC LONG\nTP: 105000\nSL: 99000",
                                    channel_identifier="@alpha", tg_message_id=2)
        assert await pending(database) == []
        assert (await open_trades(database))[0].sl_price == Decimal("98000")
        # And the option can be turned off altogether.
    finally:
        await database.close()

    engine, market, notifier, database = await build(tmp_path, "off", channel_updates=False)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, channel_identifier="@alpha", tg_message_id=1)
        await engine.handle_message("Yopamiz", channel_identifier="@alpha", tg_message_id=2)
        assert await pending(database) == []
    finally:
        await database.close()
