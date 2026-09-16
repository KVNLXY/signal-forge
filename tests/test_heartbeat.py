"""The dead man's switch: pinged after a good price-loop pass, never after a
failed one, and rate-limited so a 5-second loop does not flood the monitor."""

from __future__ import annotations

import asyncio

import httpx

from app.heartbeat import Heartbeat
from tests.conftest import BTC_LONG


class Pings:
    def __init__(self, status: int = 200) -> None:
        self.urls: list[str] = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return httpx.Response(self.status)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def test_ping_is_rate_limited_to_the_interval():
    pings = Pings()
    beat = Heartbeat("https://hc-ping.com/abc", interval_seconds=60, client=pings.client())

    assert await beat.beat()
    assert not await beat.beat()                     # too soon
    assert await beat.beat(force=True)               # the pre-flight check
    assert pings.urls == ["https://hc-ping.com/abc"] * 2
    await beat.close()


async def test_disabled_without_a_url():
    beat = Heartbeat("", client=Pings().client())
    assert not beat.enabled
    assert not await beat.beat(force=True)
    await beat.close()


async def test_a_failing_endpoint_never_raises():
    pings = Pings(status=500)
    beat = Heartbeat("https://hc-ping.com/abc", client=pings.client())
    assert not await beat.beat(force=True)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    beat = Heartbeat("https://hc-ping.com/abc",
                     client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    assert not await beat.beat(force=True)
    await beat.close()


class RecordingHeartbeat(Heartbeat):
    def __init__(self) -> None:
        super().__init__("https://hc-ping.com/abc", interval_seconds=1)
        self.beats = 0

    async def beat(self, force: bool = False) -> bool:
        self.beats += 1
        return True


async def run_loop_briefly(engine, seconds: float) -> None:
    task = asyncio.create_task(engine.run_price_loop())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_price_loop_beats_only_after_a_successful_tick(settings, database, market, notifier, whitelist):
    from app.trading.engine import TradingEngine
    from app.trading.paper import PaperExecutor

    heartbeat = RecordingHeartbeat()
    settings.price_poll_seconds = 1
    engine = TradingEngine(
        settings=settings, database=database, market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier, whitelist=whitelist, heartbeat=heartbeat,
    )
    market.set("BTCUSDT", 99000)
    await engine.handle_message(BTC_LONG, tg_message_id=1)

    await run_loop_briefly(engine, 0.3)
    assert heartbeat.beats == 1                      # the first pass

    # Prices unavailable -> the tick raises -> no beat.
    async def broken(symbols=None):
        raise RuntimeError("mexc down")

    market.get_prices = broken
    await run_loop_briefly(engine, 0.3)
    assert heartbeat.beats == 1
    assert notifier.find("price loop")
