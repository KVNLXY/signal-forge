"""Executor tests: PAPER fills and the LIVE order lifecycle (fully mocked)."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import pytest

from app.mexc.orders import OrderResult
from app.trading.base import Fill
from app.trading.live import InsufficientBalance, LiveExecutor
from app.trading.paper import PaperExecutor
from tests.conftest import FakeMarket


def order(
    status: str = "NEW",
    executed: str = "0",
    quote: str = "0",
    order_id: str = "1",
    orig: str = "0.001",
    price: str = "100500",
) -> OrderResult:
    return OrderResult(
        order_id=order_id,
        symbol="BTCUSDT",
        side="BUY",
        status=status,
        orig_qty=Decimal(orig),
        executed_qty=Decimal(executed),
        quote_qty=Decimal(quote),
        price=Decimal(price),
    )


class FakeOrders:
    """Scripted stand-in for OrderClient."""

    def __init__(
        self,
        balances: Optional[dict[str, Decimal]] = None,
        queries: Optional[list[OrderResult]] = None,
    ) -> None:
        self.balances = balances or {}
        self.queries = list(queries or [])
        self.placed: list[dict] = []
        self.cancelled: list[str] = []

    async def free_balance(self, asset: str) -> Decimal:
        return self.balances.get(asset.upper(), Decimal(0))

    async def buy_limit(self, symbol, quantity, price, client_order_id=None):
        self.placed.append({"type": "LIMIT", "side": "BUY", "symbol": symbol,
                            "quantity": quantity, "price": price})
        return order(order_id="B1")

    async def buy_market_quote(self, symbol, quote_amount, client_order_id=None):
        self.placed.append({"type": "MARKET", "side": "BUY", "symbol": symbol,
                            "quoteOrderQty": quote_amount})
        return order(order_id="B2")

    async def sell_limit(self, symbol, quantity, price, client_order_id=None):
        self.placed.append({"type": "LIMIT", "side": "SELL", "symbol": symbol,
                            "quantity": quantity, "price": price})
        return order(order_id="S1")

    async def sell_market(self, symbol, quantity, client_order_id=None):
        self.placed.append({"type": "MARKET", "side": "SELL", "symbol": symbol,
                            "quantity": quantity})
        return order(order_id="S2")

    async def query_order(self, symbol, order_id):
        if len(self.queries) > 1:
            return self.queries.pop(0)
        return self.queries[0] if self.queries else order()

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append(order_id)
        return order(status="CANCELED")


def live_executor(orders: FakeOrders, **kwargs) -> LiveExecutor:
    defaults = dict(
        market=FakeMarket({"BTCUSDT": Decimal("100500")}),
        orders=orders,
        order_timeout_seconds=0,     # no waiting in tests
        poll_interval=0,
    )
    defaults.update(kwargs)
    return LiveExecutor(**defaults)


# --------------------------------------------------------------------------- #
# paper
# --------------------------------------------------------------------------- #
async def test_paper_buy_converts_usdt_into_base_quantity():
    executor = PaperExecutor(FakeMarket(), fee_rate=Decimal("0"))
    fill = await executor.buy("BTCUSDT", Decimal("100"), Decimal("100500"))

    assert isinstance(fill, Fill)
    assert fill.quote == Decimal("100")
    assert fill.quantity == Decimal("100") / Decimal("100500")
    assert fill.price == Decimal("100500")
    assert fill.order_id.startswith("paper-")


async def test_paper_buy_applies_the_fee():
    executor = PaperExecutor(FakeMarket(), fee_rate=Decimal("0.001"))
    fill = await executor.buy("BTCUSDT", Decimal("100"), Decimal("100"))

    assert fill.fee == Decimal("0.1")
    assert fill.quantity == Decimal("99.9") / Decimal("100")


async def test_paper_sell_returns_the_proceeds_minus_fee():
    executor = PaperExecutor(FakeMarket(), fee_rate=Decimal("0.001"))
    fill = await executor.sell("BTCUSDT", Decimal("2"), Decimal("100"))

    assert fill.quantity == Decimal("2")
    assert fill.quote == Decimal("200") - Decimal("0.2")


async def test_paper_rejects_a_broken_price():
    executor = PaperExecutor(FakeMarket())
    assert await executor.buy("BTCUSDT", Decimal("100"), Decimal("0")) is None
    assert await executor.sell("BTCUSDT", Decimal("1"), Decimal("-5")) is None


# --------------------------------------------------------------------------- #
# live buy
# --------------------------------------------------------------------------- #
async def test_live_limit_buy_that_fills():
    orders = FakeOrders(
        balances={"USDT": Decimal("1000")},
        queries=[order(status="FILLED", executed="0.000994", quote="100")],
    )
    fill = await live_executor(orders).buy("BTCUSDT", Decimal("100"), Decimal("100500"))

    assert fill is not None
    assert fill.quantity == Decimal("0.000994")
    assert fill.quote == Decimal("100")
    assert not fill.partial
    placed = orders.placed[0]
    assert placed["type"] == "LIMIT"
    # 0.1% above the last price, rounded to the price step
    assert placed["price"] == Decimal("100600.50")
    assert placed["quantity"] == Decimal("0.000994")
    assert orders.cancelled == []


async def test_live_limit_buy_that_never_fills_is_cancelled():
    orders = FakeOrders(
        balances={"USDT": Decimal("1000")},
        queries=[order(status="NEW"), order(status="CANCELED")],
    )
    fill = await live_executor(orders).buy("BTCUSDT", Decimal("100"), Decimal("100500"))

    assert fill is None                       # signal stays WAITING
    assert orders.cancelled == ["B1"]


async def test_live_partial_buy_keeps_what_was_bought():
    orders = FakeOrders(
        balances={"USDT": Decimal("1000")},
        queries=[
            order(status="NEW"),
            order(status="PARTIALLY_CANCELED", executed="0.0005", quote="50.3"),
        ],
    )
    fill = await live_executor(orders).buy("BTCUSDT", Decimal("100"), Decimal("100500"))

    assert fill.quantity == Decimal("0.0005")
    assert fill.quote == Decimal("50.3")
    assert fill.partial
    assert orders.cancelled == ["B1"]


async def test_live_market_buy_spends_the_quote_amount():
    orders = FakeOrders(
        balances={"USDT": Decimal("1000")},
        queries=[order(status="FILLED", executed="0.000995", quote="100")],
    )
    fill = await live_executor(orders, use_market_order=True).buy(
        "BTCUSDT", Decimal("100"), Decimal("100500")
    )

    assert orders.placed[0]["type"] == "MARKET"
    assert orders.placed[0]["quoteOrderQty"] == Decimal("100")
    assert fill.quantity == Decimal("0.000995")


async def test_live_buy_refuses_without_enough_usdt():
    orders = FakeOrders(balances={"USDT": Decimal("10")})
    with pytest.raises(InsufficientBalance):
        await live_executor(orders).buy("BTCUSDT", Decimal("100"), Decimal("100500"))
    assert orders.placed == []


async def test_live_buy_refuses_below_the_symbol_minimum():
    orders = FakeOrders(balances={"USDT": Decimal("1000")})
    market = FakeMarket({"BTCUSDT": Decimal("100500")}, min_quote_amount=Decimal("5"))
    executor = live_executor(orders, market=market)
    with pytest.raises(Exception):
        await executor.buy("BTCUSDT", Decimal("2"), Decimal("100500"))
    assert orders.placed == []


# --------------------------------------------------------------------------- #
# live sell
# --------------------------------------------------------------------------- #
async def test_live_limit_sell_that_fills():
    orders = FakeOrders(
        balances={"BTC": Decimal("1")},
        queries=[order(status="FILLED", executed="0.001", quote="102")],
    )
    fill = await live_executor(orders).sell("BTCUSDT", Decimal("0.001"), Decimal("102000"))

    assert fill.quantity == Decimal("0.001")
    assert fill.quote == Decimal("102")
    placed = orders.placed[0]
    assert placed["type"] == "LIMIT"
    assert placed["price"] == Decimal("101898.00")     # 0.1% below the last price
    assert orders.cancelled == []


async def test_live_sell_falls_back_to_market_so_an_exit_always_happens():
    orders = FakeOrders(
        balances={"BTC": Decimal("1")},
        queries=[
            order(status="NEW"),                                   # limit poll
            order(status="CANCELED", executed="0", quote="0"),     # after cancel
            order(status="FILLED", executed="0.001", quote="97"),  # market order
        ],
    )
    fill = await live_executor(orders).sell("BTCUSDT", Decimal("0.001"), Decimal("97000"))

    assert orders.cancelled == ["S1"]
    assert [p["type"] for p in orders.placed] == ["LIMIT", "MARKET"]
    assert fill.quantity == Decimal("0.001")
    assert fill.quote == Decimal("97")


async def test_live_sell_is_capped_by_the_real_balance():
    orders = FakeOrders(
        balances={"BTC": Decimal("0.0009")},
        queries=[order(status="FILLED", executed="0.0009", quote="91.8")],
    )
    await live_executor(orders).sell("BTCUSDT", Decimal("0.001"), Decimal("102000"))

    assert orders.placed[0]["quantity"] == Decimal("0.0009")
