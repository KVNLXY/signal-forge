"""Sharlife (sharlife.my) Shariah screening as a second opinion.

Sharlife publishes its whole crypto screen on one page, each row carrying the
outcome as a data attribute:

    <tr class="crypto-row ..." onclick="window.location='/crypto-shariah/crypto/chainlink'"
        data-name="Chainlink Chainlink" data-ticker="LINK LINK" data-status="Shariah" ...>

Outcomes are "Shariah" (passed), "Grey" (questionable) and "Non-Shariah"
(failed); an empty status is an unrated listing.

This is a cross-check for the admin - scripts/hukm.py and the pre-flight
`check` show it next to the CryptoIslam ruling.  The bot itself never reads it
and never changes HALAL_COINS: the list stays the admin's decision.

Tickers are not unique (SOPH is SophiaVerse on Sharlife and Sophon on MEXC),
so every match carries the project name and the caller shows it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

LISTING_URL = "https://sharlife.my/crypto-shariah"
COIN_URL = "https://sharlife.my/crypto-shariah/crypto/{slug}"
DEFAULT_CACHE = Path("data") / "sharlife.json"
CACHE_MAX_AGE = 7 * 24 * 3600

STATUS = {"Shariah": "passed", "Grey": "grey", "Non-Shariah": "failed"}

_ROW_RE = re.compile(
    r"<tr class=\"crypto-row[^\"]*\"[^>]*onclick=\"window\.location='/crypto-shariah/crypto/"
    r"([a-z0-9\-]+)'\"([^>]*)>"
)
_ATTR_RE = re.compile(r'data-([a-z\-]+)="([^"]*)"')


@dataclass(frozen=True)
class SharlifeEntry:
    ticker: str
    name: str
    slug: str
    status: str            # passed | grey | failed | unrated
    market_cap: float

    @property
    def url(self) -> str:
        return COIN_URL.format(slug=self.slug)

    @property
    def label(self) -> str:
        return {
            "passed": "Shariah ✅", "grey": "Grey 🟠", "failed": "Non-Shariah ❌", "unrated": "unrated",
        }[self.status]


def _dedupe(value: str) -> str:
    """The page repeats a value twice in one attribute: "LINK LINK"."""
    value = value.strip()
    half = len(value) // 2
    if value and len(value) % 2 == 1 and value[:half] == value[half + 1:]:
        return value[:half]
    return value


def parse_listing(html: str) -> list[SharlifeEntry]:
    entries: list[SharlifeEntry] = []
    for slug, attrs in _ROW_RE.findall(html):
        a = dict(_ATTR_RE.findall(attrs))
        ticker = _dedupe(a.get("ticker", "")).upper()
        if not ticker:
            continue
        try:
            market_cap = float(a.get("market-cap") or 0)
        except ValueError:
            market_cap = 0.0
        entries.append(SharlifeEntry(
            ticker=ticker,
            name=_dedupe(a.get("name", "")),
            slug=slug,
            status=STATUS.get(a.get("status") or "", "unrated"),
            market_cap=market_cap,
        ))
    return entries


class SharlifeIndex:
    def __init__(self, entries: list[SharlifeEntry], fetched_at: float = 0.0) -> None:
        self.entries = entries
        self.fetched_at = fetched_at
        self._by_ticker: dict[str, list[SharlifeEntry]] = {}
        for entry in entries:
            self._by_ticker.setdefault(entry.ticker, []).append(entry)
        for hits in self._by_ticker.values():
            hits.sort(key=lambda e: -e.market_cap)

    def lookup(self, ticker: str) -> list[SharlifeEntry]:
        """Every listing with that ticker, largest project first."""
        return list(self._by_ticker.get(ticker.upper(), []))

    def __len__(self) -> int:
        return len(self.entries)

    # ------------------------------------------------------------------ #
    @classmethod
    async def load(
        cls,
        cache_path: Path = DEFAULT_CACHE,
        max_age: int = CACHE_MAX_AGE,
        client: Optional[httpx.AsyncClient] = None,
    ) -> "SharlifeIndex":
        """The cached screen, refreshed from the site when older than a week.

        A failed refresh falls back to whatever cache exists; with neither,
        the index is simply empty.
        """
        cached = cls._read_cache(cache_path)
        if cached is not None and time.time() - cached.fetched_at < max_age:
            return cached
        try:
            fresh = await cls.fetch(client)
        except Exception as exc:
            log.warning("Sharlife refresh failed: %s", exc)
            return cached or cls([])
        fresh._write_cache(cache_path)
        return fresh

    @classmethod
    async def fetch(cls, client: Optional[httpx.AsyncClient] = None) -> "SharlifeIndex":
        headers = {"User-Agent": "Mozilla/5.0 (SignalForge halal cross-check)"}
        own = client is None
        client = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
        try:
            response = await client.get(LISTING_URL, headers=headers)
            response.raise_for_status()
        finally:
            if own:
                await client.aclose()
        entries = parse_listing(response.text)
        if not entries:
            raise ValueError("Sharlife listing came back without any coin rows")
        log.info("Sharlife screen loaded: %d listings", len(entries))
        return cls(entries, fetched_at=time.time())

    @classmethod
    def _read_cache(cls, path: Path) -> Optional["SharlifeIndex"]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls([SharlifeEntry(**e) for e in payload["entries"]], payload["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"fetched_at": self.fetched_at,
                            "entries": [asdict(e) for e in self.entries]}),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - disk problem
            log.warning("could not cache the Sharlife screen: %s", exc)
