"""Sharlife cross-check: parsing, ticker collisions, cache behaviour."""

from __future__ import annotations

import json
import time

import httpx

from app.halal.sharlife import SharlifeIndex, parse_listing

ROW = (
    '<tr class="crypto-row search-item" style="" onclick="window.location=\'/crypto-shariah/crypto/{slug}\'" '
    'data-asset-id="x" data-name="{name} {name}" data-ticker="{ticker} {ticker}" data-price="1" '
    'data-change="0" data-market-cap="{cap}" data-status="{status}"><td></td></tr>'
)

HTML = "<table>" + "".join([
    ROW.format(slug="chainlink", name="Chainlink", ticker="LINK", cap="8562009542", status="Shariah"),
    ROW.format(slug="plume", name="Plume", ticker="PLUME", cap="100", status="Grey"),
    ROW.format(slug="unus-sed-leo", name="Unus Sed Leo", ticker="LEO", cap="9", status="Non-Shariah"),
    ROW.format(slug="sophia-verse", name="SophiaVerse", ticker="SOPH", cap="5", status="Grey"),
    ROW.format(slug="sophon", name="Sophon", ticker="SOPH", cap="500", status="Shariah"),
    ROW.format(slug="new-thing", name="New Thing", ticker="NEWT", cap="1", status=""),
]) + "</table>"


def test_rows_are_parsed_with_their_status():
    entries = {e.ticker: e for e in parse_listing(HTML) if e.ticker != "SOPH"}
    assert entries["LINK"].name == "Chainlink"
    assert entries["LINK"].status == "passed"
    assert entries["LINK"].url.endswith("/crypto-shariah/crypto/chainlink")
    assert entries["PLUME"].status == "grey"
    assert entries["LEO"].status == "failed"
    assert entries["NEWT"].status == "unrated"


def test_a_ticker_collision_keeps_every_project_largest_first():
    index = SharlifeIndex(parse_listing(HTML))
    hits = index.lookup("soph")
    assert [h.name for h in hits] == ["Sophon", "SophiaVerse"]
    assert index.lookup("NOPE") == []


async def test_load_uses_a_fresh_cache_and_refreshes_a_stale_one(tmp_path):
    cache = tmp_path / "sharlife.json"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=HTML)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = await SharlifeIndex.load(cache, client=client)
    assert len(first) == 6 and calls["n"] == 1

    again = await SharlifeIndex.load(cache, client=client)
    assert len(again) == 6 and calls["n"] == 1           # served from the cache

    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["fetched_at"] = time.time() - 30 * 24 * 3600  # a month old
    cache.write_text(json.dumps(payload), encoding="utf-8")
    refreshed = await SharlifeIndex.load(cache, client=client)
    assert len(refreshed) == 6 and calls["n"] == 2
    await client.aclose()


async def test_a_failed_refresh_falls_back_to_the_stale_cache(tmp_path):
    cache = tmp_path / "sharlife.json"
    cache.write_text(json.dumps({"fetched_at": 0, "entries": [
        {"ticker": "LINK", "name": "Chainlink", "slug": "chainlink", "status": "passed", "market_cap": 1.0}
    ]}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    index = await SharlifeIndex.load(cache, client=client)
    assert index.lookup("LINK")[0].status == "passed"
    await client.aclose()
