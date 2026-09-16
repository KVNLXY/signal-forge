"""Listener tests with a fake Telethon client - no network.

What matters here: posts missed while the bot was down are read back on
start (oldest first, only the fresh ones), and a live event and a read-back
post reach the engine through the same path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from telethon.tl.types import PeerUser

from app.telegram.listener import ChannelListener


@dataclass
class FakeMessage:
    id: int
    message: str
    date: datetime
    photo: object = None
    from_id: object = None

    @property
    def raw_text(self) -> str:
        return self.message


@dataclass
class FakeEntity:
    id: int
    title: str
    megagroup: bool = False
    history: list[FakeMessage] = field(default_factory=list)   # newest first


class FakeClient:
    def __init__(self, entities: dict[str, FakeEntity]) -> None:
        self._entities = entities
        self.handlers: list = []

    async def get_entity(self, identifier):
        try:
            return self._entities[str(identifier)]
        except KeyError:
            raise ValueError(f"unknown channel {identifier}")

    async def get_dialogs(self):
        return []

    def add_event_handler(self, callback, event) -> None:
        self.handlers.append(callback)

    def remove_event_handler(self, callback) -> None:
        self.handlers = [h for h in self.handlers if h is not callback]

    async def iter_messages(self, entity, limit: int = 30):
        for message in entity.history[:limit]:
            yield message

    async def iter_participants(self, entity, filter=None):
        for user_id in (10, 11):
            yield type("User", (), {"id": user_id})()


class Received:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})


def minutes_ago(n: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=n)


async def test_start_reads_back_only_the_fresh_posts_oldest_first():
    channel = FakeEntity(1001, "Signals", history=[
        FakeMessage(5, "SOL LONG\nEntry 150\nTP 160\nSL 145", minutes_ago(2)),
        FakeMessage(4, "ETH LONG\nEntry 3500\nTP 3800\nSL 3300", minutes_ago(9)),
        FakeMessage(3, "BTC LONG\nEntry 100000\nTP 105000\nSL 98000", minutes_ago(40)),
        FakeMessage(2, "old chatter", minutes_ago(300)),
    ])
    client = FakeClient({"@signals": channel})
    received = Received()
    listener = ChannelListener(client, ["@signals"], received, catch_up_minutes=15)

    await listener.start()

    assert [c["tg_message_id"] for c in received.calls] == [4, 5]      # fresh, oldest first
    assert received.calls[0]["channel_identifier"] == "@signals"
    assert received.calls[0]["channel_title"] == "Signals"
    assert received.calls[0]["tg_channel_id"] == -1001001
    assert received.calls[0]["sent_at"] == channel.history[1].date
    assert len(client.handlers) == 1                                    # live events registered


async def test_catch_up_is_off_by_default_and_survives_a_broken_channel():
    good = FakeEntity(1001, "Good", history=[FakeMessage(1, "BTC LONG\nEntry 1\nTP 2\nSL 0.5", minutes_ago(1))])
    client = FakeClient({"@good": good})
    received = Received()

    listener = ChannelListener(client, ["@good", "@missing"], received)
    await listener.start()
    assert received.calls == []                                         # catch_up_minutes=0

    async def broken(entity, limit=30):
        raise RuntimeError("flood wait")
        yield  # pragma: no cover

    listener = ChannelListener(client, ["@good"], received, catch_up_minutes=15)
    client.iter_messages = broken
    assert await listener.catch_up() == 0                               # logged, not raised


async def test_read_back_group_posts_keep_the_admin_only_rule():
    group = FakeEntity(2002, "Chat", megagroup=True, history=[
        FakeMessage(3, "member spam BUY NOW", minutes_ago(1), from_id=PeerUser(99)),
        FakeMessage(2, "admin: BTC LONG\nEntry 1\nTP 2\nSL 0.5", minutes_ago(2), from_id=PeerUser(10)),
        FakeMessage(1, "anonymous admin post", minutes_ago(3), from_id=None),
    ])
    client = FakeClient({"@chat": group})
    received = Received()
    listener = ChannelListener(client, ["@chat"], received, catch_up_minutes=15)

    await listener.start()

    assert [c["tg_message_id"] for c in received.calls] == [1, 2]


async def test_a_live_event_goes_through_the_same_path():
    channel = FakeEntity(1001, "Signals")
    client = FakeClient({"@signals": channel})
    received = Received()
    listener = ChannelListener(client, ["@signals"], received)
    await listener.start()

    class FakeEvent:
        chat = channel
        chat_id = -1001001
        message = FakeMessage(7, "BTC LONG\nEntry 1\nTP 2\nSL 0.5", minutes_ago(0))
        raw_text = message.message

    await client.handlers[0](FakeEvent())
    assert received.calls[0]["tg_message_id"] == 7
    assert received.calls[0]["channel_identifier"] == "@signals"
