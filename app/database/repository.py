"""Thin data-access helpers.  Keeps SQL out of the trading engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Asset,
    Channel,
    Message,
    Signal,
    SignalStatus,
    Trade,
    TradeStatus,
    TradeUpdate,
    UpdateStatus,
)


# --------------------------------------------------------------------------- #
# channels / messages
# --------------------------------------------------------------------------- #
async def upsert_channel(
    session: AsyncSession,
    identifier: str,
    tg_id: Optional[int] = None,
    title: Optional[str] = None,
) -> Channel:
    channel = (
        await session.execute(select(Channel).where(Channel.identifier == identifier))
    ).scalar_one_or_none()
    if channel is None:
        channel = Channel(identifier=identifier, tg_id=tg_id, title=title)
        session.add(channel)
        await session.flush()
    else:
        if tg_id is not None:
            channel.tg_id = tg_id
        if title:
            channel.title = title
    return channel


async def get_channel_by_tg_id(session: AsyncSession, tg_id: int) -> Optional[Channel]:
    return (
        await session.execute(select(Channel).where(Channel.tg_id == tg_id))
    ).scalar_one_or_none()


async def save_message(
    session: AsyncSession,
    text: str,
    channel_id: Optional[int],
    tg_message_id: Optional[int],
    is_signal: bool = False,
) -> Message:
    message = Message(
        text=text, channel_id=channel_id, tg_message_id=tg_message_id, is_signal=is_signal
    )
    session.add(message)
    await session.flush()
    return message


async def find_message(
    session: AsyncSession, channel_id: Optional[int], tg_message_id: Optional[int]
) -> Optional[Message]:
    if channel_id is None or tg_message_id is None:
        return None
    return (
        await session.execute(
            select(Message).where(
                Message.channel_id == channel_id, Message.tg_message_id == tg_message_id
            )
        )
    ).scalar_one_or_none()


async def prune_messages(session: AsyncSession, before: datetime) -> int:
    """Delete channel posts received before ``before``.  Signals keep their
    own raw_text, so nothing the trade history needs is lost."""
    result = await session.execute(delete(Message).where(Message.received_at < before))
    return int(result.rowcount or 0)


async def message_exists(
    session: AsyncSession, channel_id: Optional[int], tg_message_id: Optional[int]
) -> bool:
    if channel_id is None or tg_message_id is None:
        return False
    found = (
        await session.execute(
            select(Message.id).where(
                Message.channel_id == channel_id, Message.tg_message_id == tg_message_id
            )
        )
    ).first()
    return found is not None


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
async def create_signal(
    session: AsyncSession,
    *,
    symbol: str,
    side: str,
    entry_low: Optional[Decimal],
    entry_high: Optional[Decimal],
    sl_price: Optional[Decimal],
    tp_prices: Sequence[Decimal],
    status: SignalStatus,
    reject_reason: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    message_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    raw_text: Optional[str] = None,
    from_image: bool = False,
    entry_mode: str = "breakout",
) -> Signal:
    signal = Signal(
        message_id=message_id,
        channel_id=channel_id,
        symbol=symbol,
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        sl_price=sl_price,
        tp_prices=[format(p, "f") for p in tp_prices],
        status=status.value,
        reject_reason=reject_reason,
        expires_at=expires_at,
        raw_text=raw_text,
        from_image=from_image,
        entry_mode=entry_mode,
    )
    session.add(signal)
    await session.flush()
    return signal


async def waiting_signals(session: AsyncSession) -> list[Signal]:
    result = await session.execute(
        select(Signal).where(Signal.status == SignalStatus.WAITING.value).order_by(Signal.id)
    )
    return list(result.scalars())


async def pending_signals(session: AsyncSession) -> list[Signal]:
    """Image-derived signals waiting for the admin to confirm them."""
    result = await session.execute(
        select(Signal).where(Signal.status == SignalStatus.PENDING.value).order_by(Signal.id)
    )
    return list(result.scalars())


async def active_signals(session: AsyncSession) -> list[Signal]:
    """Everything that still occupies a coin: waiting plus pending."""
    result = await session.execute(
        select(Signal)
        .where(Signal.status.in_([SignalStatus.WAITING.value, SignalStatus.PENDING.value]))
        .order_by(Signal.id)
    )
    return list(result.scalars())


async def same_setup_seen(
    session: AsyncSession,
    symbol: str,
    entry: Decimal,
    stop_loss: Decimal,
    since: datetime,
    tolerance: Decimal = Decimal("0.002"),
) -> Optional[Signal]:
    """A recent signal with the same entry and stop (within 0.2%).

    The same chart reaches the bot more than once: the VIP group and the
    public channel post it, and result screenshots repeat the drawing.
    """
    result = await session.execute(
        select(Signal)
        .where(Signal.symbol == symbol, Signal.received_at >= since)
        .order_by(Signal.id.desc())
    )
    for signal in result.scalars():
        if signal.entry_low is None or signal.sl_price is None:
            continue
        if (
            abs(signal.entry_low - entry) <= entry * tolerance
            and abs(signal.sl_price - stop_loss) <= stop_loss * tolerance
        ):
            return signal
    return None


async def get_signal(session: AsyncSession, signal_id: int) -> Optional[Signal]:
    return await session.get(Signal, signal_id)


async def signal_for_message(session: AsyncSession, message_id: int) -> Optional[Signal]:
    """The signal that was read out of a given channel post (the latest, if
    the post produced more than one row)."""
    return (
        await session.execute(
            select(Signal).where(Signal.message_id == message_id).order_by(Signal.id.desc())
        )
    ).scalars().first()


async def close_signal(
    session: AsyncSession, signal: Signal, status: SignalStatus, reason: Optional[str] = None
) -> None:
    signal.status = status.value
    if reason:
        signal.reject_reason = reason


# --------------------------------------------------------------------------- #
# trades
# --------------------------------------------------------------------------- #
async def open_trades(session: AsyncSession) -> list[Trade]:
    result = await session.execute(
        select(Trade).where(Trade.status == TradeStatus.OPEN.value).order_by(Trade.id)
    )
    return list(result.scalars())


async def open_trades_count(session: AsyncSession) -> int:
    return len(await open_trades(session))


async def open_trade_for(session: AsyncSession, symbol: str) -> Optional[Trade]:
    return (
        await session.execute(
            select(Trade).where(Trade.symbol == symbol, Trade.status == TradeStatus.OPEN.value)
        )
    ).scalars().first()


async def open_trade_for_signal(session: AsyncSession, signal_id: int) -> Optional[Trade]:
    return (
        await session.execute(
            select(Trade).where(Trade.signal_id == signal_id, Trade.status == TradeStatus.OPEN.value)
        )
    ).scalars().first()


async def get_trade(session: AsyncSession, trade_id: int) -> Optional[Trade]:
    return await session.get(Trade, trade_id)


async def has_open_trade(session: AsyncSession, symbol: str) -> bool:
    found = (
        await session.execute(
            select(Trade.id).where(
                Trade.symbol == symbol, Trade.status == TradeStatus.OPEN.value
            )
        )
    ).first()
    return found is not None


async def create_trade(
    session: AsyncSession,
    *,
    signal_id: Optional[int],
    symbol: str,
    mode: str,
    entry_price: Decimal,
    quantity: Decimal,
    quote_spent: Decimal,
    sl_price: Decimal,
    tp_prices: Sequence[Decimal],
    tp_splits: Sequence[Decimal],
    entry_order_id: Optional[str] = None,
) -> Trade:
    trade = Trade(
        signal_id=signal_id,
        symbol=symbol,
        mode=mode,
        status=TradeStatus.OPEN.value,
        entry_price=entry_price,
        quantity=quantity,
        remaining_qty=quantity,
        quote_spent=quote_spent,
        realized_quote=Decimal(0),
        sl_price=sl_price,
        tp_prices=[format(p, "f") for p in tp_prices],
        tp_splits=[format(s, "f") for s in tp_splits],
        exits=[],
        entry_order_id=entry_order_id,
    )
    session.add(trade)
    await session.flush()
    return trade


async def all_trades(session: AsyncSession) -> list[Trade]:
    result = await session.execute(select(Trade).order_by(Trade.id))
    return list(result.scalars())


async def closed_trades(session: AsyncSession) -> list[Trade]:
    result = await session.execute(
        select(Trade).where(Trade.status == TradeStatus.CLOSED.value).order_by(Trade.id)
    )
    return list(result.scalars())


async def realized_pnl_since(session: AsyncSession, since: datetime) -> Decimal:
    """Sum of the PNL of every trade closed at or after ``since``."""
    result = await session.execute(
        select(Trade.pnl_usdt).where(
            Trade.status == TradeStatus.CLOSED.value, Trade.closed_at >= since
        )
    )
    return sum((Decimal(p) for p in result.scalars() if p is not None), Decimal(0))


async def consecutive_losses(session: AsyncSession, since: Optional[datetime] = None) -> int:
    """How many of the most recently closed trades lost, counting back from
    the latest until the first trade that did not lose.  Trades closed before
    ``since`` are not counted (the admin has cleared the streak)."""
    stmt = (
        select(Trade.pnl_usdt)
        .where(Trade.status == TradeStatus.CLOSED.value)
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
    )
    if since is not None:
        stmt = stmt.where(Trade.closed_at >= since)
    streak = 0
    for pnl in (await session.execute(stmt)).scalars():
        if pnl is None or Decimal(pnl) >= 0:
            break
        streak += 1
    return streak


@dataclass
class ChannelStats:
    """What one channel has produced: posts that parsed as signals, how many
    of them were actually bought, and how those trades ended."""

    channel: str
    signals: int = 0            # signal-shaped posts (accepted or skipped)
    accepted: int = 0           # passed every gate and waited for the entry
    trades: int = 0             # positions actually opened
    wins: int = 0
    losses: int = 0
    pnl: Decimal = Decimal(0)   # realised, closed trades only

    @property
    def win_rate(self) -> Optional[Decimal]:
        closed = self.wins + self.losses
        if not closed:
            return None
        return Decimal(self.wins) / Decimal(closed) * Decimal(100)


async def channel_stats(session: AsyncSession) -> list[ChannelStats]:
    """Per-channel scoreboard, best realised PNL first.

    Signals without a channel (tests, manual input) are grouped under "-".
    """
    channels = {c.id: c.identifier for c in (await session.execute(select(Channel))).scalars()}
    signals = list((await session.execute(select(Signal))).scalars())
    trades = list((await session.execute(select(Trade))).scalars())

    rows: dict[Optional[int], ChannelStats] = {}

    def row(channel_id: Optional[int]) -> ChannelStats:
        if channel_id not in rows:
            rows[channel_id] = ChannelStats(channel=channels.get(channel_id, "-"))
        return rows[channel_id]

    signal_channel: dict[int, Optional[int]] = {}
    for signal in signals:
        signal_channel[signal.id] = signal.channel_id
        stats = row(signal.channel_id)
        stats.signals += 1
        if signal.status != SignalStatus.SKIPPED.value:
            stats.accepted += 1

    for trade in trades:
        stats = row(signal_channel.get(trade.signal_id))
        stats.trades += 1
        if trade.status != TradeStatus.CLOSED.value:
            continue
        pnl = trade.pnl_usdt or Decimal(0)
        stats.pnl += pnl
        if pnl > 0:
            stats.wins += 1
        elif pnl < 0:
            stats.losses += 1

    return sorted(rows.values(), key=lambda r: (r.pnl, r.trades), reverse=True)


# --------------------------------------------------------------------------- #
# trade updates (follow-up posts)
# --------------------------------------------------------------------------- #
async def create_update(
    session: AsyncSession,
    *,
    symbol: str,
    action: str,
    percent: int = 100,
    price: Optional[Decimal] = None,
    to_entry: bool = False,
    trade_id: Optional[int] = None,
    signal_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    status: UpdateStatus = UpdateStatus.PENDING,
    raw_text: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> TradeUpdate:
    update = TradeUpdate(
        symbol=symbol, action=action, percent=percent, price=price, to_entry=to_entry,
        trade_id=trade_id, signal_id=signal_id, channel_id=channel_id,
        status=status.value, raw_text=raw_text, expires_at=expires_at,
    )
    session.add(update)
    await session.flush()
    return update


async def get_update(session: AsyncSession, update_id: int) -> Optional[TradeUpdate]:
    return await session.get(TradeUpdate, update_id)


async def pending_updates(session: AsyncSession) -> list[TradeUpdate]:
    result = await session.execute(
        select(TradeUpdate)
        .where(TradeUpdate.status == UpdateStatus.PENDING.value)
        .order_by(TradeUpdate.id)
    )
    return list(result.scalars())


async def close_update(
    session: AsyncSession, update: TradeUpdate, status: UpdateStatus, note: Optional[str] = None
) -> None:
    update.status = status.value
    if note:
        update.note = note[:255]


# --------------------------------------------------------------------------- #
# assets (halal whitelist)
# --------------------------------------------------------------------------- #
async def list_assets(session: AsyncSession, only_halal: bool = True) -> list[Asset]:
    stmt = select(Asset).order_by(Asset.symbol)
    if only_halal:
        stmt = stmt.where(Asset.is_halal.is_(True))
    return list((await session.execute(stmt)).scalars())


async def sync_assets(
    session: AsyncSession,
    symbols: Sequence[str],
    quote_asset: str,
    sources: Optional[dict[str, Optional[str]]] = None,
) -> None:
    """Make the assets table mirror the admin-configured whitelist.

    Symbols dropped from the config are flagged is_halal=False rather than
    deleted, so historical trades keep a readable reference.  A source given
    in the config is recorded; one that is missing leaves the stored one.
    """
    wanted = {s.upper() for s in symbols}
    sources = {k.upper(): v for k, v in (sources or {}).items()}
    existing = {a.symbol: a for a in await list_assets(session, only_halal=False)}

    for symbol in wanted:
        asset = existing.get(symbol)
        base = symbol[: -len(quote_asset)] if symbol.endswith(quote_asset) else symbol
        if asset is None:
            asset = Asset(symbol=symbol, base_asset=base, is_halal=True, note="from HALAL_COINS")
            session.add(asset)
        elif not asset.is_halal:
            asset.is_halal = True
            asset.note = "from HALAL_COINS"
        if sources.get(symbol):
            asset.source = sources[symbol]

    for symbol, asset in existing.items():
        if symbol not in wanted and asset.is_halal:
            asset.is_halal = False
            asset.note = "removed from HALAL_COINS"
    await session.flush()
