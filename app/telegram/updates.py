"""Reads the follow-up posts a channel makes about a running trade.

A signal channel does not stop talking once the entry is posted:

    "TP1 urdi, 50% yopamiz"           sell half now
    "Yopamiz ✅"                       sell everything
    "Stopni olgan joyga ko'taring"     stop-loss to the entry price
    "SL -> 0.118"                      stop-loss to a price
    "Bekor, kirmaymiz"                 forget the waiting signal

These are the only four things a post can ask for, and each one is read
here into an :class:`Update`.  Nothing here touches money: the engine ties
the update to a position and, by default, asks the admin before acting.

Only a post that is *not* a signal of its own is read as an update - the
engine calls this after the signal parser has said no.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.telegram.parser import _to_decimal, normalize

CLOSE = "close"          # sell all, or ``percent`` of what is left
STOP = "stop"            # move the stop-loss: to the entry or to ``price``
CANCEL = "cancel"        # drop a waiting signal


@dataclass(frozen=True)
class Update:
    action: str
    percent: int = 100                  # CLOSE: share of the remaining position
    price: Optional[Decimal] = None     # STOP: the new level ...
    to_entry: bool = False              # ... or "breakeven" when no level is given
    raw_text: str = ""

    def describe(self) -> str:
        if self.action == CLOSE:
            return "sell everything" if self.percent >= 100 else f"sell {self.percent}%"
        if self.action == STOP:
            return "move SL to the entry price" if self.to_entry else f"move SL to {self.price}"
        return "cancel the waiting signal"


_NUM = r"\d+(?:\.\d+)?"
_STOP_LABEL = r"(?:SL|STOP\s*-?\s*LOSS|STOPLOSS|STOP|STOPNI|СТОП)"
_BREAKEVEN = (
    r"(?:BREAK\s*-?\s*EVEN|\bB/?E\b|\bENTRY\b|ZARARSIZ|OLGAN\s*JOY|KIRGAN\s*JOY|"
    r"KIRISH\s*NARXI|KO.?TAR|БЕЗУБЫТ|\bБ/?У\b|\bВХОД)"
)
_CLOSE_WORDS = (
    r"(?:CLOSE[DS]?|EXIT|SELL|TAKE\s*PROFITS?\s*NOW|BOOK\s*PROFITS?|"
    r"YOPING|YOPAMIZ|YOPILDI|YOPDIK|SOTING|SOTAMIZ|SOTDIK|SOTIB\s*YUBOR|"
    r"ЗАКРЫ(?:ТЬ|ВАЕМ|ВАЙТЕ|ЛИ)|ВЫХОДИМ|ПРОДА[ЁЕ]М|ФИКСИРУЕМ|ФИКС)"
)
_CANCEL_WORDS = r"(?:CANCEL(?:LED)?|BEKOR|ОТМЕН(?:А|ЯЕМ|ЕН[АО]?|ИТЬ))"
_HALF = r"(?:HALF|YARMI(?:NI)?|ПОЛОВИН[АУЫ])"

_STOP_TO_ENTRY = re.compile(_STOP_LABEL + r"\b[^\n]{0,40}?" + _BREAKEVEN)
_ENTRY_TO_STOP = re.compile(_BREAKEVEN + r"[^\n]{0,20}?" + _STOP_LABEL + r"\b")
_STOP_TO_PRICE = re.compile(
    _STOP_LABEL + r"\b[^\n\d]{0,30}?(?<![A-Z0-9.])(" + _NUM + r")(?![0-9])(?!\s*%)"
)
_CLOSE = re.compile(r"\b" + _CLOSE_WORDS + r"\b")
_CANCEL = re.compile(r"\b" + _CANCEL_WORDS + r"\b")
# "+8%" / "-3%" is a result, not a share to sell.
_PERCENT = re.compile(r"(?<![\d.+\-])(\d{1,3})\s*%")
_HALF_RE = re.compile(r"\b" + _HALF + r"\b")


def parse_update(raw_text: str) -> Optional[Update]:
    """The one instruction a follow-up post carries, or None.

    Order of precedence when a post says several things: a close is acted on
    first (it moves money), then a stop move, then a cancel.
    """
    if not raw_text or not raw_text.strip():
        return None
    text = normalize(raw_text)

    if _CLOSE.search(text):
        percent = 100
        if _HALF_RE.search(text):
            percent = 50
        else:
            shares = [int(m.group(1)) for m in _PERCENT.finditer(text)]
            shares = [s for s in shares if 0 < s < 100]
            if shares:
                percent = shares[0]
        return Update(action=CLOSE, percent=percent, raw_text=raw_text)

    if _STOP_TO_ENTRY.search(text) or _ENTRY_TO_STOP.search(text):
        return Update(action=STOP, to_entry=True, raw_text=raw_text)
    match = _STOP_TO_PRICE.search(text)
    if match:
        price = _to_decimal(match.group(1))
        if price is not None:
            return Update(action=STOP, price=price, raw_text=raw_text)

    if _CANCEL.search(text):
        return Update(action=CANCEL, raw_text=raw_text)
    return None
