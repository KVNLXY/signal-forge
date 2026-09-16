"""Circuit breakers: the daily loss cap, the loss streak, the spread gate
and the admin's /pause.  Each one only ever blocks a new BUY - an open
position is still closed at TP / SL while buying is halted."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.database import repository as repo
from app.database.database import Database
from app.database.models import SignalStatus, TradeStatus, utcnow
from app.halal.whitelist import HalalWhitelist
from app.trading.engine import TradingEngine
from app.trading.paper import PaperExecutor
from tests.conftest import BTC_LONG, FakeMarket, RecordingNotifier, make_settings

ETH_LONG = "ETH LONG\nEntry: 3400-3600\nTP1: 3800\nSL: 3300"
SOL_LONG = "SOL LONG\nEntry: 150-152\nTP1: 160\nSL: 145"


async def build(tmp_path, **overrides):
    settings = make_settings(
        tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/breakers.db",
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
        settings=settings,
        database=database,
        market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier,
        whitelist=whitelist,
    )
    return engine, market, notifier, database


async def open_trades(database: Database):
    async with database.session() as session:
        return await repo.open_trades(session)


async def lose(engine, market, database, text, symbol, entry, stop, message_id):
    """Open a paper position and stop it out at once."""
    market.set(symbol, entry)
    await engine.handle_message(text, tg_message_id=message_id)
    assert any(t.symbol == symbol for t in await open_trades(database))
    market.set(symbol, stop)
    await engine.tick()
    assert not any(t.symbol == symbol for t in await open_trades(database))


# --------------------------------------------------------------------------- #
# daily loss cap
# --------------------------------------------------------------------------- #
async def test_daily_loss_cap_halts_new_buys_until_midnight(tmp_path):
    engine, market, notifier, database = await build(tmp_path, max_daily_loss_usdt=Decimal("5"))
    try:
        # 100 USDT at 100500, stopped at 97000 -> about -3.5 USDT: under the cap.
        await lose(engine, market, database, BTC_LONG, "BTCUSDT", 100500, 97000, 1)
        eth = await engine.handle_message(ETH_LONG, tg_message_id=2)
        assert eth.status == SignalStatus.TRIGGERED.value
        market.set("ETHUSDT", 3200)                      # -8.6 USDT -> over the cap
        await engine.tick()
        assert await open_trades(database) == []

        sol = await engine.handle_message(SOL_LONG, tg_message_id=3)
        assert sol.status == SignalStatus.SKIPPED.value
        assert "MAX_DAILY_LOSS_USDT" in sol.reject_reason
        assert notifier.find("TRADING HALTED")
        assert len(notifier.find("TRADING HALTED")) == 1

        # The same halt again is not announced twice.
        await engine.handle_message(SOL_LONG, tg_message_id=4)
        assert len(notifier.find("TRADING HALTED")) == 1

        # A loss from yesterday does not count.
        async with database.session() as session:
            for trade in await repo.closed_trades(session):
                trade.closed_at = utcnow() - timedelta(days=1)
        sol = await engine.handle_message(SOL_LONG, tg_message_id=5)
        assert sol.status == SignalStatus.TRIGGERED.value
    finally:
        await database.close()


async def test_a_waiting_signal_meets_the_daily_cap_at_its_entry(tmp_path):
    engine, market, notifier, database = await build(tmp_path, max_daily_loss_usdt=Decimal("1"))
    try:
        market.set("SOLUSDT", 140)                       # below the zone: waits
        sol = await engine.handle_message(SOL_LONG, tg_message_id=1)
        assert sol.status == SignalStatus.WAITING.value

        await lose(engine, market, database, BTC_LONG, "BTCUSDT", 100500, 97000, 2)

        market.set("SOLUSDT", 151)                       # entry reached, but halted
        await engine.tick()
        assert await open_trades(database) == []
        async with database.session() as session:
            sol = await repo.get_signal(session, sol.id)
        assert sol.status == SignalStatus.SKIPPED.value
        assert "MAX_DAILY_LOSS_USDT" in sol.reject_reason
    finally:
        await database.close()


# --------------------------------------------------------------------------- #
# loss streak
# --------------------------------------------------------------------------- #
async def test_loss_streak_pauses_buying_until_resume(tmp_path):
    engine, market, notifier, database = await build(tmp_path, max_consecutive_losses=2)
    try:
        await lose(engine, market, database, BTC_LONG, "BTCUSDT", 100500, 97000, 1)
        await lose(engine, market, database, ETH_LONG, "ETHUSDT", 3500, 3200, 2)

        sol = await engine.handle_message(SOL_LONG, tg_message_id=3)
        assert sol.status == SignalStatus.SKIPPED.value
        assert "2 losing trades in a row" in sol.reject_reason
        assert notifier.find("TRADING HALTED")

        answer = await engine.resume_text()
        assert answer.startswith("▶️ Resumed")
        sol = await engine.handle_message(SOL_LONG, tg_message_id=4)
        assert sol.status == SignalStatus.TRIGGERED.value
    finally:
        await database.close()


async def test_a_winning_trade_resets_the_streak(tmp_path):
    engine, market, notifier, database = await build(tmp_path, max_consecutive_losses=2)
    try:
        await lose(engine, market, database, BTC_LONG, "BTCUSDT", 100500, 97000, 1)
        market.set("ETHUSDT", 3500)
        await engine.handle_message(ETH_LONG, tg_message_id=2)
        market.set("ETHUSDT", 3800)                      # TP -> a win
        await engine.tick()
        await lose(engine, market, database, SOL_LONG, "SOLUSDT", 151, 145, 3)

        # loss, win, loss -> streak of 1, still trading
        market.set("BTCUSDT", 100500)
        btc = await engine.handle_message(BTC_LONG, tg_message_id=4)
        assert btc.status == SignalStatus.TRIGGERED.value
    finally:
        await database.close()


async def test_pause_and_resume_commands(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        assert "Paused" in await engine.pause_text()
        btc = await engine.handle_message(BTC_LONG, tg_message_id=1)
        assert btc.status == SignalStatus.SKIPPED.value
        assert "paused by the admin" in btc.reject_reason
        assert "HALTED" in await engine.status_text()

        assert "Resumed" in await engine.resume_text()
        btc = await engine.handle_message(BTC_LONG, tg_message_id=2)
        assert btc.status == SignalStatus.TRIGGERED.value
    finally:
        await database.close()


async def test_a_halt_never_stops_an_exit(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, tg_message_id=1)
        await engine.pause_text()

        market.set("BTCUSDT", 104000)                    # both targets
        await engine.tick()
        assert await open_trades(database) == []
        async with database.session() as session:
            trade = (await repo.closed_trades(session))[0]
        assert trade.status == TradeStatus.CLOSED.value
        assert trade.close_reason == "TP2"
    finally:
        await database.close()


# --------------------------------------------------------------------------- #
# spread gate
# --------------------------------------------------------------------------- #
async def test_wide_spread_holds_the_entry_until_the_book_tightens(tmp_path):
    engine, market, notifier, database = await build(
        tmp_path, max_entry_spread_percent=Decimal("0.5")
    )
    try:
        market.set("BTCUSDT", 100500)
        market.set_spread("BTCUSDT", "2")                # 2% wide
        btc = await engine.handle_message(BTC_LONG, tg_message_id=1)
        assert btc.status == SignalStatus.WAITING.value
        assert await open_trades(database) == []
        assert notifier.find("MAX_ENTRY_SPREAD_PERCENT")
        assert "2.00%" in notifier.find("MAX_ENTRY_SPREAD_PERCENT")[0]

        await engine.tick()                              # warned once, not again
        assert len(notifier.find("MAX_ENTRY_SPREAD_PERCENT")) == 1

        market.set_spread("BTCUSDT", "0.1")
        await engine.tick()
        assert len(await open_trades(database)) == 1
    finally:
        await database.close()


async def test_unreadable_book_postpones_the_buy(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        market.set("BTCUSDT", 100500)
        market.set_spread("BTCUSDT", None)
        btc = await engine.handle_message(BTC_LONG, tg_message_id=1)
        assert btc.status == SignalStatus.WAITING.value
        assert await open_trades(database) == []

        market.set_spread("BTCUSDT", "0")
        await engine.tick()
        assert len(await open_trades(database)) == 1
    finally:
        await database.close()


async def test_spread_gate_off_buys_into_any_book(tmp_path):
    engine, market, notifier, database = await build(tmp_path, max_entry_spread_percent=Decimal("0"))
    try:
        market.set("BTCUSDT", 100500)
        market.set_spread("BTCUSDT", None)               # never even asked
        btc = await engine.handle_message(BTC_LONG, tg_message_id=1)
        assert btc.status == SignalStatus.TRIGGERED.value
    finally:
        await database.close()


# --------------------------------------------------------------------------- #
# /close
# --------------------------------------------------------------------------- #
async def test_close_command_sells_the_position_and_cancels_the_waiting_signal(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        assert "Usage" in await engine.close_text("")
        assert "No open position" in await engine.close_text("btc")

        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, tg_message_id=1)
        market.set("SOLUSDT", 140)                       # waits below the zone
        await engine.handle_message(SOL_LONG, tg_message_id=2)

        market.set("BTCUSDT", 101000)
        answer = await engine.close_text("btc/usdt")
        assert "BTCUSDT closed at 101000" in answer
        assert await open_trades(database) == []
        async with database.session() as session:
            trade = (await repo.closed_trades(session))[0]
        assert trade.close_reason == "MANUAL"
        assert trade.pnl_usdt > 0
        assert notifier.find("Reason: MANUAL")

        answer = await engine.close_text("all")
        assert "SOLUSDT signal cancelled" in answer
        async with database.session() as session:
            assert await repo.active_signals(session) == []
        assert "Nothing to close" in await engine.close_text("all")
    finally:
        await database.close()


async def test_admin_commands_pass_the_argument_only_to_handlers_that_take_one():
    from app.notifications.telegram import AdminCommands

    seen: list[str] = []

    async def bare() -> str:
        seen.append("bare")
        return "ok"

    async def with_arg(argument: str) -> str:
        seen.append(argument)
        return "ok"

    admin = AdminCommands("token", "1", handlers={"status": bare, "close": with_arg})
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    admin._reply = reply                                 # no Telegram in tests
    await admin._dispatch("/status")
    await admin._dispatch("/close BTC USDT")
    await admin._dispatch("/close@signalforge_bot eth")
    await admin.close()
    assert seen == ["bare", "BTC USDT", "eth"]
    assert replies == ["ok", "ok", "ok"]


# --------------------------------------------------------------------------- #
# per-channel scoreboard
# --------------------------------------------------------------------------- #
async def test_stats_are_broken_down_by_channel(tmp_path):
    engine, market, notifier, database = await build(tmp_path)
    try:
        # @alpha: one win.  @beta: one loss, a rejected short and a skipped signal.
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, channel_identifier="@alpha", tg_message_id=1)
        market.set("BTCUSDT", 104000)
        await engine.tick()
        market.set("ETHUSDT", 3500)
        await engine.handle_message(ETH_LONG, channel_identifier="@beta", tg_message_id=3)
        market.set("ETHUSDT", 3200)
        await engine.tick()
        await engine.handle_message("SOL SHORT\nEntry 150\nTP 140\nSL 155",
                                    channel_identifier="@beta", tg_message_id=4)
        await engine.handle_message("SOL LONG\nEntry 150\nTP 160",
                                    channel_identifier="@beta", tg_message_id=5)   # no SL

        text = await engine.stats_text()
        assert "BY CHANNEL" in text
        alpha = next(line for line in text.splitlines() if line.startswith("@alpha"))
        beta = next(line for line in text.splitlines() if line.startswith("@beta"))
        assert "1 signals, 1 traded, 1W/0L (100%), +$" in alpha
        assert "3 signals, 1 traded, 0W/1L (0%), $-" in beta
        assert text.index("@alpha") < text.index("@beta")           # best PNL first

        async with database.session() as session:
            rows = {r.channel: r for r in await repo.channel_stats(session)}
            closed = await repo.closed_trades(session)
        assert rows["@alpha"].accepted == 1 and rows["@beta"].accepted == 1
        assert rows["@alpha"].pnl + rows["@beta"].pnl == sum(
            (t.pnl_usdt for t in closed), Decimal(0)
        )
    finally:
        await database.close()
