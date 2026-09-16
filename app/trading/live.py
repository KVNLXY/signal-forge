"""LIVE execution: real MEXC Spot orders.

Only reachable when TRADING_MODE=LIVE.  Rules that keep this safe:

* LIMIT orders by default; ORDER_TIMEOUT_SECONDS then cancels an unfilled one
  and the signal goes back to WAITING (requirement 12).
* A partially filled buy is kept - the trade is opened with what was actually
  bought, never with a quantity we do not own.
* An exit is guaranteed: if the LIMIT sell does not fill in time it is
  cancelled and the remainder is sold at market, so an SL always closes.
* Sell sizes are capped by the real free balance, because MEXC takes the buy
  commission out of the base asset.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from app.mexc.client import MexcError
from app.mexc.market import MarketData
from app.mexc.orders import OrderClient, OrderResult
from app.trading.base import LIVE, Fill

log = logging.getLogger(__name__)


class InsufficientBalance(MexcError):
    pass


class LiveExecutor:
    mode = LIVE

    def __init__(
        self,
        market: MarketData,
        orders: OrderClient,
        quote_asset: str = "USDT",
        use_market_order: bool = False,
        order_timeout_seconds: int = 60,
        limit_slippage_percent: Decimal = Decimal("0.1"),
        poll_interval: float = 2.0,
    ) -> None:
        self._market = market
        self._orders = orders
        self._quote_asset = quote_asset.upper()
        self._use_market_order = use_market_order
        self._timeout = order_timeout_seconds
        self._slippage = limit_slippage_percent / Decimal(100)
        self._poll_interval = poll_interval

    # ------------------------------------------------------------------ #
    # buy
    # ------------------------------------------------------------------ #
    async def buy(self, symbol: str, quote_amount: Decimal, price: Decimal) -> Optional[Fill]:
        rules = await self._market.get_rules(symbol)
        if not rules.tradable:
            raise MexcError(f"{symbol} is not tradable on MEXC spot right now")
        if quote_amount < rules.min_quote_amount:
            raise MexcError(
                f"TRADE_AMOUNT_USDT {quote_amount} is below the {symbol} minimum "
                f"of {rules.min_quote_amount} {rules.quote_asset}"
            )

        free = await self._orders.free_balance(self._quote_asset)
        if free < quote_amount:
            raise InsufficientBalance(
                f"insufficient {self._quote_asset}: need {quote_amount}, have {free}"
            )

        if self._use_market_order:
            order = await self._orders.buy_market_quote(symbol, quote_amount)
            final = await self._wait_for_fill(symbol, order.order_id, order)
        else:
            limit_price = self._market.round_step(
                price * (Decimal(1) + self._slippage), rules.price_step
            )
            quantity = self._market.round_step(quote_amount / limit_price, rules.qty_step)
            if quantity <= 0:
                raise MexcError(
                    f"{quote_amount} {self._quote_asset} is too small to buy one "
                    f"{rules.qty_step} step of {symbol}"
                )
            order = await self._orders.buy_limit(symbol, quantity, limit_price)
            final = await self._wait_for_fill(symbol, order.order_id, order)
            if not final.is_filled:
                final = await self._cancel_and_settle(symbol, order.order_id, final)

        if final.executed_qty <= 0:
            log.info("LIVE BUY %s not filled within %ss - signal stays waiting",
                     symbol, self._timeout)
            return None

        quote_spent = final.quote_qty if final.quote_qty > 0 else final.executed_qty * price
        fill = Fill(
            quantity=final.executed_qty,
            quote=quote_spent,
            price=final.avg_price if final.avg_price > 0 else price,
            order_id=final.order_id,
            partial=not final.is_filled,
        )
        log.info("LIVE BUY %s qty=%s @ %s (spent %s)",
                 symbol, fill.quantity, fill.price, fill.quote)
        return fill

    async def holdings(self, symbol: str) -> Optional[Decimal]:
        rules = await self._market.get_rules(symbol)
        return await self._orders.free_balance(
            rules.base_asset or symbol.replace(self._quote_asset, "")
        )

    # ------------------------------------------------------------------ #
    # sell
    # ------------------------------------------------------------------ #
    async def sell(self, symbol: str, quantity: Decimal, price: Decimal) -> Optional[Fill]:
        rules = await self._market.get_rules(symbol)

        # The buy commission is taken in the base asset, so the position can be
        # a hair smaller than the recorded quantity.
        free = await self._orders.free_balance(rules.base_asset or symbol.replace(self._quote_asset, ""))
        sellable = self._market.round_step(min(quantity, free) if free > 0 else quantity,
                                           rules.qty_step)
        if sellable <= 0:
            log.warning("LIVE SELL %s: nothing sellable (wanted %s, free %s)",
                        symbol, quantity, free)
            return None

        if self._use_market_order:
            order = await self._orders.sell_market(symbol, sellable)
            final = await self._wait_for_fill(symbol, order.order_id, order)
        else:
            limit_price = self._market.round_step(
                price * (Decimal(1) - self._slippage), rules.price_step
            )
            order = await self._orders.sell_limit(symbol, sellable, limit_price)
            final = await self._wait_for_fill(symbol, order.order_id, order)
            if not final.is_filled:
                final = await self._cancel_and_settle(symbol, order.order_id, final)
                remainder = self._market.round_step(sellable - final.executed_qty, rules.qty_step)
                if remainder > 0 and remainder * price >= rules.min_quote_amount:
                    # An exit must not be left hanging - finish it at market.
                    log.warning("LIVE SELL %s: limit unfilled, selling %s at market",
                                symbol, remainder)
                    market_order = await self._orders.sell_market(symbol, remainder)
                    market_final = await self._wait_for_fill(
                        symbol, market_order.order_id, market_order
                    )
                    final = self._merge(final, market_final)

        if final.executed_qty <= 0:
            log.warning("LIVE SELL %s did not fill", symbol)
            return None

        received = final.quote_qty if final.quote_qty > 0 else final.executed_qty * price
        fill = Fill(
            quantity=final.executed_qty,
            quote=received,
            price=final.avg_price if final.avg_price > 0 else price,
            order_id=final.order_id,
            partial=final.executed_qty < sellable,
        )
        log.info("LIVE SELL %s qty=%s @ %s (received %s)",
                 symbol, fill.quantity, fill.price, fill.quote)
        return fill

    # ------------------------------------------------------------------ #
    # order lifecycle helpers
    # ------------------------------------------------------------------ #
    async def _wait_for_fill(
        self, symbol: str, order_id: str, placed: OrderResult
    ) -> OrderResult:
        """Poll the order until it is done or ORDER_TIMEOUT_SECONDS passes."""
        if not order_id:
            return placed
        deadline = time.monotonic() + self._timeout
        last = placed
        while True:
            try:
                last = await self._orders.query_order(symbol, order_id)
            except MexcError as exc:
                log.warning("could not query order %s %s: %s", symbol, order_id, exc)
            if last.is_done:
                return last
            if time.monotonic() >= deadline:
                return last
            await asyncio.sleep(self._poll_interval)

    async def _cancel_and_settle(
        self, symbol: str, order_id: str, current: OrderResult
    ) -> OrderResult:
        """Cancel a stale order and report what it actually filled."""
        try:
            await self._orders.cancel_order(symbol, order_id)
        except MexcError as exc:
            # Already filled or already gone - the query below tells the truth.
            log.info("cancel of %s %s reported: %s", symbol, order_id, exc)
        try:
            return await self._orders.query_order(symbol, order_id)
        except MexcError as exc:  # pragma: no cover - defensive
            log.warning("could not re-query cancelled order %s %s: %s", symbol, order_id, exc)
            return current

    @staticmethod
    def _merge(first: OrderResult, second: OrderResult) -> OrderResult:
        """Combine a partial limit fill with the market fill that finished it."""
        from dataclasses import replace

        return replace(
            second,
            executed_qty=first.executed_qty + second.executed_qty,
            quote_qty=first.quote_qty + second.quote_qty,
        )
