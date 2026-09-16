"""The WebSocket price feed: wire-format decoding, freshness, and the engine
reading the feed first and REST only for what the feed does not have."""

from __future__ import annotations

import time
from decimal import Decimal

from app.database import repository as repo
from app.mexc.feed import PriceFeed, decode_book_ticker, decode_fields
from app.trading.engine import TradingEngine
from app.trading.paper import PaperExecutor
from tests.conftest import BTC_LONG


# --------------------------------------------------------------------------- #
# a tiny protobuf encoder, so the frames under test are built, not pasted
# --------------------------------------------------------------------------- #
def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_bytes(number: int, payload: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(payload)) + payload


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def book_frame(symbol: str, bid: str, ask: str, extra: bytes = b"") -> bytes:
    body = (
        field_bytes(1, bid.encode()) + field_bytes(2, b"1.5")
        + field_bytes(3, ask.encode()) + field_bytes(4, b"0.7")
        + field_bytes(5, b"123") + field_varint(6, 1_700_000_000_000)
    )
    return (
        field_bytes(1, b"spot@public.aggre.bookTicker.v3.api.pb@100ms@" + symbol.encode())
        + field_bytes(315, body) + field_bytes(3, symbol.encode())
        + field_varint(5, 1_700_000_000_000) + field_varint(6, 1_700_000_000_001) + extra
    )


def test_wire_format_decoder_reads_the_fields_that_matter():
    fields = decode_fields(field_bytes(1, b"abc") + field_varint(300, 7) + field_bytes(1, b"def"))
    assert fields == {1: [b"abc", b"def"], 300: [7]}

    book = decode_book_ticker(book_frame("BTCUSDT", "76039.49", "76039.50"))
    assert book is not None
    assert (book.symbol, book.bid, book.ask) == ("BTCUSDT", Decimal("76039.49"), Decimal("76039.50"))

    # Another body type (a deals push) is not a book; garbage is not a crash.
    deals = field_bytes(1, b"spot@public.aggre.deals") + field_bytes(3, b"BTCUSDT") + field_bytes(314, b"\x0a\x01x")
    assert decode_book_ticker(deals) is None
    assert decode_book_ticker(b"\xff\xff\xff") is None
    assert decode_book_ticker(b"") is None
    # Unknown fields (a newer schema) are skipped, fixed-width ones too.
    extra = field_varint(99, 5) + varint((98 << 3) | 1) + b"\x00" * 8 + varint((97 << 3) | 5) + b"\x00" * 4
    assert decode_book_ticker(book_frame("ETHUSDT", "2412", "2412.1", extra=extra)).ask == Decimal("2412.1")


def test_feed_keeps_only_fresh_books_of_watched_symbols():
    feed = PriceFeed(max_age_seconds=5)
    feed.watch(["btcusdt"])

    assert feed.push(book_frame("ETHUSDT", "2400", "2401")) is not None   # decoded ...
    assert feed.price("ETHUSDT") is None                                   # ... but not watched
    assert feed.push(book_frame("BTCUSDT", "100", "102")).bid == Decimal("100")
    assert feed.price("BTCUSDT") == Decimal("101")
    assert feed.prices(["BTCUSDT", "ETHUSDT"]) == {"BTCUSDT": Decimal("101")}
    assert feed.pushes == 1

    feed._quotes["BTCUSDT"].at = time.monotonic() - 6                     # stale
    assert feed.price("BTCUSDT") is None and feed.book("BTCUSDT") is None


async def test_engine_reads_the_feed_and_falls_back_to_rest(settings, database, market, notifier, whitelist):
    feed = PriceFeed(max_age_seconds=5)
    engine = TradingEngine(
        settings=settings, database=database, market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier, whitelist=whitelist, price_feed=feed,
    )
    calls: list[set[str]] = []
    original = market.get_prices

    async def counting(symbols=None):
        calls.append(set(symbols or []))
        return await original(symbols)

    market.get_prices = counting

    # REST says 99000 (below the zone); the feed is empty -> REST is used.
    market.set("BTCUSDT", 99000)
    signal = await engine.handle_message(BTC_LONG, tg_message_id=1)
    assert signal.status == "WAITING"
    await engine.tick()
    assert feed._wanted == {"BTCUSDT"}                    # the loop told the feed what to follow
    assert calls[-1] == {"BTCUSDT"}

    # Now the feed has a fresh book inside the zone: no REST call, trade opens.
    feed.push(book_frame("BTCUSDT", "100499", "100501"))
    calls.clear()
    await engine.tick()
    assert calls == []
    async with database.session() as session:
        trades = await repo.open_trades(session)
    assert len(trades) == 1 and trades[0].entry_price == Decimal("100500")

    # The stop through the feed, with the spread gate reading the same book.
    feed.push(book_frame("BTCUSDT", "96999", "97001"))
    await engine.tick()
    async with database.session() as session:
        assert await repo.open_trades(session) == []
        assert (await repo.closed_trades(session))[0].close_reason == "SL"

    assert "websocket" in await engine.status_text()
