"""The trading engine - every decision the bot makes lives here.

    NEW MESSAGE -> parse -> BUY? -> halal? -> entry+SL+TP? -> not expired?
                -> no open position? -> wait for entry -> risk check
                -> BUY SPOT -> monitor -> TP / SL -> SELL SPOT -> save + notify

Every gate answers "no trade" by default:
UNKNOWN / NON-HALAL / SHORT / NO SL / EXPIRED / DUPLICATE -> NO TRADE.

On top of the per-signal gates sit the circuit breakers (:meth:`_trading_halted`
and the spread check in :meth:`_open_trade`).  They only ever refuse a new BUY;
an open position is always watched and closed at TP / SL, halted or not.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Awaitable, Callable, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import repository as repo
from app.database.database import Database
from app.database.models import (
    Signal,
    SignalStatus,
    Trade,
    TradeStatus,
    TradeUpdate,
    UpdateStatus,
    ensure_utc,
    utcnow,
)
from app.halal.whitelist import HalalWhitelist
from app.heartbeat import Heartbeat
from app.mexc.feed import PriceFeed
from app.mexc.market import MarketData
from app.notifications.telegram import TelegramNotifier, fmt, fmt_money
from app.telegram.llm_reader import LlmSignalReader
from app.telegram.parser import ParsedSignal, _extract_symbol, normalize, parse_signal
from app.telegram.updates import CANCEL, CLOSE, STOP, Update, parse_update
from app.trading.base import Executor
from app.vision.chart import ChartReader, ChartReading

log = logging.getLogger(__name__)

DUST_FALLBACK_NOTIONAL = Decimal("1")
SAME_SETUP_DAYS = 7

# Captions of result / management posts.  These come with the same chart as
# the original signal, so the picture must not be read again.
_RESULT_MARKERS = (
    "%", "✅", "urdi", "urib", "berdi", "berib", "qaytdi", "yurdi", "yopib", "yopamiz",
    "yoping", "sotamiz", "soting", "sotdim", "kotarib", "ko'tarib", "kotaring",
    "full tp", "zararsiz", "tugadi",
)


def is_result_update(caption: str) -> bool:
    """True for "12%✅ stop kotarib kutamiz"-style posts about a running trade.

    Entry posts ("##glmr kelsa stop bilan kiring", "2z olamiz stop bilan",
    "sotib olamiz va stop qoyib kutamiz") carry none of these markers.
    """
    low = (caption or "").lower()
    return any(marker in low for marker in _RESULT_MARKERS)


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        market: MarketData,
        executor: Executor,
        notifier: TelegramNotifier,
        whitelist: HalalWhitelist,
        chart_reader: Optional["ChartReader"] = None,
        heartbeat: Optional[Heartbeat] = None,
        llm_reader: Optional[LlmSignalReader] = None,
        price_feed: Optional[PriceFeed] = None,
    ) -> None:
        self._settings = settings
        self._db = database
        self._market = market
        self._executor = executor
        self._notifier = notifier
        self._whitelist = whitelist
        self._chart_reader = chart_reader
        self._llm_reader = llm_reader
        self._price_feed = price_feed
        self._heartbeat = heartbeat or Heartbeat()
        self._started_at = utcnow()
        self._capacity_warned: set[int] = set()
        self._spread_warned: set[int] = set()
        self._hold_warned: set[int] = set()
        self._last_prune: Optional[datetime] = None
        # Circuit breaker state.  The admin's /pause and the cleared loss
        # streak live in memory on purpose: after a restart the bot re-reads
        # the trades and, if the last N are still losses, pauses again - a
        # breaker that forgets on restart is not a breaker.
        self._paused_by_admin = False
        self._streak_cleared_at: Optional[datetime] = None
        self._halt_notified: Optional[str] = None
        # Every path that writes runs under this lock.  A channel message,
        # a button press and the price loop would otherwise open concurrent
        # write transactions, and on SQLite the loser dies with
        # "database is locked" after the busy timeout.
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 1. a message arrives
    # ------------------------------------------------------------------ #
    async def handle_message(
        self,
        text: str,
        channel_identifier: Optional[str] = None,
        tg_channel_id: Optional[int] = None,
        tg_message_id: Optional[int] = None,
        channel_title: Optional[str] = None,
        sent_at: Optional[datetime] = None,
        image_loader: Optional[Callable[[], Awaitable[Optional[bytes]]]] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Optional[Signal]:
        """Full decision chain for one channel message."""
        async with self._write_lock:
            return await self._handle_message_locked(
                text, channel_identifier, tg_channel_id, tg_message_id,
                channel_title, sent_at, image_loader, reply_to_message_id,
            )

    async def _handle_message_locked(
        self,
        text: str,
        channel_identifier: Optional[str],
        tg_channel_id: Optional[int],
        tg_message_id: Optional[int],
        channel_title: Optional[str],
        sent_at: Optional[datetime],
        image_loader: Optional[Callable[[], Awaitable[Optional[bytes]]]],
        reply_to_message_id: Optional[int] = None,
    ) -> Optional[Signal]:
        async with self._db.session() as session:
            channel = await self._resolve_channel(
                session, channel_identifier, tg_channel_id, channel_title
            )
            channel_id = channel.id if channel else None
            source = (channel.identifier if channel else channel_identifier) or ""

            if await repo.message_exists(session, channel_id, tg_message_id):
                log.debug("message %s from %s already handled", tg_message_id, source)
                return None

            result = parse_signal(
                text,
                self._settings.quote_asset,
                infer_direction=not self._settings.require_explicit_direction,
            )
            message = await repo.save_message(
                session, text, channel_id, tg_message_id, is_signal=result.looks_like_signal
            )
            message_id = message.id

            base_time = ensure_utc(sent_at) or utcnow()

            # Not a signal of its own - but maybe about one that is running:
            # "50% yopamiz", "stop to entry", "bekor".
            if not result.ok and not result.looks_like_signal and self._settings.channel_updates:
                update = parse_update(text)
                if update is not None:
                    handled = await self._handle_update(
                        session, update, channel_id, source, base_time, reply_to_message_id
                    )
                    if handled:
                        return None

            # The text alone is not tradable: the numbers may be in the picture.
            if not result.ok and image_loader is not None:
                from_image = await self._read_chart(
                    session, image_loader, channel_id, source, base_time, caption=text,
                    message_id=message_id,
                )
                if from_image is not None:
                    return from_image

            parsed = result.signal
            if not result.ok and self._llm_worth_asking(text, result):
                from_llm = await self._read_with_llm(
                    session, text, channel_id, source, base_time, message_id
                )
                if from_llm is not None:
                    return from_llm

            if parsed is None:
                if result.looks_like_signal:
                    log.info("signal-shaped message from %s not usable: %s", source, result.reason)
                return None

            # --- BUY / LONG only -------------------------------------- #
            if not parsed.is_buy:
                log.info("SHORT signal for %s from %s ignored (spot only)", parsed.symbol, source)
                return await self._reject(
                    session, parsed, "SHORT signal - the bot is spot only", channel_id,
                    source, notify=False,
                )

            # --- halal whitelist --------------------------------------- #
            if not self._whitelist.is_allowed(parsed.symbol):
                return await self._reject(
                    session, parsed, "Coin is not in halal whitelist.", channel_id, source
                )

            # --- entry + SL + TP present and consistent ---------------- #
            if result.reason is not None:
                return await self._reject(session, parsed, result.reason, channel_id, source)

            # --- expiry (an old / replayed message is never traded) ----- #
            expires_at = base_time + timedelta(minutes=self._settings.signal_expiry_minutes)
            if expires_at <= utcnow():
                return await self._reject(
                    session, parsed,
                    f"Signal is older than SIGNAL_EXPIRY_MINUTES "
                    f"({self._settings.signal_expiry_minutes} min).",
                    channel_id, source,
                )

            # --- one position per coin --------------------------------- #
            if await repo.has_open_trade(session, parsed.symbol):
                return await self._reject(
                    session, parsed, f"A position for {parsed.symbol} is already open.",
                    channel_id, source,
                )
            if await self._has_active_signal(session, parsed.symbol):
                return await self._reject(
                    session, parsed, f"Another {parsed.symbol} signal is already waiting.",
                    channel_id, source,
                )

            # --- circuit breakers -------------------------------------- #
            halted = await self._trading_halted(session)
            if halted is not None:
                return await self._reject(session, parsed, halted, channel_id, source)

            # --- accepted ---------------------------------------------- #
            entry_high = parsed.entry_high
            if entry_high is not None and entry_high == parsed.entry_low:
                entry_high = self._with_tolerance(entry_high)
            price = await self._safe_price(parsed.symbol)
            entry_mode, expires_at = self._entry_plan(entry_high, price, base_time)
            signal = await repo.create_signal(
                session,
                symbol=parsed.symbol,
                side=parsed.direction,
                entry_low=parsed.entry_low,
                entry_high=entry_high,
                sl_price=parsed.stop_loss,
                tp_prices=parsed.take_profits,
                status=SignalStatus.WAITING,
                expires_at=expires_at,
                channel_id=channel_id,
                raw_text=parsed.raw_text,
                entry_mode=entry_mode,
                message_id=message_id,
            )
            log.info("signal accepted: %s entry %s-%s sl %s tp %s (%s)",
                     parsed.symbol, parsed.entry_low, parsed.entry_high,
                     parsed.stop_loss, parsed.take_profits, entry_mode)
            await self._notifier.signal_received(
                symbol=parsed.symbol,
                entry_low=parsed.entry_low,
                entry_high=parsed.entry_high,
                sl=parsed.stop_loss,
                take_profits=parsed.take_profits,
                halal=True,
                status=self._waiting_status(entry_mode),
                source=source,
                direction_inferred=parsed.direction_inferred,
            )

            # Entry may already be reached - check straight away.
            if price is not None:
                await self._evaluate_signal(session, signal, price)
            return signal

    def _entry_plan(
        self, entry_high: Optional[Decimal], price: Optional[Decimal], base_time: datetime
    ) -> tuple[str, datetime]:
        """How a fresh signal waits for its entry, and for how long.

        With the live price above the entry the author means "buy when it
        comes down" (a resting limit order): that can take hours, so it gets
        LIMIT_ENTRY_EXPIRY_HOURS.  Otherwise the price has to rise into the
        zone soon or the setup is stale - SIGNAL_EXPIRY_MINUTES.
        """
        settings = self._settings
        if (
            price is not None
            and entry_high is not None
            and price > entry_high
            and settings.entry_above_zone == "wait"
        ):
            return "limit", base_time + timedelta(hours=settings.limit_entry_expiry_hours)
        return "breakout", base_time + timedelta(minutes=settings.signal_expiry_minutes)

    def _with_tolerance(self, entry: Decimal) -> Decimal:
        """A single-price entry becomes a small zone, per the admins' own rule
        ("enter up to 0.2-0.3% above the level")."""
        return entry * (Decimal(1) + self._settings.entry_tolerance_percent / Decimal(100))

    def _waiting_status(self, entry_mode: str) -> str:
        if entry_mode == "limit":
            return f"WAITING for the pullback (up to {self._settings.limit_entry_expiry_hours}h)"
        return "WAITING"

    async def _reject(
        self,
        session: AsyncSession,
        parsed: ParsedSignal,
        reason: str,
        channel_id: Optional[int],
        source: str,
        notify: bool = True,
        from_image: bool = False,
    ) -> Signal:
        signal = await repo.create_signal(
            session,
            symbol=parsed.symbol,
            side=parsed.direction,
            entry_low=parsed.entry_low,
            entry_high=parsed.entry_high,
            sl_price=parsed.stop_loss,
            tp_prices=parsed.take_profits,
            status=SignalStatus.SKIPPED,
            reject_reason=reason,
            channel_id=channel_id,
            raw_text=parsed.raw_text,
            from_image=from_image,
        )
        log.info("signal skipped: %s - %s", parsed.symbol, reason)
        if notify:
            await self._notifier.signal_skipped(parsed.symbol, reason, source)
        return signal

    # ------------------------------------------------------------------ #
    # 1b. the numbers live in a chart screenshot
    # ------------------------------------------------------------------ #
    async def _read_chart(
        self,
        session: AsyncSession,
        image_loader: Callable[[], Awaitable[Optional[bytes]]],
        channel_id: Optional[int],
        source: str,
        base_time: datetime,
        caption: str = "",
        message_id: Optional[int] = None,
    ) -> Optional[Signal]:
        """Read a Long Position screenshot and park it for confirmation.

        An OCR reading is never traded on its own: it has to survive the same
        gates as a text signal, plus a comparison against the live price (a
        misread digit lands an order of magnitude away), and then it waits for
        an explicit OK unless IMAGE_SIGNAL_CONFIRMATION is turned off.
        """
        if not self._settings.read_chart_images or self._chart_reader is None:
            return None
        if is_result_update(caption):
            log.info("chart from %s is a result update, not a signal: %r", source, caption[:60])
            return None
        image = await image_loader()
        if not image:
            return None

        hint = await self._symbol_hint(caption)
        raw = self._chart_reader.ocr(image)
        reading = raw if isinstance(raw, ChartReading) else self._chart_reader.interpret(raw, hint)
        # A "11,430" label is 11.43 on a Russian-locale chart and 11430 on a US
        # one.  Once the pair is known the live price settles it, so the
        # labels are interpreted a second time with that scale (OCR itself is
        # not repeated).
        price: Optional[Decimal] = None
        if reading.symbol and not isinstance(raw, ChartReading):
            price = await self._safe_price(str(reading.symbol))
            if price is not None and price > 0:
                reading = self._chart_reader.interpret(raw, hint, price_hint=price)
        if not reading.ok or reading.entry is None or reading.stop_loss is None:
            log.info("chart from %s not readable: %s", source, reading.reason)
            return None
        symbol = str(reading.symbol)
        log.info("chart from %s read as %s entry=%s sl=%s tp=%s",
                 source, symbol, reading.entry, reading.stop_loss, reading.take_profits)

        seen = await repo.same_setup_seen(
            session, symbol, reading.entry, reading.stop_loss,
            since=utcnow() - timedelta(days=SAME_SETUP_DAYS),
        )
        if seen is not None:
            log.info("chart from %s repeats signal #%s (%s, %s) - ignored",
                     source, seen.id, symbol, seen.status)
            return None

        parsed = ParsedSignal(
            symbol=symbol,
            base_asset=symbol.replace(self._settings.quote_asset, ""),
            direction="BUY",
            direction_inferred=True,
            entry_low=reading.entry,
            entry_high=self._with_tolerance(reading.entry),
            stop_loss=reading.stop_loss,
            take_profits=list(reading.take_profits),
            raw_text=f"[chart image] {' '.join(reading.notes)}".strip(),
        )
        return await self._park_reading(
            session, parsed, price, channel_id, source, base_time, message_id,
            needs_ok=self._settings.image_signal_confirmation, from_image=True,
            image=image, notes=reading.notes, what="chart",
        )

    async def _park_reading(
        self,
        session: AsyncSession,
        parsed: ParsedSignal,
        price: Optional[Decimal],
        channel_id: Optional[int],
        source: str,
        base_time: datetime,
        message_id: Optional[int],
        *,
        needs_ok: bool,
        from_image: bool,
        image: Optional[bytes] = None,
        notes: Sequence[str] = (),
        what: str = "chart",
    ) -> Optional[Signal]:
        """The gates every machine reading (OCR or model) passes before it is
        offered: halal, live-price sanity, no duplicate, not stale - and then
        the Confirm button, unless confirmation is off for that reader."""
        symbol = parsed.symbol
        entry = parsed.entry_low
        if entry is None:
            return None

        if not self._whitelist.is_allowed(symbol):
            return await self._reject(
                session, parsed, "Coin is not in halal whitelist.", channel_id, source,
                from_image=from_image,
            )

        if price is None:
            price = await self._safe_price(symbol)
        if price is None or price <= 0:
            log.warning("no live price for %s - %s signal dropped", symbol, what)
            return None

        deviation = abs(entry - price) / price * Decimal(100)
        limit = self._settings.image_max_price_deviation_percent
        if deviation > limit:
            reason = (
                f"Read entry {fmt(entry)} is {deviation.quantize(Decimal('0.1'))}% "
                f"away from the live price {fmt(price)} - probably a misread."
            )
            return await self._reject(session, parsed, reason, channel_id, source, from_image=from_image)

        if await repo.has_open_trade(session, symbol):
            log.info("%s signal for %s ignored: position already open", what, symbol)
            return None
        if await self._has_active_signal(session, symbol):
            log.info("%s signal for %s ignored: another signal is already active", what, symbol)
            return None

        if needs_ok:
            # The window for pressing Confirm; the entry plan is made once
            # the admin has said yes.
            entry_mode = "breakout"
            expires_at = base_time + timedelta(minutes=self._settings.signal_expiry_minutes)
        else:
            entry_mode, expires_at = self._entry_plan(parsed.entry_high, price, base_time)
        if expires_at <= utcnow():
            log.info("%s signal for %s ignored: already expired", what, symbol)
            return None

        signal = await repo.create_signal(
            session,
            symbol=symbol,
            side="BUY",
            entry_low=parsed.entry_low,
            entry_high=parsed.entry_high,
            sl_price=parsed.stop_loss,
            tp_prices=parsed.take_profits,
            status=SignalStatus.PENDING if needs_ok else SignalStatus.WAITING,
            expires_at=expires_at,
            channel_id=channel_id,
            raw_text=parsed.raw_text,
            from_image=from_image,
            entry_mode=entry_mode,
            message_id=message_id,
        )

        if needs_ok:
            if what == "chart":
                title = "📷 SIGNAL FROM A CHART IMAGE"
                footer = "Read by OCR - check it against the chart before confirming."
            else:
                title = "🤖 SIGNAL READ BY CLAUDE"
                footer = "The parser could not read this post; Claude did. Check it against the post before confirming."
            await self._notifier.signal_needs_confirmation(
                signal_id=signal.id,
                symbol=symbol,
                entry=entry,
                sl=parsed.stop_loss or Decimal(0),
                take_profits=parsed.take_profits,
                live_price=price,
                source=source,
                image=image,
                notes=notes,
                title=title,
                footer=footer,
            )
        else:
            await self._notifier.signal_received(
                symbol=symbol,
                entry_low=parsed.entry_low,
                entry_high=parsed.entry_high,
                sl=parsed.stop_loss,
                take_profits=parsed.take_profits,
                halal=True,
                status=self._waiting_status(entry_mode),
                source=source,
                direction_inferred=True,
            )
            await self._evaluate_signal(session, signal, price)
        return signal

    # ------------------------------------------------------------------ #
    # 1c. the parser gave up - ask the model
    # ------------------------------------------------------------------ #
    def _llm_worth_asking(self, text: str, result) -> bool:
        """Only posts that plausibly carry levels are sent to the model: the
        parser saw a signal shape, or the post has at least two numbers.  The
        rest (chatter, results) never costs a call."""
        if not self._settings.llm_fallback or self._llm_reader is None or not self._llm_reader.available:
            return False
        if result.signal is not None and not result.looks_like_signal:
            return False                    # a bare SHORT/SELL word, nothing else
        if result.looks_like_signal:
            return True
        numbers = len(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?", text))
        return numbers >= 2 and len(text) <= 1500

    async def _read_with_llm(
        self,
        session: AsyncSession,
        text: str,
        channel_id: Optional[int],
        source: str,
        base_time: datetime,
        message_id: Optional[int],
    ) -> Optional[Signal]:
        assert self._llm_reader is not None
        reading = await self._llm_reader.read(text)
        if reading is None:
            return None
        symbol = self._whitelist.normalize(reading.base_asset)
        log.info("LLM read post from %s as %s %s entry=%s-%s sl=%s tp=%s",
                 source, symbol, reading.direction, reading.entry_low, reading.entry_high,
                 reading.stop_loss, reading.take_profits)

        if reading.direction != "BUY":
            log.info("LLM reading for %s is a SHORT - ignored (spot only)", symbol)
            return None
        if reading.entry_low is None or reading.stop_loss is None or not reading.take_profits:
            missing = [n for n, v in (("entry", reading.entry_low), ("SL", reading.stop_loss),
                                      ("TP", reading.take_profits)) if not v]
            log.info("LLM reading for %s incomplete (missing %s) - not offered", symbol, ", ".join(missing))
            return None
        entry_high = reading.entry_high if reading.entry_high is not None else reading.entry_low
        if entry_high == reading.entry_low:
            entry_high = self._with_tolerance(entry_high)
        if reading.stop_loss >= reading.entry_low or not any(tp > entry_high for tp in reading.take_profits):
            log.info("LLM reading for %s has no long geometry - not offered", symbol)
            return None

        parsed = ParsedSignal(
            symbol=symbol,
            base_asset=reading.base_asset,
            direction="BUY",
            direction_inferred=True,
            entry_low=reading.entry_low,
            entry_high=entry_high,
            stop_loss=reading.stop_loss,
            take_profits=[tp for tp in reading.take_profits if tp > entry_high],
            raw_text=f"[llm] {text}"[:4000],
        )
        return await self._park_reading(
            session, parsed, None, channel_id, source, base_time, message_id,
            needs_ok=self._settings.llm_signal_confirmation, from_image=False,
            notes=reading.notes, what="llm",
        )

    # ------------------------------------------------------------------ #
    # confirmation buttons
    # ------------------------------------------------------------------ #
    async def handle_button(self, data: str) -> str:
        """Handle a confirm / reject press on a chart signal."""
        async with self._write_lock:
            return await self._handle_button_locked(data)

    async def _handle_button_locked(self, data: str) -> str:
        parts = data.split(":")
        if len(parts) != 3 or parts[0] not in ("sig", "upd") or not parts[1].isdigit():
            return "Unknown button."
        if parts[0] == "upd":
            return await self._handle_update_button(int(parts[1]), parts[2])
        signal_id, action = int(parts[1]), parts[2]

        async with self._db.session() as session:
            signal = await repo.get_signal(session, signal_id)
            if signal is None:
                return "That signal is gone."
            if signal.status != SignalStatus.PENDING.value:
                return f"{signal.symbol} is already {signal.status}."

            if action == "no":
                await repo.close_signal(
                    session, signal, SignalStatus.SKIPPED, "Rejected by the admin."
                )
                log.info("chart signal %s rejected by admin", signal.id)
                return f"❌ {signal.symbol} rejected - no trade."

            if action != "ok":
                return "Unknown button."

            expires_at = ensure_utc(signal.expires_at)
            if expires_at is not None and utcnow() >= expires_at:
                await repo.close_signal(
                    session, signal, SignalStatus.EXPIRED, "Confirmed too late."
                )
                return f"⌛ {signal.symbol} expired before it was confirmed."
            if await repo.has_open_trade(session, signal.symbol):
                await repo.close_signal(
                    session, signal, SignalStatus.SKIPPED,
                    f"A position for {signal.symbol} is already open.",
                )
                return f"{signal.symbol} already has an open position."

            signal.status = SignalStatus.WAITING.value
            log.info("chart signal %s confirmed by admin", signal.id)
            price = await self._safe_price(signal.symbol)
            signal.entry_mode, signal.expires_at = self._entry_plan(
                signal.entry_high or signal.entry_low, price, utcnow()
            )
            if price is not None:
                # _evaluate_signal updates this same object in place; refreshing
                # from the database here would throw the confirmation away.
                await self._evaluate_signal(session, signal, price)
            # _evaluate_signal may already have opened, skipped or expired it.
            if signal.status == SignalStatus.TRIGGERED.value:
                return f"✅ {signal.symbol} confirmed - position opened."
            if signal.status == SignalStatus.WAITING.value:
                if signal.entry_mode == "limit":
                    return (
                        f"✅ {signal.symbol} confirmed - waiting for the price to come "
                        f"down to {fmt(signal.entry_low or signal.entry_high)} "
                        f"(up to {self._settings.limit_entry_expiry_hours}h)."
                    )
                return f"✅ {signal.symbol} confirmed - waiting for the entry price."
            return f"{signal.symbol} confirmed, but no trade: {signal.reject_reason}"

    # ------------------------------------------------------------------ #
    # 2. price loop
    # ------------------------------------------------------------------ #
    async def tick(self) -> None:
        """One pass over waiting signals and open trades."""
        async with self._write_lock:
            await self._tick_locked()

    async def _tick_locked(self) -> None:
        async with self._db.session() as session:
            await self._expire_pending(session)
            signals = await repo.waiting_signals(session)
            trades = await repo.open_trades(session)
            symbols = {s.symbol for s in signals} | {t.symbol for t in trades}
            if self._price_feed is not None:
                self._price_feed.watch(symbols)
            if not symbols:
                await self._prune_messages(session)
                return

            prices = await self._current_prices(symbols)

            for signal in signals:
                price = prices.get(signal.symbol)
                try:
                    if price is None:
                        await repo.close_signal(
                            session, signal, SignalStatus.SKIPPED,
                            f"{signal.symbol} is not listed on MEXC spot.",
                        )
                        await self._notifier.signal_skipped(
                            signal.symbol, f"{signal.symbol} is not listed on MEXC spot."
                        )
                        continue
                    await self._evaluate_signal(session, signal, price)
                except Exception as exc:
                    log.exception("evaluating signal %s failed", signal.id)
                    await self._notifier.error(f"signal {signal.symbol}", exc)

            for trade in trades:
                price = prices.get(trade.symbol)
                if price is None:
                    log.warning("no price for open trade %s", trade.symbol)
                    continue
                try:
                    await self._monitor_trade(session, trade, price)
                    if trade.status == TradeStatus.OPEN.value:
                        await self._check_hold_time(session, trade, price)
                except Exception as exc:
                    log.exception("monitoring trade %s failed", trade.id)
                    await self._notifier.error(f"trade {trade.symbol}", exc)

            await self._prune_messages(session)

    async def _check_hold_time(self, session: AsyncSession, trade: Trade, price: Decimal) -> None:
        """A position stuck between SL and TP for MAX_HOLD_HOURS is reported
        once, or sold at market when MAX_HOLD_ACTION=close."""
        hours = self._settings.max_hold_hours
        if hours <= 0:
            return
        opened_at = ensure_utc(trade.opened_at)
        if opened_at is None or utcnow() - opened_at < timedelta(hours=hours):
            return
        age = int((utcnow() - opened_at).total_seconds() // 3600)
        if self._settings.max_hold_action == "close":
            log.info("trade %s open for %dh (MAX_HOLD_HOURS=%d) - closing", trade.id, age, hours)
            await self._exit(session, trade, price, reason="TIMEOUT")
            return
        if trade.id in self._hold_warned:
            return
        self._hold_warned.add(trade.id)
        unrealised = trade.remaining_qty * price - trade.quote_spent * (
            trade.remaining_qty / trade.quantity if trade.quantity > 0 else Decimal(0)
        )
        await self._notifier.info(
            f"⏰ {trade.symbol} has been open for {age}h (MAX_HOLD_HOURS={hours}): "
            f"price {fmt(price)}, SL {fmt(trade.sl_price)}, next TP "
            f"{fmt(trade.take_profits[trade.tp_hit]) if trade.tp_hit < len(trade.take_profits) else '-'}, "
            f"unrealised {fmt_money(unrealised)}.  /close {trade.symbol} frees the slot."
        )

    async def _prune_messages(self, session: AsyncSession) -> None:
        days = self._settings.message_retention_days
        if days <= 0:
            return
        now = utcnow()
        if self._last_prune is not None and now - self._last_prune < timedelta(hours=1):
            return
        self._last_prune = now
        deleted = await repo.prune_messages(session, now - timedelta(days=days))
        if deleted:
            log.info("pruned %d channel post(s) older than %d days", deleted, days)

    async def _expire_pending(self, session: AsyncSession) -> None:
        """A chart signal (or channel update) nobody confirmed in time is
        dropped, not acted on."""
        for update in await repo.pending_updates(session):
            expires_at = ensure_utc(update.expires_at)
            if expires_at is None or utcnow() < expires_at:
                continue
            await repo.close_update(session, update, UpdateStatus.EXPIRED, "Not confirmed in time.")
            log.info("channel update %s expired", update.id)
            await self._notifier.info(
                f"⌛ {update.symbol} update ({self._describe(update)}) expired unconfirmed."
            )
        for signal in await repo.pending_signals(session):
            expires_at = ensure_utc(signal.expires_at)
            if expires_at is None or utcnow() < expires_at:
                continue
            reason = (
                f"Not confirmed within {self._settings.signal_expiry_minutes} minutes."
            )
            await repo.close_signal(session, signal, SignalStatus.EXPIRED, reason)
            log.info("pending chart signal %s expired", signal.id)
            await self._notifier.signal_skipped(signal.symbol, reason)

    async def run_price_loop(self) -> None:
        log.info("price loop started (every %ss)", self._settings.price_poll_seconds)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("price loop iteration failed")
                await self._notifier.error("price loop", exc)
            else:
                # Only a pass that really checked every position counts as
                # alive - a loop that keeps failing must trip the monitor.
                await self._heartbeat.beat()
            await asyncio.sleep(self._settings.price_poll_seconds)

    # ------------------------------------------------------------------ #
    # 3. entry
    # ------------------------------------------------------------------ #
    async def _evaluate_signal(self, session: AsyncSession, signal: Signal, price: Decimal) -> None:
        expires_at = ensure_utc(signal.expires_at)
        if expires_at is not None and utcnow() >= expires_at:
            if signal.entry_mode == "limit":
                reason = (
                    f"Price did not come down to the entry within "
                    f"{self._settings.limit_entry_expiry_hours} hours."
                )
            else:
                reason = (
                    f"Entry was not reached within "
                    f"{self._settings.signal_expiry_minutes} minutes."
                )
            await repo.close_signal(session, signal, SignalStatus.EXPIRED, reason)
            await self._notifier.signal_skipped(signal.symbol, reason)
            log.info("signal %s expired", signal.id)
            return

        entry_low = signal.entry_low
        entry_high = signal.entry_high or signal.entry_low
        if entry_low is None or entry_high is None:  # pragma: no cover - guarded at creation
            await repo.close_signal(session, signal, SignalStatus.SKIPPED, "no entry price")
            return

        if signal.entry_mode == "limit":
            # A resting buy: it fills the moment the price touches the zone from
            # above.  If the price crashes straight through the stop first, the
            # setup is gone and the order is pulled.
            if signal.sl_price is not None and price <= signal.sl_price:
                reason = (
                    f"Price {fmt(price)} fell through the stop {fmt(signal.sl_price)} "
                    f"before the entry {fmt(entry_high)} was filled."
                )
                await repo.close_signal(session, signal, SignalStatus.SKIPPED, reason)
                await self._notifier.signal_skipped(signal.symbol, reason)
                return
            if price > entry_high:
                return  # still above the zone - keep waiting for the pullback
            await self._open_trade(session, signal, price)
            return

        if price > entry_high:
            reason = (
                f"Price {fmt(price)} is already above the entry zone "
                f"({fmt(entry_low)}-{fmt(entry_high)})."
            )
            await repo.close_signal(session, signal, SignalStatus.SKIPPED, reason)
            await self._notifier.signal_skipped(signal.symbol, reason)
            return

        if price < entry_low:
            return  # keep waiting for the price to rise into the zone

        await self._open_trade(session, signal, price)

    async def _open_trade(self, session: AsyncSession, signal: Signal, price: Decimal) -> None:
        settings = self._settings

        if await repo.has_open_trade(session, signal.symbol):
            reason = f"A position for {signal.symbol} is already open."
            await repo.close_signal(session, signal, SignalStatus.SKIPPED, reason)
            await self._notifier.signal_skipped(signal.symbol, reason)
            return

        # A signal accepted before a breaker tripped meets it here instead.
        halted = await self._trading_halted(session)
        if halted is not None:
            await repo.close_signal(session, signal, SignalStatus.SKIPPED, halted)
            await self._notifier.signal_skipped(signal.symbol, halted)
            return

        open_count = len(await repo.open_trades(session))
        if open_count >= settings.max_open_trades:
            if signal.id not in self._capacity_warned:
                self._capacity_warned.add(signal.id)
                log.info("MAX_OPEN_TRADES reached (%d) - %s keeps waiting",
                         settings.max_open_trades, signal.symbol)
                await self._notifier.info(
                    f"⏸ {signal.symbol} entry reached but MAX_OPEN_TRADES "
                    f"({settings.max_open_trades}) is full - still waiting."
                )
            return

        if not await self._spread_acceptable(signal):
            return

        amount = settings.trade_amount_usdt
        try:
            fill = await self._executor.buy(signal.symbol, amount, price)
        except Exception as exc:
            reason = f"Buy failed: {type(exc).__name__}: {exc}"
            log.exception("buy failed for %s", signal.symbol)
            await repo.close_signal(session, signal, SignalStatus.SKIPPED, reason[:250])
            await self._notifier.error(f"BUY {signal.symbol}", exc)
            return

        if fill is None:
            log.info("%s not filled yet - signal stays waiting", signal.symbol)
            return

        take_profits, splits = self._plan_targets(signal.take_profits)
        if not take_profits:  # pragma: no cover - guarded at creation
            log.error("signal %s has no take profit left", signal.id)
            return

        trade = await repo.create_trade(
            session,
            signal_id=signal.id,
            symbol=signal.symbol,
            mode=self._executor.mode,
            entry_price=fill.price,
            quantity=fill.quantity,
            quote_spent=fill.quote,
            sl_price=signal.sl_price or Decimal(0),
            tp_prices=take_profits,
            tp_splits=splits,
            entry_order_id=fill.order_id,
        )
        await repo.close_signal(session, signal, SignalStatus.TRIGGERED)
        self._capacity_warned.discard(signal.id)
        self._spread_warned.discard(signal.id)

        log.info("trade %s opened: %s qty=%s @ %s (%s)",
                 trade.id, trade.symbol, fill.quantity, fill.price, self._executor.mode)
        await self._notifier.trade_opened(
            symbol=trade.symbol,
            price=fill.price,
            quote_amount=fill.quote,
            sl=trade.sl_price,
            take_profits=take_profits,
            mode=self._executor.mode,
        )

    # ------------------------------------------------------------------ #
    # 3a. follow-up posts about a running trade
    # ------------------------------------------------------------------ #
    async def _handle_update(
        self,
        session: AsyncSession,
        update: Update,
        channel_id: Optional[int],
        source: str,
        base_time: datetime,
        reply_to_message_id: Optional[int],
    ) -> bool:
        """Tie a follow-up post to a position; ask, or act.  False = not ours."""
        trade, signal = await self._find_update_target(
            session, update.raw_text, channel_id, reply_to_message_id
        )
        if trade is None and signal is None:
            log.info("update-like post from %s matches no position: %r", source, update.raw_text[:60])
            return False
        symbol = trade.symbol if trade is not None else signal.symbol

        # A "close" for a signal that never entered is a cancel; a stop move
        # for it changes the level it will trade with.
        action = update.action
        if trade is None and action == CLOSE:
            action = CANCEL
        if trade is not None and action == CANCEL:
            log.info("cancel for %s ignored - the position is already open", symbol)
            return True

        row = await repo.create_update(
            session,
            symbol=symbol,
            action=action,
            percent=update.percent,
            price=update.price,
            to_entry=update.to_entry,
            trade_id=trade.id if trade is not None else None,
            signal_id=signal.id if signal is not None else None,
            channel_id=channel_id,
            raw_text=update.raw_text,
            expires_at=base_time + timedelta(minutes=self._settings.signal_expiry_minutes),
        )
        log.info("channel update for %s from %s: %s", symbol, source, self._describe(row))

        if not self._settings.update_confirmation:
            outcome = await self._apply_update(session, row)
            await self._notifier.info(f"📝 {symbol} update applied: {outcome}")
            return True

        price = await self._safe_price(symbol)
        state = self._update_state(trade, signal, price)
        warning = self._update_warning(row, trade, price)
        await self._notifier.update_needs_confirmation(
            update_id=row.id, symbol=symbol, instruction=self._describe(row),
            source=source, quoted=update.raw_text, state=state, warning=warning,
        )
        return True

    async def _find_update_target(
        self,
        session: AsyncSession,
        text: str,
        channel_id: Optional[int],
        reply_to_message_id: Optional[int],
    ) -> tuple[Optional[Trade], Optional[Signal]]:
        """The position a post is about: named coin, else the post it replies
        to, else the channel's only open position."""
        found = _extract_symbol(normalize(text))
        if found is not None:
            symbol = self._whitelist.normalize(found[0])
            trade = await repo.open_trade_for(session, symbol)
            if trade is not None:
                return trade, None
            for signal in await repo.active_signals(session):
                if signal.symbol == symbol:
                    return None, signal
            # The post names a coin we do not hold: it is about someone
            # else's trade, never about ours - no guessing from here on.
            return None, None

        original = await repo.find_message(session, channel_id, reply_to_message_id)
        if original is not None:
            signal = await repo.signal_for_message(session, original.id)
            if signal is not None:
                trade = await repo.open_trade_for_signal(session, signal.id)
                if trade is not None:
                    return trade, None
                if signal.status in (SignalStatus.WAITING.value, SignalStatus.PENDING.value):
                    return None, signal

        if channel_id is not None:
            trades = []
            for trade in await repo.open_trades(session):
                origin = await repo.get_signal(session, trade.signal_id) if trade.signal_id else None
                if origin is not None and origin.channel_id == channel_id:
                    trades.append(trade)
            if len(trades) == 1:
                return trades[0], None
            signals = [s for s in await repo.active_signals(session) if s.channel_id == channel_id]
            if not trades and len(signals) == 1:
                return None, signals[0]
        return None, None

    @staticmethod
    def _describe(row: TradeUpdate) -> str:
        if row.action == CLOSE:
            return "sell everything" if row.percent >= 100 else f"sell {row.percent}% of what is left"
        if row.action == STOP:
            return "move SL to the entry price" if row.to_entry else f"move SL to {fmt(row.price)}"
        return "cancel the waiting signal"

    @staticmethod
    def _update_state(
        trade: Optional[Trade], signal: Optional[Signal], price: Optional[Decimal]
    ) -> list[str]:
        lines = []
        if trade is not None:
            lines.append(
                f"Position: entry {fmt(trade.entry_price)}, SL {fmt(trade.sl_price)}, "
                f"{fmt(trade.remaining_qty)} left, TP hit {trade.tp_hit}/{len(trade.take_profits)}"
            )
        elif signal is not None:
            lines.append(
                f"Waiting signal: entry {fmt(signal.entry_low)}-{fmt(signal.entry_high)}, "
                f"SL {fmt(signal.sl_price)}"
            )
        if price is not None:
            lines.append(f"Live price: {fmt(price)}")
        return lines

    @staticmethod
    def _update_warning(row: TradeUpdate, trade: Optional[Trade], price: Optional[Decimal]) -> str:
        if row.action != STOP or trade is None:
            return ""
        new_sl = trade.entry_price if row.to_entry else row.price
        if new_sl is None:
            return ""
        if new_sl < trade.sl_price:
            return f"The new SL {fmt(new_sl)} is BELOW the current {fmt(trade.sl_price)} - a stop is never widened; this will be refused."
        if price is not None and new_sl >= price:
            return f"The live price {fmt(price)} is already at or under the new SL {fmt(new_sl)} - confirming sells on the next tick."
        return ""

    async def _handle_update_button(self, update_id: int, action: str) -> str:
        async with self._db.session() as session:
            row = await repo.get_update(session, update_id)
            if row is None:
                return "That update is gone."
            if row.status != UpdateStatus.PENDING.value:
                return f"{row.symbol} update is already {row.status.lower()}."
            if action == "no":
                await repo.close_update(session, row, UpdateStatus.REJECTED, "Ignored by the admin.")
                return f"❌ {row.symbol}: update ignored."
            if action != "ok":
                return "Unknown button."
            expires_at = ensure_utc(row.expires_at)
            if expires_at is not None and utcnow() >= expires_at:
                await repo.close_update(session, row, UpdateStatus.EXPIRED, "Confirmed too late.")
                return f"⌛ {row.symbol} update expired before it was confirmed."
            return await self._apply_update(session, row)

    async def _apply_update(self, session: AsyncSession, row: TradeUpdate) -> str:
        """Carry out a confirmed update.  Returns the line shown to the admin."""
        trade = await repo.get_trade(session, row.trade_id) if row.trade_id else None
        signal = await repo.get_signal(session, row.signal_id) if row.signal_id else None

        async def done(status: UpdateStatus, text: str) -> str:
            await repo.close_update(session, row, status, text)
            log.info("channel update %s %s: %s", row.id, status.value, text)
            return text

        if row.action == CANCEL:
            if signal is None or signal.status not in (
                SignalStatus.WAITING.value, SignalStatus.PENDING.value
            ):
                return await done(UpdateStatus.FAILED, f"{row.symbol}: no waiting signal to cancel.")
            await repo.close_signal(session, signal, SignalStatus.SKIPPED, "Cancelled by the channel.")
            return await done(UpdateStatus.APPLIED, f"🚫 {row.symbol} signal cancelled.")

        if row.action == STOP and trade is None and signal is not None:
            new_sl = row.price
            if new_sl is None or signal.entry_low is None or new_sl >= signal.entry_low:
                return await done(UpdateStatus.FAILED,
                                  f"{row.symbol}: the new SL must be a price below the entry.")
            signal.sl_price = new_sl
            return await done(UpdateStatus.APPLIED, f"🛡 {row.symbol} waiting signal: SL set to {fmt(new_sl)}.")

        if trade is None or trade.status != TradeStatus.OPEN.value:
            return await done(UpdateStatus.FAILED, f"{row.symbol}: the position is no longer open.")

        price = await self._safe_price(trade.symbol)
        if price is None:
            return await done(UpdateStatus.FAILED, f"{row.symbol}: no live price - nothing done.")

        if row.action == STOP:
            new_sl = trade.entry_price if row.to_entry else row.price
            if new_sl is None:
                return await done(UpdateStatus.FAILED, f"{row.symbol}: no SL level given.")
            if new_sl < trade.sl_price:
                return await done(UpdateStatus.FAILED,
                                  f"{row.symbol}: SL {fmt(new_sl)} is below the current {fmt(trade.sl_price)} - a stop is never widened.")
            old_sl = trade.sl_price
            trade.sl_price = new_sl
            note = "" if new_sl < price else f" (live price {fmt(price)} is under it - sells on the next tick)"
            return await done(UpdateStatus.APPLIED,
                              f"🛡 {row.symbol} SL {fmt(old_sl)} -> {fmt(new_sl)}{note}.")

        # CLOSE
        quantity = None if row.percent >= 100 else trade.remaining_qty * Decimal(row.percent) / Decimal(100)
        sold = await self._exit(session, trade, price, reason="CHANNEL", quantity=quantity)
        if not sold:
            return await done(UpdateStatus.FAILED, f"{row.symbol}: the sell did not fill.")
        if trade.status == TradeStatus.CLOSED.value:
            return await done(UpdateStatus.APPLIED,
                              f"🔴 {row.symbol} closed at {fmt(price)}: {fmt_money(trade.pnl_usdt)}.")
        left = trade.remaining_qty / trade.quantity * Decimal(100) if trade.quantity > 0 else Decimal(0)
        return await done(UpdateStatus.APPLIED,
                          f"🟠 {row.symbol}: {row.percent}% sold at {fmt(price)}, {left.quantize(Decimal('1'))}% left.")

    # ------------------------------------------------------------------ #
    # 3b. circuit breakers - a "no" here blocks every new BUY
    # ------------------------------------------------------------------ #
    async def _trading_halted(self, session: AsyncSession) -> Optional[str]:
        """Why no position may be opened right now, or None.

        Three breakers, checked in order of how deliberate they are: the
        admin's /pause, the loss streak (cleared by /resume), and the daily
        loss cap (lifts by itself at midnight UTC).  The first one that
        trips is reported to Telegram once, not on every signal.
        """
        settings = self._settings
        reason: Optional[str] = None

        if self._paused_by_admin:
            reason = "Trading is paused by the admin (/resume to continue)."

        if reason is None and settings.max_consecutive_losses > 0:
            streak = await repo.consecutive_losses(session, self._streak_cleared_at)
            if streak >= settings.max_consecutive_losses:
                reason = (
                    f"{streak} losing trades in a row (MAX_CONSECUTIVE_LOSSES="
                    f"{settings.max_consecutive_losses}) - buying is paused until /resume."
                )

        if reason is None and settings.max_daily_loss_usdt > 0:
            day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            pnl = await repo.realized_pnl_since(session, day_start)
            if pnl <= -settings.max_daily_loss_usdt:
                reason = (
                    f"Today's realised PNL is {fmt_money(pnl)} (MAX_DAILY_LOSS_USDT="
                    f"{settings.max_daily_loss_usdt}) - no new buys until midnight UTC."
                )

        if reason != self._halt_notified:
            self._halt_notified = reason
            if reason is not None:
                log.warning("trading halted: %s", reason)
                await self._notifier.info(f"⛔ TRADING HALTED\n\n{reason}")
            else:
                log.info("trading resumed")
        return reason

    async def _spread_acceptable(self, signal: Signal) -> bool:
        """The order book has to be tight enough to enter and leave again.

        A wide spread is paid twice - on the buy and on the eventual sell -
        and a LIMIT order in an empty book rarely fills at all.  The signal is
        not dropped: the book may tighten, so it keeps waiting and expires on
        its own schedule.  An unreadable book means no buy this tick, never a
        blind one.
        """
        limit = self._settings.max_entry_spread_percent
        if limit <= 0:
            return True
        try:
            book = await self._current_book(signal.symbol)
        except Exception as exc:
            log.warning("order book for %s unavailable, entry postponed: %s", signal.symbol, exc)
            return False
        spread = book.spread_percent
        if spread <= limit:
            return True
        if signal.id not in self._spread_warned:
            self._spread_warned.add(signal.id)
            log.info("spread on %s is %.3f%% (> %s%%) - %s keeps waiting",
                     signal.symbol, spread, limit, signal.symbol)
            await self._notifier.info(
                f"⏸ {signal.symbol} entry reached but the spread is "
                f"{spread.quantize(Decimal('0.01'))}% (bid {fmt(book.bid)} / ask "
                f"{fmt(book.ask)}), above MAX_ENTRY_SPREAD_PERCENT={limit}% - still waiting."
            )
        return False

    def pause(self) -> None:
        self._paused_by_admin = True

    def resume(self) -> None:
        """Lift the admin pause and forgive the current loss streak."""
        self._paused_by_admin = False
        self._streak_cleared_at = utcnow()

    async def pause_text(self) -> str:
        self.pause()
        return "⏸ Paused - no new buys. Open positions are still watched for TP / SL. /resume to continue."

    async def resume_text(self) -> str:
        self.resume()
        async with self._db.session() as session:
            still = await self._trading_halted(session)
        if still is not None:
            return f"▶️ Resumed, but buying is still blocked: {still}"
        return "▶️ Resumed - new signals are traded again."

    async def reconcile(self) -> list[str]:
        """LIVE: line the open trades up with what the account really holds.

        After a restart - or after a sale made by hand on the exchange - the
        database may remember a position the account no longer has.  Left
        alone, the next SL sell fails for lack of balance.  A position the
        account still holds in part is tracked at the real size; one that is
        gone (dust or nothing left) is closed as EXTERNAL with the current
        price standing in for the unknown proceeds.
        """
        notes: list[str] = []
        async with self._write_lock:
            async with self._db.session() as session:
                for trade in await repo.open_trades(session):
                    held = await self._executor.holdings(trade.symbol)
                    if held is None:
                        continue                          # paper: nothing to compare
                    if held >= trade.remaining_qty:
                        continue                          # all there
                    price = await self._safe_price(trade.symbol)
                    if price is None:
                        notes.append(f"⚠️ {trade.symbol}: exchange holds {fmt(held)} of "
                                     f"{fmt(trade.remaining_qty)} - no price, left as is.")
                        continue
                    min_notional = await self._min_notional(trade.symbol)
                    if held * price >= min_notional:
                        notes.append(
                            f"⚠️ {trade.symbol}: exchange holds {fmt(held)}, the books said "
                            f"{fmt(trade.remaining_qty)} - now tracking {fmt(held)}."
                        )
                        trade.remaining_qty = held
                        continue
                    gone = trade.remaining_qty - held
                    trade.exits = list(trade.exits or []) + [{
                        "reason": "EXTERNAL", "qty": format(gone, "f"),
                        "price": format(price, "f"), "quote": format(gone * price, "f"),
                        "at": utcnow().isoformat(), "estimated": True,
                    }]
                    trade.realized_quote = trade.realized_quote + gone * price
                    trade.remaining_qty = held
                    trade.status = TradeStatus.CLOSED.value
                    trade.closed_at = utcnow()
                    trade.close_reason = "EXTERNAL"
                    trade.exit_price = price
                    trade.pnl_usdt = trade.realized_quote + held * price - trade.quote_spent
                    trade.pnl_percent = (
                        trade.pnl_usdt / trade.quote_spent * Decimal(100)
                        if trade.quote_spent > 0 else Decimal(0)
                    )
                    notes.append(
                        f"🔴 {trade.symbol}: not in the account any more - closed as EXTERNAL, "
                        f"PNL {fmt_money(trade.pnl_usdt)} estimated at {fmt(price)}."
                    )
        for note in notes:
            log.warning("reconcile: %s", note)
        return notes

    async def reconcile_text(self) -> str:
        notes = await self.reconcile()
        return "\n".join(notes) if notes else "✅ Open positions match the account."

    async def close_text(self, args: str = "") -> str:
        """/close SYMBOL - sell the whole position now; /close all - every one.

        Closing through the bot keeps the books straight: a sale made by hand
        on the exchange leaves the trade open here and the next SL sell fails
        for lack of balance.  A waiting signal for the coin is cancelled too.
        """
        target = args.strip().upper()
        if not target:
            return "Usage: /close SYMBOL  (or /close all)"
        async with self._write_lock:
            async with self._db.session() as session:
                trades = await repo.open_trades(session)
                signals = await repo.active_signals(session)
                if target != "ALL":
                    symbol = self._whitelist.normalize(target)
                    trades = [t for t in trades if t.symbol == symbol]
                    signals = [s for s in signals if s.symbol == symbol]
                    if not trades and not signals:
                        return f"No open position or waiting signal for {symbol}."
                lines: list[str] = []
                for trade in trades:
                    price = await self._safe_price(trade.symbol)
                    if price is None:
                        lines.append(f"⚠️ {trade.symbol}: no live price - not closed.")
                        continue
                    if await self._exit(session, trade, price, reason="MANUAL"):
                        lines.append(
                            f"🔴 {trade.symbol} closed at {fmt(price)}: "
                            f"{fmt_money(trade.pnl_usdt)}"
                        )
                    else:
                        lines.append(f"⚠️ {trade.symbol}: the sell did not fill - still open.")
                for signal in signals:
                    await repo.close_signal(
                        session, signal, SignalStatus.SKIPPED, "Cancelled by the admin (/close)."
                    )
                    lines.append(f"🚫 {signal.symbol} signal cancelled.")
        return "\n".join(lines) or "Nothing to close."

    def _plan_targets(
        self, take_profits: Sequence[Decimal]
    ) -> tuple[list[Decimal], list[Decimal]]:
        """Pair the signal TPs with the configured position splits.

        Fewer TPs than splits: the last TP takes the leftover share, so the
        position is always fully closed (30/30/40 with two TPs -> 30/70).
        More TPs than splits: the extra targets are ignored.
        """
        ratios = self._settings.tp_split_ratios
        tps = list(take_profits)[: len(ratios)]
        if not tps:
            return [], []
        splits = list(ratios[: len(tps)])
        splits[-1] = Decimal(1) - sum(splits[:-1])
        return tps, splits

    # ------------------------------------------------------------------ #
    # 4. TP / SL monitoring
    # ------------------------------------------------------------------ #
    async def _monitor_trade(self, session: AsyncSession, trade: Trade, price: Decimal) -> None:
        if trade.sl_price is not None and price <= trade.sl_price:
            await self._exit(session, trade, price, reason="SL")
            return

        take_profits = trade.take_profits
        splits = trade.splits
        # A fast move can clear several targets between two polls.
        while trade.status == TradeStatus.OPEN.value and trade.tp_hit < len(take_profits):
            index = trade.tp_hit
            if price < take_profits[index]:
                break
            is_last = index == len(take_profits) - 1
            portion = (
                trade.remaining_qty
                if is_last
                else min(trade.quantity * splits[index], trade.remaining_qty)
            )
            sold = await self._exit(
                session, trade, price, reason=f"TP{index + 1}", quantity=portion
            )
            if not sold:
                break
            trade.tp_hit = index + 1
            if (
                index == 0
                and trade.status == TradeStatus.OPEN.value
                and self._settings.move_sl_to_entry_after_tp1
                and trade.sl_price < trade.entry_price
            ):
                # "1 TP olsa stopni olgan joyga ko'tarasiz": the rest of the
                # position can no longer lose.
                trade.sl_price = trade.entry_price
                log.info("trade %s: stop moved to the entry %s after TP1",
                         trade.id, fmt(trade.entry_price))
                await self._notifier.info(
                    f"🛡 {trade.symbol}: stop moved to the entry {fmt(trade.entry_price)} "
                    f"after TP1 - the rest of the position is risk-free."
                )

    async def _exit(
        self,
        session: AsyncSession,
        trade: Trade,
        price: Decimal,
        reason: str,
        quantity: Optional[Decimal] = None,
    ) -> bool:
        """Sell part (TP) or all (SL / last TP) of the position."""
        wanted = trade.remaining_qty if quantity is None else min(quantity, trade.remaining_qty)
        if wanted <= 0:
            return False

        min_notional = await self._min_notional(trade.symbol)
        # Never leave an unsellable crumb behind: if the rest would be dust,
        # sell the whole remainder now.
        rest = trade.remaining_qty - wanted
        if rest > 0 and rest * price < min_notional:
            wanted = trade.remaining_qty

        fill = await self._executor.sell(trade.symbol, wanted, price)
        if fill is None:
            log.warning("sell of %s %s did not fill", wanted, trade.symbol)
            return False

        trade.remaining_qty = trade.remaining_qty - fill.quantity
        trade.realized_quote = trade.realized_quote + fill.quote
        trade.exits = list(trade.exits or []) + [
            {
                "reason": reason,
                "qty": format(fill.quantity, "f"),
                "price": format(fill.price, "f"),
                "quote": format(fill.quote, "f"),
                "at": utcnow().isoformat(),
            }
        ]

        cost_basis = (
            trade.quote_spent * (fill.quantity / trade.quantity)
            if trade.quantity > 0
            else Decimal(0)
        )
        portion_pnl = fill.quote - cost_basis

        leftover_value = trade.remaining_qty * price
        fully_closed = (
            reason == "SL" or trade.remaining_qty <= 0 or leftover_value < min_notional
        )

        if fully_closed:
            sold_qty = trade.quantity - trade.remaining_qty
            trade.status = TradeStatus.CLOSED.value
            trade.closed_at = utcnow()
            trade.close_reason = reason
            trade.exit_price = (
                trade.realized_quote / sold_qty if sold_qty > 0 else fill.price
            )
            # Any unsold dust is valued at the exit price so PNL is not skewed.
            trade.pnl_usdt = trade.realized_quote + leftover_value - trade.quote_spent
            trade.pnl_percent = (
                trade.pnl_usdt / trade.quote_spent * Decimal(100)
                if trade.quote_spent > 0
                else Decimal(0)
            )
            log.info("trade %s closed (%s): pnl %s", trade.id, reason, trade.pnl_usdt)
            await self._notifier.trade_closed(
                symbol=trade.symbol,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                pnl=trade.pnl_usdt,
                pnl_percent=trade.pnl_percent,
                reason=reason,
                mode=trade.mode,
            )
        else:
            remaining_percent = (
                trade.remaining_qty / trade.quantity * Decimal(100)
                if trade.quantity > 0
                else Decimal(0)
            )
            log.info("trade %s partial exit (%s): pnl %s, %s%% left",
                     trade.id, reason, portion_pnl, remaining_percent)
            await self._notifier.trade_closed(
                symbol=trade.symbol,
                entry_price=trade.entry_price,
                exit_price=fill.price,
                pnl=portion_pnl,
                pnl_percent=(
                    portion_pnl / cost_basis * Decimal(100) if cost_basis > 0 else None
                ),
                reason=reason,
                mode=trade.mode,
                partial=True,
                remaining_percent=remaining_percent,
            )
        return True

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    async def _resolve_channel(
        self,
        session: AsyncSession,
        identifier: Optional[str],
        tg_channel_id: Optional[int],
        title: Optional[str],
    ):
        if identifier:
            return await repo.upsert_channel(session, identifier, tg_channel_id, title)
        if tg_channel_id is not None:
            channel = await repo.get_channel_by_tg_id(session, tg_channel_id)
            if channel is not None:
                return channel
            return await repo.upsert_channel(session, str(tg_channel_id), tg_channel_id, title)
        return None

    async def _has_active_signal(self, session: AsyncSession, symbol: str) -> bool:
        """Waiting for its entry, or waiting for the admin to confirm it."""
        return any(s.symbol == symbol for s in await repo.active_signals(session))

    async def _symbol_hint(self, caption: str) -> Optional[str]:
        """The coin a caption names, for a chart that does not show it.

        Only two shapes count: a hashtagged word ("##zama stop bilan kiring")
        or a caption that is nothing but the ticker ("ICP").  The word must be
        a whitelisted coin or a pair MEXC actually lists, so ordinary words
        never turn into a symbol.
        """
        from app.telegram.parser import STOPWORDS

        tokens = [t.strip("#$*_`") for t in (caption or "").split()]
        candidates: list[str] = []
        if len(tokens) == 1:
            candidates.append(tokens[0])
        candidates += [t.strip("#$*_`") for t in (caption or "").split() if t.startswith(("#", "$"))]

        quote = self._settings.quote_asset
        for raw in candidates:
            word = raw.upper()
            if not re.fullmatch(r"[A-Z0-9]{2,10}", word) or word in STOPWORDS:
                continue
            symbol = word if word.endswith(quote) else f"{word}{quote}"
            if self._whitelist.is_allowed(symbol):
                return symbol
            try:
                if symbol in await self._market.get_prices([symbol]):
                    return symbol
            except Exception as exc:
                log.debug("symbol hint lookup for %s failed: %s", symbol, exc)
        return None

    async def _current_prices(self, symbols: set[str]) -> dict[str, Decimal]:
        """Fresh feed prices where they exist, one REST call for the rest."""
        prices: dict[str, Decimal] = {}
        if self._price_feed is not None:
            prices = self._price_feed.prices(symbols)
        missing = symbols - set(prices)
        if missing:
            prices.update(await self._market.get_prices(missing))
        return prices

    async def _safe_price(self, symbol: str) -> Optional[Decimal]:
        if self._price_feed is not None:
            pushed = self._price_feed.price(symbol)
            if pushed is not None:
                return pushed
        try:
            return await self._market.get_price(symbol)
        except Exception as exc:
            log.warning("price lookup for %s failed: %s", symbol, exc)
            return None

    async def _current_book(self, symbol: str):
        if self._price_feed is not None:
            book = self._price_feed.book(symbol)
            if book is not None:
                return book
        return await self._market.get_book(symbol)

    async def _min_notional(self, symbol: str) -> Decimal:
        try:
            rules = await self._market.get_rules(symbol)
            return rules.min_quote_amount
        except Exception:
            return DUST_FALLBACK_NOTIONAL

    # ------------------------------------------------------------------ #
    # reporting
    # ------------------------------------------------------------------ #
    async def stats_text(self) -> str:
        async with self._db.session() as session:
            trades = await repo.all_trades(session)
            by_channel = await repo.channel_stats(session)
        closed = [t for t in trades if t.status == TradeStatus.CLOSED.value]
        wins = [t for t in closed if (t.pnl_usdt or Decimal(0)) > 0]
        losses = [t for t in closed if (t.pnl_usdt or Decimal(0)) < 0]
        total_pnl = sum((t.pnl_usdt or Decimal(0) for t in closed), Decimal(0))
        win_rate = (
            Decimal(len(wins)) / Decimal(len(closed)) * Decimal(100) if closed else Decimal(0)
        )
        open_count = len([t for t in trades if t.status == TradeStatus.OPEN.value])
        return "\n".join([
            "📊 STATISTICS",
            "",
            f"Total trades: {len(closed)}",
            f"Winning trades: {len(wins)}",
            f"Losing trades: {len(losses)}",
            f"Win rate: {win_rate.quantize(Decimal('0.1'))}%",
            f"Total PNL: {fmt_money(total_pnl)}",
            "",
            f"Open now: {open_count}",
            f"Mode: {self._settings.trading_mode}",
            "",
            "📣 BY CHANNEL",
            "",
            *(self._channel_lines(by_channel) or ["(no signals yet)"]),
        ])

    @staticmethod
    def _channel_lines(rows: Sequence["repo.ChannelStats"]) -> list[str]:
        """One line per channel: which ones earn their place and which do not.

            @alpha_signals: 14 signals, 9 traded, 6W/2L (75%), +$12.40
        """
        lines = []
        for row in rows:
            rate = f" ({row.win_rate.quantize(Decimal('1'))}%)" if row.win_rate is not None else ""
            lines.append(
                f"{row.channel}: {row.signals} signals, {row.trades} traded, "
                f"{row.wins}W/{row.losses}L{rate}, {fmt_money(row.pnl)}"
            )
        return lines

    async def positions_text(self) -> str:
        async with self._db.session() as session:
            trades = await repo.open_trades(session)
            signals = await repo.waiting_signals(session)
        lines = ["📂 OPEN POSITIONS", ""]
        if not trades:
            lines.append("(none)")
        for trade in trades:
            lines.append(
                f"{trade.symbol}  entry {fmt(trade.entry_price)}  "
                f"qty {fmt(trade.remaining_qty)}  SL {fmt(trade.sl_price)}  "
                f"TP hit {trade.tp_hit}/{len(trade.take_profits)}"
            )
        lines += ["", "⏳ WAITING SIGNALS", ""]
        if not signals:
            lines.append("(none)")
        for signal in signals:
            lines.append(
                f"{signal.symbol}  entry {fmt(signal.entry_low)}-{fmt(signal.entry_high)}  "
                f"SL {fmt(signal.sl_price)}"
            )
        return "\n".join(lines)

    async def status_text(self) -> str:
        uptime = utcnow() - self._started_at
        settings = self._settings
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._db.session() as session:
            open_count = len(await repo.open_trades(session))
            waiting = len(await repo.waiting_signals(session))
            halted = await self._trading_halted(session)
            today_pnl = await repo.realized_pnl_since(session, day_start)
            streak = await repo.consecutive_losses(session, self._streak_cleared_at)

        daily_cap = f"${settings.max_daily_loss_usdt}" if settings.max_daily_loss_usdt else "off"
        streak_cap = str(settings.max_consecutive_losses or "off")
        spread_cap = (
            f"{settings.max_entry_spread_percent}%" if settings.max_entry_spread_percent else "off"
        )

        return "\n".join([
            "🤖 SIGNALFORGE",
            "",
            f"Mode: {settings.trading_mode}",
            f"Uptime: {str(uptime).split('.')[0]}",
            f"Channels: {len(settings.channels)}",
            f"Halal coins: {len(self._whitelist)}",
            f"Open trades: {open_count} / {settings.max_open_trades}",
            f"Waiting signals: {waiting}",
            f"Trade amount: ${settings.trade_amount_usdt}",
            "Chart images: "
            + (
                "off"
                if not settings.read_chart_images
                else ("with Confirm button" if settings.image_signal_confirmation
                      else "auto (no Confirm)")
            ),
            "",
            "Buying: " + ("⛔ HALTED - " + halted if halted else "▶️ on"),
            f"Today's PNL: {fmt_money(today_pnl)} (daily loss cap {daily_cap})",
            f"Loss streak: {streak} (pause at {streak_cap})",
            f"Max entry spread: {spread_cap}",
            "Max hold: " + (f"{settings.max_hold_hours}h ({settings.max_hold_action})"
                            if settings.max_hold_hours else "off"),
            "Heartbeat: "
            + (f"every {self._heartbeat.interval}s" if self._heartbeat.enabled else "off"),
            "Prices: " + (
                f"websocket ({'connected' if self._price_feed.connected else 'reconnecting'}, "
                f"{self._price_feed.pushes} pushes), REST fallback"
                if self._price_feed is not None else f"REST every {settings.price_poll_seconds}s"
            ),
        ])

    async def halal_text(self) -> str:
        async with self._db.session() as session:
            sources = await self._whitelist.sources(session)
        lines = [
            f"{symbol} - {source}" if source else f"{symbol} - (no source recorded)"
            for symbol, source in sorted(sources.items())
        ]
        return "\n".join([
            "✅ HALAL WHITELIST",
            "",
            *(lines or ["(empty - nothing can be traded)"]),
            "",
            "Set by HALAL_COINS in .env only (BTC:source,ETH:source).",
        ])
