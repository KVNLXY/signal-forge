"""Shared execution types.

PAPER and LIVE differ only in what happens at the exchange; the engine talks
to both through this one interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

PAPER = "PAPER"
LIVE = "LIVE"


@dataclass(frozen=True)
class Fill:
    """The result of an executed buy or sell."""

    quantity: Decimal          # base asset bought / sold
    quote: Decimal             # quote spent (buy) or received (sell), net of fee
    price: Decimal             # average fill price
    order_id: Optional[str] = None
    fee: Decimal = Decimal(0)  # fee in quote asset
    partial: bool = False      # only part of the requested size was filled


@runtime_checkable
class Executor(Protocol):
    """What the engine needs from an execution backend."""

    mode: str

    async def buy(self, symbol: str, quote_amount: Decimal, price: Decimal) -> Optional[Fill]:
        """Spend ``quote_amount`` USDT on ``symbol`` around ``price``.

        Returns None when nothing was filled (e.g. a LIMIT order timed out) -
        the signal then stays WAITING and is re-checked on the next tick.
        """

    async def sell(self, symbol: str, quantity: Decimal, price: Decimal) -> Optional[Fill]:
        """Sell ``quantity`` of the base asset around ``price``."""

    async def holdings(self, symbol: str) -> Optional[Decimal]:
        """How much of the base asset the account really holds, or None when
        there is no account to ask (paper trading)."""
