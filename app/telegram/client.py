"""Telethon client factory.

The signal channels belong to other people, so the bot reads them with a
normal Telegram *user* account (a bot account cannot join arbitrary channels).
The session is kept as a StringSession in the environment - never on disk.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

log = logging.getLogger(__name__)


def build_client(api_id: int, api_hash: str, session: str) -> TelegramClient:
    if not api_id or not api_hash:
        raise ValueError("TELEGRAM_API_ID / TELEGRAM_API_HASH are not configured")
    if not session:
        raise ValueError(
            "TELEGRAM_SESSION is empty - run `python scripts/login.py` once to create it"
        )
    return TelegramClient(StringSession(session), api_id, api_hash)


async def connect(client: TelegramClient) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "the Telegram session is not authorised - regenerate it with scripts/login.py"
        )
    me = await client.get_me()
    log.info("telegram connected as %s (id=%s)", getattr(me, "username", None), getattr(me, "id", None))
