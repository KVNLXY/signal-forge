"""Logging with secret redaction.

Anything that looks like an API secret / token / session string is masked
before it reaches a handler, so a stray ``logger.debug(params)`` can never
leak credentials (requirement 23).
"""

from __future__ import annotations

import logging
from typing import Iterable


class RedactFilter(logging.Filter):
    """Replace known secret values with ``***`` in every log record."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - broken format args
            return True
        redacted = message
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    handler.addFilter(RedactFilter(secrets))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # third-party libraries are chatty at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
