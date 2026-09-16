"""Listens to the configured channels and hands every new post to the engine.

A disconnect never kills the bot: Telethon reconnects, and the outer loop
restarts the listener if the connection is lost for good.  On every (re)start
the last few minutes of each channel are read back, so a signal posted while
the bot was down is not lost - the engine's expiry gate drops anything stale
and its message log drops anything already handled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Sequence

from telethon import TelegramClient, events, utils
from telethon.tl.types import ChannelParticipantsAdmins, PeerUser

log = logging.getLogger(__name__)

MessageHandler = Callable[..., Awaitable[object]]


def _as_identifier(raw: str) -> object:
    """@name, https://t.me/name or -1001234567890 -> what Telethon expects."""
    value = raw.strip()
    if value.startswith("https://t.me/") or value.startswith("t.me/"):
        value = "@" + value.split("t.me/", 1)[1].strip("/")
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _chat_id_of(entity: object) -> int:
    """The -100xxxxxxxxxx form an event carries, for a resolved entity."""
    try:
        return int(utils.get_peer_id(entity))
    except (TypeError, ValueError):
        raw = int(getattr(entity, "id", 0) or 0)
        return int(f"-100{raw}") if raw > 0 else raw


class ChannelListener:
    def __init__(
        self,
        client: TelegramClient,
        channels: Sequence[str],
        on_message: MessageHandler,
        on_error: Optional[Callable[[str, BaseException], Awaitable[None]]] = None,
        group_admin_only: bool = True,
        catch_up_minutes: int = 0,
        catch_up_limit: int = 30,
    ) -> None:
        self._client = client
        self._channels = list(channels)
        self._on_message = on_message
        self._on_error = on_error
        self._group_admin_only = group_admin_only
        self._catch_up_minutes = catch_up_minutes
        self._catch_up_limit = catch_up_limit
        self._entities: list[object] = []
        self._labels: dict[int, str] = {}
        self._registered = False
        # Discussion groups: everybody can post, so only the admins count as a
        # signal source.  id -> (admin user ids, fetched at).
        self._group_admins: dict[int, tuple[set[int], float]] = {}

    # ------------------------------------------------------------------ #
    async def resolve_channels(self) -> list[object]:
        """Turn the configured names into entities, skipping the broken ones."""
        entities: list[object] = []
        dialogs_loaded = False

        for raw in self._channels:
            entity, error = await self._try_resolve(raw)

            # A private channel (numeric id, joined by an invite link) is only
            # resolvable once the dialog list has filled the entity cache.
            if entity is None and not dialogs_loaded:
                dialogs_loaded = True
                log.info("resolving %s needs the dialog list, loading it once", raw)
                try:
                    await self._client.get_dialogs()
                except Exception as exc:  # pragma: no cover - network path
                    log.warning("could not load the dialog list: %s", exc)
                else:
                    entity, error = await self._try_resolve(raw)

            if entity is None:
                log.error("cannot resolve channel %s: %s", raw, error)
                if self._on_error and error is not None:
                    await self._on_error(f"channel {raw}", error)
                continue

            entities.append(entity)
            self._remember(entity, raw)

        self._entities = entities
        return entities

    async def _try_resolve(self, raw: str) -> tuple[Optional[object], Optional[BaseException]]:
        try:
            return await self._client.get_entity(_as_identifier(raw)), None
        except Exception as exc:
            return None, exc

    def _remember(self, entity: object, raw: str) -> None:
        """Map the entity id back to the configured name, for readable logs."""
        entity_id = getattr(entity, "id", None)
        if entity_id is not None:
            self._labels[int(entity_id)] = raw
        kind = "group" if getattr(entity, "megagroup", False) else "channel"
        log.info("listening to %s (%s, %s)", raw, getattr(entity, "title", ""), kind)

    async def _admins_of(self, entity: object) -> set[int]:
        """Admin user ids of a group, refreshed once an hour."""
        entity_id = int(getattr(entity, "id", 0) or 0)
        cached = self._group_admins.get(entity_id)
        if cached and time.monotonic() - cached[1] < 3600:
            return cached[0]
        admins: set[int] = set()
        try:
            async for user in self._client.iter_participants(
                entity, filter=ChannelParticipantsAdmins
            ):
                admins.add(int(user.id))
        except Exception as exc:
            log.warning("could not list admins of %s: %s", entity_id, exc)
            if cached:
                return cached[0]
        self._group_admins[entity_id] = (admins, time.monotonic())
        return admins

    async def _from_trusted_sender(self, message, chat) -> bool:
        """In a channel every post is the owner's.  In a group only an admin
        counts: a post made anonymously as the group (from_id is empty - only
        admins can do that) or one signed by an admin account."""
        if not self._group_admin_only:
            return True
        if chat is None:
            return False                         # unknown origin - do not trade on it
        if not getattr(chat, "megagroup", False):
            return True                          # a broadcast channel
        sender = getattr(message, "from_id", None)
        if sender is None:
            return True                          # anonymous admin post
        if isinstance(sender, PeerUser):
            return int(sender.user_id) in await self._admins_of(chat)
        return False                             # posted as some other channel

    async def start(self) -> None:
        await self.resolve_channels()
        if not self._entities:
            log.warning("no channels resolved - the bot will not receive any signal")

        # Re-registering on reconnect would deliver every message twice.
        if self._registered:
            self._client.remove_event_handler(self._dispatch)
        self._client.add_event_handler(
            self._dispatch, events.NewMessage(chats=self._entities or None)
        )
        self._registered = True
        await self.catch_up()

    async def catch_up(self) -> int:
        """Hand the posts of the last CATCH_UP minutes to the engine, oldest
        first.  Returns how many were handed over."""
        if self._catch_up_minutes <= 0 or not self._entities:
            return 0
        since = datetime.now(timezone.utc) - timedelta(minutes=self._catch_up_minutes)
        handled = 0
        for entity in self._entities:
            label = self._labels.get(int(getattr(entity, "id", 0) or 0), "?")
            recent = []
            try:
                async for message in self._client.iter_messages(entity, limit=self._catch_up_limit):
                    sent_at = getattr(message, "date", None)
                    if sent_at is None or sent_at < since:
                        break
                    recent.append(message)
            except Exception as exc:
                log.warning("could not read back %s: %s", label, exc)
                continue
            for message in reversed(recent):
                await self._handle(message, entity, _chat_id_of(entity))
                handled += 1
        if handled:
            log.info("caught up on %d post(s) from the last %d min", handled, self._catch_up_minutes)
        return handled

    @staticmethod
    def _image_loader(message):
        """Download the photo only if the engine actually asks for it."""
        if getattr(message, "photo", None) is None:
            return None

        async def load() -> Optional[bytes]:
            try:
                return await message.download_media(file=bytes)
            except Exception as exc:
                log.warning("could not download the chart image: %s", exc)
                return None

        return load

    async def _dispatch(self, event) -> None:
        chat = getattr(event, "chat", None)
        if chat is None:
            try:
                chat = await event.get_chat()
            except Exception as exc:  # pragma: no cover - network path
                log.warning("could not resolve the chat of a message: %s", exc)
        await self._handle(event.message, chat, int(getattr(event, "chat_id", 0) or 0))

    async def _handle(self, message, chat, chat_id: int) -> None:
        """One post - live from the event handler or read back on start."""
        text = getattr(message, "raw_text", None) or getattr(message, "message", "") or ""
        image_loader = self._image_loader(message)
        if not text.strip() and image_loader is None:
            return
        # -100xxxxxxxxxx (chat_id) vs xxxxxxxxxx (entity id)
        short_id = int(str(chat_id).replace("-100", "", 1)) if chat_id else 0
        identifier = self._labels.get(short_id) or self._labels.get(chat_id) or str(chat_id)
        title = getattr(chat, "title", None)

        if not await self._from_trusted_sender(message, chat):
            log.debug("member post in %s ignored (%s chars)", identifier, len(text))
            return

        log.info("new message from %s (%s chars%s)",
                 identifier, len(text), ", with image" if image_loader else "")
        try:
            await self._on_message(
                text,
                channel_identifier=identifier,
                tg_channel_id=chat_id,
                tg_message_id=int(getattr(message, "id", 0) or 0),
                channel_title=title,
                sent_at=getattr(message, "date", None),
                image_loader=image_loader,
                reply_to_message_id=getattr(message, "reply_to_msg_id", None),
            )
        except Exception as exc:
            log.exception("handling message from %s failed", identifier)
            if self._on_error:
                await self._on_error(f"message from {identifier}", exc)

    async def run_forever(self) -> None:
        """Keep the listener alive across disconnects."""
        while True:
            try:
                if not self._client.is_connected():
                    await self._client.connect()
                await self.start()
                await self._client.run_until_disconnected()
                log.warning("telegram disconnected - reconnecting in 10s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("telegram listener crashed")
                if self._on_error:
                    await self._on_error("telegram listener", exc)
            await asyncio.sleep(10)
