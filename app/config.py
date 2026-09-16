"""Configuration.  Everything comes from the environment / .env file.

Secrets are wrapped in ``SecretStr`` so they never end up in a log line or a
traceback by accident (see also :mod:`app.logging_setup`).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Literal, Optional

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TradingMode = Literal["PAPER", "LIVE"]


def split_csv(raw: str) -> list[str]:
    """Split a comma/semicolon separated env value into clean items."""
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- mode --------------------------------------------------------------
    # Never defaults to LIVE.  Live trading is an explicit, deliberate choice.
    trading_mode: TradingMode = "PAPER"

    # --- telegram user account (listener) ----------------------------------
    telegram_api_id: int = 0
    telegram_api_hash: SecretStr = SecretStr("")
    telegram_session: SecretStr = SecretStr("")
    telegram_channels: str = ""

    # --- mexc --------------------------------------------------------------
    mexc_api_key: SecretStr = SecretStr("")
    mexc_api_secret: SecretStr = SecretStr("")
    mexc_base_url: str = "https://api.mexc.com"
    mexc_recv_window: int = 5000

    # --- notifications -----------------------------------------------------
    telegram_admin_bot_token: SecretStr = SecretStr("")
    telegram_admin_chat_id: str = ""

    # --- halal whitelist ---------------------------------------------------
    halal_coins: str = ""

    # --- risk --------------------------------------------------------------
    trade_amount_usdt: Decimal = Decimal("100")
    max_open_trades: int = 3
    # Circuit breakers.  Each one only ever blocks a new BUY - open positions
    # are always monitored and closed at TP / SL.  0 turns a breaker off.
    # Realised loss over the current UTC day at which buying stops until
    # midnight UTC.
    max_daily_loss_usdt: Decimal = Decimal("0")
    # Closed trades lost in a row at which buying pauses until /resume.
    max_consecutive_losses: int = 0
    # A BUY is held back while (ask - bid) / mid is wider than this: an
    # illiquid book costs the spread on the way in and again on the way out,
    # and a LIMIT order in it rarely fills.
    max_entry_spread_percent: Decimal = Decimal("1")
    # A position that has sat between SL and TP for this long is reported
    # (warn) or sold at market (close), so it stops occupying a slot.  0 = off.
    max_hold_hours: int = 0
    max_hold_action: Literal["warn", "close"] = "warn"

    # --- signal / order handling -------------------------------------------
    signal_expiry_minutes: int = 15
    order_timeout_seconds: int = 60
    use_market_order: bool = False
    # When false, a setup written without the word LONG/BUY is read from its
    # own geometry (SL below entry, TP above it). Set true to demand the word.
    require_explicit_direction: bool = False
    # A signal whose entry is below the live price ("kelsa" - buy when it
    # comes down): wait for the pullback like a resting limit order, or skip
    # it as a missed move (the original rule).
    entry_above_zone: Literal["wait", "skip"] = "wait"
    # How long such a pullback may take before the signal is dropped.
    limit_entry_expiry_hours: int = 24
    # In a discussion group only the admins are a signal source; members'
    # posts (and their screenshots) are ignored.
    group_admin_only: bool = True
    # On (re)start read back the last SIGNAL_EXPIRY_MINUTES of every channel,
    # so a signal posted while the bot was down is still traded if fresh.
    catch_up_on_start: bool = True
    # A post the regex parser cannot read but that still looks like it
    # carries levels is handed to Claude (needs the anthropic package and a
    # key); the reading then waits for your Confirm button like a chart does.
    llm_fallback: bool = True
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-opus-5"
    llm_signal_confirmation: bool = True
    # Follow-up posts about a running trade ("50% yopamiz", "stop to entry",
    # "bekor") are read and tied to the position ...
    channel_updates: bool = True
    # ... and wait for your Confirm button unless this is turned off.
    update_confirmation: bool = True
    # A single-price entry is treated as a small zone: the admins' own rule is
    # "enter up to 0.2-0.3% above the level".
    entry_tolerance_percent: Decimal = Decimal("0.3")
    # After TP1 the stop moves up to the entry price (a free trade).
    move_sl_to_entry_after_tp1: bool = True
    # Chart screenshots: read the levels off the picture (OCR + colour).
    read_chart_images: bool = True
    # An image-derived signal waits for an explicit OK in Telegram.
    image_signal_confirmation: bool = True
    # A read whose entry is further than this from the live price is dropped:
    # a misread digit lands an order of magnitude away and is caught here.
    image_max_price_deviation_percent: Decimal = Decimal("20")
    tp_splits: str = "30,30,40"
    # How far through the spread a LIMIT order is placed so it actually fills.
    limit_slippage_percent: Decimal = Decimal("0.1")
    price_poll_seconds: int = 5
    # rest: the loop polls /ticker/price every PRICE_POLL_SECONDS.
    # websocket: the exchange pushes every top-of-book change and the loop
    # reads the cache (set PRICE_POLL_SECONDS=1 to act on it); a symbol
    # whose last push is older than the max age falls back to REST.
    price_feed: Literal["rest", "websocket"] = "rest"
    price_feed_max_age_seconds: int = 10
    paper_fee_rate: Decimal = Decimal("0.0005")

    # --- infrastructure ----------------------------------------------------
    # Dead man's switch: this URL is pinged after every successful price-loop
    # pass (rate-limited to the interval).  The stop-loss only exists while
    # this process runs, so a monitor that misses the ping is your alarm.
    heartbeat_url: str = ""
    heartbeat_interval_seconds: int = 60
    # Channel posts older than this are deleted from the messages table once
    # an hour; signals and trades are never touched.  0 = keep everything.
    message_retention_days: int = 90
    database_url: str = "postgresql+asyncpg://signalforge:signalforge@localhost:5432/signalforge"
    log_level: str = "INFO"
    quote_asset: str = "USDT"

    # ------------------------------------------------------------------ #
    # normalisation
    # ------------------------------------------------------------------ #
    @field_validator("entry_above_zone", "max_hold_action", "price_feed", mode="before")
    @classmethod
    def _lower_mode(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _upper_mode(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("log_level", "quote_asset", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("trade_amount_usdt", "paper_fee_rate", "limit_slippage_percent",
                     "image_max_price_deviation_percent", "max_daily_loss_usdt",
                     "max_entry_spread_percent", "entry_tolerance_percent", mode="before")
    @classmethod
    def _decimal(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return Decimal(value.strip())
            except InvalidOperation as exc:  # pragma: no cover - config error path
                raise ValueError(f"not a number: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.trade_amount_usdt <= 0:
            raise ValueError("TRADE_AMOUNT_USDT must be > 0")
        if self.max_open_trades < 1:
            raise ValueError("MAX_OPEN_TRADES must be >= 1")
        if self.signal_expiry_minutes < 1:
            raise ValueError("SIGNAL_EXPIRY_MINUTES must be >= 1")
        if self.price_poll_seconds < 1:
            raise ValueError("PRICE_POLL_SECONDS must be >= 1")
        if self.limit_entry_expiry_hours < 1:
            raise ValueError("LIMIT_ENTRY_EXPIRY_HOURS must be >= 1")
        if self.max_daily_loss_usdt < 0:
            raise ValueError("MAX_DAILY_LOSS_USDT must be >= 0 (0 = off)")
        if self.max_consecutive_losses < 0:
            raise ValueError("MAX_CONSECUTIVE_LOSSES must be >= 0 (0 = off)")
        if self.max_entry_spread_percent < 0:
            raise ValueError("MAX_ENTRY_SPREAD_PERCENT must be >= 0 (0 = off)")
        if self.heartbeat_interval_seconds < 1:
            raise ValueError("HEARTBEAT_INTERVAL_SECONDS must be >= 1")
        if self.max_hold_hours < 0:
            raise ValueError("MAX_HOLD_HOURS must be >= 0 (0 = off)")
        if self.price_feed_max_age_seconds < 1:
            raise ValueError("PRICE_FEED_MAX_AGE_SECONDS must be >= 1")
        if self.message_retention_days < 0:
            raise ValueError("MESSAGE_RETENTION_DAYS must be >= 0 (0 = keep)")
        # raises on a malformed TP_SPLITS value at start-up instead of mid-trade
        _ = self.tp_split_ratios
        return self

    # ------------------------------------------------------------------ #
    # derived values
    # ------------------------------------------------------------------ #
    @property
    def is_live(self) -> bool:
        return self.trading_mode == "LIVE"

    @property
    def channels(self) -> list[str]:
        return split_csv(self.telegram_channels)

    @property
    def halal_symbols(self) -> list[str]:
        """Whitelist entries, normalised to full symbols (BTC -> BTCUSDT)."""
        return list(self.halal_sources)

    @property
    def halal_sources(self) -> dict[str, Optional[str]]:
        """Symbol -> where the ruling came from, or None.

        An entry may carry its source after the ticker, so the list documents
        itself: ``BTC:https://sharlife.my/...,ETH:CryptoIslam 2026-03``.
        The source is a note for the admin and the assets table - the bot
        never reads it to decide anything.
        """
        out: dict[str, Optional[str]] = {}
        for item in split_csv(self.halal_coins):
            ticker, _, source = item.partition(":")
            symbol = ticker.strip().upper().replace("/", "").replace("-", "").replace("_", "")
            if not symbol:
                continue
            if not symbol.endswith(self.quote_asset):
                symbol = f"{symbol}{self.quote_asset}"
            if symbol not in out or (source.strip() and not out[symbol]):
                out[symbol] = source.strip() or None
        return out

    @property
    def tp_split_ratios(self) -> list[Decimal]:
        """TP_SPLITS as fractions of the position, e.g. [0.3, 0.3, 0.4]."""
        parts = split_csv(self.tp_splits)
        if not parts:
            return [Decimal(1)]
        try:
            values = [Decimal(p) for p in parts]
        except InvalidOperation as exc:
            raise ValueError(f"TP_SPLITS is not numeric: {self.tp_splits!r}") from exc
        if any(v <= 0 for v in values):
            raise ValueError("TP_SPLITS values must be > 0")
        total = sum(values)
        if total != Decimal(100):
            raise ValueError(f"TP_SPLITS must sum to 100, got {total}")
        return [v / Decimal(100) for v in values]

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.telegram_admin_bot_token.get_secret_value() and self.telegram_admin_chat_id)

    def secret_values(self) -> list[str]:
        """Every secret string, for the log redaction filter."""
        candidates = [
            self.mexc_api_secret.get_secret_value(),
            self.mexc_api_key.get_secret_value(),
            self.telegram_api_hash.get_secret_value(),
            self.telegram_session.get_secret_value(),
            self.telegram_admin_bot_token.get_secret_value(),
            self.llm_api_key.get_secret_value(),
        ]
        return [c for c in candidates if len(c) >= 8]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
