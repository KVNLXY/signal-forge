"""Backtest of a Telegram group's chart signals, step 1 of 3.

    python scripts/backtest/collect.py <out_dir>            read the signals (OCR)
    python scripts/backtest/run.py     <out_dir>            replay them on MEXC candles
    python scripts/backtest/report.py  <out_dir> <out.pdf>  write the PDF

Group id, topics and the start date are constants at the top of collect.py.
"""


import asyncio
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from decimal import Decimal  # noqa: E402

from telethon.tl.types import MessageMediaPhoto  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.mexc.client import MexcClient, PUBLIC_TICKER_PRICE  # noqa: E402
from app.telegram.client import build_client, connect  # noqa: E402
from app.telegram.parser import STOPWORDS  # noqa: E402
from app.trading.engine import is_result_update  # noqa: E402
from app.vision.chart import ChartReader  # noqa: E402

OUT_DIR = pathlib.Path(sys.argv[1])
OUT_DIR.mkdir(exist_ok=True)
IMG_DIR = OUT_DIR / "images"
IMG_DIR.mkdir(exist_ok=True)
GROUP_ID = -1002479769237
TOPICS = {105: "SIGNAL ORTA VA QISQA VAHTGA", 2: "SCALP SIGNAL"}
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def caption_hint(caption: str, listed: set[str]) -> str | None:
    tokens = [t.strip("#$*_`") for t in caption.split()]
    candidates = []
    if len(tokens) == 1:
        candidates.append(tokens[0])
    candidates += [t.strip("#$*_`") for t in caption.split() if t.startswith(("#", "$"))]
    # first word of a short caption ("Lpt Stop bilan", "Api3 orta mudatga")
    if tokens:
        candidates.append(tokens[0])
    for raw in candidates:
        word = raw.upper()
        if not re.fullmatch(r"[A-Z0-9]{2,10}", word) or word in STOPWORDS:
            continue
        symbol = f"{word}USDT"
        if symbol in listed:
            return symbol
    return None


async def main() -> None:
    s = get_settings()
    mexc = MexcClient()
    tickers = await mexc.request("GET", PUBLIC_TICKER_PRICE)
    listed = {str(t["symbol"]).upper() for t in tickers}
    price_cache: dict[tuple, float | None] = {}

    async def price_at(symbol: str, when) -> float | None:
        key = (symbol, when.strftime("%Y-%m-%d %H:%M"))
        if key not in price_cache:
            try:
                rows = await mexc.request("GET", "/api/v3/klines", {
                    "symbol": symbol, "interval": "15m",
                    "startTime": int(when.timestamp() * 1000), "limit": 1,
                })
                price_cache[key] = float(rows[0][1]) if rows else None
            except Exception:
                price_cache[key] = None
        return price_cache[key]

    client = build_client(
        s.telegram_api_id,
        s.telegram_api_hash.get_secret_value(),
        s.telegram_session.get_secret_value(),
    )
    await connect(client)
    reader = ChartReader()
    reader.available
    records: list[dict] = []
    stats = {"admin_photo_posts": 0, "result_updates": 0, "readable": 0, "unreadable": 0}
    try:
        await client.get_dialogs()
        entity = await client.get_entity(GROUP_ID)
        for topic_id, topic in TOPICS.items():
            async for m in client.iter_messages(entity, reply_to=topic_id):
                if m.date < SINCE:
                    break
                if m.from_id is not None:            # member, not the anonymous admin
                    continue
                if not isinstance(m.media, MessageMediaPhoto):
                    continue
                stats["admin_photo_posts"] += 1
                caption = (m.text or "").strip()
                if is_result_update(caption):
                    stats["result_updates"] += 1
                    continue
                path = IMG_DIR / f"{m.id}.jpg"
                if not path.exists():
                    await client.download_media(m, file=str(path))
                hint = caption_hint(caption, listed)
                raw = reader.ocr(path.read_bytes())
                price0 = None
                if isinstance(raw, tuple):
                    reading = reader.interpret(raw, hint)
                    if reading.symbol and str(reading.symbol) in listed:
                        price0 = await price_at(str(reading.symbol), m.date)
                        if price0:
                            reading = reader.interpret(raw, hint, price_hint=Decimal(str(price0)))
                else:
                    reading = raw
                # the live bot's misread gate: an entry far from the price is a
                # lost decimal point, not a level
                if reading.ok and price0 and reading.entry is not None:
                    deviation = abs(float(reading.entry) - price0) / price0 * 100
                    if deviation > 20:
                        reading.reason = f"misread: entry {reading.entry} is {deviation:.0f}% from price {price0}"
                rec = {
                    "price0": price0,
                    "id": m.id,
                    "date": m.date.astimezone(timezone.utc).isoformat(),
                    "topic": topic,
                    "caption": caption,
                    "ok": reading.ok,
                    "symbol": reading.symbol,
                    "entry": str(reading.entry) if reading.entry is not None else None,
                    "sl": str(reading.stop_loss) if reading.stop_loss is not None else None,
                    "tps": [str(t) for t in reading.take_profits],
                    "reason": reading.reason,
                    "notes": reading.notes,
                }
                records.append(rec)
                stats["readable" if reading.ok else "unreadable"] += 1
                print(f"{m.id} {m.date:%Y-%m-%d} {topic[:5]} "
                      f"{'OK ' + str(reading.symbol) if reading.ok else 'no: ' + str(reading.reason)[:50]}",
                      flush=True)
    finally:
        await client.disconnect()
        await mexc.close()

    (OUT_DIR / "signals.json").write_text(
        json.dumps({"stats": stats, "records": records}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(stats))


asyncio.run(main())
