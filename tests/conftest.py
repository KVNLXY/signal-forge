"""Shared test fixtures.

Everything runs against a temporary SQLite database and fake market data -
no network, no exchange, no real money (requirement 24).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

import pytest
import pytest_asyncio

from app.config import Settings
from app.database.database import Database
from app.halal.whitelist import HalalWhitelist
from app.mexc.market import BookTicker, MarketData, SymbolRules
from app.notifications.telegram import TelegramNotifier
from app.trading.engine import TradingEngine
from app.trading.paper import PaperExecutor


class FakeMarket:
    """Stand-in for MarketData with prices the test controls."""

    def __init__(
        self,
        prices: Optional[dict[str, Decimal]] = None,
        min_quote_amount: Decimal = Decimal("5"),
    ) -> None:
        self.prices: dict[str, Decimal] = dict(prices or {})
        self.min_quote_amount = min_quote_amount
        # Spread in percent per symbol; a symbol not listed here has a tight
        # book (bid == ask == price).  None makes the book unreadable.
        self.spreads: dict[str, Optional[Decimal]] = {}

    def set(self, symbol: str, price: object) -> None:
        self.prices[symbol.upper()] = Decimal(str(price))

    def set_spread(self, symbol: str, percent: Optional[object]) -> None:
        self.spreads[symbol.upper()] = None if percent is None else Decimal(str(percent))

    async def get_book(self, symbol: str) -> BookTicker:
        symbol = symbol.upper()
        price = await self.get_price(symbol)
        spread = self.spreads.get(symbol, Decimal(0))
        if spread is None:
            raise RuntimeError(f"no order book for {symbol}")
        half = price * spread / Decimal(200)
        return BookTicker(symbol=symbol, bid=price - half, ask=price + half)

    async def get_price(self, symbol: str) -> Decimal:
        try:
            return self.prices[symbol.upper()]
        except KeyError:
            raise RuntimeError(f"no test price for {symbol}")

    async def get_prices(self, symbols: Optional[Iterable[str]] = None) -> dict[str, Decimal]:
        if symbols is None:
            return dict(self.prices)
        wanted = {s.upper() for s in symbols}
        return {s: p for s, p in self.prices.items() if s in wanted}

    async def get_rules(self, symbol: str, refresh: bool = False) -> SymbolRules:
        return SymbolRules(
            symbol=symbol.upper(),
            status="ENABLED",
            base_asset=symbol.upper().replace("USDT", ""),
            quote_asset="USDT",
            price_step=Decimal("0.01"),
            qty_step=Decimal("0.000001"),
            min_quote_amount=self.min_quote_amount,
            spot_trading_allowed=True,
        )

    @staticmethod
    def round_step(value: Decimal, step: Decimal) -> Decimal:
        return MarketData.round_step(value, step)


class RecordingNotifier(TelegramNotifier):
    """Captures messages instead of calling Telegram."""

    def __init__(self) -> None:
        super().__init__("", "")
        self.messages: list[str] = []
        self.buttons: list[list] = []
        self.photos: list[bytes] = []

    async def send(self, text: str, buttons: Optional[list] = None) -> bool:
        self.messages.append(text)
        if buttons:
            self.buttons.append(buttons)
        return True

    async def send_photo(
        self, image: bytes, caption: str, buttons: Optional[list] = None
    ) -> bool:
        self.photos.append(image)
        return await self.send(caption, buttons)

    @property
    def callback_data(self) -> list[str]:
        return [
            button["callback_data"]
            for keyboard in self.buttons
            for row in keyboard
            for button in row
        ]

    def find(self, needle: str) -> list[str]:
        return [m for m in self.messages if needle in m]

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ""


def make_settings(tmp_path, **overrides) -> Settings:
    values: dict[str, object] = {
        "trading_mode": "PAPER",
        "database_url": f"sqlite+aiosqlite:///{tmp_path.as_posix()}/signalforge-test.db",
        "halal_coins": "BTCUSDT,ETHUSDT,SOLUSDT",
        "trade_amount_usdt": Decimal("100"),
        "max_open_trades": 3,
        "signal_expiry_minutes": 15,
        "tp_splits": "30,30,40",
        "paper_fee_rate": Decimal("0"),   # exact numbers in tests
        "telegram_channels": "@test_channel",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def market() -> FakeMarket:
    return FakeMarket({"BTCUSDT": Decimal("100500"), "ETHUSDT": Decimal("3500")})


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest_asyncio.fixture
async def database(settings) -> Database:
    db = Database(settings.database_url)
    await db.create_all()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def whitelist(settings, database) -> HalalWhitelist:
    wl = HalalWhitelist(quote_asset=settings.quote_asset)
    async with database.session() as session:
        await wl.sync_from_config(session, settings.halal_symbols)
    return wl


@pytest_asyncio.fixture
async def engine(settings, database, market, notifier, whitelist) -> TradingEngine:
    return TradingEngine(
        settings=settings,
        database=database,
        market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier,
        whitelist=whitelist,
    )


# --------------------------------------------------------------------------- #
# sample signals
# --------------------------------------------------------------------------- #
BTC_LONG = """BTC LONG

Entry: 100000-101000
TP1: 102000
TP2: 104000
SL: 98000"""

BTC_SHORT = """BTC SHORT

Entry: 100000-101000
TP1: 98000
SL: 102000"""

BTC_NO_SL = """BTC LONG

Entry: 100000-101000
TP1: 102000"""

UNKNOWN_COIN = """ABC LONG

Entry: 1.0-1.1
TP1: 1.5
SL: 0.9"""
