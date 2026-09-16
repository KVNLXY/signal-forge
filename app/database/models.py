"""Database models.

The five tables of the spec - channels, messages, signals, trades, assets -
plus trade_updates for the follow-up posts a channel makes about a running
trade.  Money is stored as text and converted to :class:`Decimal` so no value
ever passes through a binary float.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Attach UTC to a naive timestamp (SQLite hands datetimes back naive)."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Money(TypeDecorator):
    """Exact decimal stored as text (identical on PostgreSQL and SQLite)."""

    impl = String(48)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[str]:
        if value is None:
            return None
        return format(Decimal(str(value)), "f")

    def process_result_value(self, value: Any, dialect: Any) -> Optional[Decimal]:
        if value is None:
            return None
        return Decimal(value)


class SignalStatus(str, enum.Enum):
    PENDING = "PENDING"      # read from an image, waiting for the admin OK
    WAITING = "WAITING"      # accepted, waiting for the entry price
    TRIGGERED = "TRIGGERED"  # a trade was opened from it
    SKIPPED = "SKIPPED"      # rejected by a rule (see reject_reason)
    EXPIRED = "EXPIRED"      # entry never reached within SIGNAL_EXPIRY_MINUTES


class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class UpdateStatus(str, enum.Enum):
    PENDING = "PENDING"      # read from a channel post, waiting for the admin OK
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"    # by the admin
    EXPIRED = "EXPIRED"      # nobody pressed Confirm in time
    FAILED = "FAILED"        # confirmed, but could not be carried out (see note)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identifier: Mapped[str] = mapped_column(String(128), unique=True)  # as configured
    tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    messages: Mapped[list["Message"]] = relationship(back_populates="channel")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("channel_id", "tg_message_id", name="uq_message_per_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[Optional[int]] = mapped_column(ForeignKey("channels.id"), nullable=True)
    tg_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_signal: Mapped[bool] = mapped_column(Boolean, default=False)

    channel: Mapped[Optional[Channel]] = relationship(back_populates="messages")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)
    channel_id: Mapped[Optional[int]] = mapped_column(ForeignKey("channels.id"), nullable=True)

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))  # always BUY; SHORT never gets here
    entry_low: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    entry_high: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    sl_price: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    tp_prices: Mapped[list[str]] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(16), default=SignalStatus.WAITING.value, index=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    from_image: Mapped[bool] = mapped_column(Boolean, default=False)
    # breakout: the price is expected to rise into the zone (a move that is
    # already above it was missed).  limit: the entry sits below the live
    # price and the bot waits for the pullback, like a resting buy order.
    entry_mode: Mapped[str] = mapped_column(String(10), default="breakout")

    trades: Mapped[list["Trade"]] = relationship(back_populates="signal")

    @property
    def take_profits(self) -> list[Decimal]:
        return [Decimal(p) for p in (self.tp_prices or [])]


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("signals.id"), nullable=True)

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(8))  # PAPER | LIVE
    status: Mapped[str] = mapped_column(String(16), default=TradeStatus.OPEN.value, index=True)

    entry_price: Mapped[Decimal] = mapped_column(Money)
    quantity: Mapped[Decimal] = mapped_column(Money)           # base asset bought
    remaining_qty: Mapped[Decimal] = mapped_column(Money)      # not yet sold
    quote_spent: Mapped[Decimal] = mapped_column(Money)        # USDT actually spent
    realized_quote: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))

    sl_price: Mapped[Decimal] = mapped_column(Money)
    tp_prices: Mapped[list[str]] = mapped_column(JSON, default=list)
    tp_splits: Mapped[list[str]] = mapped_column(JSON, default=list)
    tp_hit: Mapped[int] = mapped_column(Integer, default=0)

    exits: Mapped[list[dict]] = mapped_column(JSON, default=list)
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)  # avg
    pnl_usdt: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    pnl_percent: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)
    close_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    entry_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    signal: Mapped[Optional[Signal]] = relationship(back_populates="trades")

    @property
    def take_profits(self) -> list[Decimal]:
        return [Decimal(p) for p in (self.tp_prices or [])]

    @property
    def splits(self) -> list[Decimal]:
        return [Decimal(s) for s in (self.tp_splits or [])]


class TradeUpdate(Base):
    """A follow-up instruction from a channel about a running trade or a
    waiting signal: sell (part), move the stop, cancel.  Recorded first, acted
    on after the admin's Confirm (or at once when UPDATE_CONFIRMATION=false)."""

    __tablename__ = "trade_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[Optional[int]] = mapped_column(ForeignKey("trades.id"), nullable=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("signals.id"), nullable=True)
    channel_id: Mapped[Optional[int]] = mapped_column(ForeignKey("channels.id"), nullable=True)

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(10))            # close | stop | cancel
    percent: Mapped[int] = mapped_column(Integer, default=100)  # close: share of what is left
    price: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)  # stop: new level
    to_entry: Mapped[bool] = mapped_column(Boolean, default=False)         # stop: breakeven

    status: Mapped[str] = mapped_column(String(16), default=UpdateStatus.PENDING.value, index=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Asset(Base):
    """The halal whitelist.  Only an admin ever writes here."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    base_asset: Mapped[str] = mapped_column(String(24))
    is_halal: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Where the ruling came from (a screening page, a scholar, a date) - the
    # admin writes it in HALAL_COINS; it is recorded, never interpreted.
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
