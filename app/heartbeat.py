"""Dead man's switch: one ping after every successful price-loop pass.

The stop-loss lives in this process, not on the exchange (MEXC spot has no
stop orders), so a bot that has silently died - or one that runs but cannot
fetch prices - leaves every open position without a stop.  A heartbeat URL
(healthchecks.io, Uptime Kuma, cronitor, ...) that stops receiving pings is
what raises the alarm on your phone.

The ping is rate-limited to HEARTBEAT_INTERVAL_SECONDS and is only sent after
a tick that actually read prices and checked every position; a loop that is
alive but failing sends nothing, on purpose.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(
        self,
        url: str = "",
        interval_seconds: int = 60,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._url = url.strip()
        self._interval = max(1, interval_seconds)
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._last_sent = 0.0
        self._failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    @property
    def interval(self) -> int:
        return self._interval

    async def beat(self, force: bool = False) -> bool:
        """Ping if the interval has passed.  Never raises - a monitoring
        outage must not touch trading."""
        if not self._url:
            return False
        now = time.monotonic()
        if not force and now - self._last_sent < self._interval:
            return False
        try:
            response = await self._client.get(self._url)
        except httpx.HTTPError as exc:
            self._failures += 1
            if self._failures in (1, 10, 100):      # log the first, then rarely
                log.warning("heartbeat ping failed (%d in a row): %s", self._failures, exc)
            return False
        self._last_sent = now
        if response.status_code >= 400:
            self._failures += 1
            log.warning("heartbeat endpoint answered %s", response.status_code)
            return False
        if self._failures:
            log.info("heartbeat ping works again after %d failures", self._failures)
        self._failures = 0
        return True

    async def close(self) -> None:
        await self._client.aclose()
