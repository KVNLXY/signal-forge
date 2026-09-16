"""A second reader for posts the regex parser could not turn into a signal.

The channels write in three languages with their own habits, and every new
shape used to mean a new regex.  When the parser gives up on a post that
still looks like it carries levels, the post is handed to Claude with a fixed
JSON shape to fill in.  Two rules keep this honest with money:

* the model copies numbers, it never invents them - every price comes back
  as the string that was written, and the same gates a text signal passes
  (halal list, live-price sanity, no duplicate) are applied afterwards;
* by default the reading is only *offered*: it waits for the same Confirm
  button a chart screenshot does.  Nothing is bought on the model's word.

The SDK is optional.  Without ``anthropic`` installed, or without a key, the
reader reports itself unavailable and the bot works exactly as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel

from app.telegram.parser import BUY, SHORT, _to_decimal

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"

# Sent once per call, unchanged, so it caches; the post itself is the user turn.
SYSTEM_PROMPT = """You read cryptocurrency trading signals posted in Telegram channels. Posts are in English, Uzbek or Russian, often mixed, with emoji and Telegram markdown.

Fill the JSON schema from what is WRITTEN in the post and nothing else:
- is_signal: true only if the post tells readers to open a trade in a named coin. Results ("TP1 hit"), analysis, chatter, and management of an existing trade ("close 50%", "stop to breakeven") are NOT signals.
- symbol: the coin ticker without the quote asset (BTC, not BTCUSDT). null if none.
- side: "long" for buy/long, "short" for sell/short. If no side word is written, use the levels: stop below entry and target above it is "long"; the mirror is "short"; otherwise null.
- entry_low / entry_high: the entry price or zone, copied exactly as written (decimal strings, no thousands separators, no currency signs). A single price goes in both fields. Uzbek "Kirish"/"Xarid", Russian "Вход"/"Закуп" mean entry.
- stop_loss: the stop price as written. Uzbek "Stop"/"Zarar", Russian "Стоп". null if not written.
- take_profits: every target price in the order written. Uzbek "Profit"/"Foyda"/"Maqsad", Russian "Цель"/"Тейк".
- notes: one short sentence on anything ambiguous you had to decide.

Never invent or compute a number. Percentages are not prices. If a value is not in the post, it is null (or an empty list)."""


class LlmReading(BaseModel):
    is_signal: bool
    symbol: Optional[str] = None
    side: Optional[str] = None
    entry_low: Optional[str] = None
    entry_high: Optional[str] = None
    stop_loss: Optional[str] = None
    take_profits: list[str] = []
    notes: Optional[str] = None


@dataclass(frozen=True)
class LlmSignal:
    """What the engine works with: the reading turned into prices."""

    base_asset: str
    direction: str                          # BUY | SHORT
    entry_low: Optional[Decimal]
    entry_high: Optional[Decimal]
    stop_loss: Optional[Decimal]
    take_profits: list[Decimal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


Ask = Callable[[str], Awaitable[Optional[LlmReading]]]


def _clean(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None:
        return None
    cleaned = str(raw).strip().replace(",", "").replace("$", "").replace(" ", "")
    return _to_decimal(cleaned) if cleaned else None


def to_signal(reading: LlmReading) -> Optional[LlmSignal]:
    """A reading becomes a signal only when it is a signal with a coin and
    a side; the prices are left as read - the engine's gates judge them."""
    if not reading.is_signal or not reading.symbol:
        return None
    side = (reading.side or "").strip().lower()
    if side not in ("long", "short"):
        return None
    base = "".join(ch for ch in reading.symbol.upper() if ch.isalnum())
    if not base:
        return None
    low, high = _clean(reading.entry_low), _clean(reading.entry_high)
    if low is None or high is None:
        low = high = low or high
    if low is not None and high is not None and low > high:
        low, high = high, low
    targets = [t for t in (_clean(v) for v in reading.take_profits) if t is not None]
    notes = [f"Read by Claude ({reading.notes.strip()})" if reading.notes else "Read by Claude"]
    return LlmSignal(
        base_asset=base,
        direction=BUY if side == "long" else SHORT,
        entry_low=low,
        entry_high=high,
        stop_loss=_clean(reading.stop_loss),
        take_profits=targets,
        notes=notes,
    )


class LlmSignalReader:
    """Asks Claude to read a post; ``ask`` is injectable so tests never call out."""

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        ask: Optional[Ask] = None,
        timeout: float = 30.0,
    ) -> None:
        self._model = model
        self._ask = ask
        self._client = None
        self._reason = ""
        if ask is None:
            if not api_key:
                self._reason = "no LLM_API_KEY"
            else:
                try:
                    import anthropic
                except ImportError:
                    self._reason = "the anthropic package is not installed"
                else:
                    self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)

    @property
    def available(self) -> bool:
        return self._ask is not None or self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    @property
    def model(self) -> str:
        return self._model

    async def read(self, text: str) -> Optional[LlmSignal]:
        """The signal in a post, or None.  Never raises - a reader outage
        must not take the bot down, the post is simply not traded."""
        if not text or not text.strip() or not self.available:
            return None
        try:
            reading = await (self._ask(text) if self._ask is not None else self._ask_claude(text))
        except Exception as exc:
            log.warning("LLM reader failed: %s: %s", type(exc).__name__, exc)
            return None
        if reading is None:
            return None
        return to_signal(reading)

    async def _ask_claude(self, text: str) -> Optional[LlmReading]:
        assert self._client is not None
        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=1024,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": text[:4000]}],
            output_format=LlmReading,
            output_config={"effort": "low"},
        )
        if response.stop_reason == "refusal":
            log.info("LLM reader: the model declined the post")
            return None
        usage = response.usage
        log.info("LLM reader: %s tokens in / %s out (cache read %s)",
                 usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens)
        return response.parsed_output
