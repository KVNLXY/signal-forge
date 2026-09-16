"""MEXC market data: prices and symbol trading rules.

Used by both PAPER and LIVE mode - paper trading uses real prices, it only
skips the order endpoints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Iterable, Optional

from app.mexc.client import (
    PUBLIC_BOOK_TICKER,
    PUBLIC_EXCHANGE_INFO,
    PUBLIC_TICKER_PRICE,
    MexcClient,
    MexcError,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SymbolRules:
    """Everything needed to size an order correctly for one symbol."""

    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    price_step: Decimal          # smallest price increment
    qty_step: Decimal            # smallest base-quantity increment
    min_quote_amount: Decimal    # smallest order value in quote asset
    spot_trading_allowed: bool

    @property
    def tradable(self) -> bool:
        # MEXC reports an enabled spot symbol as status "1" or "ENABLED"
        return self.spot_trading_allowed and str(self.status).upper() in {"1", "ENABLED", "TRADING"}


@dataclass(frozen=True)
class BookTicker:
    """Best bid and ask - what a taker actually pays and receives."""

    symbol: str
    bid: Decimal
    ask: Decimal

    @property
    def spread_percent(self) -> Decimal:
        """(ask - bid) / mid, in percent.  A crossed or empty book is 0."""
        mid = (self.ask + self.bid) / 2
        if mid <= 0 or self.ask < self.bid:
            return Decimal(0)
        return (self.ask - self.bid) / mid * Decimal(100)


def _step_from_digits(digits: object, fallback: Decimal) -> Decimal:
    """quotePrecision-style field: a number of decimal places -> a step."""
    try:
        places = int(str(digits).strip())
    except (TypeError, ValueError):
        return fallback
    if places < 0 or places > 18:
        return fallback
    return Decimal(1).scaleb(-places)


def _decimal_or(value: object, fallback: Decimal) -> Decimal:
    """baseSizePrecision / quoteAmountPrecision: already a size, not a count."""
    if value is None:
        return fallback
    try:
        parsed = Decimal(str(value).strip())
    except Exception:  # pragma: no cover - malformed payload
        return fallback
    return parsed if parsed > 0 else fallback


class MarketData:
    def __init__(self, client: MexcClient) -> None:
        self._client = client
        self._rules: dict[str, SymbolRules] = {}

    # ------------------------------------------------------------------ #
    # prices
    # ------------------------------------------------------------------ #
    async def get_price(self, symbol: str) -> Decimal:
        data = await self._client.request(
            "GET", PUBLIC_TICKER_PRICE, {"symbol": symbol.upper()}
        )
        if isinstance(data, list):  # defensive: MEXC returns a list without symbol
            data = data[0] if data else {}
        price = data.get("price") if isinstance(data, dict) else None
        if price is None:
            raise MexcError(f"no price returned for {symbol}")
        return Decimal(str(price))

    async def get_prices(self, symbols: Optional[Iterable[str]] = None) -> dict[str, Decimal]:
        """Prices for many symbols in a single call (one request per poll)."""
        wanted = {s.upper() for s in symbols} if symbols is not None else None
        if wanted is not None and not wanted:
            return {}
        data = await self._client.request("GET", PUBLIC_TICKER_PRICE)
        if isinstance(data, dict):
            data = [data]
        out: dict[str, Decimal] = {}
        for item in data:
            symbol = str(item.get("symbol", "")).upper()
            if wanted is not None and symbol not in wanted:
                continue
            raw = item.get("price")
            if raw is None:
                continue
            try:
                out[symbol] = Decimal(str(raw))
            except Exception:  # pragma: no cover - malformed payload
                continue
        return out

    async def get_book(self, symbol: str) -> BookTicker:
        """Best bid / ask for one symbol."""
        data = await self._client.request(
            "GET", PUBLIC_BOOK_TICKER, {"symbol": symbol.upper()}
        )
        if isinstance(data, list):
            data = data[0] if data else {}
        bid, ask = data.get("bidPrice"), data.get("askPrice")
        if bid is None or ask is None:
            raise MexcError(f"no order book returned for {symbol}")
        return BookTicker(symbol=symbol.upper(), bid=Decimal(str(bid)), ask=Decimal(str(ask)))

    # ------------------------------------------------------------------ #
    # symbol rules
    # ------------------------------------------------------------------ #
    async def get_rules(self, symbol: str, refresh: bool = False) -> SymbolRules:
        symbol = symbol.upper()
        if not refresh and symbol in self._rules:
            return self._rules[symbol]

        payload = await self._client.request("GET", PUBLIC_EXCHANGE_INFO, {"symbol": symbol})
        entries = payload.get("symbols") or []
        entry = next((e for e in entries if str(e.get("symbol", "")).upper() == symbol), None)
        if entry is None:
            raise MexcError(f"symbol {symbol} is not listed on MEXC spot")

        # quotePrecision counts decimal places; baseSizePrecision is a step and
        # quoteAmountPrecision is the smallest order value in quote currency.
        price_step = _step_from_digits(entry.get("quotePrecision"), Decimal("0.00000001"))
        qty_step = _decimal_or(
            entry.get("baseSizePrecision"),
            _step_from_digits(entry.get("baseAssetPrecision"), Decimal("0.00000001")),
        )
        min_quote = _decimal_or(entry.get("quoteAmountPrecision"), Decimal("1"))

        # Some symbols also carry Binance-style filters; they win when present.
        for flt in entry.get("filters") or []:
            kind = flt.get("filterType")
            if kind == "PRICE_FILTER" and flt.get("tickSize"):
                price_step = Decimal(str(flt["tickSize"]))
            elif kind == "LOT_SIZE" and flt.get("stepSize"):
                qty_step = Decimal(str(flt["stepSize"]))
            elif kind in {"MIN_NOTIONAL", "NOTIONAL"} and flt.get("minNotional"):
                min_quote = Decimal(str(flt["minNotional"]))

        rules = SymbolRules(
            symbol=symbol,
            status=str(entry.get("status", "")),
            base_asset=str(entry.get("baseAsset", "")),
            quote_asset=str(entry.get("quoteAsset", "")),
            price_step=price_step,
            qty_step=qty_step,
            min_quote_amount=min_quote,
            spot_trading_allowed=bool(entry.get("isSpotTradingAllowed", True)),
        )
        self._rules[symbol] = rules
        log.info(
            "MEXC rules %s: price_step=%s qty_step=%s min_quote=%s tradable=%s",
            rules.symbol, rules.price_step, rules.qty_step, rules.min_quote_amount, rules.tradable,
        )
        return rules

    # ------------------------------------------------------------------ #
    # rounding
    # ------------------------------------------------------------------ #
    @staticmethod
    def round_step(value: Decimal, step: Decimal) -> Decimal:
        """Round down onto the exchange grid (never ask for more than we have)."""
        if step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    async def round_quantity(self, symbol: str, quantity: Decimal) -> Decimal:
        rules = await self.get_rules(symbol)
        return self.round_step(quantity, rules.qty_step)

    async def round_price(self, symbol: str, price: Decimal) -> Decimal:
        rules = await self.get_rules(symbol)
        return self.round_step(price, rules.price_step)
