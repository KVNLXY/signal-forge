"""MEXC client tests - all mocked, no request ever leaves the process."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.mexc.client import MexcAPIError, MexcClient, MexcError
from app.mexc.market import MarketData
from app.mexc.orders import OrderClient

SECRET = "test-secret-value"
KEY = "test-api-key"

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "1",
            "baseAsset": "BTC",
            "baseAssetPrecision": 6,
            "quoteAsset": "USDT",
            "quotePrecision": 2,
            "quoteAssetPrecision": 2,
            "baseSizePrecision": "0.000001",
            "quoteAmountPrecision": "5",
            "isSpotTradingAllowed": True,
            "orderTypes": ["LIMIT", "MARKET"],
            "filters": [],
        }
    ]
}


class Recorder:
    """Collects the requests a test makes and replies with canned payloads."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = responses or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(200, json={})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def query(self, index: int = -1) -> dict[str, str]:
        raw = self.requests[index].url.query.decode()
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_client(recorder: Recorder, **kwargs) -> MexcClient:
    return MexcClient(
        api_key=KEY, api_secret=SECRET, transport=recorder.transport(), **kwargs
    )


# --------------------------------------------------------------------------- #
# signing
# --------------------------------------------------------------------------- #
async def test_signed_request_carries_a_correct_hmac_and_the_api_key_header():
    recorder = Recorder([httpx.Response(200, json={"orderId": 1})])
    client = make_client(recorder)

    await client.request("POST", "/api/v3/order", {"symbol": "BTCUSDT"}, signed=True)
    await client.close()

    raw = recorder.last.url.query.decode()
    body, _, signature = raw.partition("&signature=")
    expected = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()

    assert signature == expected
    assert recorder.last.headers["X-MEXC-APIKEY"] == KEY
    params = recorder.query()
    assert params["symbol"] == "BTCUSDT"
    assert "timestamp" in params and params["recvWindow"] == "5000"
    assert SECRET not in raw           # the secret itself is never transmitted


async def test_public_request_is_not_signed():
    recorder = Recorder([httpx.Response(200, json={"symbol": "BTCUSDT", "price": "100"})])
    client = make_client(recorder)

    await client.request("GET", "/api/v3/ticker/price", {"symbol": "BTCUSDT"})
    await client.close()

    assert "signature" not in recorder.last.url.query.decode()
    assert "X-MEXC-APIKEY" not in recorder.last.headers


async def test_business_error_payload_becomes_an_exception():
    recorder = Recorder([
        httpx.Response(400, json={"code": 700002, "msg": "Signature for this request is not valid"})
    ])
    client = make_client(recorder)

    with pytest.raises(MexcAPIError) as excinfo:
        await client.request("POST", "/api/v3/order", {"symbol": "BTCUSDT"}, signed=True)
    await client.close()

    assert excinfo.value.code == 700002
    assert "Signature" in excinfo.value.message


async def test_server_error_is_retried(monkeypatch):
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.mexc.client.asyncio.sleep", no_sleep)
    recorder = Recorder([
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, json={"serverTime": 1700000000000}),
    ])
    client = make_client(recorder)

    payload = await client.request("GET", "/api/v3/time")
    await client.close()

    assert payload["serverTime"] == 1700000000000
    assert len(recorder.requests) == 2


async def test_missing_credentials_block_signed_calls():
    recorder = Recorder()
    client = MexcClient(transport=recorder.transport())
    with pytest.raises(Exception):
        await client.request("GET", "/api/v3/account", signed=True)
    await client.close()
    assert recorder.requests == []


# --------------------------------------------------------------------------- #
# orders
# --------------------------------------------------------------------------- #
async def test_limit_buy_sends_the_documented_parameters():
    recorder = Recorder([httpx.Response(200, json={
        "symbol": "BTCUSDT", "orderId": "9001", "price": "100500", "origQty": "0.001",
        "type": "LIMIT", "side": "BUY", "transactTime": 1700000000000,
    })])
    client = make_client(recorder)
    orders = OrderClient(client)

    result = await orders.buy_limit("BTCUSDT", Decimal("0.001"), Decimal("100500"))
    await client.close()

    assert recorder.last.method == "POST"
    assert recorder.last.url.path == "/api/v3/order"
    params = recorder.query()
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "BUY"
    assert params["type"] == "LIMIT"
    assert params["quantity"] == "0.001"
    assert params["price"] == "100500"
    assert "quoteOrderQty" not in params        # empty values are dropped
    assert result.order_id == "9001"


async def test_market_buy_uses_quote_order_qty():
    recorder = Recorder([httpx.Response(200, json={"orderId": "9002"})])
    client = make_client(recorder)
    orders = OrderClient(client)

    await orders.buy_market_quote("BTCUSDT", Decimal("100"))
    await client.close()

    params = recorder.query()
    assert params["type"] == "MARKET"
    assert params["quoteOrderQty"] == "100"
    assert "quantity" not in params
    assert "price" not in params


async def test_query_and_cancel_use_the_order_id():
    recorder = Recorder([
        httpx.Response(200, json={
            "symbol": "BTCUSDT", "orderId": "9003", "status": "FILLED",
            "origQty": "0.001", "executedQty": "0.001", "cummulativeQuoteQty": "100.5",
            "price": "100500",
        }),
        httpx.Response(200, json={"symbol": "BTCUSDT", "orderId": "9003", "status": "CANCELED"}),
    ])
    client = make_client(recorder)
    orders = OrderClient(client)

    filled = await orders.query_order("BTCUSDT", "9003")
    cancelled = await orders.cancel_order("BTCUSDT", "9003")
    await client.close()

    assert recorder.requests[0].method == "GET"
    assert recorder.requests[1].method == "DELETE"
    assert recorder.query(0)["orderId"] == "9003"
    assert filled.is_filled and filled.executed_qty == Decimal("0.001")
    assert filled.avg_price == Decimal("100.5") / Decimal("0.001")
    assert cancelled.status == "CANCELED"


async def test_free_balance_reads_the_account_endpoint():
    recorder = Recorder([httpx.Response(200, json={
        "canTrade": True, "canWithdraw": False,
        "balances": [{"asset": "USDT", "free": "250.5", "locked": "0"}],
    })])
    client = make_client(recorder)
    orders = OrderClient(client)

    balance = await orders.free_balance("USDT")
    await client.close()

    assert recorder.last.url.path == "/api/v3/account"
    assert balance == Decimal("250.5")


# --------------------------------------------------------------------------- #
# market data
# --------------------------------------------------------------------------- #
async def test_get_prices_filters_the_full_ticker_list():
    recorder = Recorder([httpx.Response(200, json=[
        {"symbol": "BTCUSDT", "price": "100500.12"},
        {"symbol": "ETHUSDT", "price": "3500.5"},
        {"symbol": "DOGEUSDT", "price": "0.1"},
    ])])
    client = make_client(recorder)
    market = MarketData(client)

    prices = await market.get_prices(["BTCUSDT", "ETHUSDT"])
    await client.close()

    assert prices == {"BTCUSDT": Decimal("100500.12"), "ETHUSDT": Decimal("3500.5")}
    assert "symbol" not in recorder.query()      # one call for every symbol


async def test_symbol_rules_and_rounding():
    recorder = Recorder([httpx.Response(200, json=EXCHANGE_INFO)])
    client = make_client(recorder)
    market = MarketData(client)

    rules = await market.get_rules("BTCUSDT")
    assert rules.price_step == Decimal("0.01")
    assert rules.qty_step == Decimal("0.000001")
    assert rules.min_quote_amount == Decimal("5")     # 5 USDT, not 1e-5
    assert rules.tradable

    # cached: no second exchangeInfo call
    assert await market.round_quantity("BTCUSDT", Decimal("0.00123456789")) == Decimal("0.001234")
    assert await market.round_price("BTCUSDT", Decimal("100500.129")) == Decimal("100500.12")
    await client.close()
    assert len(recorder.requests) == 1


async def test_unlisted_symbol_raises():
    recorder = Recorder([httpx.Response(200, json={"symbols": []})])
    client = make_client(recorder)
    market = MarketData(client)

    with pytest.raises(Exception):
        await market.get_rules("NOPEUSDT")
    await client.close()


async def test_book_ticker_gives_the_spread_in_percent():
    recorder = Recorder([
        httpx.Response(200, json={
            "symbol": "BTCUSDT", "bidPrice": "99900", "bidQty": "1",
            "askPrice": "100100", "askQty": "1",
        })
    ])
    client = make_client(recorder)
    market = MarketData(client)

    book = await market.get_book("btcusdt")
    await client.close()

    assert recorder.last.url.path == "/api/v3/ticker/bookTicker"
    assert recorder.query()["symbol"] == "BTCUSDT"
    assert (book.bid, book.ask) == (Decimal("99900"), Decimal("100100"))
    assert book.spread_percent == Decimal("0.2")


async def test_book_without_prices_raises():
    recorder = Recorder([httpx.Response(200, json={"symbol": "BTCUSDT"})])
    client = make_client(recorder)
    with pytest.raises(MexcError):
        await MarketData(client).get_book("BTCUSDT")
    await client.close()
