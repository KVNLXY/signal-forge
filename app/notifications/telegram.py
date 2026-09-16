"""Telegram notifications (and a small admin command interface).

Messages are sent as plain text through the Bot API, so no escaping surprises.
A notification failure is logged and swallowed - it must never break trading.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from decimal import Decimal
from typing import Awaitable, Callable, Optional, Sequence

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE = 4000


def fmt(value: Optional[Decimal]) -> str:
    """Compact price: 100000, 0.0025, 3500.5 - never scientific notation."""
    if value is None:
        return "-"
    value = Decimal(value)
    normalized = value.normalize()
    text = format(normalized, "f")
    return text


def fmt_money(value: Optional[Decimal]) -> str:
    if value is None:
        return "-"
    quantized = Decimal(value).quantize(Decimal("0.01"))
    sign = "+" if quantized > 0 else ""
    return f"{sign}${quantized}"


def fmt_list(values: Sequence[Decimal]) -> str:
    return " / ".join(fmt(v) for v in values) if values else "-"


def fmt_entry(low: Optional[Decimal], high: Optional[Decimal]) -> str:
    if low is None:
        return "-"
    if high is None or high == low:
        return fmt(low)
    return f"{fmt(low)}-{fmt(high)}"


class TelegramNotifier:
    """Sends the run-time reports described in the spec."""

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        error_throttle_seconds: int = 300,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        self._client = httpx.AsyncClient(timeout=15.0)
        self._error_throttle = error_throttle_seconds
        self._last_error: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    async def send(self, text: str, buttons: Optional[list[list[dict]]] = None) -> bool:
        if not self._enabled:
            log.info("notification (telegram disabled):\n%s", text)
            return False
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text[:MAX_MESSAGE],
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        for attempt in (1, 2):
            try:
                response = await self._client.post(
                    f"{TELEGRAM_API}/bot{self._token}/sendMessage", json=payload
                )
                if response.status_code == 200:
                    return True
                log.warning("telegram sendMessage failed (%s): %s",
                            response.status_code, response.text[:200])
            except httpx.HTTPError as exc:
                log.warning("telegram sendMessage error (attempt %d): %s", attempt, exc)
            await asyncio.sleep(1)
        return False

    async def send_photo(
        self, image: bytes, caption: str, buttons: Optional[list[list[dict]]] = None
    ) -> bool:
        """Send the chart itself, so the numbers can be checked against it."""
        if not self._enabled:
            log.info("photo notification (telegram disabled):\n%s", caption)
            return False
        data: dict[str, str] = {"chat_id": self._chat_id, "caption": caption[:1024]}
        if buttons:
            data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        try:
            response = await self._client.post(
                f"{TELEGRAM_API}/bot{self._token}/sendPhoto",
                data=data,
                files={"photo": ("chart.jpg", image, "image/jpeg")},
            )
            if response.status_code == 200:
                return True
            log.warning("telegram sendPhoto failed (%s): %s",
                        response.status_code, response.text[:200])
        except httpx.HTTPError as exc:
            log.warning("telegram sendPhoto error: %s", exc)
        # Fall back to text so the signal is never silently lost.
        return await self.send(caption, buttons)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # message templates
    # ------------------------------------------------------------------ #
    async def signal_received(
        self,
        symbol: str,
        entry_low: Optional[Decimal],
        entry_high: Optional[Decimal],
        sl: Optional[Decimal],
        take_profits: Sequence[Decimal],
        halal: bool = True,
        status: str = "WAITING",
        source: str = "",
        direction_inferred: bool = False,
    ) -> None:
        lines = [
            "📡 SIGNAL",
            "",
            f"{symbol} LONG",
            "",
            f"Entry: {fmt_entry(entry_low, entry_high)}",
            f"SL: {fmt(sl)}",
            f"TP: {fmt_list(take_profits)}",
            "",
            f"Halal: {'✅' if halal else '❌'}",
            f"Status: {status}",
        ]
        if direction_inferred:
            lines.append("Direction: read from entry/SL/TP (no LONG/BUY word)")
        if source:
            lines.append(f"Source: {source}")
        await self.send("\n".join(lines))

    async def signal_skipped(self, symbol: str, reason: str, source: str = "") -> None:
        lines = ["⚪ SIGNAL SKIPPED", "", symbol, "", "Reason:", reason]
        if source:
            lines += ["", f"Source: {source}"]
        await self.send("\n".join(lines))

    async def trade_opened(
        self,
        symbol: str,
        price: Decimal,
        quote_amount: Decimal,
        sl: Decimal,
        take_profits: Sequence[Decimal],
        mode: str,
    ) -> None:
        text = "\n".join([
            "🟢 BUY",
            "",
            symbol,
            "",
            f"Price: {fmt(price)}",
            f"Amount: ${Decimal(quote_amount).quantize(Decimal('0.01'))}",
            "",
            f"SL: {fmt(sl)}",
            f"TP: {fmt_list(take_profits)}",
            "",
            f"Mode: {mode}",
        ])
        await self.send(text)

    async def trade_closed(
        self,
        symbol: str,
        entry_price: Decimal,
        exit_price: Decimal,
        pnl: Decimal,
        pnl_percent: Optional[Decimal],
        reason: str,
        mode: str,
        partial: bool = False,
        remaining_percent: Optional[Decimal] = None,
    ) -> None:
        lines = [
            "🟡 PARTIAL SELL" if partial else "🔴 SELL",
            "",
            symbol,
            "",
            f"Entry: {fmt(entry_price)}",
            f"Exit: {fmt(exit_price)}",
            "",
            f"PNL: {fmt_money(pnl)}",
        ]
        if pnl_percent is not None:
            lines[-1] += f" ({Decimal(pnl_percent).quantize(Decimal('0.01'))}%)"
        lines += ["", f"Reason: {reason}"]
        if partial and remaining_percent is not None:
            lines.append(f"Remaining: {Decimal(remaining_percent).quantize(Decimal('0.1'))}%")
        lines += ["", f"Mode: {mode}"]
        await self.send("\n".join(lines))

    async def signal_needs_confirmation(
        self,
        signal_id: int,
        symbol: str,
        entry: Decimal,
        sl: Decimal,
        take_profits: Sequence[Decimal],
        live_price: Optional[Decimal],
        source: str = "",
        image: Optional[bytes] = None,
        notes: Sequence[str] = (),
        title: str = "📷 SIGNAL FROM A CHART IMAGE",
        footer: str = "Read by OCR - check it against the chart before confirming.",
    ) -> None:
        """Ask before trading anything that was read off a picture (or by
        the model) rather than by the parser."""
        lines = [
            title,
            "",
            f"{symbol} LONG",
            "",
            f"Entry: {fmt(entry)}",
            f"SL: {fmt(sl)}",
            f"TP: {fmt_list(take_profits)}",
        ]
        if live_price is not None:
            drift = (entry - live_price) / live_price * Decimal(100) if live_price else None
            lines.append("")
            lines.append(f"Live price: {fmt(live_price)}")
            if drift is not None:
                lines.append(f"Entry vs price: {drift.quantize(Decimal('0.01'))}%")
        if notes:
            lines += ["", *[str(note) for note in notes]]
        if source:
            lines += ["", f"Source: {source}"]
        lines += ["", footer]

        buttons = [[
            {"text": "✅ Confirm", "callback_data": f"sig:{signal_id}:ok"},
            {"text": "❌ Reject", "callback_data": f"sig:{signal_id}:no"},
        ]]
        text = "\n".join(lines)
        if image:
            await self.send_photo(image, text, buttons)
        else:
            await self.send(text, buttons)

    async def update_needs_confirmation(
        self,
        update_id: int,
        symbol: str,
        instruction: str,
        source: str = "",
        quoted: str = "",
        state: Sequence[str] = (),
        warning: str = "",
    ) -> None:
        """Ask before acting on a follow-up post about a running trade."""
        lines = ["📝 CHANNEL UPDATE", "", f"{symbol}: {instruction}"]
        if state:
            lines += ["", *state]
        if quoted:
            lines += ["", f"«{quoted[:300]}»"]
        if source:
            lines.append(f"- {source}")
        if warning:
            lines += ["", f"⚠️ {warning}"]
        buttons = [[
            {"text": "✅ Do it", "callback_data": f"upd:{update_id}:ok"},
            {"text": "❌ Ignore", "callback_data": f"upd:{update_id}:no"},
        ]]
        await self.send("\n".join(lines), buttons)

    async def error(self, context: str, exc: BaseException) -> None:
        """Report a failure, throttled so one broken loop cannot spam."""
        key = f"{context}:{type(exc).__name__}"
        now = time.monotonic()
        last = self._last_error.get(key, 0.0)
        if now - last < self._error_throttle:
            return
        self._last_error[key] = now
        await self.send("\n".join([
            "🚨 ERROR",
            "",
            context,
            "",
            f"{type(exc).__name__}: {exc}"[:1000],
        ]))

    async def info(self, text: str) -> None:
        await self.send(text)


class AdminCommands:
    """Minimal /command interface on the notification bot (admin chat only)."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        handlers: dict[str, Callable[..., Awaitable[str]]],
        poll_timeout: int = 30,
        on_button: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> None:
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._handlers = handlers
        self._on_button = on_button
        self._poll_timeout = poll_timeout
        self._offset: Optional[int] = None
        self._client = httpx.AsyncClient(timeout=poll_timeout + 15)
        self._enabled = bool(bot_token and chat_id)

    async def run_forever(self) -> None:
        if not self._enabled:
            log.info("admin commands disabled (no bot token / chat id)")
            return
        log.info("admin commands ready: %s", ", ".join(sorted(self._handlers)))
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let the poller die
                log.warning("admin command poll failed: %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(5)

    async def _poll_once(self) -> None:
        params: dict[str, object] = {"timeout": self._poll_timeout}
        if self._offset is not None:
            params["offset"] = self._offset
        response = await self._client.get(
            f"{TELEGRAM_API}/bot{self._token}/getUpdates", params=params
        )
        if response.status_code != 200:
            await asyncio.sleep(5)
            return
        for update in response.json().get("result", []):
            self._offset = int(update["update_id"]) + 1

            callback = update.get("callback_query")
            if callback is not None:
                await self._handle_button(callback)
                continue

            message = update.get("message") or update.get("channel_post") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = str(message.get("text", "")).strip()
            if chat_id != self._chat_id or not text.startswith("/"):
                continue
            await self._dispatch(text)

    async def _handle_button(self, callback: dict) -> None:
        """A confirm / reject button was pressed on a chart signal."""
        callback_id = str(callback.get("id", ""))
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        data = str(callback.get("data", ""))

        if chat_id != self._chat_id or self._on_button is None:
            await self._answer_callback(callback_id, "Not allowed.")
            return
        try:
            answer = await self._on_button(data)
        except Exception as exc:
            log.exception("button %s failed", data)
            answer = f"Failed: {type(exc).__name__}: {exc}"

        await self._answer_callback(callback_id, answer[:200])
        # Drop the buttons so the same signal cannot be confirmed twice.
        message_id = message.get("message_id")
        if message_id is not None:
            await self._clear_buttons(int(message_id), answer)

    async def _answer_callback(self, callback_id: str, text: str) -> None:
        if not callback_id:
            return
        try:
            await self._client.post(
                f"{TELEGRAM_API}/bot{self._token}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text, "show_alert": False},
            )
        except httpx.HTTPError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)

    async def _clear_buttons(self, message_id: int, outcome: str) -> None:
        for endpoint, payload in (
            ("editMessageReplyMarkup",
             {"chat_id": self._chat_id, "message_id": message_id,
              "reply_markup": {"inline_keyboard": []}}),
            ("sendMessage",
             {"chat_id": self._chat_id, "text": outcome[:MAX_MESSAGE],
              "reply_to_message_id": message_id}),
        ):
            try:
                await self._client.post(f"{TELEGRAM_API}/bot{self._token}/{endpoint}", json=payload)
            except httpx.HTTPError as exc:
                log.warning("%s failed: %s", endpoint, exc)

    async def _dispatch(self, text: str) -> None:
        head, _, argument = text.strip().partition(" ")
        command = head.lstrip("/").split("@")[0].lower()
        handler = self._handlers.get(command)
        if handler is None:
            known = ", ".join(f"/{name}" for name in sorted(self._handlers))
            await self._reply(f"Unknown command. Available: {known}")
            return
        try:
            # "/close BTC": a handler that takes a parameter gets the rest
            # of the line; the others are called bare.
            if inspect.signature(handler).parameters:
                await self._reply(await handler(argument.strip()))
            else:
                await self._reply(await handler())
        except Exception as exc:
            log.exception("admin command /%s failed", command)
            await self._reply(f"Command failed: {type(exc).__name__}: {exc}")

    async def _reply(self, text: str) -> None:
        try:
            await self._client.post(
                f"{TELEGRAM_API}/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text[:MAX_MESSAGE]},
            )
        except httpx.HTTPError as exc:
            log.warning("admin reply failed: %s", exc)

    async def close(self) -> None:
        await self._client.aclose()
