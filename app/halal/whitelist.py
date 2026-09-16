"""Halal whitelist.

The bot NEVER decides on its own that a coin is halal.  The list is set by the
admin through HALAL_COINS, mirrored into the ``assets`` table at start-up, and
read back from there at runtime.  Anything that is not on the list is not
traded - no exceptions, no guessing.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import repository as repo

log = logging.getLogger(__name__)


class HalalWhitelist:
    def __init__(self, symbols: Iterable[str] = (), quote_asset: str = "USDT") -> None:
        self._quote_asset = quote_asset.upper()
        self._symbols: set[str] = {self.normalize(s) for s in symbols}

    # ------------------------------------------------------------------ #
    def normalize(self, symbol: str) -> str:
        """BTC, btc/usdt, BTC-USDT -> BTCUSDT."""
        cleaned = symbol.upper().replace("/", "").replace("-", "").replace("_", "").strip()
        if not cleaned.endswith(self._quote_asset):
            cleaned = f"{cleaned}{self._quote_asset}"
        return cleaned

    def is_allowed(self, symbol: str) -> bool:
        return self.normalize(symbol) in self._symbols

    @property
    def symbols(self) -> list[str]:
        return sorted(self._symbols)

    def __len__(self) -> int:
        return len(self._symbols)

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and self.is_allowed(symbol)

    # ------------------------------------------------------------------ #
    async def load_from_db(self, session: AsyncSession) -> "HalalWhitelist":
        assets = await repo.list_assets(session, only_halal=True)
        self._symbols = {self.normalize(a.symbol) for a in assets}
        log.info("halal whitelist loaded: %s", ", ".join(self.symbols) or "(empty)")
        return self

    async def sync_from_config(
        self,
        session: AsyncSession,
        configured: Iterable[str],
        reload: bool = True,
        sources: Optional[dict[str, Optional[str]]] = None,
    ) -> "HalalWhitelist":
        """Push the admin-configured list into ``assets`` and reload."""
        symbols = [self.normalize(s) for s in configured]
        await repo.sync_assets(
            session, symbols, self._quote_asset,
            sources={self.normalize(k): v for k, v in (sources or {}).items()},
        )
        if reload:
            await self.load_from_db(session)
        return self

    async def sources(self, session: AsyncSession) -> dict[str, Optional[str]]:
        """Symbol -> recorded source, for the whitelisted coins."""
        return {
            self.normalize(a.symbol): a.source
            for a in await repo.list_assets(session, only_halal=True)
        }


def reason_for(symbol: str, whitelist: HalalWhitelist) -> Optional[str]:
    """Human-readable rejection reason, or None when the coin is allowed."""
    if whitelist.is_allowed(symbol):
        return None
    return "Coin is not in halal whitelist."
