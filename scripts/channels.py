"""List the channels this Telegram account can read.

    python scripts/channels.py

Prints, for every channel and group you are in, the exact value to put in
TELEGRAM_CHANNELS.  Public channels show as @username; private ones (joined by
invite link) show as a numeric id - both work.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.telegram.client import build_client, connect  # noqa: E402


def safe(text: str) -> str:
    """Channel names carry every alphabet; consoles often do not."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:  # pragma: no cover - exotic console
        return text.encode("ascii", errors="replace").decode("ascii")


async def main() -> int:
    settings = get_settings()
    client = build_client(
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
        settings.telegram_session.get_secret_value(),
    )
    await connect(client)
    try:
        configured = {c.lower() for c in settings.channels}
        rows: list[tuple[str, str, str, bool]] = []

        async for dialog in client.iter_dialogs():
            if not dialog.is_channel:
                continue
            entity = dialog.entity
            username = getattr(entity, "username", None)
            value = f"@{username}" if username else str(dialog.id)
            kind = "channel" if getattr(entity, "broadcast", False) else "group"
            rows.append((value, kind, dialog.name or "", value.lower() in configured))

        if not rows:
            print("This account is not in any channel yet - join the signal channels first.")
            return 1

        print()
        print(f"{'VALUE':<26} {'TYPE':<8} NAME")
        print("-" * 78)
        for value, kind, name, selected in sorted(rows, key=lambda r: (r[1], r[2].lower())):
            mark = " <- in TELEGRAM_CHANNELS" if selected else ""
            print(safe(f"{value:<26} {kind:<8} {name[:40]}{mark}"))

        print()
        print("Copy the ones you want into .env, comma separated:")
        print("TELEGRAM_CHANNELS=@first,@second")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
