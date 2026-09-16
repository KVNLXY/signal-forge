"""Signals read off chart screenshots: extraction, gates and confirmation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.database import repository as repo
from app.database.database import Database
from app.database.models import SignalStatus, TradeStatus, utcnow
from app.halal.whitelist import HalalWhitelist
from app.trading.engine import TradingEngine
from app.trading.paper import PaperExecutor
from app.vision.chart import ChartReading, _to_decimal, classify_colour
from tests.conftest import FakeMarket, RecordingNotifier, make_settings

IMAGE = b"not-a-real-jpeg"


class FakeReader:
    """Stands in for the OCR reader with a prepared result."""

    def __init__(self, reading: ChartReading) -> None:
        self.reading = reading
        self.calls = 0
        self.hints: list = []

    @property
    def available(self) -> bool:
        return True

    def read(self, image: bytes, symbol_hint=None, price_hint=None) -> ChartReading:
        self.calls += 1
        self.hints.append(symbol_hint)
        return self.reading

    # the engine OCRs once and interprets twice (the second time with the
    # live price); the fake hands the prepared reading back both times
    def ocr(self, image: bytes):
        self.calls += 1
        return ("raw", None)

    def interpret(self, raw, symbol_hint=None, price_hint=None) -> ChartReading:
        if price_hint is None:
            self.hints.append(symbol_hint)
        return self.reading


def long_reading(**overrides) -> ChartReading:
    values = dict(
        symbol="BTCUSDT",
        entry=Decimal("100000"),
        stop_loss=Decimal("98000"),
        take_profits=[Decimal("104000")],
        ocr_score=0.8,
    )
    values.update(overrides)
    return ChartReading(**values)


async def build(tmp_path, reading: ChartReading, name: str = "img", **settings_overrides):
    settings = make_settings(
        tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/{name}.db",
        **settings_overrides,
    )
    database = Database(settings.database_url)
    await database.create_all()
    market = FakeMarket({"BTCUSDT": Decimal("100000"), "ETHUSDT": Decimal("3500")})
    notifier = RecordingNotifier()
    whitelist = HalalWhitelist(quote_asset=settings.quote_asset)
    async with database.session() as session:
        await whitelist.sync_from_config(session, settings.halal_symbols)
    engine = TradingEngine(
        settings=settings,
        database=database,
        market=market,
        executor=PaperExecutor(market, settings.paper_fee_rate),
        notifier=notifier,
        whitelist=whitelist,
        chart_reader=FakeReader(reading),
    )
    return engine, database, market, notifier


async def loader() -> bytes:
    return IMAGE


# --------------------------------------------------------------------------- #
# the reader itself
# --------------------------------------------------------------------------- #
def test_colours_are_named_from_the_label_background():
    assert classify_colour((73, 151, 131)) == "green"      # take profit tag
    assert classify_colour((247, 235, 97)) == "yellow"     # entry tag
    assert classify_colour((222, 77, 83)) == "red"         # stop tag
    assert classify_colour((254, 254, 254)) == "white"     # live price
    assert classify_colour((1, 1, 1)) == "dark"            # axis tick


def test_prices_are_read_in_every_locale():
    assert _to_decimal("0,06128") == Decimal("0.06128")     # decimal comma
    assert _to_decimal("1 234,56") == Decimal("1234.56")    # space thousands
    assert _to_decimal("1,234.56") == Decimal("1234.56")    # comma thousands
    assert _to_decimal("16.57") == Decimal("16.57")
    assert _to_decimal("0") is None


# --------------------------------------------------------------------------- #
# engine flow
# --------------------------------------------------------------------------- #
async def test_a_readable_chart_waits_for_confirmation(tmp_path):
    engine, database, market, notifier = await build(tmp_path, long_reading())
    try:
        market.set("BTCUSDT", 100000)
        signal = await engine.handle_message("##btc stop bilan kiring", image_loader=loader)

        assert signal is not None
        assert signal.status == SignalStatus.PENDING.value
        assert signal.from_image is True
        assert notifier.find("SIGNAL FROM A CHART IMAGE")
        assert notifier.photos == [IMAGE]
        assert f"sig:{signal.id}:ok" in notifier.callback_data
        assert f"sig:{signal.id}:no" in notifier.callback_data

        async with database.session() as session:      # nothing traded yet
            assert await repo.open_trades(session) == []
    finally:
        await database.close()


async def test_confirming_opens_the_trade(tmp_path):
    engine, database, market, notifier = await build(tmp_path, long_reading())
    try:
        market.set("BTCUSDT", 100000)
        signal = await engine.handle_message("chart", image_loader=loader)

        answer = await engine.handle_button(f"sig:{signal.id}:ok")

        assert "confirmed" in answer
        async with database.session() as session:
            trades = await repo.open_trades(session)
            assert len(trades) == 1
            assert trades[0].symbol == "BTCUSDT"
            assert trades[0].sl_price == Decimal("98000")
        assert notifier.find("🟢 BUY")
    finally:
        await database.close()


async def test_confirming_below_the_entry_persists_the_waiting_state(tmp_path):
    engine, database, market, _notifier = await build(tmp_path, long_reading(), name="belowentry")
    try:
        market.set("BTCUSDT", 90000)              # entry 100000 not reached yet
        signal = await engine.handle_message("chart", image_loader=loader)

        answer = await engine.handle_button(f"sig:{signal.id}:ok")

        assert "waiting for the entry price" in answer
        async with database.session() as session:
            stored = await repo.get_signal(session, signal.id)
            assert stored.status == SignalStatus.WAITING.value
            assert await repo.open_trades(session) == []

        market.set("BTCUSDT", 100000)             # entry reached -> trades
        await engine.tick()
        async with database.session() as session:
            assert len(await repo.open_trades(session)) == 1
    finally:
        await database.close()


async def test_confirming_with_the_price_above_the_entry_waits_for_the_pullback(tmp_path):
    """A chart entry below the live price is a resting buy: confirm -> wait."""
    engine, database, market, _notifier = await build(tmp_path, long_reading(), name="pullback")
    try:
        market.set("BTCUSDT", 100000)
        signal = await engine.handle_message("chart", image_loader=loader)

        market.set("BTCUSDT", 108000)             # above the 100000 entry at the OK
        answer = await engine.handle_button(f"sig:{signal.id}:ok")

        assert "come down to 100000" in answer
        async with database.session() as session:
            stored = await repo.get_signal(session, signal.id)
            assert stored.status == SignalStatus.WAITING.value
            assert stored.entry_mode == "limit"
            assert (stored.expires_at.replace(tzinfo=None) - utcnow().replace(tzinfo=None)
                    ).total_seconds() > 23 * 3600
            assert await repo.open_trades(session) == []

        market.set("BTCUSDT", 99900)              # the pullback arrives
        await engine.tick()
        async with database.session() as session:
            assert len(await repo.open_trades(session)) == 1
    finally:
        await database.close()


async def test_confirming_a_missed_entry_with_the_skip_rule(tmp_path):
    engine, database, market, _notifier = await build(
        tmp_path, long_reading(), name="missed", entry_above_zone="skip"
    )
    try:
        market.set("BTCUSDT", 100000)
        signal = await engine.handle_message("chart", image_loader=loader)

        market.set("BTCUSDT", 108000)             # ran away before the OK
        answer = await engine.handle_button(f"sig:{signal.id}:ok")

        assert "no trade" in answer
        assert "above the entry zone" in answer
        async with database.session() as session:
            stored = await repo.get_signal(session, signal.id)
            assert stored.status == SignalStatus.SKIPPED.value
            assert await repo.open_trades(session) == []
    finally:
        await database.close()


async def test_rejecting_leaves_no_trade(tmp_path):
    engine, database, market, notifier = await build(tmp_path, long_reading())
    try:
        signal = await engine.handle_message("chart", image_loader=loader)
        answer = await engine.handle_button(f"sig:{signal.id}:no")

        assert "rejected" in answer
        async with database.session() as session:
            stored = await repo.get_signal(session, signal.id)
            assert stored.status == SignalStatus.SKIPPED.value
            assert stored.reject_reason == "Rejected by the admin."
            assert await repo.open_trades(session) == []
    finally:
        await database.close()


async def test_a_button_cannot_be_pressed_twice(tmp_path):
    engine, database, _market, _notifier = await build(tmp_path, long_reading())
    try:
        signal = await engine.handle_message("chart", image_loader=loader)
        await engine.handle_button(f"sig:{signal.id}:no")
        again = await engine.handle_button(f"sig:{signal.id}:ok")
        assert "already" in again
    finally:
        await database.close()


async def test_a_misread_price_is_rejected_against_the_live_market(tmp_path):
    # OCR dropped the leading "0," - the entry lands 100x away from the market.
    reading = long_reading(entry=Decimal("98000"), stop_loss=Decimal("50"),
                           take_profits=[Decimal("104000")])
    reading.entry = Decimal("980")          # 99% below the live 100000
    engine, database, market, notifier = await build(tmp_path, reading, name="misread")
    try:
        market.set("BTCUSDT", 100000)
        signal = await engine.handle_message("chart", image_loader=loader)

        assert signal.status == SignalStatus.SKIPPED.value
        assert "away from the live price" in signal.reject_reason
        assert notifier.find("SIGNAL SKIPPED")
    finally:
        await database.close()


async def test_a_non_halal_coin_from_an_image_is_rejected(tmp_path):
    engine, database, market, notifier = await build(
        tmp_path, long_reading(symbol="DOGEUSDT", entry=Decimal("0.1"),
                               stop_loss=Decimal("0.09"), take_profits=[Decimal("0.13")]),
        name="nonhalal",
    )
    try:
        signal = await engine.handle_message("chart", image_loader=loader)
        assert signal.status == SignalStatus.SKIPPED.value
        assert signal.reject_reason == "Coin is not in halal whitelist."
    finally:
        await database.close()


async def test_an_unreadable_chart_is_silently_ignored(tmp_path):
    engine, database, _market, notifier = await build(
        tmp_path, ChartReading(reason="no entry (yellow) level on the chart"), name="unread",
    )
    try:
        assert await engine.handle_message("just a chart", image_loader=loader) is None
        assert notifier.messages == []
    finally:
        await database.close()


async def test_an_unconfirmed_signal_expires(tmp_path):
    engine, database, _market, notifier = await build(tmp_path, long_reading(), name="expire")
    try:
        signal = await engine.handle_message("chart", image_loader=loader)
        async with database.session() as session:
            stored = await repo.get_signal(session, signal.id)
            stored.expires_at = utcnow() - timedelta(seconds=1)

        await engine.tick()

        async with database.session() as session:
            stored = await repo.get_signal(session, signal.id)
            assert stored.status == SignalStatus.EXPIRED.value
        assert notifier.find("Not confirmed within")
    finally:
        await database.close()


async def test_confirmation_can_be_switched_off(tmp_path):
    engine, database, market, notifier = await build(
        tmp_path, long_reading(), name="auto", image_signal_confirmation=False,
    )
    try:
        market.set("BTCUSDT", 100000)
        signal = await engine.handle_message("chart", image_loader=loader)

        assert signal.status == SignalStatus.TRIGGERED.value
        async with database.session() as session:
            assert len(await repo.open_trades(session)) == 1
        assert notifier.find("🟢 BUY")
        assert notifier.callback_data == []
    finally:
        await database.close()


async def test_a_text_signal_never_reaches_the_image_reader(tmp_path):
    engine, database, market, _notifier = await build(tmp_path, long_reading(), name="textwins")
    try:
        market.set("BTCUSDT", 99000)
        signal = await engine.handle_message(
            "BTC LONG\nEntry: 100000-101000\nTP1: 102000\nSL: 98000", image_loader=loader
        )
        assert signal.status == SignalStatus.WAITING.value
        assert signal.from_image is False
        assert engine._chart_reader.calls == 0
    finally:
        await database.close()


async def test_an_image_signal_does_not_duplicate_an_open_position(tmp_path):
    engine, database, market, _notifier = await build(tmp_path, long_reading(), name="dupe")
    try:
        market.set("BTCUSDT", 100000)
        first = await engine.handle_message("chart", tg_message_id=1, image_loader=loader)
        await engine.handle_button(f"sig:{first.id}:ok")

        second = await engine.handle_message("chart", tg_message_id=2, image_loader=loader)
        assert second is None
        async with database.session() as session:
            trades = await repo.open_trades(session)
            assert len(trades) == 1
            assert trades[0].status == TradeStatus.OPEN.value
    finally:
        await database.close()


# --------------------------------------------------------------------------- #
# the caption names the coin when the chart does not
# --------------------------------------------------------------------------- #
async def test_caption_ticker_is_offered_to_the_reader_as_a_hint(tmp_path):
    engine, database, market, notifier = await build(tmp_path, long_reading(), "hint1")
    reader = engine._chart_reader
    try:
        await engine.handle_message("ICP", tg_message_id=1, image_loader=loader)
        await engine.handle_message("##sol stop bilan kiring", tg_message_id=2, image_loader=loader)
        await engine.handle_message("Alhamdulillah", tg_message_id=3, image_loader=loader)
        await engine.handle_message("Zec full tp", tg_message_id=4, image_loader=loader)
    finally:
        await database.close()

    # ICP is not whitelisted and the fake market does not list it -> no hint;
    # SOL is whitelisted -> hinted; a plain word -> nothing; "Zec full tp" is
    # a result update and never reaches the reader at all.
    assert reader.hints == [None, "SOLUSDT", None]


async def test_caption_hint_for_a_listed_but_not_whitelisted_coin(tmp_path):
    engine, database, market, notifier = await build(tmp_path, long_reading(), "hint2")
    market.set("ICPUSDT", "2.78")           # listed on the exchange, not halal-listed
    try:
        await engine.handle_message("ICP", tg_message_id=1, image_loader=loader)
    finally:
        await database.close()
    assert engine._chart_reader.hints == ["ICPUSDT"]


# --------------------------------------------------------------------------- #
# interpretation of OCR labels (no OCR engine involved)
# --------------------------------------------------------------------------- #
def _picture(colours: dict[str, tuple[int, int, int]]):
    """A 400x400 canvas with one solid 40x40 patch per label colour."""
    import numpy as np

    canvas = np.zeros((400, 400, 3), dtype=np.uint8)
    boxes = {}
    for index, (name, rgb) in enumerate(colours.items()):
        y = 10 + index * 60
        canvas[y:y + 40, 300:340] = rgb
        boxes[name] = [[300, y], [340, y], [340, y + 40], [300, y + 40]]
    return canvas, boxes


def _interpret(labels: list[tuple[str, str]], symbol_hint=None):
    from app.vision.chart import ChartReader

    colours = {
        "green": (38, 166, 154), "yellow": (240, 200, 60),
        "red": (220, 60, 60), "white": (250, 250, 250),
    }
    canvas, boxes = _picture(colours)
    result = []
    for text, colour in labels:
        result.append((boxes[colour], text, 0.95))
    return ChartReader()._interpret(result, canvas, symbol_hint)


def test_implausible_targets_from_a_lost_decimal_point_are_dropped():
    reading = _interpret([
        ("GLMRUSDT", "white"),
        ("0,006519", "yellow"), ("0,005975", "red"),
        ("0,006876", "green"), ("0,019549", "green"), ("20000", "green"),
    ])
    assert reading.ok
    assert reading.take_profits == [Decimal("0.006876"), Decimal("0.019549")]
    assert any("implausible" in note for note in reading.notes)


def test_a_stop_far_below_the_entry_is_a_misread_not_a_setup():
    reading = _interpret([
        ("ZAMAUSDT", "white"),
        ("0,05183", "yellow"), ("0,00495", "red"), ("0,06128", "green"),
    ])
    assert not reading.ok
    assert "implausibly far below" in reading.reason


def test_the_caption_names_the_pair_when_the_chart_does_not():
    labels = [("0,05183", "yellow"), ("0,04957", "red"), ("0,06128", "green")]
    assert not _interpret(labels).ok
    reading = _interpret(labels, symbol_hint="ZAMAUSDT")
    assert reading.ok
    assert reading.symbol == "ZAMAUSDT"
    assert any("caption" in note for note in reading.notes)


def test_blue_line_labels_above_the_entry_are_targets():
    from app.vision.chart import ChartReader, classify_colour

    assert classify_colour((41, 98, 255)) == "blue"
    assert classify_colour((33, 150, 243)) == "blue"
    assert classify_colour((38, 166, 154)) == "green"      # the profit box stays green

    colours = {
        "blue_hi": (41, 98, 255), "blue_lo": (41, 98, 255),
        "yellow": (240, 200, 60), "red": (220, 60, 60), "white": (250, 250, 250),
    }
    canvas, boxes = _picture(colours)
    result = [
        (boxes["white"], "DASHUSDT", 0.95),
        (boxes["yellow"], "65,95", 0.95),
        (boxes["red"], "63,69", 0.95),
        (boxes["blue_hi"], "68,35", 0.95),
        (boxes["blue_lo"], "60,00", 0.95),                  # a line under the entry
    ]
    reading = ChartReader()._interpret(result, canvas)
    assert reading.ok
    assert reading.take_profits == [Decimal("68.35")]
    assert any("blue" in note for note in reading.notes)


# --------------------------------------------------------------------------- #
# result posts and repeated charts
# --------------------------------------------------------------------------- #
def test_result_captions_are_recognised():
    from app.trading.engine import is_result_update

    for caption in (
        "12.53%✅stop kotarib kutamiz", "Zec full tp", "1tp urdi", "Glmr limitga kelib 12% berdi",
        "8%berib qaytdi yopib yuboramiz", "25%✅ 1tp urdi 50%sotib stop kotaring", "Tugadi",
    ):
        assert is_result_update(caption), caption
    for caption in (
        "##glmr kelsa stop bilan kiring", "2z olamiz  stop bilan hozir arzon", "Kelsa scalp",
        "Lsk pulni bolib kiring stop shart", "Api3 orta mudatga olsa boladi", "Kirish limit 0.0615",
        "Biz hozi sotib olamiz va pasdagi qizil linaga stop qoyib kutamiz", "ICP", "",
    ):
        assert not is_result_update(caption), caption


async def test_a_result_post_never_reaches_the_reader(tmp_path):
    engine, database, market, notifier = await build(tmp_path, long_reading(), "result")
    try:
        await engine.handle_message("12%✅ stop kotarib kutamiz", tg_message_id=1, image_loader=loader)
        assert engine._chart_reader.calls == 0
        async with database.session() as session:
            assert await repo.active_signals(session) == []
    finally:
        await database.close()


async def test_the_same_chart_posted_twice_is_read_once(tmp_path):
    """The VIP group and the public channel post the same picture."""
    engine, database, market, notifier = await build(tmp_path, long_reading(), "repeat")
    try:
        market.set("BTCUSDT", 99000)
        first = await engine.handle_message("##btc kelsa stop bilan kiring",
                                            channel_identifier="@vip", tg_message_id=1,
                                            image_loader=loader)
        assert first is not None

        # the same setup closes (say it expired) and the chart shows up again
        async with database.session() as session:
            stored = await repo.get_signal(session, first.id)
            await repo.close_signal(session, stored, SignalStatus.EXPIRED, "test")

        again = await engine.handle_message("##btc kelsa stop bilan kiring",
                                            channel_identifier="@public", tg_message_id=2,
                                            image_loader=loader)
        assert again is None
        async with database.session() as session:
            assert await repo.active_signals(session) == []
    finally:
        await database.close()


def test_a_decimal_comma_with_three_digits_is_settled_by_the_live_price():
    from decimal import Decimal as D

    from app.vision.chart import _to_decimal

    assert _to_decimal("11,430") == D("11430")                      # no hint: US thousands
    assert _to_decimal("11,430", D("11.6")) == D("11.430")          # Russian-locale chart
    assert _to_decimal("11,430", D("11800")) == D("11430")          # a real eleven thousand
    assert _to_decimal("1,234.56", D("1200")) == D("1234.56")       # unambiguous shapes untouched
    assert _to_decimal("0,06128", D("0.05")) == D("0.06128")
    assert _to_decimal("12,345,678", D("5")) == D("12345678")       # two commas: never a decimal
