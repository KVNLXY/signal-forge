"""Read a signal off a TradingView chart screenshot.

Many channels post the numbers only as a picture: a Long Position drawing with
the levels shown as coloured labels on the price axis.  The colour is what
carries the meaning, so the reader is colour-first and OCR-second:

    green  label  ->  take profit          (the profit zone)
    blue   label  ->  take profit          (a horizontal line above the entry)
    yellow label  ->  entry
    red    label  ->  stop loss
    white  label  ->  the live price       (ignored)
    dark   label  ->  an ordinary axis tick (ignored)

Nothing here guesses.  A reading is returned only when a symbol, an entry, a
stop and at least one target were all found AND they form a long setup
(sl < entry < tp).  Everything else comes back with the reason it failed, and
the caller does not trade it.

OCR is optional: without rapidocr-onnxruntime installed the reader reports
itself unavailable and the bot simply keeps working on text signals.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

# A target must sit clearly above the entry; the live-price tag drawn inside
# the green profit box is only a hair above it and must not be read as a TP.
MIN_TP_DISTANCE = Decimal("1.005")

# Sanity bounds for a spot setup.  Anything outside them is an OCR error, not
# a level: a target 10x the entry or a stop 80% below it does not happen on a
# Long Position drawing.
MAX_TP_MULTIPLE = Decimal("10")
MIN_SL_FRACTION = Decimal("0.2")

# Screenshots are shrunk to this before OCR.  Price labels stay well above the
# size OCR needs, and it keeps the memory spike small on a 2 GB server.
MAX_IMAGE_SIDE = 1600

_SYMBOL_RE = re.compile(r"^([A-Z][A-Z0-9]{1,11})(USDT|USDC|USD)$")
_NUMBER_RE = re.compile(r"^[\s]*([0-9][0-9\s.,]*)[\s]*$")
_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$")


@dataclass
class ChartReading:
    """What the reader could make of one image."""

    symbol: Optional[str] = None
    entry: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profits: list[Decimal] = field(default_factory=list)
    reason: Optional[str] = None
    ocr_score: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reason is None and self.symbol is not None and self.entry is not None


def _parse(cleaned: str) -> Optional[Decimal]:
    if cleaned.count(".") > 1:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _to_decimal(text: str, scale_hint: Optional[Decimal] = None) -> Optional[Decimal]:
    """0,06128 / 1 234,56 / 1,234.56 / 0.06128 -> Decimal.

    "11,430" is the one ambiguous shape: eleven thousand (US thousands
    separator) or 11.43 (a decimal comma, which is what a Russian-locale
    TradingView draws).  With a scale hint - the live price of the coin - the
    reading closest to it wins; without one it stays a thousands separator.
    """
    cleaned = text.strip().replace(" ", "").replace("\u00a0", "")
    if not cleaned:
        return None
    if _THOUSANDS_RE.match(cleaned):          # 1,234 / 12,345,678 - or 11,430?
        as_thousands = _parse(cleaned.replace(",", ""))
        if scale_hint is None or scale_hint <= 0 or cleaned.count(",") > 1:
            return as_thousands
        as_decimal = _parse(cleaned.replace(",", "."))
        if as_thousands is None or as_decimal is None:
            return as_thousands or as_decimal
        # compare on a log scale: which reading is the right order of magnitude
        hint = float(scale_hint)
        gap_thousands = abs(math.log10(float(as_thousands) / hint))
        gap_decimal = abs(math.log10(float(as_decimal) / hint))
        return as_decimal if gap_decimal < gap_thousands else as_thousands
    if "," in cleaned and "." in cleaned:     # 1,234.56
        cleaned = cleaned.replace(",", "")
    else:                                     # 0,06128 -> decimal comma
        cleaned = cleaned.replace(",", ".")
    return _parse(cleaned)


def classify_colour(rgb: Sequence[int]) -> str:
    """Name the background colour behind a label."""
    r, g, b = (int(c) for c in rgb[:3])
    if r > 200 and g > 200 and b > 200:
        return "white"
    if r < 70 and g < 70 and b < 70:
        return "dark"
    if r > 140 and g > 140 and b < 140:
        return "yellow"
    if r > 130 and r > g * 1.4 and r > b * 1.4:
        return "red"
    if g > 90 and g > r * 1.2 and g >= b:
        return "green"
    if b > 150 and b > r * 1.3 and b > g * 1.1:
        return "blue"
    return "other"


class ChartReader:
    """Colour + OCR reader for chart screenshots."""

    def __init__(self, min_ocr_score: float = 0.55) -> None:
        self._min_score = min_ocr_score
        self._engine: Any = None
        self._unavailable_reason: Optional[str] = None

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self) -> Any:
        if self._engine is not None or self._unavailable_reason is not None:
            return self._engine
        try:
            from rapidocr_onnxruntime import RapidOCR  # heavy, imported once
        except ImportError as exc:
            self._unavailable_reason = str(exc)
            log.warning("chart reading disabled - rapidocr-onnxruntime is not installed")
            return None
        self._engine = RapidOCR()
        log.info("chart reader ready (OCR + colour)")
        return self._engine

    # ------------------------------------------------------------------ #
    def read(
        self,
        image: bytes,
        symbol_hint: Optional[str] = None,
        price_hint: Optional[Decimal] = None,
    ) -> ChartReading:
        raw = self.ocr(image)
        if isinstance(raw, ChartReading):
            return raw
        return self.interpret(raw, symbol_hint, price_hint)

    def ocr(self, image: bytes):
        """The expensive half: decode + OCR.  Returns (labels, picture), or a
        ChartReading carrying the reason it could not be done."""
        engine = self._load()
        if engine is None:
            return ChartReading(reason="chart reading is not installed")
        try:
            picture = self._decode(image)
        except Exception as exc:
            return ChartReading(reason=f"image could not be opened: {exc}")
        try:
            result, _ = engine(picture)
        except Exception as exc:  # pragma: no cover - OCR engine failure
            log.warning("OCR failed: %s", exc)
            return ChartReading(reason=f"OCR failed: {exc}")
        return (result or [], picture)

    def interpret(
        self,
        raw,
        symbol_hint: Optional[str] = None,
        price_hint: Optional[Decimal] = None,
    ) -> ChartReading:
        """The cheap half - can be repeated once the live price is known."""
        result, picture = raw
        return self._interpret(result, picture, symbol_hint, price_hint)

    @staticmethod
    def _decode(image: bytes) -> Any:
        """Decode a screenshot, shrinking oversized ones first.

        Phone screenshots arrive at 1200x2600 or larger, and OCR on the full
        frame is the single biggest memory spike the bot has.  The price labels
        stay far above the size OCR needs after the shrink, and because the box
        coordinates come back in the same scaled space nothing else changes.
        """
        import io

        import numpy as np
        from PIL import Image

        picture = Image.open(io.BytesIO(image))
        # JPEG-only hint: lets the decoder do the first halving itself, so the
        # full-size frame is never allocated.
        picture.draft("RGB", (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        picture = picture.convert("RGB")

        longest = max(picture.size)
        if longest > MAX_IMAGE_SIDE:
            scale = MAX_IMAGE_SIDE / longest
            picture = picture.resize(
                (max(round(picture.width * scale), 1), max(round(picture.height * scale), 1)),
                Image.LANCZOS,
            )
        return np.array(picture)

    # ------------------------------------------------------------------ #
    def _interpret(
        self,
        result: list,
        picture: Any,
        symbol_hint: Optional[str] = None,
        price_hint: Optional[Decimal] = None,
    ) -> ChartReading:
        import numpy as np

        height, width, _ = picture.shape
        labels: list[tuple[str, str, float, int, int]] = []  # text, colour, score, x0, y0

        for box, text, score in result:
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            if score < self._min_score:
                continue
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            x0, x1 = max(min(xs), 0), min(max(xs), width)
            y0, y1 = max(min(ys), 0), min(max(ys), height)
            patch = picture[y0:y1, x0:x1]
            if patch.size == 0:
                continue
            colour = classify_colour(np.median(patch.reshape(-1, 3), axis=0))
            labels.append((str(text).strip(), colour, score, x0, y0))

        reading = ChartReading()
        reading.ocr_score = (
            sum(item[2] for item in labels) / len(labels) if labels else 0.0
        )

        # The pair name is often missing from a cropped screenshot while the
        # caption names it ("ICP").  Levels are read first so that the caller
        # still learns why a chart is not a setup, and the hint fills the name.
        symbol = self._pick_symbol(labels, height)
        if symbol is None and symbol_hint:
            symbol = symbol_hint
            reading.notes.append(f"pair taken from the caption: {symbol_hint}")
        reading.symbol = symbol

        numbers: dict[str, list[Decimal]] = {
            "green": [], "blue": [], "yellow": [], "red": [], "white": [],
        }
        for text, colour, _score, _x0, _y0 in labels:
            if colour not in numbers:
                continue
            match = _NUMBER_RE.match(text)
            if not match:
                continue
            value = _to_decimal(match.group(1), price_hint)
            if value is not None:
                numbers[colour].append(value)

        if not numbers["yellow"]:
            reading.reason = "no entry (yellow) level on the chart"
            return reading
        if not numbers["red"]:
            reading.reason = "no stop loss (red) level on the chart"
            return reading

        # One entry, one stop: the extremes of a long position box.
        entry = max(numbers["yellow"])
        stop_loss = min(numbers["red"])
        # A target more than MAX_TP_MULTIPLE above the entry is an OCR slip
        # (a lost decimal point turns 0.0195 into 19549), not a target.
        # Targets: the green profit box of a Long Position drawing, or the
        # blue labels of plain horizontal lines drawn above the entry (the
        # group's own rule: "oq lina 1tp 2tp ko'k yozuvdagi").  A blue label
        # below the entry is just another line, never a target.
        candidates = numbers["green"] + [tp for tp in numbers["blue"] if tp > entry]
        absurd = [tp for tp in candidates if tp > entry * MAX_TP_MULTIPLE]
        targets = sorted(
            {tp for tp in candidates
             if entry * MIN_TP_DISTANCE < tp <= entry * MAX_TP_MULTIPLE}
        )
        if absurd:
            reading.notes.append(f"ignored implausible targets {absurd}")
        if numbers["blue"] and not numbers["green"] and targets:
            reading.notes.append("targets taken from blue line labels")

        reading.entry = entry
        reading.stop_loss = stop_loss
        reading.take_profits = targets

        if not targets:
            reading.reason = "no take profit (green) level above the entry"
            return reading
        if stop_loss >= entry:
            reading.reason = f"stop {stop_loss} is not below the entry {entry} (not a long)"
            return reading
        if stop_loss < entry * MIN_SL_FRACTION:
            reading.reason = (
                f"stop {stop_loss} is implausibly far below the entry {entry} - "
                f"probably a misread digit"
            )
            return reading

        if symbol is None:
            reading.reason = "no trading pair on the chart and none in the caption"
            return reading

        if numbers["white"]:
            reading.notes.append(f"chart price {numbers['white'][0]}")
        if len(numbers["yellow"]) > 1:
            reading.notes.append(f"{len(numbers['yellow'])} yellow labels, used {entry}")
        return reading

    @staticmethod
    def _pick_symbol(labels: list, height: int) -> Optional[str]:
        """The pair on the position tag, not one from the watchlist below."""
        candidates = []
        for text, colour, score, _x0, y0 in labels:
            cleaned = re.sub(r"[^A-Z0-9]", "", str(text).upper())
            match = _SYMBOL_RE.match(cleaned)
            if not match:
                continue
            # A coloured tag is the drawing itself; plain text lower down is
            # usually the watchlist, so it ranks last.
            rank = 0 if colour in {"green", "yellow", "red"} else 1
            candidates.append((rank, y0 / max(height, 1), -score, cleaned))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][3]
