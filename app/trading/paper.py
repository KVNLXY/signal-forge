"""PAPER execution: real MEXC prices, simulated orders.

Nothing here ever touches a private endpoint, so a paper run cannot place an
order even if the API key happens to have trading rights.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Optional

from app.mexc.market import MarketData
from app.trading.base import PAPER, Fill

log = logging.getLogger(__name__)


class PaperExecutor:
    """Fills at the price the engine observed, minus a configurable fee."""

    mode = PAPER

    def __init__(self, market: MarketData, fee_rate: Decimal = Decimal("0.0005")) -> None:
        self._market = market
        self._fee_rate = fee_rate

    async def buy(self, symbol: str, quote_amount: Decimal, price: Decimal) -> Optional[Fill]:
        if price <= 0:
            log.warning("paper buy skipped: invalid price %s for %s", price, symbol)
            return None
        fee = quote_amount * self._fee_rate
        quantity = (quote_amount - fee) / price
        fill = Fill(
            quantity=quantity,
            quote=quote_amount,          # USDT leaving the (virtual) balance
            price=price,
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            fee=fee,
        )
        log.info("PAPER BUY %s qty=%s @ %s (spent %s USDT, fee %s)",
                 symbol, quantity, price, quote_amount, fee)
        return fill

    async def holdings(self, symbol: str) -> Optional[Decimal]:
        return None                      # nothing real to compare against

    async def sell(self, symbol: str, quantity: Decimal, price: Decimal) -> Optional[Fill]:
        if price <= 0 or quantity <= 0:
            log.warning("paper sell skipped: qty=%s price=%s for %s", quantity, price, symbol)
            return None
        gross = quantity * price
        fee = gross * self._fee_rate
        fill = Fill(
            quantity=quantity,
            quote=gross - fee,           # USDT returning to the (virtual) balance
            price=price,
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            fee=fee,
        )
        log.info("PAPER SELL %s qty=%s @ %s (received %s USDT, fee %s)",
                 symbol, quantity, price, gross - fee, fee)
        return fill
