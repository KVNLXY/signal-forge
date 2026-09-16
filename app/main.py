"""SignalForge entry point.

    python -m app.main          run the bot
    python -m app.main check    verify config, MEXC, Telegram and the whitelist
    python -m app.main stats    print the trade statistics and exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from app.config import Settings, get_settings
from app.database import repository as repo
from app.database.database import Database
from app.halal.whitelist import HalalWhitelist
from app.heartbeat import Heartbeat
from app.telegram.llm_reader import LlmSignalReader
from app.logging_setup import setup_logging
from app.mexc.client import MexcClient, MexcError
from app.mexc.feed import PriceFeed
from app.mexc.market import MarketData
from app.mexc.orders import OrderClient
from app.notifications.telegram import AdminCommands, TelegramNotifier
from app.trading.base import Executor
from app.trading.engine import TradingEngine
from app.trading.live import LiveExecutor
from app.trading.paper import PaperExecutor
from app.vision.chart import ChartReader

log = logging.getLogger(__name__)

BANNER = r"""
 ____  _                   _ _____
/ ___|(_) __ _ _ __   __ _| |  ___|__  _ __ __ _  ___
\___ \| |/ _` | '_ \ / _` | | |_ / _ \| '__/ _` |/ _ \
 ___) | | (_| | | | | (_| | |  _| (_) | | | (_| |  __/
|____/|_|\__, |_| |_|\__,_|_|_|  \___/|_|  \__, |\___|
         |___/                             |___/
 halal spot signals -> MEXC spot
"""


class Application:
    """Owns every long-lived object so shutdown can be clean."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.database_url)
        self.mexc = MexcClient(
            api_key=settings.mexc_api_key.get_secret_value(),
            api_secret=settings.mexc_api_secret.get_secret_value(),
            base_url=settings.mexc_base_url,
            recv_window=settings.mexc_recv_window,
        )
        self.market = MarketData(self.mexc)
        self.orders = OrderClient(self.mexc)
        self.notifier = TelegramNotifier(
            bot_token=settings.telegram_admin_bot_token.get_secret_value(),
            chat_id=settings.telegram_admin_chat_id,
        )
        self.whitelist = HalalWhitelist(quote_asset=settings.quote_asset)
        self.chart_reader = ChartReader() if settings.read_chart_images else None
        self.heartbeat = Heartbeat(settings.heartbeat_url, settings.heartbeat_interval_seconds)
        self.price_feed = (
            PriceFeed(max_age_seconds=settings.price_feed_max_age_seconds)
            if settings.price_feed == "websocket" else None
        )
        self.llm_reader = (
            LlmSignalReader(settings.llm_api_key.get_secret_value(), settings.llm_model)
            if settings.llm_fallback else None
        )
        self.engine: TradingEngine | None = None

    # ------------------------------------------------------------------ #
    async def bootstrap(self) -> TradingEngine:
        await self.database.create_all()
        async with self.database.session() as session:
            await self.whitelist.sync_from_config(
                session, self.settings.halal_symbols, sources=self.settings.halal_sources
            )
            for identifier in self.settings.channels:
                await repo.upsert_channel(session, identifier)

        if not self.whitelist.symbols:
            log.warning("HALAL_COINS is empty - every signal will be skipped")

        if self.llm_reader is not None and not self.llm_reader.available:
            log.warning("LLM_FALLBACK is on but unusable (%s) - unreadable posts are dropped",
                        self.llm_reader.unavailable_reason)

        if self.chart_reader is not None and not self.chart_reader.available:
            log.warning(
                "READ_CHART_IMAGES is on but rapidocr-onnxruntime is missing - "
                "chart screenshots will be ignored"
            )

        executor = await self._build_executor()
        self.engine = TradingEngine(
            settings=self.settings,
            database=self.database,
            market=self.market,
            executor=executor,
            notifier=self.notifier,
            whitelist=self.whitelist,
            chart_reader=self.chart_reader,
            heartbeat=self.heartbeat,
            llm_reader=self.llm_reader,
            price_feed=self.price_feed,
        )
        return self.engine

    async def _build_executor(self) -> Executor:
        if not self.settings.is_live:
            log.info("TRADING_MODE=PAPER - real prices, simulated orders")
            return PaperExecutor(self.market, self.settings.paper_fee_rate)

        log.warning("TRADING_MODE=LIVE - real MEXC spot orders will be placed")
        await self.mexc.sync_time()
        account = await self.orders.account()
        if not account.get("canTrade", False):
            raise MexcError("the MEXC API key cannot trade - check its permissions")
        if account.get("canWithdraw", False):
            message = (
                "MEXC API key has WITHDRAWAL enabled. SignalForge never withdraws, "
                "but the key should be re-issued with withdrawal turned OFF."
            )
            log.warning(message)
            await self.notifier.info(f"⚠️ {message}")
        if self.settings.read_chart_images and not self.settings.image_signal_confirmation:
            message = (
                "LIVE with IMAGE_SIGNAL_CONFIRMATION=false: signals read off chart "
                "screenshots will buy real coins without your Confirm. Set it to true "
                "unless that is intended."
            )
            log.warning(message)
            await self.notifier.info(f"⚠️ {message}")
        return LiveExecutor(
            market=self.market,
            orders=self.orders,
            quote_asset=self.settings.quote_asset,
            use_market_order=self.settings.use_market_order,
            order_timeout_seconds=self.settings.order_timeout_seconds,
            limit_slippage_percent=self.settings.limit_slippage_percent,
        )

    async def close(self) -> None:
        await self.notifier.close()
        await self.heartbeat.close()
        await self.mexc.close()
        await self.database.close()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
async def run(settings: Settings) -> int:
    from app.telegram.client import build_client, connect
    from app.telegram.listener import ChannelListener

    app = Application(settings)
    engine = await app.bootstrap()

    telegram = build_client(
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
        settings.telegram_session.get_secret_value(),
    )
    await connect(telegram)

    listener = ChannelListener(
        telegram, settings.channels, engine.handle_message, on_error=app.notifier.error,
        group_admin_only=settings.group_admin_only,
        catch_up_minutes=settings.signal_expiry_minutes if settings.catch_up_on_start else 0,
    )
    admin = AdminCommands(
        bot_token=settings.telegram_admin_bot_token.get_secret_value(),
        chat_id=settings.telegram_admin_chat_id,
        handlers={
            "stats": engine.stats_text,
            "status": engine.status_text,
            "positions": engine.positions_text,
            "halal": engine.halal_text,
            "pause": engine.pause_text,
            "resume": engine.resume_text,
            "close": engine.close_text,
            "reconcile": engine.reconcile_text,
            "help": _help_text,
        },
        on_button=engine.handle_button,
    )

    await app.notifier.info(
        "\n".join([
            "🤖 SIGNALFORGE STARTED",
            "",
            f"Mode: {settings.trading_mode}",
            f"Channels: {len(settings.channels)}",
            f"Halal coins: {', '.join(app.whitelist.symbols) or '(none)'}",
            f"Trade amount: ${settings.trade_amount_usdt}",
            f"Max open trades: {settings.max_open_trades}",
            _breakers_line(settings),
        ])
    )

    if settings.is_live:
        notes = await engine.reconcile()
        if notes:
            await app.notifier.info("🧾 RECONCILED AFTER START\n\n" + "\n".join(notes))

    tasks = [
        asyncio.create_task(listener.run_forever(), name="telegram-listener"),
        asyncio.create_task(engine.run_price_loop(), name="price-loop"),
        asyncio.create_task(admin.run_forever(), name="admin-commands"),
    ]
    if app.price_feed is not None:
        tasks.append(asyncio.create_task(app.price_feed.run_forever(), name="price-feed"))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        log.info("shutting down")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await admin.close()
        try:
            await telegram.disconnect()
        except Exception:  # pragma: no cover - best effort
            pass
        await app.close()
    return 0


async def _help_text() -> str:
    return "\n".join([
        "Commands:",
        "/status     - mode, uptime, capacity",
        "/positions  - open trades and waiting signals",
        "/stats      - win rate and total PNL",
        "/halal      - the whitelist",
        "/pause      - stop opening new positions (exits keep running)",
        "/resume     - lift /pause and forgive the current loss streak",
        "/close X    - sell the whole X position now (/close all - every one)",
        "/reconcile  - LIVE: line the open positions up with the account",
    ])


def _breakers_line(settings: Settings) -> str:
    """One line for the start-up report and the pre-flight check."""
    parts = [
        "daily loss cap "
        + ("$" + str(settings.max_daily_loss_usdt) if settings.max_daily_loss_usdt else "off"),
        f"loss streak {settings.max_consecutive_losses or 'off'}",
        "max spread "
        + (str(settings.max_entry_spread_percent) + "%" if settings.max_entry_spread_percent else "off"),
    ]
    return "Breakers: " + ", ".join(parts)


async def stats(settings: Settings) -> int:
    app = Application(settings)
    engine = await app.bootstrap()
    print(await engine.stats_text())
    print()
    print(await engine.positions_text())
    await app.close()
    return 0


async def check(settings: Settings) -> int:
    """Pre-flight check - safe to run before going anywhere near LIVE."""
    ok = True
    print(f"mode:            {settings.trading_mode}")
    print(f"channels:        {', '.join(settings.channels) or '(none configured)'}")
    print(f"halal whitelist: {', '.join(settings.halal_symbols) or '(empty)'}")
    print(f"trade amount:    ${settings.trade_amount_usdt}  max open: {settings.max_open_trades}")
    print(f"order type:      {'MARKET' if settings.use_market_order else 'LIMIT'}")
    print(f"breakers:        {_breakers_line(settings)[len('Breakers: '):]}")

    app = Application(settings)
    try:
        await app.database.create_all()
        print("database:        ok")
    except Exception as exc:
        ok = False
        print(f"database:        FAILED - {exc}")

    try:
        await app.mexc.ping()
        prices = await app.market.get_prices(settings.halal_symbols)
        print(f"mexc:            ok ({len(prices)}/{len(settings.halal_symbols)} whitelist "
              f"symbols priced)")
        for symbol in settings.halal_symbols:
            price = prices.get(symbol)
            if price is None:
                # Kept on purpose: the whitelist is a halal list, not a MEXC
                # list.  A signal for it is skipped with a clear reason.
                print(f"  {symbol}: not listed on MEXC spot (kept - signals for it will be skipped)")
            else:
                rules = await app.market.get_rules(symbol)
                print(f"  {symbol}: {price}  min order {rules.min_quote_amount} "
                      f"{rules.quote_asset}  tradable={rules.tradable}")
                if settings.trade_amount_usdt < rules.min_quote_amount:
                    ok = False
                    print(f"    TRADE_AMOUNT_USDT is below the minimum for {symbol}")
    except Exception as exc:
        ok = False
        print(f"mexc:            FAILED - {exc}")

    if settings.is_live:
        try:
            await app.mexc.sync_time()
            account = await app.orders.account()
            free = Decimal("0")
            for balance in account.get("balances", []):
                if str(balance.get("asset")).upper() == settings.quote_asset:
                    free = Decimal(str(balance.get("free", "0")))
            print(f"mexc account:    canTrade={account.get('canTrade')} "
                  f"canWithdraw={account.get('canWithdraw')} free {settings.quote_asset}={free}")
            if account.get("canWithdraw"):
                print("    WARNING: withdrawal permission should be OFF")
            if not account.get("canTrade"):
                ok = False
        except Exception as exc:
            ok = False
            print(f"mexc account:    FAILED - {exc}")

    # Second-opinion screen: informational only, never changes the whitelist.
    try:
        from app.halal.sharlife import SharlifeIndex

        sharlife = await SharlifeIndex.load()
        quote = settings.quote_asset
        flagged = []
        for symbol in settings.halal_symbols:
            ticker = symbol[: -len(quote)] if symbol.endswith(quote) else symbol
            for hit in sharlife.lookup(ticker):
                if hit.status in ("grey", "failed"):
                    flagged.append(f"{ticker} {hit.label} ({hit.name})")
        if not sharlife.entries:
            print("sharlife:        unavailable (no cache yet)")
        elif flagged:
            print(f"sharlife:        {len(flagged)} whitelisted coin(s) not clean - your call:")
            for line in flagged:
                print(f"  {line}")
        else:
            print("sharlife:        ok (no whitelisted coin is Grey / Non-Shariah)")
    except Exception as exc:  # never block the check on a third-party site
        print(f"sharlife:        skipped - {exc}")

    reader = app.chart_reader
    print(f"chart images:    {'ok' if reader and reader.available else 'off / not installed'}")

    if settings.price_feed == "websocket":
        try:
            import websockets  # noqa: F401
        except ImportError:
            ok = False
            print("price feed:      FAILED - PRICE_FEED=websocket needs the 'websockets' package")
        else:
            print(f"price feed:      websocket (REST fallback after "
                  f"{settings.price_feed_max_age_seconds}s without a push)")
    else:
        print(f"price feed:      REST every {settings.price_poll_seconds}s")

    reader = app.llm_reader
    if reader is None:
        print("llm fallback:    off")
    elif reader.available:
        print(f"llm fallback:    ok ({reader.model}"
              f"{', with Confirm button' if settings.llm_signal_confirmation else ', auto - no Confirm'})")
    else:
        print(f"llm fallback:    unusable - {reader.unavailable_reason}")

    if app.heartbeat.enabled:
        pinged = await app.heartbeat.beat(force=True)
        print(f"heartbeat:       {'ok' if pinged else 'FAILED'} "
              f"(every {settings.heartbeat_interval_seconds}s)")
        ok = ok and pinged
    else:
        print("heartbeat:       off - set HEARTBEAT_URL so a dead bot raises an alarm"
              + (" (LIVE without it means an unwatched stop-loss)" if settings.is_live else ""))

    if settings.notifications_enabled:
        sent = await app.notifier.send("✅ SignalForge check: notifications work.")
        print(f"notifications:   {'ok' if sent else 'FAILED'}")
        ok = ok and sent
    else:
        print("notifications:   disabled (no bot token / chat id)")

    try:
        from app.telegram.client import build_client, connect

        telegram = build_client(
            settings.telegram_api_id,
            settings.telegram_api_hash.get_secret_value(),
            settings.telegram_session.get_secret_value(),
        )
        await connect(telegram)
        from app.telegram.listener import ChannelListener

        listener = ChannelListener(telegram, settings.channels, _noop)
        resolved = await listener.resolve_channels()
        print(f"telegram:        ok ({len(resolved)}/{len(settings.channels)} channels resolved)")
        ok = ok and len(resolved) == len(settings.channels)
        await telegram.disconnect()
    except Exception as exc:
        ok = False
        print(f"telegram:        FAILED - {exc}")

    await app.close()
    print()
    print("RESULT:          " + ("ready" if ok else "not ready - fix the FAILED lines above"))
    return 0 if ok else 1


async def _noop(*args: object, **kwargs: object) -> None:
    return None


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signalforge", description=__doc__)
    parser.add_argument(
        "command", nargs="?", default="run", choices=["run", "check", "stats"]
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    if args.command == "run":
        print(BANNER)
        log.info("starting SignalForge in %s mode", settings.trading_mode)

    handler = {"run": run, "check": check, "stats": stats}[args.command]
    try:
        return asyncio.run(handler(settings))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        log.info("stopped by user")
        return 0


if __name__ == "__main__":
    sys.exit(main())
