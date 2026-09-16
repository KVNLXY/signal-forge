"""Live best bid / ask over the MEXC spot WebSocket.

The price loop polls REST every PRICE_POLL_SECONDS; on a coin that drops
through the stop in two seconds that is two seconds of extra loss.  With
PRICE_FEED=websocket the loop reads from this feed instead - the exchange
pushes every change of the top of the book (`spot@public.aggre.bookTicker`)
and the loop only has to look at the cache.  REST stays the fallback: a
symbol whose last push is older than PRICE_FEED_MAX_AGE_SECONDS, or a feed
that is not connected, is priced over REST as before.

MEXC serves these streams as protobuf.  Only two messages matter here, so
they are decoded by hand from the wire format (protobuf-encoding is small:
a varint tag, then a varint / length-delimited value) instead of pulling in
a generated schema and its dependency chain:

    PushDataV3ApiWrapper  channel = 1 (string), symbol = 3 (string),
                          publicAggreBookTicker = 315 (message)
    PublicAggreBookTickerV3Api  bidPrice = 1, bidQuantity = 2,
                                askPrice = 3, askQuantity = 4 (strings)

Source: https://github.com/mexcdevelop/websocket-proto
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from app.mexc.market import BookTicker

log = logging.getLogger(__name__)

SPOT_WS_URL = "wss://wbs-api.mexc.com/ws"
BOOK_TICKER_TOPIC = "spot@public.aggre.bookTicker.v3.api.pb@100ms@{symbol}"
MAX_TOPICS_PER_CONNECTION = 30           # MEXC's documented limit per socket

_WRAPPER_CHANNEL = 1
_WRAPPER_SYMBOL = 3
_WRAPPER_AGGRE_BOOK_TICKER = 315
_BOOK_BID_PRICE = 1
_BOOK_ASK_PRICE = 3


# --------------------------------------------------------------------------- #
# protobuf wire format, just enough
# --------------------------------------------------------------------------- #
def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def decode_fields(data: bytes) -> dict[int, list[object]]:
    """Field number -> values (int for varints, bytes for length-delimited)."""
    fields: dict[int, list[object]] = {}
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 0x07
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _text(fields: dict[int, list[object]], number: int) -> Optional[str]:
    values = fields.get(number)
    if not values or not isinstance(values[-1], bytes):
        return None
    return values[-1].decode("utf-8", errors="replace")


def decode_book_ticker(frame: bytes) -> Optional[BookTicker]:
    """A wrapper frame -> the book it carries, or None for anything else."""
    try:
        wrapper = decode_fields(frame)
        body = wrapper.get(_WRAPPER_AGGRE_BOOK_TICKER)
        if not body or not isinstance(body[-1], bytes):
            return None
        book = decode_fields(body[-1])
        symbol = _text(wrapper, _WRAPPER_SYMBOL)
        bid, ask = _text(book, _BOOK_BID_PRICE), _text(book, _BOOK_ASK_PRICE)
        if not symbol or bid is None or ask is None:
            return None
        return BookTicker(symbol=symbol.upper(), bid=Decimal(bid), ask=Decimal(ask))
    except (ValueError, InvalidOperation, IndexError) as exc:
        log.debug("undecodable feed frame: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# the feed
# --------------------------------------------------------------------------- #
@dataclass
class _Quote:
    book: BookTicker
    at: float                             # monotonic seconds


class PriceFeed:
    """Keeps the latest top-of-book per symbol; the price loop reads it."""

    def __init__(self, max_age_seconds: float = 10.0, url: str = SPOT_WS_URL) -> None:
        self._max_age = max_age_seconds
        self._url = url
        self._quotes: dict[str, _Quote] = {}
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._changed = asyncio.Event()
        self._ws = None
        self._connected = False
        self._pushes = 0

    # ---- what the engine reads ------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def pushes(self) -> int:
        return self._pushes

    def book(self, symbol: str) -> Optional[BookTicker]:
        """The last pushed book, if it is fresh enough to act on."""
        quote = self._quotes.get(symbol.upper())
        if quote is None or time.monotonic() - quote.at > self._max_age:
            return None
        return quote.book

    def price(self, symbol: str) -> Optional[Decimal]:
        """Mid-price from the fresh book, or None -> the caller asks REST."""
        book = self.book(symbol)
        if book is None:
            return None
        return (book.bid + book.ask) / 2

    def prices(self, symbols: Iterable[str]) -> dict[str, Decimal]:
        out = {}
        for symbol in symbols:
            price = self.price(symbol)
            if price is not None:
                out[symbol.upper()] = price
        return out

    def watch(self, symbols: Iterable[str]) -> None:
        """Set the symbols to follow; the connection (re)subscribes itself."""
        wanted = {s.upper() for s in symbols}
        if len(wanted) > MAX_TOPICS_PER_CONNECTION:
            log.warning("feed: %d symbols wanted, following the first %d",
                        len(wanted), MAX_TOPICS_PER_CONNECTION)
            wanted = set(sorted(wanted)[:MAX_TOPICS_PER_CONNECTION])
        if wanted != self._wanted:
            self._wanted = wanted
            self._changed.set()

    # ---- the connection ------------------------------------------------- #
    def push(self, frame: bytes) -> Optional[BookTicker]:
        """Feed one raw frame in (also used by the tests).  A symbol that is
        no longer watched is dropped, however long the exchange keeps
        pushing it after the unsubscribe."""
        book = decode_book_ticker(frame)
        if book is not None and book.symbol in self._wanted:
            self._quotes[book.symbol] = _Quote(book, time.monotonic())
            self._pushes += 1
        return book

    async def run_forever(self) -> None:
        """Connect, keep the subscriptions in step with ``watch``, reconnect."""
        try:
            import websockets
        except ImportError:
            log.error("PRICE_FEED=websocket needs the 'websockets' package - falling back to REST")
            return
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._url, ping_interval=None, max_size=2**20) as ws:
                    self._ws = ws
                    self._connected = True
                    self._subscribed = set()
                    self._changed.set()
                    backoff = 1.0
                    log.info("price feed connected")
                    await asyncio.gather(self._receive(ws), self._maintain(ws))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("price feed dropped (%s: %s) - reconnecting in %.0fs",
                            type(exc).__name__, exc, backoff)
            finally:
                self._connected = False
                self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _receive(self, ws) -> None:
        async for frame in ws:
            if isinstance(frame, bytes):
                self.push(frame)
            elif isinstance(frame, str):
                self._on_text(frame)

    def _on_text(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        if data.get("msg") == "PONG":
            return
        if data.get("code") not in (None, 0):
            log.warning("price feed: %s", text[:200])
        else:
            log.debug("price feed: %s", text[:200])

    async def _maintain(self, ws) -> None:
        """Subscribe / unsubscribe as the watch list changes; ping every 20s
        (MEXC closes a socket that stays silent for 60s)."""
        while True:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=20.0)
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"method": "PING"}))
                continue
            self._changed.clear()
            add = sorted(self._wanted - self._subscribed)
            drop = sorted(self._subscribed - self._wanted)
            if add:
                await ws.send(json.dumps({
                    "method": "SUBSCRIPTION",
                    "params": [BOOK_TICKER_TOPIC.format(symbol=s) for s in add],
                }))
            if drop:
                await ws.send(json.dumps({
                    "method": "UNSUBSCRIPTION",
                    "params": [BOOK_TICKER_TOPIC.format(symbol=s) for s in drop],
                }))
                for symbol in drop:
                    self._quotes.pop(symbol, None)
            self._subscribed = set(self._wanted)
            if add or drop:
                log.info("price feed following %s", ", ".join(sorted(self._subscribed)) or "(nothing)")
