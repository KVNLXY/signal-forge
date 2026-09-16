"""The model as a second reader: only asked when a post plausibly carries
levels, never trusted without the Confirm button, never able to crash the
bot.  Claude itself is replaced by an injected ``ask`` - no network."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.database import repository as repo
from app.database.database import Database
from app.database.models import SignalStatus
from app.halal.whitelist import HalalWhitelist
from app.telegram.llm_reader import LlmReading, LlmSignalReader, to_signal
from app.trading.engine import TradingEngine
from app.trading.paper import PaperExecutor
from tests.conftest import BTC_LONG, FakeMarket, RecordingNotifier, make_settings

# A post the regex parser cannot read: no label it knows, a side it must infer.
ODD_POST = "BTC ni 100000 dan olamiz 🚀 105000 va 108000 gacha, stop 98000 pastida"


class FakeClaude:
    def __init__(self, reading: Optional[LlmReading] = None, fail: bool = False) -> None:
        self.reading = reading
        self.fail = fail
        self.asked: list[str] = []

    async def __call__(self, text: str) -> Optional[LlmReading]:
        self.asked.append(text)
        if self.fail:
            raise RuntimeError("api down")
        return self.reading


BTC_READING = LlmReading(
    is_signal=True, symbol="BTC", side="long", entry_low="100,000", entry_high="100,000",
    stop_loss="98000", take_profits=["105000", "$108000"], notes="side inferred from the levels",
)


# --------------------------------------------------------------------------- #
# the reading -> signal conversion
# --------------------------------------------------------------------------- #
def test_reading_becomes_prices_and_a_side():
    signal = to_signal(BTC_READING)
    assert signal is not None
    assert signal.base_asset == "BTC" and signal.direction == "BUY"
    assert (signal.entry_low, signal.entry_high) == (Decimal("100000"), Decimal("100000"))
    assert signal.stop_loss == Decimal("98000")
    assert signal.take_profits == [Decimal("105000"), Decimal("108000")]
    assert signal.notes == ["Read by Claude (side inferred from the levels)"]


def test_reading_without_the_essentials_is_nothing():
    assert to_signal(LlmReading(is_signal=False)) is None
    assert to_signal(LlmReading(is_signal=True, symbol=None, side="long")) is None
    assert to_signal(LlmReading(is_signal=True, symbol="BTC", side=None)) is None
    assert to_signal(LlmReading(is_signal=True, symbol="BTC", side="maybe")) is None

    # A reversed zone is put the right way round; one bound fills the other.
    signal = to_signal(LlmReading(is_signal=True, symbol="eth/usdt", side="long",
                                  entry_low="3600", entry_high="3400"))
    assert signal.base_asset == "ETHUSDT"        # the whitelist normalises it later
    assert (signal.entry_low, signal.entry_high) == (Decimal("3400"), Decimal("3600"))
    signal = to_signal(LlmReading(is_signal=True, symbol="SOL", side="short", entry_low="150"))
    assert signal.direction == "SHORT" and signal.entry_high == Decimal("150")


async def test_reader_is_unavailable_without_key_and_never_raises():
    reader = LlmSignalReader(api_key="")
    assert not reader.available and "LLM_API_KEY" in reader.unavailable_reason
    assert await reader.read(ODD_POST) is None

    broken = FakeClaude(fail=True)
    reader = LlmSignalReader(ask=broken)
    assert reader.available
    assert await reader.read(ODD_POST) is None
    assert broken.asked == [ODD_POST]


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
async def build(tmp_path, claude: FakeClaude, name: str = "llm", **overrides):
    settings = make_settings(
        tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/{name}.db",
        **overrides,
    )
    database = Database(settings.database_url)
    await database.create_all()
    market = FakeMarket({"BTCUSDT": Decimal("100500"), "ETHUSDT": Decimal("3500")})
    whitelist = HalalWhitelist(quote_asset=settings.quote_asset)
    async with database.session() as session:
        await whitelist.sync_from_config(session, settings.halal_symbols)
    notifier = RecordingNotifier()
    engine = TradingEngine(
        settings=settings, database=database, market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier, whitelist=whitelist,
        llm_reader=LlmSignalReader(ask=claude, model="claude-opus-5"),
    )
    return engine, market, notifier, database


async def open_trades(database: Database):
    async with database.session() as session:
        return await repo.open_trades(session)


async def test_unreadable_post_is_read_by_claude_and_waits_for_confirm(tmp_path):
    claude = FakeClaude(BTC_READING)
    engine, market, notifier, database = await build(tmp_path, claude)
    try:
        market.set("BTCUSDT", 100200)                     # inside entry + tolerance
        signal = await engine.handle_message(ODD_POST, channel_identifier="@alpha", tg_message_id=1)

        assert claude.asked == [ODD_POST]
        assert signal is not None and signal.status == SignalStatus.PENDING.value
        assert signal.symbol == "BTCUSDT" and not signal.from_image
        assert signal.raw_text.startswith("[llm] ")
        assert notifier.find("SIGNAL READ BY CLAUDE")
        assert "Read by Claude (side inferred from the levels)" in notifier.last
        assert "Check it against the post" in notifier.last
        assert notifier.callback_data[-2:] == [f"sig:{signal.id}:ok", f"sig:{signal.id}:no"]
        assert await open_trades(database) == []

        answer = await engine.handle_button(f"sig:{signal.id}:ok")
        assert "position opened" in answer
        trade = (await open_trades(database))[0]
        assert trade.sl_price == Decimal("98000")
        assert trade.take_profits == [Decimal("105000"), Decimal("108000")]
    finally:
        await database.close()


async def test_claude_is_not_asked_about_chatter_or_when_the_parser_succeeded(tmp_path):
    claude = FakeClaude(BTC_READING)
    engine, market, notifier, database = await build(tmp_path, claude)
    try:
        await engine.handle_message("good morning traders", tg_message_id=1)
        await engine.handle_message("BTC looks strong, 1 more push", tg_message_id=2)   # one number
        await engine.handle_message("TP1 urdi 12% ✅", tg_message_id=3)
        assert claude.asked == []

        market.set("BTCUSDT", 99000)
        signal = await engine.handle_message(BTC_LONG, tg_message_id=4)
        assert signal.status == SignalStatus.WAITING.value
        assert claude.asked == []                                       # the parser did it

        # Two numbers and no labels: worth one call.
        await engine.handle_message("BTC 100000 dan 105000 ga boradi", tg_message_id=5)
        assert len(claude.asked) == 1
    finally:
        await database.close()


async def test_readings_that_fail_the_gates_are_not_offered(tmp_path):
    short = LlmReading(is_signal=True, symbol="BTC", side="short", entry_low="100000",
                       stop_loss="102000", take_profits=["95000"])
    engine, market, notifier, database = await build(tmp_path, FakeClaude(short), "short")
    try:
        assert await engine.handle_message(ODD_POST, tg_message_id=1) is None
        async with database.session() as session:
            assert await repo.pending_signals(session) == []
    finally:
        await database.close()

    no_sl = LlmReading(is_signal=True, symbol="BTC", side="long", entry_low="100000",
                       take_profits=["105000"])
    engine, market, notifier, database = await build(tmp_path, FakeClaude(no_sl), "nosl")
    try:
        assert await engine.handle_message(ODD_POST, tg_message_id=1) is None
        assert notifier.messages == []
    finally:
        await database.close()

    far = LlmReading(is_signal=True, symbol="BTC", side="long", entry_low="10000",
                     stop_loss="9800", take_profits=["10500"])              # a lost zero
    engine, market, notifier, database = await build(tmp_path, FakeClaude(far), "far")
    try:
        signal = await engine.handle_message(ODD_POST, tg_message_id=1)
        assert signal.status == SignalStatus.SKIPPED.value
        assert "probably a misread" in signal.reject_reason
    finally:
        await database.close()

    doge = LlmReading(is_signal=True, symbol="DOGE", side="long", entry_low="0.1",
                      stop_loss="0.09", take_profits=["0.12"])
    engine, market, notifier, database = await build(tmp_path, FakeClaude(doge), "doge")
    try:
        signal = await engine.handle_message("DOGE 0.1 dan 0.12 gacha, stop 0.09", tg_message_id=1)
        assert signal.reject_reason == "Coin is not in halal whitelist."
    finally:
        await database.close()


async def test_confirmation_off_trades_the_reading_like_text(tmp_path):
    engine, market, notifier, database = await build(
        tmp_path, FakeClaude(BTC_READING), "auto", llm_signal_confirmation=False
    )
    try:
        market.set("BTCUSDT", 100200)
        signal = await engine.handle_message(ODD_POST, tg_message_id=1)
        assert signal.status == SignalStatus.TRIGGERED.value
        assert len(await open_trades(database)) == 1
    finally:
        await database.close()


async def test_fallback_switched_off_or_broken_changes_nothing(tmp_path):
    claude = FakeClaude(BTC_READING)
    engine, market, notifier, database = await build(tmp_path, claude, "off", llm_fallback=False)
    try:
        assert await engine.handle_message(ODD_POST, tg_message_id=1) is None
        assert claude.asked == []
    finally:
        await database.close()

    engine, market, notifier, database = await build(tmp_path, FakeClaude(fail=True), "broken")
    try:
        assert await engine.handle_message(ODD_POST, tg_message_id=1) is None
        assert notifier.messages == []
    finally:
        await database.close()
