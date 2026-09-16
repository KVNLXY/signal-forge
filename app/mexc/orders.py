"""MEXC Spot order endpoints.

Parameter names follow the official spot v3 docs:

    POST   /api/v3/order       symbol, side, type, quantity | quoteOrderQty,
                               price, newClientOrderId, recvWindow, timestamp
    GET    /api/v3/order       symbol, orderId | origClientOrderId
    DELETE /api/v3/order       symbol, orderId | origClientOrderId
    GET    /api/v3/openOrders  symbol
    GET    /api/v3/account     (balances)

Note that the new-order response only echoes the order, not its fill state,
so callers query the order afterwards to learn what was executed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from app.mexc.client import (
    PRIVATE_ACCOUNT,
    PRIVATE_OPEN_ORDERS,
    PRIVATE_ORDER,
    MexcClient,
    MexcError,
)

log = logging.getLogger(__name__)

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

TYPE_LIMIT = "LIMIT"
TYPE_MARKET = "MARKET"

STATUS_NEW = "NEW"
STATUS_FILLED = "FILLED"
STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"
STATUS_CANCELED = "CANCELED"
STATUS_PARTIALLY_CANCELED = "PARTIALLY_CANCELED"

DONE_STATUSES = {STATUS_FILLED, STATUS_CANCELED, STATUS_PARTIALLY_CANCELED}


def _num(value: Optional[Decimal]) -> Optional[str]:
    """Serialise a Decimal without scientific notation."""
    return None if value is None else format(value, "f")


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value)) if value is not None else Decimal(default)
    except Exception:  # pragma: no cover - malformed payload
        return Decimal(default)


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    symbol: str
    side: str
    status: str
    orig_qty: Decimal
    executed_qty: Decimal
    quote_qty: Decimal          # cummulativeQuoteQty: quote actually spent/received
    price: Decimal              # order price (0 for market orders)

    @property
    def is_filled(self) -> bool:
        return self.status == STATUS_FILLED

    @property
    def is_done(self) -> bool:
        return self.status in DONE_STATUSES

    @property
    def has_fill(self) -> bool:
        return self.executed_qty > 0

    @property
    def avg_price(self) -> Decimal:
        if self.executed_qty > 0 and self.quote_qty > 0:
            return self.quote_qty / self.executed_qty
        return self.price

    @classmethod
    def from_payload(cls, payload: dict, fallback_symbol: str = "", fallback_side: str = "") -> "OrderResult":
        return cls(
            order_id=str(payload.get("orderId", "")),
            symbol=str(payload.get("symbol", fallback_symbol)),
            side=str(payload.get("side", fallback_side)),
            status=str(payload.get("status", STATUS_NEW)),
            orig_qty=_dec(payload.get("origQty")),
            executed_qty=_dec(payload.get("executedQty")),
            quote_qty=_dec(payload.get("cummulativeQuoteQty")),
            price=_dec(payload.get("price")),
        )


class OrderClient:
    def __init__(self, client: MexcClient) -> None:
        self._client = client

    # ------------------------------------------------------------------ #
    # placing
    # ------------------------------------------------------------------ #
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[Decimal] = None,
        quote_order_qty: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        if order_type == TYPE_LIMIT and (quantity is None or price is None):
            raise MexcError("a LIMIT order needs both quantity and price")
        if order_type == TYPE_MARKET and quantity is None and quote_order_qty is None:
            raise MexcError("a MARKET order needs quantity or quoteOrderQty")

        params = {
            "symbol": symbol.upper(),
            "side": side,
            "type": order_type,
            "quantity": _num(quantity),
            "quoteOrderQty": _num(quote_order_qty),
            "price": _num(price),
            "newClientOrderId": client_order_id,
        }
        payload = await self._client.request("POST", PRIVATE_ORDER, params, signed=True)
        log.info(
            "MEXC order placed: %s %s %s qty=%s quote=%s price=%s id=%s",
            symbol, side, order_type, _num(quantity), _num(quote_order_qty), _num(price),
            payload.get("orderId"),
        )
        return OrderResult.from_payload(payload, symbol.upper(), side)

    async def buy_limit(
        self, symbol: str, quantity: Decimal, price: Decimal, client_order_id: Optional[str] = None
    ) -> OrderResult:
        return await self.place_order(
            symbol, SIDE_BUY, TYPE_LIMIT, quantity=quantity, price=price,
            client_order_id=client_order_id,
        )

    async def buy_market_quote(
        self, symbol: str, quote_amount: Decimal, client_order_id: Optional[str] = None
    ) -> OrderResult:
        """Spend a fixed amount of USDT at market price."""
        return await self.place_order(
            symbol, SIDE_BUY, TYPE_MARKET, quote_order_qty=quote_amount,
            client_order_id=client_order_id,
        )

    async def sell_limit(
        self, symbol: str, quantity: Decimal, price: Decimal, client_order_id: Optional[str] = None
    ) -> OrderResult:
        return await self.place_order(
            symbol, SIDE_SELL, TYPE_LIMIT, quantity=quantity, price=price,
            client_order_id=client_order_id,
        )

    async def sell_market(
        self, symbol: str, quantity: Decimal, client_order_id: Optional[str] = None
    ) -> OrderResult:
        return await self.place_order(
            symbol, SIDE_SELL, TYPE_MARKET, quantity=quantity, client_order_id=client_order_id,
        )

    # ------------------------------------------------------------------ #
    # managing
    # ------------------------------------------------------------------ #
    async def query_order(self, symbol: str, order_id: str) -> OrderResult:
        payload = await self._client.request(
            "GET", PRIVATE_ORDER, {"symbol": symbol.upper(), "orderId": order_id}, signed=True
        )
        return OrderResult.from_payload(payload, symbol.upper())

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        payload = await self._client.request(
            "DELETE", PRIVATE_ORDER, {"symbol": symbol.upper(), "orderId": order_id}, signed=True
        )
        log.info("MEXC order cancelled: %s %s", symbol, order_id)
        return OrderResult.from_payload(payload, symbol.upper())

    async def open_orders(self, symbol: str) -> list[OrderResult]:
        payload = await self._client.request(
            "GET", PRIVATE_OPEN_ORDERS, {"symbol": symbol.upper()}, signed=True
        )
        return [OrderResult.from_payload(item, symbol.upper()) for item in payload or []]

    # ------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------ #
    async def account(self) -> dict:
        return await self._client.request("GET", PRIVATE_ACCOUNT, signed=True)

    async def free_balance(self, asset: str) -> Decimal:
        data = await self.account()
        for balance in data.get("balances", []):
            if str(balance.get("asset", "")).upper() == asset.upper():
                return _dec(balance.get("free"))
        return Decimal(0)

    async def can_trade(self) -> bool:
        data = await self.account()
        return bool(data.get("canTrade", False))
