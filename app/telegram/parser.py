"""Signal parser.

Turns free-form channel text into a structured signal.  It is deliberately
conservative: anything it cannot read with confidence is reported as unusable,
because the top-level rule is UNKNOWN -> NO TRADE.

Handled shapes (and mixtures of them):

    BTC LONG                |  BTCUSDT LONG    |  #BTC LONG      |  #AVAX
    Entry: 100000-101000    |  Entry 100500    |  BUY 100500     |  Kirish: $12.6
    TP1: 102000             |  TP 103000       |  TP1 102000     |  TP 1: $13.75
    TP2: 104000             |  SL 99000        |  TP2 104000     |  Stop: $11.94
    SL: 98000                                  |  STOP 99000

Labels are read in English, Uzbek (Kirish / Profit / Stop / Zarar) and Russian
(Вход / Цель / Стоп), Telegram markdown is stripped, and when the post never
writes LONG or BUY the side is derived from the numbers - see
:func:`_infer_direction`.

The coin is read from the most explicit shape first (BTC/USDT, then
"Pair: BTC", then #BTC, then the word next to LONG).  Any dollar-stable quote
(USD, USDC, BUSD) is our USDT market; a coin-quoted pair (ETH/BTC) is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

BUY = "BUY"
SHORT = "SHORT"

DEFAULT_QUOTE = "USDT"
# Dollar-stable quotes are all the same market for our purpose: a channel that
# writes BTC/USD or BTC/USDC means the price we trade on the USDT pair.
QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "USD")
# A coin quoted in another coin (ETH/BTC) is a different market with a
# different price scale - it can never be bought with the bot's USDT.
CROSS_QUOTES = ("BTC", "ETH", "BNB")
_QUOTE_ALT = "|".join(QUOTE_ASSETS)
_CROSS_ALT = "|".join(CROSS_QUOTES)

# Tokens that must never be mistaken for a coin ticker.
STOPWORDS = {
    "LONG", "SHORT", "BUY", "SELL", "ENTRY", "ENTRIES", "TP", "SL", "STOP", "STOPLOSS",
    "TARGET", "TARGETS", "TAKE", "PROFIT", "LOSS", "PRICE", "ZONE", "RANGE", "NOW",
    "SPOT", "FUTURES", "MARGIN", "LEVERAGE", "CROSS", "ISOLATED", "SIGNAL", "TRADE",
    "OPEN", "CLOSE", "LIMIT", "MARKET", "CMP", "DCA", "RISK", "TERM", "VIP", "FREE",
    "USDT", "USDC", "BUSD", "USD", "NEW", "UPDATE", "SCALP", "SWING", "HOLD", "WAIT",
    "AT", "TO", "AND", "THE", "FOR", "WITH", "MOVE", "SETUP", "ALERT", "PUMP", "GAIN",
    # Field names that sit right in front of the side ("Direction: LONG") and
    # name suffixes that sit right in front of it ("NEAR Protocol LONG").
    "PAIR", "COIN", "SYMBOL", "TICKER", "ASSET", "DIRECTION", "SIDE", "TYPE",
    "PROTOCOL", "NETWORK", "CHAIN", "FINANCE", "PERPETUAL", "URGENT",
    # Uzbek / Russian label words
    "KIRISH", "KIRIB", "XARID", "SOTIB", "OLISH", "FOYDA", "MAQSAD", "ZARAR", "NUQTASI",
    "NARXI", "ZONASI", "ВХОД", "ВХОДА", "ПОКУПКА", "ЗАКУП", "ЦЕЛЬ", "ТЕЙК", "СТОП",
}

_NUM = r"\d+(?:\.\d+)?"
# A standalone number: not glued to a word (so TP1 never yields 1), not a
# keycap emoji (1️⃣ is "1" + U+FE0F U+20E3) and not a percentage ("(+2.5%)").
_STANDALONE_NUM = re.compile(r"(?<![A-Z0-9.])" + _NUM + r"(?![0-9\uFE0F\u20E3])(?!\s*%)")
# A list index at the start of a value line: "1) 36670", "1. 36670", "1: 36670".
# A dot or dash only counts as the marker when whitespace follows it, so the
# price "1.43" is never split into an index and "43".
_LIST_MARKER = re.compile(r"^[^\S\n]*\d{1,2}[^\S\n]*(?:[):]|[.\-](?=\s))")
# A line that continues a target list: an index, a keycap or a bullet, then a
# number, and no words - "2) 36220 - 20%" yes, "2. 10x leverage" no.
_LIST_LINE = re.compile(
    r"^[^\S\n]*(?:\d{1,2}[^\S\n]*(?:[):]|[.\-](?=\s))|\d\uFE0F?\u20E3|[-•●▪‣➤→✅🎯🔸🔹]+)"
    r"[^\S\n]*[$€]?\d[^A-ZА-Я\n]*$"
)

# What may follow a label before its value: a separator and, at most, one line
# break - "Entry Zone:" with the numbers on the next line is common, but a
# blank line ends the label, so an empty "Target :" never reads the "1" out of
# the "TARGET 1 : 1.43" that follows it.
_LABEL_TAIL = r"[^\S\n]*[:=\-@]?[^\S\n]*\n?[^\S\n]*"

# The optional index after a label (TP1, TP 2:, Entry 2:) must not swallow the
# price: detached digits only count as an index when a separator follows them,
# and a dot is a separator only when no digit follows it - "TP 14.5" is a price.
_INDEX = r"(?:\d{1,2}|[^\S\n]*\d{1,2}(?=[^\S\n]*(?:[:)]|\.(?!\d))))?"

# Labels are matched in English, Uzbek and Russian - the channels write in all
# three, often mixed inside one post.
_ENTRY_LABEL = re.compile(
    r"\b(?:ENTRY(?:\s*(?:ZONE|PRICE|POINT|RANGE|AROUND))?" + _INDEX +
    r"|BUY(?:\s*(?:ZONE|PRICE|RANGE|AREA|AROUND|AT))?|GET\s*IN"
    # LONG only carries the price with an explicit separator ("LONG : 1.38"),
    # so "BTC LONG 10x" stays a side and never becomes an entry of 10.
    r"|LONG(?=[^\S\n]*[:=@])"
    # A bare "Zone:" is an entry zone when it starts the line - "TP Zone" is not.
    r"|(?<![A-Z] )(?<![A-Z])ZONE"
    r"|LIMIT(?:\s*ORDER)?|OPEN"
    r"|KIRISH(?:\s*(?:NUQTASI|NARXI|ZONASI))?|KIRIB|XARID|SOTIB\s*OLISH"
    r"|ВХОДА?|ПОКУПКА|ЗАКУП)\b" + _LABEL_TAIL
)
# "Stop Targets:" (the Cornix layout) is a stop, not a target.
_TP_LABEL = re.compile(
    r"\b(?:TP|TAKE\s*-?\s*PROFIT|PROFIT|FOYDA|MAQSAD|(?<!STOP )(?<!STOP)TARGETS?|ЦЕЛЬ|ТЕЙК)"
    + _INDEX + _LABEL_TAIL
)
_SL_LABEL = re.compile(
    r"\b(?:SL|STOP\s*-?\s*LOSS|STOPLOSS|STOP(?:\s*TARGETS?)?|ZARAR(?:\s*TO.?XTATISH)?|СТОП)\b"
    + _LABEL_TAIL
)

_RANGE_SEP = r"(?:\s*(?:-|~|/|\.\.\.|\.\.|TO)\s*)"
_ENTRY_VALUES = re.compile(r"(" + _NUM + r")" + _RANGE_SEP + r"(" + _NUM + r")|(" + _NUM + r")")

_SYMBOL_WITH_QUOTE = re.compile(
    r"[#$]?\b([A-Z][A-Z0-9]{1,14})\s*[/\-_]?\s*(" + _QUOTE_ALT + r")\b"
)
# A coin-quoted pair needs an explicit separator: "ETH / BTC", never "SOL BTC".
_SYMBOL_WITH_CROSS_QUOTE = re.compile(
    r"\b([A-Z][A-Z0-9]{1,14})\s*[/\-_]\s*(" + _CROSS_ALT + r")\b"
)
# "Pair: AVAX-PERP", "Coin: #SOL" - the ticker named by a field label.
_SYMBOL_AFTER_FIELD = re.compile(
    r"\b(?:PAIR|COIN|SYMBOL|TOKEN|TICKER|ASSET|JUFTLIK|ПАРА|МОНЕТА|ТИКЕР)"
    r"[^\S\n]*[:=\-]?[^\S\n]*[#$]?([A-Z][A-Z0-9]{1,14})\b"
)
_HASHTAG = re.compile(r"[#$]([A-Z][A-Z0-9]{1,14})\b")
# Up to two words in front of the side, so "NEAR Protocol LONG" and
# "Signal BTC LONG" both yield the coin once the stopwords are dropped.
_SYMBOL_BEFORE_SIDE = re.compile(
    r"((?:\b[A-Z][A-Z0-9]{1,14}\b[\s:,\-]*){1,2})\b(?:LONG|SHORT|BUY|SELL)\b"
)
_SYMBOL_AFTER_SIDE = re.compile(r"\b(?:LONG|SHORT|BUY|SELL)\b[\s:,\-]*\b([A-Z][A-Z0-9]{1,14})\b")
_WORD = re.compile(r"[A-Z][A-Z0-9]{1,14}")

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "‐": "-"})
# Telegram markdown glues itself to words (**Kirish:**, __#SOL__) and would
# break both the labels and the ticker match.
_MARKDOWN = re.compile(r"[*_`]+")
# Currency and decoration in front of a price: Kirish: **$12.6
_PRICE_JUNK = "$€₽*_~≈ \t"


@dataclass(frozen=True)
class ParsedSignal:
    symbol: str                       # BTCUSDT
    base_asset: str                   # BTC
    direction: str                    # BUY | SHORT
    direction_inferred: bool = False  # read from SL/TP geometry, not from a word
    entry_low: Optional[Decimal] = None
    entry_high: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profits: list[Decimal] = field(default_factory=list)
    raw_text: str = ""

    @property
    def is_buy(self) -> bool:
        return self.direction == BUY


@dataclass(frozen=True)
class ParseResult:
    """Outcome of parsing one message.

    signal is filled whenever a coin and a direction were found - even for a
    SHORT, so the caller can log and ignore it explicitly.  reason is set when
    the message cannot produce a tradable signal.  looks_like_signal says
    whether the message was signal-shaped at all: plain chat is dropped
    silently, while a broken signal is worth reporting.
    """

    signal: Optional[ParsedSignal] = None
    reason: Optional[str] = None
    looks_like_signal: bool = False

    @property
    def ok(self) -> bool:
        return self.signal is not None and self.reason is None


def normalize(text: str) -> str:
    text = text.translate(_DASHES).upper()
    text = text.replace(" ", " ")
    text = _MARKDOWN.sub(" ", text)
    return _THOUSANDS.sub("", text)          # 100,000 -> 100000


def _to_decimal(raw: str) -> Optional[Decimal]:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def _numbers_after(text: str, start: int) -> list[Decimal]:
    """Every standalone number from start to the end of that line."""
    end = text.find("\n", start)
    chunk = text[start:] if end == -1 else text[start:end]
    chunk = _LIST_MARKER.sub("", chunk, count=1)     # "1) 36670" -> " 36670"
    out: list[Decimal] = []
    for match in _STANDALONE_NUM.finditer(chunk):
        value = _to_decimal(match.group(0))
        if value is not None:
            out.append(value)
    return out


def _extract_symbol(text: str) -> Optional[tuple[str, str]]:
    """Return (base_asset, quote_asset), or None when no ticker is readable.

    The quote is the one written in the post (USDT, USD, BTC, ...); the caller
    decides whether that market is tradable.  Shapes are tried from the most
    to the least explicit, so "#SOL ... ETH/BTC looks weak" still reads SOL.
    """
    for match in _SYMBOL_WITH_QUOTE.finditer(text):
        base, quote = match.group(1), match.group(2)
        if base not in STOPWORDS:
            return base, quote
    for pattern in (_SYMBOL_AFTER_FIELD, _HASHTAG):
        for match in pattern.finditer(text):
            base = match.group(1)
            if base not in STOPWORDS:
                return base, DEFAULT_QUOTE
    for match in _SYMBOL_WITH_CROSS_QUOTE.finditer(text):
        base, quote = match.group(1), match.group(2)
        if base not in STOPWORDS:
            return base, quote
    for match in _SYMBOL_BEFORE_SIDE.finditer(text):
        # The word nearest the side wins once the field names are dropped:
        # "Signal BTC LONG" -> BTC, "NEAR Protocol LONG" -> NEAR.
        words = [w for w in _WORD.findall(match.group(1)) if w not in STOPWORDS]
        if words:
            return words[-1], DEFAULT_QUOTE
    for match in _SYMBOL_AFTER_SIDE.finditer(text):
        base = match.group(1)
        if base not in STOPWORDS:
            return base, DEFAULT_QUOTE
    return None


def _extract_direction(text: str) -> Optional[str]:
    # LONG/SHORT win over BUY/SELL, so "LONG ... sell at TP" stays a BUY.
    if re.search(r"\bLONG\b", text):
        return BUY
    if re.search(r"\bSHORT\b", text):
        return SHORT
    if re.search(r"\bBUY\b", text):
        return BUY
    if re.search(r"\bSELL\b", text):
        return SHORT
    return None


def _infer_direction(
    entry_low: Optional[Decimal],
    entry_high: Optional[Decimal],
    stop_loss: Optional[Decimal],
    take_profits: list[Decimal],
) -> Optional[str]:
    """Read the direction from the numbers when no LONG/SHORT word is written.

    Many channels post a complete setup without ever naming the side:

        #AVAX
        Kirish: 12.6      SL below the entry and TP above it can only be a BUY;
        TP 1: 13.75       the mirror image can only be a SHORT, which is then
        Stop: 11.94       ignored like any other short.

    Anything that is not unambiguous stays undecided, so it is not traded.
    """
    if entry_low is None or entry_high is None or stop_loss is None or not take_profits:
        return None
    if stop_loss < entry_low and any(tp > entry_high for tp in take_profits):
        return BUY
    if stop_loss > entry_high and any(tp < entry_low for tp in take_profits):
        return SHORT
    return None


def _entry_values_after(text: str, start: int) -> Optional[tuple[Decimal, Decimal]]:
    """The price or price range that follows an entry label, as (low, high)."""
    tail_end = text.find("\n", start)
    tail = text[start:] if tail_end == -1 else text[start:tail_end]
    match = _ENTRY_VALUES.match(tail.strip().lstrip(_PRICE_JUNK))
    if not match:
        return None
    if match.group(1) and match.group(2):
        low, high = _to_decimal(match.group(1)), _to_decimal(match.group(2))
    else:
        low = high = _to_decimal(match.group(3))
    if low is None or high is None:
        return None
    return (low, high) if low <= high else (high, low)


def _extract_entry(text: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """The entry zone.

    The first entry label wins.  Further labels only widen the zone when they
    are numbered DCA legs ("Entry: 0.142" then "Entry 2: 0.138" is one ladder,
    bought as the zone 0.138-0.142); any other later label is ignored, so an
    "Open: 12:00" further down can never stretch the zone to 12.
    """
    low = high = None
    for label in _ENTRY_LABEL.finditer(text):
        values = _entry_values_after(text, label.end())
        if values is None:
            continue
        if low is None or high is None:
            low, high = values
        elif any(ch.isdigit() for ch in label.group(0)):
            low, high = min(low, values[0]), max(high, values[1])
    return low, high


def _extract_take_profits(text: str) -> list[Decimal]:
    """Every target after a TP label, including a list on the following lines:

        Targets:
        1) 36670 - 20%      each line is an index / bullet plus a number and
        2) 36220 - 20%      nothing else; the first line that is not stops
        Stop: 39100         the list.
    """
    values: list[Decimal] = []

    def add(found: list[Decimal]) -> None:
        for value in found:
            if value not in values:
                values.append(value)

    for label in _TP_LABEL.finditer(text):
        add(_numbers_after(text, label.end()))
        line_end = text.find("\n", label.end())
        while line_end != -1:
            start = line_end + 1
            line_end = text.find("\n", start)
            line = text[start:] if line_end == -1 else text[start:line_end]
            if not _LIST_LINE.match(line):
                break
            add(_numbers_after(text, start))
    return sorted(values)


def _extract_stop_loss(text: str) -> Optional[Decimal]:
    for label in _SL_LABEL.finditer(text):
        numbers = _numbers_after(text, label.end())
        if numbers:
            return numbers[0]
    return None


def parse_signal(
    raw_text: str, quote_asset: str = DEFAULT_QUOTE, infer_direction: bool = True
) -> ParseResult:
    """Parse one channel message into a ParseResult."""
    if not raw_text or not raw_text.strip():
        return ParseResult(reason="empty message")

    text = normalize(raw_text)

    has_entry_label = bool(_ENTRY_LABEL.search(text))
    has_tp_label = bool(_TP_LABEL.search(text))
    has_sl_label = bool(_SL_LABEL.search(text))
    label_count = sum((has_entry_label, has_tp_label, has_sl_label))

    entry_low, entry_high = _extract_entry(text)
    stop_loss = _extract_stop_loss(text)
    take_profits = _extract_take_profits(text)

    direction = _extract_direction(text)
    inferred = False
    if direction is None and infer_direction:
        direction = _infer_direction(entry_low, entry_high, stop_loss, take_profits)
        inferred = direction is not None

    looks_like_signal = (direction is not None and label_count >= 1) or label_count >= 2

    if direction is None:
        return ParseResult(
            reason="no BUY/LONG/SHORT direction found", looks_like_signal=looks_like_signal
        )

    found = _extract_symbol(text)
    if found is None:
        return ParseResult(reason="coin symbol not found", looks_like_signal=looks_like_signal)
    base_asset, parsed_quote = found
    if parsed_quote in CROSS_QUOTES:
        # ETH/BTC is priced in BTC - nothing here can be bought with USDT.
        return ParseResult(
            reason=f"{base_asset}/{parsed_quote} is not a {quote_asset} pair",
            looks_like_signal=True,
        )
    # Every dollar-stable quote is traded on the bot's own pair.
    symbol = f"{base_asset}{quote_asset}"

    if direction == SHORT:
        # Spot only - a short can never be executed.  Reported, never traded.
        signal = ParsedSignal(
            symbol=symbol, base_asset=base_asset, direction=SHORT,
            direction_inferred=inferred, raw_text=raw_text,
        )
        return ParseResult(signal=signal, reason="SHORT signal (spot only)", looks_like_signal=True)

    signal_shaped = looks_like_signal or entry_low is not None

    def result(reason: Optional[str], tps: list[Decimal], shaped: bool) -> ParseResult:
        parsed = ParsedSignal(
            symbol=symbol,
            base_asset=base_asset,
            direction=BUY,
            direction_inferred=inferred,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=tps,
            raw_text=raw_text,
        )
        return ParseResult(signal=parsed, reason=reason, looks_like_signal=shaped)

    missing = []
    if entry_low is None:
        missing.append("entry")
    if stop_loss is None:
        missing.append("SL")
    if not take_profits:
        missing.append("TP")
    if missing:
        return result("missing " + ", ".join(missing), take_profits, signal_shaped)

    assert entry_low is not None and entry_high is not None and stop_loss is not None
    if stop_loss >= entry_low:
        return result(f"SL {stop_loss} is not below the entry {entry_low}", take_profits, True)

    usable_tps = [tp for tp in take_profits if tp > entry_high]
    if not usable_tps:
        return result(f"no TP above the entry {entry_high}", take_profits, True)

    return result(None, usable_tps, True)
