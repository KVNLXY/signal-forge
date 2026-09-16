"""Hold-time limit, reconciliation with the account, message pruning."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select

from app.database import repository as repo
from app.database.database import Database
from app.database.models import Message, utcnow
from app.halal.whitelist import HalalWhitelist
from app.trading.engine import TradingEngine
from app.trading.paper import PaperExecutor
from tests.conftest import BTC_LONG, FakeMarket, RecordingNotifier, make_settings


class AccountedPaper(PaperExecutor):
    """Paper fills, plus an account balance the test can set - the LIVE shape."""

    def __init__(self, market, fee_rate) -> None:
        super().__init__(market, fee_rate)
        self.balances: dict[str, Optional[Decimal]] = {}

    async def holdings(self, symbol: str) -> Optional[Decimal]:
        return self.balances.get(symbol.upper())


async def build(tmp_path, **overrides):
    settings = make_settings(
        tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/housekeeping.db",
        **overrides,
    )
    database = Database(settings.database_url)
    await database.create_all()
    market = FakeMarket({"BTCUSDT": Decimal("100500"), "ETHUSDT": Decimal("3500")})
    whitelist = HalalWhitelist(quote_asset=settings.quote_asset)
    async with database.session() as session:
        await whitelist.sync_from_config(session, settings.halal_symbols)
    notifier = RecordingNotifier()
    executor = AccountedPaper(market, settings.paper_fee_rate)
    engine = TradingEngine(
        settings=settings, database=database, market=market, executor=executor,
        notifier=notifier, whitelist=whitelist,
    )
    return engine, market, notifier, database, executor


async def open_trades(database: Database):
    async with database.session() as session:
        return await repo.open_trades(session)


async def age_trade(database: Database, hours: int) -> None:
    async with database.session() as session:
        for trade in await repo.open_trades(session):
            trade.opened_at = utcnow() - timedelta(hours=hours)


# --------------------------------------------------------------------------- #
# MAX_HOLD_HOURS
# --------------------------------------------------------------------------- #
async def test_hold_limit_warns_once_and_leaves_the_position(tmp_path):
    engine, market, notifier, database, _ = await build(tmp_path, max_hold_hours=24)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, tg_message_id=1)
        market.set("BTCUSDT", 101000)                    # between SL and TP1
        await engine.tick()
        assert not notifier.find("MAX_HOLD_HOURS")       # too young

        await age_trade(database, 25)
        await engine.tick()
        await engine.tick()
        warnings = notifier.find("MAX_HOLD_HOURS")
        assert len(warnings) == 1
        assert "open for 25h" in warnings[0] and "/close BTCUSDT" in warnings[0]
        assert len(await open_trades(database)) == 1
    finally:
        await database.close()


async def test_hold_limit_can_close_at_market(tmp_path):
    engine, market, notifier, database, _ = await build(
        tmp_path, max_hold_hours=24, max_hold_action="close"
    )
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, tg_message_id=1)
        await age_trade(database, 30)
        market.set("BTCUSDT", 101000)
        await engine.tick()

        assert await open_trades(database) == []
        async with database.session() as session:
            trade = (await repo.closed_trades(session))[0]
        assert trade.close_reason == "TIMEOUT"
        assert trade.pnl_usdt > 0
        assert notifier.find("Reason: TIMEOUT")
    finally:
        await database.close()


async def test_hold_limit_never_fires_before_tp_and_sl_are_checked(tmp_path):
    engine, market, notifier, database, _ = await build(
        tmp_path, max_hold_hours=1, max_hold_action="close"
    )
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, tg_message_id=1)
        await age_trade(database, 5)
        market.set("BTCUSDT", 104000)                    # both targets in one tick
        await engine.tick()
        async with database.session() as session:
            trade = (await repo.closed_trades(session))[0]
        assert trade.close_reason == "TP2"               # not TIMEOUT
    finally:
        await database.close()


# --------------------------------------------------------------------------- #
# reconciliation
# --------------------------------------------------------------------------- #
async def test_reconcile_tracks_a_partial_balance_and_closes_a_vanished_one(tmp_path):
    engine, market, notifier, database, executor = await build(tmp_path)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, tg_message_id=1)
        market.set("ETHUSDT", 3500)
        await engine.handle_message("ETH LONG\nEntry: 3400-3600\nTP1: 3800\nSL: 3300", tg_message_id=2)
        btc, eth = await open_trades(database)

        executor.balances = {
            "BTCUSDT": btc.remaining_qty / 2,            # half sold by hand
            "ETHUSDT": Decimal("0.0001"),                # dust: it is gone
        }
        market.set("ETHUSDT", 3700)
        notes = await engine.reconcile()

        assert len(notes) == 2
        assert "BTCUSDT" in notes[0] and "now tracking" in notes[0]
        assert "ETHUSDT" in notes[1] and "EXTERNAL" in notes[1]

        open_now = await open_trades(database)
        assert [t.symbol for t in open_now] == ["BTCUSDT"]
        assert open_now[0].remaining_qty == btc.remaining_qty / 2
        async with database.session() as session:
            closed = (await repo.closed_trades(session))[0]
        assert closed.symbol == "ETHUSDT"
        assert closed.close_reason == "EXTERNAL"
        assert closed.exits[-1]["estimated"] is True
        assert closed.pnl_usdt > 0                       # estimated at 3700 vs 3500 entry

        # A second run finds nothing to do.
        assert await engine.reconcile() == []
        assert "match the account" in await engine.reconcile_text()
    finally:
        await database.close()


async def test_reconcile_is_a_no_op_for_paper(tmp_path):
    engine, market, notifier, database, executor = await build(tmp_path)
    try:
        market.set("BTCUSDT", 100500)
        await engine.handle_message(BTC_LONG, tg_message_id=1)
        assert executor.balances == {}                    # holdings() -> None
        assert await engine.reconcile() == []
        assert len(await open_trades(database)) == 1
    finally:
        await database.close()


# --------------------------------------------------------------------------- #
# message retention
# --------------------------------------------------------------------------- #
async def test_old_channel_posts_are_pruned_hourly(tmp_path):
    engine, market, notifier, database, _ = await build(tmp_path, message_retention_days=30)
    try:
        await engine.handle_message("good morning", tg_message_id=1)
        await engine.handle_message(BTC_LONG, tg_message_id=2)
        async with database.session() as session:
            for message in (await session.execute(select(Message))).scalars():
                if message.tg_message_id == 1:
                    message.received_at = utcnow() - timedelta(days=40)

        async def count() -> int:
            async with database.session() as session:
                return int((await session.execute(select(func.count(Message.id)))).scalar_one())

        await engine.tick()                              # first pass prunes
        assert await count() == 1
        await engine.handle_message("still here", tg_message_id=3)
        async with database.session() as session:
            for message in (await session.execute(select(Message))).scalars():
                message.received_at = utcnow() - timedelta(days=40)
        await engine.tick()                              # within the hour: nothing
        assert await count() == 2
        engine._last_prune = utcnow() - timedelta(hours=2)
        await engine.tick()
        assert await count() == 0
        async with database.session() as session:
            assert len(await repo.waiting_signals(session)) + len(await repo.open_trades(session)) == 1
    finally:
        await database.close()


async def test_retention_off_keeps_everything(tmp_path):
    engine, market, notifier, database, _ = await build(tmp_path, message_retention_days=0)
    try:
        await engine.handle_message("hello", tg_message_id=1)
        async with database.session() as session:
            for message in (await session.execute(select(Message))).scalars():
                message.received_at = utcnow() - timedelta(days=400)
        await engine.tick()
        async with database.session() as session:
            assert (await session.execute(select(func.count(Message.id)))).scalar_one() == 1
    finally:
        await database.close()
