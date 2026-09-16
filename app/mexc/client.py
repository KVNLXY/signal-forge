"""MEXC Spot API v3 transport.

Endpoints, parameter names and the signature scheme follow the official MEXC
spot v3 documentation (https://mexcdevelop.github.io/apidocs/spot_v3_en/):

* base url            https://api.mexc.com
* signed requests     HMAC SHA256 over the raw query string, using the API
                      secret as key; the hex digest goes into the ``signature``
                      parameter and the key into the ``X-MEXC-APIKEY`` header
* signed requests also carry ``timestamp`` (ms) and optional ``recvWindow``

Only market-data, spot order and account endpoints are used.  Nothing here
can withdraw funds.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

PUBLIC_TIME = "/api/v3/time"
PUBLIC_PING = "/api/v3/ping"
PUBLIC_EXCHANGE_INFO = "/api/v3/exchangeInfo"
PUBLIC_TICKER_PRICE = "/api/v3/ticker/price"
PUBLIC_BOOK_TICKER = "/api/v3/ticker/bookTicker"
PUBLIC_DEPTH = "/api/v3/depth"
PRIVATE_ORDER = "/api/v3/order"
PRIVATE_OPEN_ORDERS = "/api/v3/openOrders"
PRIVATE_ACCOUNT = "/api/v3/account"


class MexcError(Exception):
    """Any MEXC failure the caller may want to report but survive."""


class MexcAPIError(MexcError):
    """MEXC answered with an error payload."""

    def __init__(self, code: int, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(f"MEXC error {code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


class MexcNetworkError(MexcError):
    """Timeout / connection problem after all retries."""


class MexcClient:
    """Small async REST client.  One instance is shared by the whole app."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.mexc.com",
        recv_window: int = 5000,
        timeout: float = 10.0,
        max_retries: int = 3,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret.encode()
        self._base_url = base_url.rstrip("/")
        self._recv_window = recv_window
        self._max_retries = max_retries
        self._time_offset_ms = 0
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
            transport=transport,   # tests inject a MockTransport here
        )

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _sign(self, query: str) -> str:
        return hmac.new(self._api_secret, query.encode(), hashlib.sha256).hexdigest()

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    async def sync_time(self) -> int:
        """Align our clock with MEXC so signed requests are not rejected."""
        data = await self.request("GET", PUBLIC_TIME)
        server_ms = int(data["serverTime"])
        self._time_offset_ms = server_ms - int(time.time() * 1000)
        log.info("MEXC time offset: %d ms", self._time_offset_ms)
        return self._time_offset_ms

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        """Perform one API call, retrying transient network/5xx failures."""
        clean: dict[str, Any] = {
            key: value for key, value in (params or {}).items() if value is not None
        }
        if signed:
            if not self.has_credentials:
                raise MexcError("MEXC API credentials are not configured")
            clean["recvWindow"] = self._recv_window
            clean["timestamp"] = self._timestamp()

        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            query = urlencode(clean, doseq=True)
            if signed:
                query = f"{query}&signature={self._sign(query)}"
            url = f"{path}?{query}" if query else path
            headers = {"X-MEXC-APIKEY": self._api_key} if signed else None

            try:
                response = await self._client.request(method, url, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning("MEXC %s %s network error (%d/%d): %s",
                            method, path, attempt, self._max_retries, exc)
                await asyncio.sleep(min(2 ** attempt * 0.5, 5))
                if signed:
                    clean["timestamp"] = self._timestamp()
                continue

            if response.status_code >= 500:
                last_error = MexcAPIError(response.status_code, response.text[:200],
                                          response.status_code)
                log.warning("MEXC %s %s server error %s (%d/%d)",
                            method, path, response.status_code, attempt, self._max_retries)
                await asyncio.sleep(min(2 ** attempt * 0.5, 5))
                if signed:
                    clean["timestamp"] = self._timestamp()
                continue

            return self._parse(response)

        raise MexcNetworkError(
            f"MEXC {method} {path} failed after {self._max_retries} attempts: {last_error}"
        )

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            raise MexcAPIError(response.status_code, response.text[:200], response.status_code)

        # MEXC reports business errors as {"code": <int>, "msg": "..."}
        if isinstance(payload, dict) and "code" in payload and payload.get("code") not in (0, 200):
            raise MexcAPIError(
                int(payload.get("code", -1)),
                str(payload.get("msg", "unknown error")),
                response.status_code,
            )
        if response.status_code >= 400:
            raise MexcAPIError(response.status_code, response.text[:200], response.status_code)
        return payload

    async def ping(self) -> bool:
        await self.request("GET", PUBLIC_PING)
        return True

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MexcClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
